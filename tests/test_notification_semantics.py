"""违规事件订阅通知端到端语义参考实现（不依赖 FastAPI/SQLAlchemy，仅用标准库）。

在无法安装 Web 依赖的环境中，用内存存储复刻：

- app/api.py 三条片段写路径（录入 / 更正 / 批量调整）的事件生成判定：
  仅事务前后违规规则**集合**变化才各产生一个事件；失败请求与幂等重放不产生事件；
- 订阅登记 / 停用的判定顺序（同 ID 幂等、异内容 409、司机缺失 404、
  停用同事务取消待投递）；
- app/dispatcher.py 的投递语义：同一（订阅, 司机）按提交顺序逐条投递，
  失败保留待重试并记录次数与最近错误，不阻塞其他组；重试沿用事件 ID；
  停用后待投递事件不再发送。
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H, Seg, evaluate_driver
from app.notification import (
    DELIVERY_CANCELLED,
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    SUB_ACTIVE,
    SUB_DISABLED,
    build_payload,
    canonical_rules,
    changed_rules,
    new_event_id,
    retry_delay_seconds,
    rule_set,
    subscription_matches,
)
from app.reassignment import (
    BatchRejection,
    Move,
    SegView,
    canonical_payload,
    ensure_unique_segment_refs,
    final_layout,
    payload_fingerprint,
)

BASE = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
RETRY_BASE, RETRY_MAX = 2.0, 60.0


def t(h, m=0, day=0):
    return BASE + timedelta(days=day, hours=h, minutes=m)


class HttpError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class MemDB:
    """内存表 + 事件生成 + 订阅 + 投递（复刻 api.py / dispatcher.py 的判定顺序）。"""

    def __init__(self):
        self.drivers: dict[str, dict] = {}
        self.segments: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.events: list[dict] = []  # 下标即全局提交顺序（对应 seq）
        self.deliveries: dict[int, dict] = {}
        self._delivery_seq = 0
        self._tick = 0

    # ------------------------------------------------------------ 基础
    def _now(self):
        """每次写事务推进的逻辑时钟（对应 api 层 datetime.now）。"""
        self._tick += 1
        return BASE + timedelta(days=30, seconds=self._tick)

    def add_driver(self, d):
        self.drivers[d] = {"driver_id": d, "name": d}

    def driver_segments(self, driver):
        return sorted(
            (s for s in self.segments.values() if s["driver_id"] == driver),
            key=lambda s: (s["start"], s["end"], s["segment_id"]),
        )

    def _rule_set(self, driver):
        result = evaluate_driver(
            Seg(s["segment_id"], s["start"], s["end"], s["type"])
            for s in self.driver_segments(driver)
        )
        return rule_set(result)

    # ------------------------------------------------------------ 事件生成（复刻 api._emit_violation_events）
    def _emit(self, before_sets, after_sets=None, committed_at=None):
        committed_at = committed_at or self._now()
        for driver_id in sorted(before_sets):
            before = before_sets[driver_id]
            after = after_sets[driver_id] if after_sets is not None else self._rule_set(driver_id)
            if before == after:
                continue  # 集合未变化：不产生事件
            event = {
                "seq": len(self.events),
                "event_id": new_event_id(),
                "driver_id": driver_id,
                "rules_before": before,
                "rules_after": after,
                "committed_at": committed_at,
            }
            self.events.append(event)
            for sub in self.subscriptions.values():
                if sub["status"] != SUB_ACTIVE:
                    continue
                if sub["driver_id"] is not None and sub["driver_id"] != driver_id:
                    continue
                if subscription_matches(sub["driver_id"], sub["rules"], driver_id, before, after):
                    self._delivery_seq += 1
                    self.deliveries[self._delivery_seq] = {
                        "delivery_id": self._delivery_seq,
                        "event_seq": event["seq"],
                        "event_id": event["event_id"],
                        "subscription_id": sub["subscription_id"],
                        "driver_id": driver_id,
                        "status": DELIVERY_PENDING,
                        "attempts": 0,
                        "last_error": None,
                        "next_retry_at": None,
                    }

    # ------------------------------------------------------------ 片段写路径（复刻 api.py 判定顺序）
    def create_segment(self, sid, driver, sh, eh, typ="DRIVING", day=0, _replay_check=True):
        if _replay_check and sid in self.segments:
            raise HttpError(409, {"error": "segment_content_conflict"})  # 异内容/重试：不产生事件
        if driver not in self.drivers:
            raise HttpError(404, {"error": "driver_not_found"})
        start, end = t(sh, day=day), t(eh, day=day)
        for s in self.driver_segments(driver):
            if s["start"] < end and s["end"] > start:
                raise HttpError(409, {"error": "segment_overlap"})  # 失败请求：不产生事件
        before = {driver: self._rule_set(driver)}
        self.segments[sid] = {
            "segment_id": sid, "driver_id": driver,
            "start": start, "end": end, "type": typ, "revision": 1,
        }
        self._emit(before)

    def create_segment_idempotent(self, sid, driver, sh, eh, typ="DRIVING", day=0):
        """POST /segments 的幂等重试分支：同 ID 同内容直接返回既有记录，不产生事件。"""
        seg = self.segments[sid]
        assert (
            seg["driver_id"] == driver and seg["start"] == t(sh, day=day)
            and seg["end"] == t(eh, day=day) and seg["type"] == typ
        )
        return seg

    def patch_segment(self, sid, expected_revision, *, driver_id=None, sh=None, eh=None, typ=None):
        seg = self.segments.get(sid)
        if seg is None:
            raise HttpError(404, {"error": "segment_not_found"})
        if seg["revision"] != expected_revision:
            raise HttpError(409, {"error": "revision_conflict"})  # 失败请求：不产生事件
        new_driver = driver_id if driver_id is not None else seg["driver_id"]
        before = {d: self._rule_set(d) for d in {seg["driver_id"], new_driver}}
        old_driver = seg["driver_id"]
        seg["driver_id"] = new_driver
        if sh is not None:
            seg["start"] = t(sh)
        if eh is not None:
            seg["end"] = t(eh)
        if typ is not None:
            seg["type"] = typ
        seg["revision"] += 1
        self._emit(before)

    def submit_batch(self, batch_id, moves: list[Move]):
        """复刻 POST /reassignment-batches：幂等重放与整批拒绝都不产生事件。"""
        try:
            ensure_unique_segment_refs(m.segment_id for m in moves)
        except BatchRejection as exc:
            raise HttpError(exc.status_code, {"error": exc.code})
        payload = canonical_payload(moves)
        fp = payload_fingerprint(payload)
        prior = self.batches.get(batch_id)
        if prior is not None:
            if prior["fingerprint"] != fp:
                raise HttpError(409, {"error": "batch_content_conflict"})
            if prior["status"] >= 400:
                raise HttpError(prior["status"], dict(prior["response"]))
            return {**prior["response"], "replayed": True}  # 重放：不产生事件

        snapshot = {sid: dict(s) for sid, s in self.segments.items()}
        try:
            result = self._apply_batch(batch_id, moves)
        except BatchRejection as exc:
            self.batches[batch_id] = {
                "fingerprint": fp, "status": exc.status_code,
                "response": {"error": exc.code},
            }
            self.segments = snapshot  # 原子拒绝：零改动、零事件
            raise HttpError(exc.status_code, {"error": exc.code})
        self.batches[batch_id] = {"fingerprint": fp, "status": 201, "response": result}
        return result

    def _apply_batch(self, batch_id, moves):
        by_id = {}
        for m in moves:
            seg = self.segments.get(m.segment_id)
            if seg is None:
                raise BatchRejection(status_code=404, code="segment_not_found", message="x")
            if seg["revision"] != m.expected_revision:
                raise BatchRejection(status_code=409, code="revision_conflict", message="x")
            by_id[m.segment_id] = seg
        for d in {m.driver_id for m in moves}:
            if d not in self.drivers:
                raise BatchRejection(status_code=404, code="driver_not_found", message="x")
        affected = sorted({s["driver_id"] for s in by_id.values()} | {m.driver_id for m in moves})
        before_sets = {d: self._rule_set(d) for d in affected}
        existing = [
            SegView(s["segment_id"], s["driver_id"], s["start"], s["end"], s["type"])
            for d in affected for s in self.driver_segments(d)
        ]
        final_layout(existing, moves)
        for m in moves:
            seg = by_id[m.segment_id]
            seg.update(driver_id=m.driver_id, start=m.start, end=m.end, type=m.type)
            seg["revision"] += 1
        after_sets = {d: self._rule_set(d) for d in affected}
        committed_at = self._now()
        self._emit(before_sets, after_sets, committed_at)
        return {"batch_id": batch_id, "replayed": False, "affected": affected}

    # ------------------------------------------------------------ 订阅（复刻 api.py 判定顺序）
    def register_subscription(self, sub_id, driver_id, rules, callback_url, secret):
        rules = canonical_rules(rules)
        existing = self.subscriptions.get(sub_id)
        if existing is not None:
            same = (
                existing["driver_id"] == driver_id and existing["rules"] == rules
                and existing["callback_url"] == callback_url and existing["secret"] == secret
            )
            if not same:
                raise HttpError(409, {"error": "subscription_content_conflict"})
            return existing  # 幂等重试（已停用保持停用）
        if driver_id is not None and driver_id not in self.drivers:
            raise HttpError(404, {"error": "driver_not_found"})
        sub = {
            "subscription_id": sub_id, "driver_id": driver_id, "rules": rules,
            "callback_url": callback_url, "secret": secret, "status": SUB_ACTIVE,
        }
        self.subscriptions[sub_id] = sub
        return sub

    def deactivate_subscription(self, sub_id):
        sub = self.subscriptions.get(sub_id)
        if sub is None:
            raise HttpError(404, {"error": "subscription_not_found"})
        if sub["status"] == SUB_DISABLED:
            return sub  # 幂等
        sub["status"] = SUB_DISABLED
        for d in self.deliveries.values():  # 同事务取消待投递
            if d["subscription_id"] == sub_id and d["status"] == DELIVERY_PENDING:
                d["status"] = DELIVERY_CANCELLED
        return sub

    # ------------------------------------------------------------ 投递（复刻 dispatcher.py 组内语义）
    def dispatch_group(self, sub_id, driver_id, transport, now=None):
        """处理一个 (订阅, 司机) 组：按提交顺序逐条投递，失败即停（不越过队首）。

        ``transport(url, body) -> (ok, error)`` 模拟回调结果。
        """
        now = now or self._now()
        sent = []
        while True:
            head = self._head(sub_id, driver_id)
            if head is None:
                break
            if head["next_retry_at"] is not None and head["next_retry_at"] > now:
                break  # 队首退避中：组内后续事件不能越过
            sub = self.subscriptions[head["subscription_id"]]
            if sub["status"] != SUB_ACTIVE:
                break  # 订阅已停用：不再发送
            event = self.events[head["event_seq"]]
            body = build_payload(
                event_id=event["event_id"], subscription_id=sub_id,
                driver_id=event["driver_id"], rules_before=event["rules_before"],
                rules_after=event["rules_after"], committed_at=event["committed_at"],
            )
            ok, error = transport(sub["callback_url"], body)
            head["attempts"] += 1
            if ok:
                head["status"] = DELIVERY_DELIVERED
                head["last_error"] = None
                head["next_retry_at"] = None
                sent.append(body)
            else:
                head["last_error"] = error
                delay = retry_delay_seconds(head["attempts"], RETRY_BASE, RETRY_MAX)
                head["next_retry_at"] = now + timedelta(seconds=delay)
                break  # 失败：保留待重试，组内停止（不阻塞其他组）
        return sent

    def _head(self, sub_id, driver_id):
        pending = [
            d for d in self.deliveries.values()
            if d["subscription_id"] == sub_id and d["driver_id"] == driver_id
            and d["status"] == DELIVERY_PENDING
        ]
        return min(pending, key=lambda d: d["event_seq"]) if pending else None

    def driver_events(self, driver_id):
        return [e for e in self.events if e["driver_id"] == driver_id]

    def sub_deliveries(self, sub_id):
        return [d for d in self.deliveries.values() if d["subscription_id"] == sub_id]


def ok_transport(url, body):
    return True, None


def fail_transport(url, body):
    return False, "HTTP 500"


def mv(sid, rev, driver, sh, eh, typ="DRIVING", day=0):
    return Move(sid, rev, driver, t(sh, day=day), t(eh, day=day), typ)


class EventGenerationTest(unittest.TestCase):
    """事件生成：仅集合变化产生事件；失败与幂等重放不产生。"""

    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")

    def test_violation_appears_generates_one_event(self):
        # 驾驶 08-12 + 12-13：累计驾驶 5h > 4h，越限
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.assertEqual(self.db.events, [])  # 首段未越限：集合未变，无事件
        self.db.create_segment("SEG-B", "D1", 12, 13)
        events = self.db.driver_events("D1")
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["rules_before"], [])
        self.assertEqual(evt["rules_after"], [RULE_DRIVING_STINT])
        self.assertTrue(evt["event_id"].startswith("evt_"))
        self.assertIsNotNone(evt["committed_at"])

    def test_violation_resolved_generates_event(self):
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.db.create_segment("SEG-B", "D1", 12, 13)
        # 把 SEG-B 改成 40 分钟休息：违规消除
        self.db.patch_segment("SEG-B", 1, eh=12 + 40 / 60, typ="REST")
        events = self.db.driver_events("D1")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["rules_before"], [RULE_DRIVING_STINT])
        self.assertEqual(events[1]["rules_after"], [])

    def test_same_rule_set_no_event_even_if_violation_detail_changes(self):
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.db.create_segment("SEG-B", "D1", 12, 13)
        # 更正 SEG-A 为 08:30-12：累计驾驶 3.5h+1h=4.5h 仍越限，规则集合不变 -> 无事件
        self.db.patch_segment("SEG-A", 1, sh=8.5)
        self.assertEqual(len(self.db.driver_events("D1")), 1)

    def test_failed_requests_generate_no_events(self):
        self.db.create_segment("SEG-A", "D1", 8, 12)
        with self.assertRaises(HttpError):  # 重叠 409
            self.db.create_segment("SEG-X", "D1", 9, 10)
        with self.assertRaises(HttpError):  # 修订号不符 409
            self.db.patch_segment("SEG-A", 7, eh=13)
        with self.assertRaises(HttpError):  # 片段不存在 404
            self.db.patch_segment("NOPE", 1, eh=13)
        with self.assertRaises(HttpError):  # 司机不存在 404
            self.db.create_segment("SEG-Y", "GHOST", 8, 9)
        self.assertEqual(self.db.events, [])

    def test_idempotent_segment_replay_generates_no_event(self):
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.db.create_segment("SEG-B", "D1", 12, 13)
        n = len(self.db.events)
        self.db.create_segment_idempotent("SEG-B", "D1", 12, 13)  # 幂等重试
        self.assertEqual(len(self.db.events), n)

    def test_batch_events_per_affected_driver(self):
        # D1: SEG-A 08-12 驾驶；D2: SEG-B 12-13 驾驶（同日）。批次把 SEG-B 调给 D1：
        # D1 变为 08-12 + 12-13 连续驾驶 -> 越限（产生事件）；D2 始终合规（无事件）
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.db.create_segment("SEG-B", "D2", 12, 13)
        self.db.submit_batch("B1", [mv("SEG-B", 1, "D1", 12, 13)])
        self.assertEqual(len(self.db.driver_events("D1")), 1)
        self.assertEqual(self.db.driver_events("D2"), [])

    def test_batch_rejection_and_replay_generate_no_events(self):
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.db.create_segment("SEG-B", "D2", 12, 13)
        with self.assertRaises(HttpError):  # 修订号过期 -> 整批拒绝
            self.db.submit_batch("B2", [mv("SEG-B", 9, "D1", 12, 13)])
        self.assertEqual(self.db.events, [])
        self.db.submit_batch("B3", [mv("SEG-B", 1, "D1", 12, 13)])
        n = len(self.db.events)
        self.db.submit_batch("B3", [mv("SEG-B", 1, "D1", 12, 13)])  # 同内容重放
        self.assertEqual(len(self.db.events), n)

    def test_events_have_stable_ids_and_commit_order(self):
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.db.create_segment("SEG-B", "D1", 12, 13)
        self.db.patch_segment("SEG-B", 1, eh=12 + 40 / 60, typ="REST")
        events = self.db.driver_events("D1")
        self.assertEqual([e["seq"] for e in events], [0, 1])  # 提交顺序
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])
        self.assertGreater(events[1]["committed_at"], events[0]["committed_at"])


class SubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")

    def test_register_idempotent_and_conflict(self):
        sub = self.db.register_subscription("S1", "D1", [RULE_DRIVING_STINT], "http://a/cb", "secret-123")
        again = self.db.register_subscription("S1", "D1", [RULE_DRIVING_STINT], "http://a/cb", "secret-123")
        self.assertIs(again, sub)
        with self.assertRaises(HttpError) as ctx:
            self.db.register_subscription("S1", "D1", [RULE_ROLLING_24H], "http://a/cb", "secret-123")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "subscription_content_conflict")

    def test_register_missing_driver_404(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.register_subscription("S1", "GHOST", [RULE_DRIVING_STINT], "http://a/cb", "secret-123")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_deactivate_idempotent_and_missing_404(self):
        self.db.register_subscription("S1", None, [RULE_DRIVING_STINT], "http://a/cb", "secret-123")
        sub = self.db.deactivate_subscription("S1")
        self.assertEqual(sub["status"], SUB_DISABLED)
        self.assertIs(self.db.deactivate_subscription("S1"), sub)  # 幂等
        with self.assertRaises(HttpError) as ctx:
            self.db.deactivate_subscription("NOPE")
        self.assertEqual(ctx.exception.status_code, 404)


class DeliveryMatchingTest(unittest.TestCase):
    """投递记录的创建：司机范围 ∩ 关注规则（按变化规则匹配）。"""

    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")
        # D1 造出累计驾驶越限的固定套路
        self.db.create_segment("SEG-A", "D1", 8, 12)

    def _trigger_d1_violation(self):
        self.db.create_segment("SEG-B", "D1", 12, 13)

    def test_delivery_created_for_matching_subscription(self):
        self.db.register_subscription("S1", "D1", [RULE_DRIVING_STINT], "http://a/cb", "secret-123")
        self._trigger_d1_violation()
        deliveries = self.db.sub_deliveries("S1")
        self.assertEqual(len(deliveries), 1)
        d = deliveries[0]
        self.assertEqual(d["status"], DELIVERY_PENDING)
        self.assertEqual(d["attempts"], 0)
        self.assertEqual(d["event_id"], self.db.driver_events("D1")[0]["event_id"])

    def test_driver_scope_and_rule_filter(self):
        # S2 只关注 D2；S3 只关注滚动 24h 规则 -> 两者都不应收到 D1 的累计驾驶事件
        self.db.register_subscription("S1", None, [RULE_DRIVING_STINT], "http://a/1", "secret-123")
        self.db.register_subscription("S2", "D2", [RULE_DRIVING_STINT], "http://a/2", "secret-123")
        self.db.register_subscription("S3", None, [RULE_ROLLING_24H], "http://a/3", "secret-123")
        self._trigger_d1_violation()
        self.assertEqual(len(self.db.sub_deliveries("S1")), 1)
        self.assertEqual(self.db.sub_deliveries("S2"), [])
        self.assertEqual(self.db.sub_deliveries("S3"), [])

    def test_rule_present_before_and_after_does_not_trigger(self):
        # 先越限（事件 1），再补录其他值勤使滚动 24h 也越限（事件 2，仅滚动规则变化）：
        # 只关注累计驾驶规则的订阅只收到事件 1
        self.db.register_subscription("S1", None, [RULE_DRIVING_STINT], "http://a/1", "secret-123")
        self._trigger_d1_violation()  # 事件 1：{} -> {STINT}
        # 其他值勤 13-20：24h 窗口值勤合计 5h驾驶+7h其他=12h > 10h -> 事件 2：{STINT} -> {STINT, ROLLING}
        self.db.create_segment("SEG-C", "D1", 13, 20, "OTHER_DUTY")
        events = self.db.driver_events("D1")
        self.assertEqual(len(events), 2)
        self.assertEqual(changed_rules(events[1]["rules_before"], events[1]["rules_after"]),
                         [RULE_ROLLING_24H])
        deliveries = self.db.sub_deliveries("S1")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["event_id"], events[0]["event_id"])

    def test_no_subscription_event_still_recorded(self):
        self._trigger_d1_violation()
        self.assertEqual(len(self.db.events), 1)  # 事件落库与订阅无关
        self.assertEqual(self.db.deliveries, {})

    def test_subscription_registered_later_does_not_backfill(self):
        self._trigger_d1_violation()
        self.db.register_subscription("S1", None, [RULE_DRIVING_STINT], "http://a/1", "secret-123")
        self.assertEqual(self.db.sub_deliveries("S1"), [])


class DeliveryDispatchTest(unittest.TestCase):
    """投递语义：组内按序、失败重试、停用取消、互不阻塞。"""

    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")
        self.db.register_subscription("S1", None, [RULE_DRIVING_STINT], "http://a/d1", "secret-123")
        # D1：越限 -> 消除（两个事件）；D2：越限（一个事件）
        self.db.create_segment("SEG-A", "D1", 8, 12)
        self.db.create_segment("SEG-B", "D1", 12, 13)
        self.db.patch_segment("SEG-B", 1, eh=12 + 40 / 60, typ="REST")
        self.db.create_segment("SEG-C", "D2", 8, 12, day=1)
        self.db.create_segment("SEG-D", "D2", 12, 13, day=1)

    def test_delivered_in_commit_order_with_same_event_id(self):
        bodies = self.db.dispatch_group("S1", "D1", ok_transport)
        self.assertEqual(len(bodies), 2)
        events = self.db.driver_events("D1")
        self.assertEqual(bodies[0]["event_id"], events[0]["event_id"])  # 先提交先投递
        self.assertEqual(bodies[1]["event_id"], events[1]["event_id"])
        self.assertEqual(bodies[0]["rules_after"], [RULE_DRIVING_STINT])
        self.assertEqual(bodies[1]["rules_after"], [])
        for d in self.db.sub_deliveries("S1"):
            if d["driver_id"] == "D1":
                self.assertEqual(d["status"], DELIVERY_DELIVERED)
                self.assertEqual(d["attempts"], 1)
                self.assertIsNone(d["last_error"])

    def test_failure_keeps_pending_and_records_attempts_and_error(self):
        self.db.dispatch_group("S1", "D1", fail_transport)
        head = self.db._head("S1", "D1")
        self.assertIsNotNone(head)
        self.assertEqual(head["attempts"], 1)
        self.assertEqual(head["last_error"], "HTTP 500")
        self.assertIsNotNone(head["next_retry_at"])
        # 重试沿用同一事件 ID；成功后继续投递下一条
        bodies = self.db.dispatch_group("S1", "D1", ok_transport,
                                        now=head["next_retry_at"] + timedelta(seconds=1))
        events = self.db.driver_events("D1")
        self.assertEqual([b["event_id"] for b in bodies],
                         [events[0]["event_id"], events[1]["event_id"]])
        d1 = [d for d in self.db.sub_deliveries("S1") if d["driver_id"] == "D1"]
        self.assertEqual(d1[0]["attempts"], 2)  # 失败一次 + 成功一次
        self.assertEqual(d1[0]["status"], DELIVERY_DELIVERED)
        self.assertIsNone(d1[0]["last_error"])

    def test_head_failure_blocks_group_but_not_other_groups(self):
        # D1 组失败：D1 第二条不越过队首；D2 组不受影响
        self.db.dispatch_group("S1", "D1", fail_transport)
        sent_d2 = self.db.dispatch_group("S1", "D2", ok_transport)
        self.assertEqual(len(sent_d2), 1)  # 其他司机正常投递
        d1_pending = [d for d in self.db.sub_deliveries("S1")
                      if d["driver_id"] == "D1" and d["status"] == DELIVERY_PENDING]
        self.assertEqual(len(d1_pending), 2)  # 队首失败，后续不越过

    def test_backoff_blocks_until_due(self):
        self.db.dispatch_group("S1", "D1", fail_transport)
        head = self.db._head("S1", "D1")
        # 退避未到期：不再尝试（attempts 不增加）
        sent = self.db.dispatch_group("S1", "D1", ok_transport,
                                      now=head["next_retry_at"] - timedelta(seconds=1))
        self.assertEqual(sent, [])
        self.assertEqual(head["attempts"], 1)

    def test_deactivate_cancels_pending_and_stops_sending(self):
        self.db.deactivate_subscription("S1")
        for d in self.db.sub_deliveries("S1"):
            self.assertEqual(d["status"], DELIVERY_CANCELLED)
        sent = self.db.dispatch_group("S1", "D1", ok_transport)
        self.assertEqual(sent, [])  # 停用后不再发送
        # 停用后再产生新事件（次日连续驾驶 4h+1h 越限）：事件照常落库，
        # 但不再为已停用订阅生成待投递记录
        n_events = len(self.db.driver_events("D1"))
        n_deliveries = len(self.db.sub_deliveries("S1"))
        self.db.create_segment("SEG-E", "D1", 14, 18, "OTHER_DUTY", day=2)
        self.db.create_segment("SEG-F", "D1", 18, 22, day=2)
        self.db.create_segment("SEG-G", "D1", 22, 23, day=2)
        self.assertEqual(len(self.db.driver_events("D1")), n_events + 1)
        self.assertEqual(len(self.db.sub_deliveries("S1")), n_deliveries)


if __name__ == "__main__":
    unittest.main()
