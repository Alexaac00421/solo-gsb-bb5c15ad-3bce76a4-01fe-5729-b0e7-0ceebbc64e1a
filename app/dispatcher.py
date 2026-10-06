"""违规变更事件的异步 HTTP 回调投递器（进程内后台守护线程）。

保持现有启动方式（uvicorn 单进程）：应用 lifespan 启动后自动拉起一个
轮询线程与一个小工作线程池，无需额外进程/中间件。

投递语义：

- 事件与投递记录在片段变更的同一事务内落库，投递全部异步进行；回调失败
  （连接错误 / 超时 / 非 2xx）**不回滚已提交片段**，只记录投递状态并退避重试；
- “同一订阅下同一司机的事件按提交顺序投递”：worker 每轮为每个
  （订阅, 司机）队列只取 ``event_id`` 最小的待投递队头，前一条未到终态
  （SUCCEEDED / CANCELLED）或仍在退避窗口时，后续事件不会越过它；
- 不同订阅、不同司机的队列相互独立，失败互不阻塞；
- 重试沿用同一事件 ID 与同一载荷，签名时间戳每次重新生成；
- 订阅停用（见 API 的 disable 端点）后待投递记录被标记 CANCELLED，不再发送。

数据库为 PostgreSQL 时，worker 在事务内以 ``FOR UPDATE SKIP LOCKED`` 行锁
认领队头（锁持有到投递结果落库提交），多进程部署不会重复投递；其他数据库
（如本地 sqlite）退化为普通查询，由进程内 ``_inflight`` 集合保证本进程不
重复发送。整体为至少一次投递，接收方按 ``X-Event-Id`` 幂等即可。
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, models
from .db import SessionLocal
from .notifications import (
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SUCCEEDED,
    payload_bytes,
    sign_payload,
)

logger = logging.getLogger("notifications.dispatcher")


def backoff_delay(attempts: int) -> float:
    """第 ``attempts`` 次（从 1 起）失败后的退避秒数。"""
    raw = config.DISPATCH_RETRY_BASE * config.DISPATCH_RETRY_FACTOR ** max(0, attempts - 1)
    return min(raw, config.DISPATCH_RETRY_MAX)


def _is_postgres(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _as_utc(dt: datetime | None) -> datetime | None:
    """统一为带 UTC 时区的 datetime（sqlite 等可能返回 naive datetime）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class DeliveryDispatcher:
    """轮询线程 + 工作线程池的异步投递器；单例由 lifespan 启停。"""

    def __init__(self, session_factory=SessionLocal) -> None:
        self._session_factory = session_factory
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")
        # 进程内认领护栏（sqlite 等无咨询锁时防同进程重复发送）
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="webhook-dispatch", daemon=True)
        self._thread.start()
        logger.info("违规通知投递器已启动")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._pool.shutdown(wait=False)
        logger.info("违规通知投递器已停止")

    def wake(self) -> None:
        """新事件落库后可立即唤醒一轮轮询（默认也会按轮询间隔自动醒来）。"""
        self._wake.set()

    # ------------------------------------------------------------ 轮询循环
    def _run(self) -> None:
        while not self._stop.is_set():
            # 先清旧唤醒再认领，避免“提交后唤醒”在 wait 阶段被丢失；
            # 认领期间到达的 wake 会在随后立即生效（wait 直接返回）。
            self._wake.clear()
            try:
                heads = self._claim_heads()
            except Exception:  # 数据库瞬态错误不应杀死投递线程
                logger.exception("认领待投递队头失败，下一轮重试")
                heads = []
            for delivery_id in heads:
                self._pool.submit(self._deliver, delivery_id)
            self._wake.wait(timeout=config.DISPATCH_POLL_INTERVAL)

    def _claim_heads(self) -> list[int]:
        """返回本轮可发送的各 (订阅, 司机) 队列队头 delivery_id。

        可发送 = 状态为 PENDING/FAILED、订阅仍 ACTIVE、退避窗口已过。
        同一 (订阅, 司机) 只放行 ``event_id`` 最小的可发送记录，且其前面
        不得存在仍在 PENDING/FAILED 的更早事件（哪怕更早事件还在退避窗口：
        后到事件也必须等待，保证严格按提交顺序投递）。
        """
        now = datetime.now(timezone.utc)
        db = self._session_factory()
        try:
            due = (DELIVERY_PENDING, DELIVERY_FAILED)
            due_rows = list(
                db.execute(
                    select(
                        models.EventDelivery.delivery_id,
                        models.EventDelivery.subscription_id,
                        models.EventDelivery.driver_id,
                        models.EventDelivery.event_id,
                    )
                    .join(
                        models.NotificationSubscription,
                        models.EventDelivery.subscription_id
                        == models.NotificationSubscription.subscription_id,
                    )
                    .where(
                        models.EventDelivery.status.in_(due),
                        models.NotificationSubscription.status == "ACTIVE",
                        (models.EventDelivery.next_attempt_at.is_(None))
                        | (models.EventDelivery.next_attempt_at <= now),
                    )
                    .order_by(
                        models.EventDelivery.subscription_id,
                        models.EventDelivery.driver_id,
                        models.EventDelivery.event_id,
                    )
                ).all()
            )

            chosen: list[int] = []
            seen_groups: set[tuple[str, str]] = set()
            for delivery_id, subscription_id, driver_id, event_id in due_rows:
                group = (subscription_id, driver_id)
                if group in seen_groups:
                    continue  # 每组排序后的第一条即组内最小 event_id
                # 防御乱序数据：同组更早事件仍未到终态则整条队列阻塞
                blocked = db.execute(
                    select(models.EventDelivery.delivery_id)
                    .where(
                        models.EventDelivery.subscription_id == subscription_id,
                        models.EventDelivery.driver_id == driver_id,
                        models.EventDelivery.event_id < event_id,
                        models.EventDelivery.status.in_(due),
                    )
                    .limit(1)
                ).first()
                if blocked is not None:
                    continue
                seen_groups.add(group)
                chosen.append(delivery_id)
                if len(chosen) >= config.DISPATCH_BATCH_SIZE:
                    break
            return chosen
        finally:
            db.close()

    # ------------------------------------------------------------ 单次投递
    def _deliver(self, delivery_id: int) -> None:
        with self._inflight_lock:
            if delivery_id in self._inflight:
                return
            self._inflight.add(delivery_id)
        try:
            self._deliver_locked(delivery_id)
        except Exception:
            logger.exception("投递 delivery_id=%s 时发生未预期错误", delivery_id)
        finally:
            with self._inflight_lock:
                self._inflight.discard(delivery_id)
            self.wake()  # 尽快推进同队列的下一条 / 失败后的状态刷新

    def _deliver_locked(self, delivery_id: int) -> None:
        db = self._session_factory()
        try:
            # 在事务内认领队头行：PostgreSQL 用 FOR UPDATE SKIP LOCKED，
            # 其他进程正在发送同一行时直接跳过（锁持有到结果落库提交）；
            # 其他数据库（sqlite 等）退化为普通查询，由进程内 _inflight 防重。
            stmt = select(models.EventDelivery).where(
                models.EventDelivery.delivery_id == delivery_id
            )
            if _is_postgres(db):
                stmt = stmt.with_for_update(skip_locked=True)
            d = db.execute(stmt).scalars().first()
            if d is None or d.status not in (DELIVERY_PENDING, DELIVERY_FAILED):
                db.rollback()
                return
            # 退避窗口可能在认领后到达：未到点则跳过，下轮再试（保持行状态不变）
            now = datetime.now(timezone.utc)
            next_at = _as_utc(d.next_attempt_at)
            if next_at is not None and next_at > now:
                db.rollback()
                return
            sub = db.get(models.NotificationSubscription, d.subscription_id)
            if sub is None or sub.status != "ACTIVE":
                # 订阅在认领后被停用：停用语义由 disable 端点置 CANCELLED，
                # 这里仅做竞态兜底，绝不发送
                db.rollback()
                return
            event = db.get(models.ViolationEvent, d.event_id)
            if event is None:  # 理论不可能（同事务外键）
                db.rollback()
                return

            body = payload_bytes(event.payload)
            timestamp = str(int(time.time()))
            signature = sign_payload(sub.secret, timestamp, body)
            req = urllib.request.Request(
                sub.callback_url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Event-Id": str(event.event_id),
                    "X-Event-Type": event.payload["event_type"],
                    "X-Delivery-Id": str(d.delivery_id),
                    "X-Signature-Timestamp": timestamp,
                    "X-Signature": f"v1={signature}",
                    "User-Agent": "rail-duty-compliance-webhook/1.0",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=config.DISPATCH_TIMEOUT) as resp:
                    code = resp.getcode()
                    resp.read(256)
                ok = 200 <= code < 300
                error = None
            except urllib.error.HTTPError as exc:
                code = exc.code
                ok = 200 <= code < 300
                error = None
                if not ok:
                    try:
                        error = f"HTTP {code}: {exc.read(512).decode('utf-8', 'replace')}"
                    except Exception:
                        error = f"HTTP {code}: {exc.reason}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                code = None
                ok = False
                error = f"{type(exc).__name__}: {exc}"

            d.attempts += 1
            d.last_response_code = code
            if ok:
                d.status = DELIVERY_SUCCEEDED
                d.last_error = None
                d.next_attempt_at = None
                d.delivered_at = now
            else:
                # 失败保留待重试：不影响其他司机/订阅，不回滚已提交片段
                d.status = DELIVERY_FAILED
                d.last_error = (error or "投递失败")[:1000]
                d.next_attempt_at = now + timedelta(seconds=backoff_delay(d.attempts))
                logger.warning(
                    "事件 %s 投递订阅 %s 第 %s 次失败：%s",
                    d.event_id,
                    d.subscription_id,
                    d.attempts,
                    d.last_error,
                )
            db.commit()  # 释放行锁；随后失败的重试沿用同一事件 ID
        finally:
            db.close()


dispatcher = DeliveryDispatcher()
