"""违规事件回调投递器：后台线程轮询待投递记录，异步发送签名回调。

- 按（订阅, 司机）分组：同组严格按事件提交顺序（seq）逐条投递，组内前一条
  未成功不会投递后一条；不同司机、不同订阅的组互不阻塞（线程池并行）。
- 回调报文用订阅的共享密钥做 HMAC-SHA256 签名（``X-Signature: sha256=<hex>``），
  重试沿用同一事件 ID 与报文；接收方可按事件 ID 去重（至少一次投递语义）。
- 超时或非 2xx：保留 PENDING，记录尝试次数与最近错误，按指数退避等待重试；
  2xx：标记 DELIVERED。投递器使用独立会话，与片段写事务互不影响，
  投递失败不会回滚已提交的片段。
- 订阅停用后其待投递记录被登记路径取消（CANCELLED）；本模块查询时也只选
  活跃订阅，双重保证停用后不再发送。
"""

from __future__ import annotations

import logging
import socket
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from . import models, notification
from .config import (
    DISPATCH_ENABLED,
    DISPATCH_POLL_INTERVAL,
    DISPATCH_RETRY_BASE_SECONDS,
    DISPATCH_RETRY_MAX_SECONDS,
    DISPATCH_TIMEOUT,
    DISPATCH_WORKERS,
)
from .db import SessionLocal

logger = logging.getLogger("duty.dispatcher")


def _aware(dt: datetime) -> datetime:
    """数据库读出的时间统一规范为带 UTC 时区的 datetime（sqlite 读回为 naive）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _http_post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[bool, str | None]:
    """POST 回调报文，返回 (是否 2xx, 错误描述)。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:  # 非 2xx 响应
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, socket.timeout, OSError) as exc:  # 超时/连接失败等
        reason = getattr(exc, "reason", exc)
        return False, f"{type(exc).__name__}: {reason}"
    except Exception as exc:  # 兜底：意外错误同样记录为可重试失败
        return False, f"{type(exc).__name__}: {exc}"
    if 200 <= status < 300:
        return True, None
    return False, f"HTTP {status}"


class Dispatcher:
    """待投递事件的后台轮询投递器（线程池按组并行，组内串行保序）。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=DISPATCH_WORKERS, thread_name_prefix="dispatch")
        self._inflight: set[tuple[str, str]] = set()  # 处理中的 (订阅, 司机) 组
        self._lock = threading.Lock()

    def start(self) -> None:
        if not DISPATCH_ENABLED:
            logger.info("回调投递器已禁用（DISPATCH_ENABLED=0）")
            return
        self._thread = threading.Thread(target=self._run, name="dispatch-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # 进行中的 HTTP 调用受 DISPATCH_TIMEOUT 约束，shutdown 有界
        self._pool.shutdown(wait=True)

    # ------------------------------------------------------------ 轮询

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception:
                logger.exception("投递轮询周期失败，下一周期继续")
            self._stop.wait(DISPATCH_POLL_INTERVAL)

    def _cycle(self) -> None:
        for sub_id, driver_id in self._due_groups():
            key = (sub_id, driver_id)
            with self._lock:
                if key in self._inflight:
                    continue
                self._inflight.add(key)
            self._pool.submit(self._process_group, sub_id, driver_id)

    def _due_groups(self) -> list[tuple[str, str]]:
        """找出“队首待投递记录已到期”的 (订阅, 司机) 组（仅活跃订阅）。"""
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            rows = db.execute(
                select(
                    models.Delivery.subscription_id,
                    models.Delivery.driver_id,
                    models.Delivery.event_seq,
                    models.Delivery.next_retry_at,
                )
                .join(
                    models.Subscription,
                    models.Subscription.subscription_id == models.Delivery.subscription_id,
                )
                .where(
                    models.Delivery.status == notification.DELIVERY_PENDING,
                    models.Subscription.status == notification.SUB_ACTIVE,
                )
            ).all()
        heads: dict[tuple[str, str], tuple[int, datetime | None]] = {}
        for sub_id, driver_id, seq, next_retry in rows:
            key = (sub_id, driver_id)
            current = heads.get(key)
            if current is None or seq < current[0]:
                heads[key] = (seq, next_retry)
        return [
            key
            for key, (_seq, next_retry) in heads.items()
            if next_retry is None or _aware(next_retry) <= now
        ]

    # ------------------------------------------------------------ 组内投递

    def _process_group(self, subscription_id: str, driver_id: str) -> None:
        try:
            while not self._stop.is_set():
                if not self._deliver_head(subscription_id, driver_id):
                    break
        except Exception:
            logger.exception("投递组 (%s, %s) 处理失败", subscription_id, driver_id)
        finally:
            with self._lock:
                self._inflight.discard((subscription_id, driver_id))

    def _deliver_head(self, subscription_id: str, driver_id: str) -> bool:
        """投递该组最早的待投递事件；投递成功返回 True（可继续下一条）。"""
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            row = db.execute(
                select(models.Delivery, models.ViolationEvent, models.Subscription)
                .join(
                    models.ViolationEvent,
                    models.ViolationEvent.seq == models.Delivery.event_seq,
                )
                .join(
                    models.Subscription,
                    models.Subscription.subscription_id == models.Delivery.subscription_id,
                )
                .where(
                    models.Delivery.subscription_id == subscription_id,
                    models.Delivery.driver_id == driver_id,
                    models.Delivery.status == notification.DELIVERY_PENDING,
                    models.Subscription.status == notification.SUB_ACTIVE,
                )
                .order_by(models.Delivery.event_seq)
                .limit(1)
            ).first()
            if row is None:
                return False
            delivery, event, sub = row
            if delivery.next_retry_at is not None and _aware(delivery.next_retry_at) > now:
                return False  # 队首尚在退避等待：组内后续事件也不能越过

            payload = notification.build_payload(
                event_id=event.event_id,
                subscription_id=sub.subscription_id,
                driver_id=event.driver_id,
                rules_before=event.rules_before,
                rules_after=event.rules_after,
                committed_at=_aware(event.committed_at),
            )
            body = notification.encode_payload(payload)
            headers = {
                "Content-Type": "application/json",
                "X-Event-Id": event.event_id,
                "X-Delivery-Id": str(delivery.delivery_id),
                "X-Signature": notification.sign_body(sub.secret, body),
            }
            ok, error = _http_post(sub.callback_url, body, headers, DISPATCH_TIMEOUT)

            finished = datetime.now(timezone.utc)
            attempts = delivery.attempts + 1
            if ok:
                values = {
                    "status": notification.DELIVERY_DELIVERED,
                    "attempts": attempts,
                    "last_error": None,
                    "last_attempt_at": finished,
                    "next_retry_at": None,
                    "delivered_at": finished,
                }
            else:
                delay = notification.retry_delay_seconds(
                    attempts, DISPATCH_RETRY_BASE_SECONDS, DISPATCH_RETRY_MAX_SECONDS
                )
                values = {
                    "attempts": attempts,
                    "last_error": (error or "unknown")[:500],
                    "last_attempt_at": finished,
                    "next_retry_at": finished + timedelta(seconds=delay),
                }
            # 条件更新：仅当记录仍是 PENDING 才落结果——订阅停用的并发取消不会被覆盖
            db.execute(
                update(models.Delivery)
                .where(
                    models.Delivery.delivery_id == delivery.delivery_id,
                    models.Delivery.status == notification.DELIVERY_PENDING,
                )
                .values(**values)
            )
            db.commit()
            return ok
