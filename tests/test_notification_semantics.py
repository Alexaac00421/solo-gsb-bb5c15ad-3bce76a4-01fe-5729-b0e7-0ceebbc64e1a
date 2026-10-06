"""违规变更通知端到端语义参考实现（不依赖 FastAPI/SQLAlchemy，仅用标准库）。

在无法安装 Web 依赖的环境中，用内存存储复刻 app/api.py 中片段写路径的
事件产生（``_emit_violation_events``）、app/dispatcher.py 的队头投递/重试，
以及订阅停用语义，覆盖需求关键场景：

- 片段录入 / 更正 / 跨司机批量调整成功提交时，按受影响司机比较事务前后的
  违规**规则集合**，仅集合变化才各产生一个事件（稳定事件 ID、司机、变化前后
  规则、提交时刻）；集合相同（即使首次越限时刻/参与片段变化）不产生事件；
- 失败请求（重叠 / 修订号过期 / 整批拒绝）与幂等重放不产生事件；
- 订阅范围：指定司机 vs 全体司机；关注规则与“变化前 ∪ 变化后”有交集才投递；
- 同一订阅下同一司机的事件按提交顺序投递：队头失败退避时，后续事件不超车，
  其他司机/订阅不被阻塞；超时或非 2xx 保留重试、记录次数与最近错误，
  重试沿用同一事件 ID；
- 停用订阅后待投递事件标记 CANCELLED 不再发送，已成功记录保持 SUCCEEDED；
- 回调以共享密钥 HMAC-SHA256 签名（app.notifications.sign_payload/verify）。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from app.compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H, Seg, evaluate_driver
from app.notifications import (
    DELIVERY_CANCELLED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SUCCEEDED,
    SUB_ACTIVE,
    SUB_DISABLED,
    SubscriptionError,
    payload_bytes,
    rule_set,
    rule_set_delta,
    subscription_matches,
    validate_callback_url,
    validate_rules,
    validate_secret,
    verify_signature,
    sign_payload,
)
from app.reassignment import BatchRejection, Move, final_layout

BASE = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def t(h, m=0, day=0):
    return BASE + timedelta(days=day, hours=h, minutes=m)


class HttpError(Exception):
    def __init__(self, status_code, code):
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class MemDB:
    """内存表，复刻 api.py 写路径 + dispatcher 的投递判定顺序。"""

    def __init__(self):
        self.drivers: dict[str, dict] = {}
        self.segments: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.subs: dict[str, dict] = {}
        self.events: dict[int, dict] = {}
        self.deliveries: dict[int, dict] = {}
        self._event_seq = 0
        self._delivery_seq = 0
        self.tick = 0  # 假时钟（退避用）
        # 传输脚本：sub_id -> deque(结果)；元素 "ok" / (status, reason) / "NETERR"
        self.scripts: dict[str, list] = {}
        self.sent: list[tuple] = []  # (sub_id, driver_id, event_id, attempts)
        self.signed_bodies: list[tuple] = []  # (secret, timestamp, body, signature)

    # ------------------------------------------------------------ 基础数据
    def add_driver(self, d):
        self.drivers[d] = d

    def driver_segments(self, driver):
        return sorted(
            (s for s in self.segments.values() if s["driver_id"] == driver),
            key=lambda s: (s["start"], s["end"], s["segment_id"]),
        )

    def evaluate(self, driver):
        return evaluate_driver(
            Seg(s["segment_id"], s["start"], s["end"], s["type"])
            for s in self.driver_segments(driver)
        )

    def add_subscription(self, sid, *, driver_id=None, rules=(RULE_DRIVING_STINT,),
                         url="http://scheduler/hook", secret="shared-secret-00"):
        rules = validate_rules(list(rules))
        validate_callback_url(url)
        validate_secret(secret)
        self.subs[sid] = {
            "subscription_id": sid, "driver_id": driver_id, "rules": rules,
            "callback_url": url, "secret": secret, "status": SUB_ACTIVE,
        }
        return sid

    # ------------------------------------------------------------ 事件产生/扇出
    def _emit(self, deltas: dict[str, frozenset | None], committed_at):
        """对应 api._emit_violation_events：仅集合变化产生事件；事件始终落库，
        投递记录仅为匹配的活跃订阅扇出（无订阅时仍有事件可查）。"""
        active = [s for s in self.subs.values() if s["status"] == SUB_ACTIVE]
        for driver_id in sorted(deltas):
            delta = rule_set_delta(deltas[driver_id][0], deltas[driver_id][1])
            if delta is None:
                continue
            self._event_seq += 1
            eid = self._event_seq
            payload = {
                "event_id": eid,
                "event_type": "driver.violation_rules.changed",
                "driver_id": driver_id,
                "rules_before": delta["rules_before"],
                "rules_after": delta["rules_after"],
                "committed_at": committed_at.isoformat(),
            }
            self.events[eid] = {"event_id": eid, "payload": payload}
            before, after = deltas[driver_id]
            for sub in active:
                if not subscription_matches(
                    subscription_driver_id=sub["driver_id"],
                    subscription_rules=frozenset(sub["rules"]),
                    event_driver_id=driver_id,
                    rules_before=before,
                    rules_after=after,
                ):
                    continue
                self._delivery_seq += 1
                self.deliveries[self._delivery_seq] = {
                    "delivery_id": self._delivery_seq, "event_id": eid,
                    "subscription_id": sub["subscription_id"], "driver_id": driver_id,
                    "status": DELIVERY_PENDING, "attempts": 0, "last_error": None,
                    "last_response_code": None, "next_attempt_at": None,
                }

    # ------------------------------------------------------------ 片段写路径
    def add_segment(self, sid, driver, sh, eh, typ="DRIVING", day=0):
        """对应 POST /segments 真实新增路径（幂等重放由调用方提前返回，不进这里）。"""
        if driver not in self.drivers:
            raise HttpError(404, "driver_not_found")
        for x in self.driver_segments(driver):  # 重叠校验，相接合法
            if x["start"] < t(eh, day=day) and x["end"] > t(sh, day=day):
                raise HttpError(409, "segment_overlap")
        committed_at = datetime.now(timezone.utc)
        before = rule_set(self.evaluate(driver))
        self.segments[sid] = {
            "segment_id": sid, "driver_id": driver, "start": t(sh, day=day),
            "end": t(eh, day=day), "type": typ, "revision": 1,
        }
        after = rule_set(self.evaluate(driver))
        self._emit({driver: (before, after)}, committed_at)

    def post_segment_idempotent(self, sid, driver, sh, eh, typ="DRIVING", day=0):
        """对应 POST /segments：已存在且内容一致 -> 幂等重放，不产生事件。"""
        existing = self.segments.get(sid)
        if existing is not None:
            if (existing["driver_id"], existing["start"], existing["end"], existing["type"]) != (
                driver, t(sh, day=day), t(eh, day=day), typ
            ):
                raise HttpError(409, "segment_content_conflict")
            return  # 幂等重放：不产生事件
        self.add_segment(sid, driver, sh, eh, typ, day)

    def patch_segment(self, sid, *, expected_revision, driver_id=None, sh=None, sm=0,
                      eh=None, em=0, typ=None, day=0):
        """对应 PATCH /segments/{id}：修订号不符 409；成功才对源∪目标司机发事件。"""
        seg = self.segments.get(sid)
        if seg is None:
            raise HttpError(404, "segment_not_found")
        if seg["revision"] != expected_revision:
            raise HttpError(409, "revision_conflict")  # 失败：无改动、无事件
        new_driver = driver_id or seg["driver_id"]
        new_start = t(sh, sm, day) if sh is not None else seg["start"]
        new_end = t(eh, em, day) if eh is not None else seg["end"]
        new_type = typ or seg["type"]
        if new_end <= new_start:
            raise HttpError(422, "invalid_time")
        for x in self.driver_segments(new_driver):
            if x["segment_id"] == sid:
                continue
            if x["start"] < new_end and x["end"] > new_start:
                raise HttpError(409, "segment_overlap")  # 失败：无改动、无事件

        old_driver = seg["driver_id"]
        affected = sorted({old_driver, new_driver})
        committed_at = datetime.now(timezone.utc)
        before = {d: rule_set(self.evaluate(d)) for d in affected}
        seg.update(driver_id=new_driver, start=new_start, end=new_end, type=new_type)
        seg["revision"] += 1
        after = {d: rule_set(self.evaluate(d)) for d in affected}
        self._emit({d: (before[d], after[d]) for d in affected}, committed_at)

    def submit_batch(self, batch_id, moves: list[Move]):
        """对应 POST /reassignment-batches：成功才按受影响司机发事件。"""
        prior = self.batches.get(batch_id)
        if prior is not None:
            if prior["status"] >= 400:
                raise HttpError(prior["status"], "replayed_failure")  # 原样重放首次拒绝
            return {"replayed": True}  # 成功批次幂等重放：不产生事件

        affected = sorted({self.segments[m.segment_id]["driver_id"] for m in moves}
                          | {m.driver_id for m in moves})
        committed_at = datetime.now(timezone.utc)
        before = {d: rule_set(self.evaluate(d)) for d in affected}
        try:
            for m in moves:
                seg = self.segments.get(m.segment_id)
                if seg is None:
                    raise BatchRejection(status_code=404, code="segment_not_found", message="x")
                if seg["revision"] != m.expected_revision:
                    raise BatchRejection(status_code=409, code="revision_conflict", message="x")
            existing = [
                _SegView(x["segment_id"], x["driver_id"], x["start"], x["end"], x["type"])
                for d in affected for x in self.driver_segments(d)
            ]
            final_layout(existing, moves)  # 最终排布重叠 -> BatchRejection
        except BatchRejection as exc:
            self.batches[batch_id] = {"status": exc.status_code}
            raise HttpError(exc.status_code, exc.code)  # 整批拒绝：无改动、无事件

        for m in moves:
            seg = self.segments[m.segment_id]
            seg.update(driver_id=m.driver_id, start=m.start, end=m.end, type=m.type)
            seg["revision"] += 1
        after = {d: rule_set(self.evaluate(d)) for d in affected}
        self._emit({d: (before[d], after[d]) for d in affected}, committed_at)
        self.batches[batch_id] = {"status": 201}
        return {"replayed": False}

    # ------------------------------------------------------------ 投递（dispatcher）
    def script(self, sid, results):
        self.scripts[sid] = list(results)

    def _transport(self, sub, event):
        """脚本化传输：返回 (ok, code, error)；同时记录带签名的请求体。"""
        import time as _time
        results = self.scripts.get(sub["subscription_id"], ["ok"])
        outcome = results.pop(0) if results else "ok"
        body = payload_bytes(event["payload"])
        ts = str(int(_time.time()))
        sig = sign_payload(sub["secret"], ts, body)
        self.signed_bodies.append((sub["secret"], ts, body, sig))
        if outcome == "ok":
            return True, 200, None
        if outcome == "NETERR":
            return False, None, "URLError: connection refused"
        code, reason = outcome
        return False, code, f"HTTP {code}: {reason}"

    def dispatch_once(self):
        """对应 dispatcher 一轮：每 (订阅, 司机) 只放行队头；退避窗口/前序未终态则阻塞。

        假时钟不随轮询自动推进（与真实退避基于墙上时钟一致）；需要越过退避
        窗口时由测试显式 ``db.tick += N``。
        """
        due_status = (DELIVERY_PENDING, DELIVERY_FAILED)
        candidates = [
            d for d in self.deliveries.values()
            if d["status"] in due_status
            and self.subs[d["subscription_id"]]["status"] == SUB_ACTIVE
            and (d["next_attempt_at"] is None or d["next_attempt_at"] <= self.tick)
        ]
        groups: dict[tuple[str, str], dict] = {}
        for d in candidates:
            g = (d["subscription_id"], d["driver_id"])
            # 组内只取最小 event_id，且其前不得有更早的未终态事件（严格保序、不超车）
            earlier = any(
                x["subscription_id"] == g[0] and x["driver_id"] == g[1]
                and x["event_id"] < d["event_id"] and x["status"] in due_status
                for x in self.deliveries.values()
            )
            if earlier:
                continue
            if g not in groups or d["event_id"] < groups[g]["event_id"]:
                groups[g] = d
        for d in groups.values():
            self._send(d)

    def _send(self, d):
        sub = self.subs[d["subscription_id"]]
        event = self.events[d["event_id"]]
        ok, code, error = self._transport(sub, event)
        d["attempts"] += 1
        d["last_response_code"] = code
        self.sent.append((sub["subscription_id"], d["driver_id"], d["event_id"], d["attempts"]))
        if ok:
            d["status"] = DELIVERY_SUCCEEDED
            d["last_error"] = None
            d["next_attempt_at"] = None
        else:
            d["status"] = DELIVERY_FAILED
            d["last_error"] = error
            d["next_attempt_at"] = self.tick + min(2 ** max(0, d["attempts"] - 1), 300)

    def disable(self, sid):
        sub = self.subs[sid]
        sub["status"] = SUB_DISABLED
        for d in self.deliveries.values():
            if d["subscription_id"] == sid and d["status"] in (DELIVERY_PENDING, DELIVERY_FAILED):
                d["status"] = DELIVERY_CANCELLED
                d["next_attempt_at"] = None

    def deliveries_of(self, sid):
        return sorted(
            (d for d in self.deliveries.values() if d["subscription_id"] == sid),
            key=lambda d: (d["driver_id"], d["event_id"]),
        )


class _SegView:
    def __init__(self, segment_id, driver_id, start, end, type):
        self.segment_id = segment_id
        self.driver_id = driver_id
        self.start = start
        self.end = end
        self.type = type


# ------------------------------------------------------------ 事件产生语义


class EventEmissionTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")
        self.db.add_subscription("SUB-ALL")  # 全体司机、默认关注累计驾驶规则

    def test_segment_entry_creates_one_event_with_stable_payload(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)  # 恰好 4h，合规
        self.assertEqual(self.db.events, {})  # 集合空 -> 空，无事件
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 累计 5h 越限
        self.assertEqual(set(self.db.events), {1})
        e = self.db.events[1]["payload"]
        self.assertEqual(e["event_id"], 1)  # 稳定事件 ID
        self.assertEqual(e["driver_id"], "D1")
        self.assertEqual(e["rules_before"], [])
        self.assertEqual(e["rules_after"], [RULE_DRIVING_STINT])
        self.assertEqual(e["event_type"], "driver.violation_rules.changed")
        self.assertTrue(e["committed_at"])  # 提交时刻

    def test_set_unchanged_no_event_even_if_violation_detail_changes(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 越限于 12:00
        self.assertEqual(set(self.db.events), {1})
        # 延长 SEG-B 到 12:30：首次越限时刻/参与量变化，但规则集合仍是 {stint} -> 无事件
        self.db.patch_segment("SEG-B", expected_revision=1, eh=12, em=30)
        self.assertEqual(set(self.db.events), {1})

    def test_correction_resolving_violation_emits_event(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)
        # SEG-B 改成 40 分钟合格休息：违规消除 {stint} -> {}
        self.db.patch_segment("SEG-B", expected_revision=1, sh=12, eh=12, em=40, typ="REST")
        self.assertEqual(set(self.db.events), {1, 2})
        self.assertEqual(self.db.events[2]["payload"]["rules_before"], [RULE_DRIVING_STINT])
        self.assertEqual(self.db.events[2]["payload"]["rules_after"], [])

    def test_failed_requests_produce_no_event(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)
        # 过期修订号 -> 409，无改动、无事件
        with self.assertRaises(HttpError) as ctx:
            self.db.patch_segment("SEG-B", expected_revision=99, eh=14)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.db.segments["SEG-B"]["revision"], 1)
        # 重叠 -> 409，无事件
        with self.assertRaises(HttpError):
            self.db.add_segment("SEG-X", "D1", 9, 11)
        self.assertEqual(set(self.db.events), {1})

    def test_idempotent_replay_produces_no_event(self):
        self.db.post_segment_idempotent("SEG-A", "D1", 8, 12)
        self.db.post_segment_idempotent("SEG-B", "D1", 12, 13)
        self.assertEqual(set(self.db.events), {1})
        # 同 ID 同内容重放：返回成功但不产生第二个事件
        self.db.post_segment_idempotent("SEG-B", "D1", 12, 13)
        self.assertEqual(set(self.db.events), {1})

    def test_batch_success_only_changed_drivers_get_events(self):
        # D1：08-12 + 12-13 累计驾驶越限；D2 空闲
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)
        # 把 SEG-B 整批改调到 D2（同一天同时刻）：D1 恢复合规，D2 只开 1h 合规
        moves = [Move("SEG-B", 1, "D2", t(12), t(13), "DRIVING")]
        res = self.db.submit_batch("B1", moves)
        self.assertFalse(res["replayed"])
        # 仅 D1 集合 {stint}->{} 产生事件；D2 空->空无事件
        drivers = {eid: e["payload"]["driver_id"] for eid, e in self.db.events.items()}
        self.assertEqual(drivers, {1: "D1", 2: "D1"})
        self.assertEqual(self.db.events[2]["payload"]["rules_before"], [RULE_DRIVING_STINT])
        self.assertEqual(self.db.events[2]["payload"]["rules_after"], [])
        # 幂等重放：不再产生事件
        self.db.submit_batch("B1", moves)
        self.assertEqual(set(self.db.events), {1, 2})

    def test_batch_failure_produces_no_event_and_changes_nothing(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)
        # 修订号已过期 -> 整批拒绝
        with self.assertRaises(HttpError) as ctx:
            self.db.submit_batch("B2", [Move("SEG-B", 99, "D2", t(12), t(13), "DRIVING")])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.db.segments["SEG-B"]["driver_id"], "D1")
        self.assertEqual(self.db.segments["SEG-B"]["revision"], 1)
        self.assertEqual(set(self.db.events), {1})
        # 失败批次同批次号重放仍失败，不产生事件
        with self.assertRaises(HttpError):
            self.db.submit_batch("B2", [Move("SEG-B", 99, "D2", t(12), t(13), "DRIVING")])
        self.assertEqual(set(self.db.events), {1})

    def test_event_ids_monotonic_with_commit_order(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 事件 1
        self.db.add_segment("SEG-Y", "D2", 6, 12, "OTHER_DUTY")
        self.db.add_segment("SEG-Z", "D2", 13, 19, "OTHER_DUTY")  # 滚动 24h 越限 -> 事件 2
        self.assertEqual([self.db.events[i]["payload"]["event_id"] for i in (1, 2)], [1, 2])
        self.assertEqual(self.db.events[2]["payload"]["driver_id"], "D2")
        self.assertEqual(self.db.events[2]["payload"]["rules_after"], [RULE_ROLLING_24H])

    def test_event_persisted_even_without_subscriptions(self):
        # 一个订阅都没有：事件仍与片段同事务落库（供事件查询/审计），只是没有投递记录
        self.db.subs.clear()
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 累计驾驶越限
        self.assertEqual(set(self.db.events), {1})
        self.assertEqual(self.db.events[1]["payload"]["rules_after"], [RULE_DRIVING_STINT])
        self.assertEqual(self.db.deliveries, {})


# ------------------------------------------------------------ 订阅匹配 / 扇出


class SubscriptionFanoutTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")

    def test_driver_specific_vs_all_drivers(self):
        self.db.add_subscription("SUB-D1", driver_id="D1")
        self.db.add_subscription("SUB-ALL")
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # D1 越限
        # D1 专属与全体订阅各收到一条；没有 D2 的任何投递
        self.assertEqual(
            sorted((d["subscription_id"], d["driver_id"]) for d in self.db.deliveries.values()),
            [("SUB-ALL", "D1"), ("SUB-D1", "D1")],
        )

        # D2 也发生累计驾驶越限：只有全体订阅收到，D1 专属不收
        self.db.add_segment("SEG-Y", "D2", 8, 12)
        self.db.add_segment("SEG-Z", "D2", 12, 13)
        d2_rows = [d for d in self.db.deliveries.values() if d["driver_id"] == "D2"]
        self.assertEqual([d["subscription_id"] for d in d2_rows], ["SUB-ALL"])

    def test_rule_filter_matches_union_before_after(self):
        # 只关注滚动 24h 规则：累计驾驶违规的新增/消除都不应命中
        self.db.add_subscription("SUB-ROLL", rules=[RULE_ROLLING_24H])
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 累计驾驶 {} -> {stint}
        self.assertEqual(self.db.deliveries, {})
        self.db.patch_segment("SEG-B", expected_revision=1, sh=12, eh=12, em=40, typ="REST")
        self.assertEqual(self.db.deliveries, {})  # {stint} -> {} 仍不涉及滚动规则

        # 规则间转换场景：R2 -> R1，关注 R1 的订阅通过 rules_after 命中
        self.assertTrue(
            subscription_matches(
                subscription_driver_id=None, subscription_rules=frozenset({RULE_ROLLING_24H}),
                event_driver_id="D1", rules_before=frozenset({RULE_DRIVING_STINT}),
                rules_after=frozenset({RULE_ROLLING_24H}),
            )
        )

    def test_invalid_subscription_params_rejected(self):
        for bad_rules in ([], ["NO_SUCH_RULE"], [RULE_DRIVING_STINT, "X"]):
            with self.assertRaises(SubscriptionError):
                validate_rules(bad_rules)
        with self.assertRaises(SubscriptionError):
            validate_secret("short")
        for bad_url in ("ftp://x/h", "not-a-url", "https://", "http://no-host"):
            with self.assertRaises(SubscriptionError):
                validate_callback_url(bad_url)


# ------------------------------------------------------------ 投递顺序 / 重试 / 停用


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")
        self.db.add_subscription("SUB-ALL")

    def _violation_events_for_two_drivers(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 事件 1（D1）
        self.db.add_segment("SEG-Y", "D2", 6, 12, "OTHER_DUTY")
        self.db.add_segment("SEG-Z", "D2", 13, 19, "OTHER_DUTY")  # 事件 2（D2，滚动规则不匹配默认订阅）
        # 默认订阅只关注累计驾驶：仅事件 1 有投递。再造 D1 违规消除 -> 事件 3
        self.db.patch_segment("SEG-B", expected_revision=1, sh=12, eh=12, em=40, typ="REST")
        return 1, 3

    def test_same_subscription_same_driver_in_commit_order(self):
        # 默认订阅关注累计驾驶；让 D1 连续产生两个该规则事件
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 事件 1：{} -> {stint}
        self.db.patch_segment("SEG-B", expected_revision=1, sh=12, eh=12, em=40, typ="REST")  # 事件 2：{stint} -> {}
        pending = self.db.deliveries_of("SUB-ALL")
        self.assertEqual([d["event_id"] for d in pending], [1, 2])

        # 队头事件 1 先失败（500）：一轮只发送队头，事件 2 不得超车
        self.db.script("SUB-ALL", [(500, "server error")])
        self.db.dispatch_once()
        self.assertEqual(self.db.sent, [("SUB-ALL", "D1", 1, 1)])
        head = self.db.deliveries_of("SUB-ALL")[0]
        self.assertEqual(head["status"], DELIVERY_FAILED)
        self.assertEqual(head["attempts"], 1)
        self.assertIn("500", head["last_error"])

        # 立刻再轮询：队头仍在退避窗口，整组阻塞，事件 2 不发
        self.db.dispatch_once()
        self.assertEqual(self.db.sent, [("SUB-ALL", "D1", 1, 1)])

        # 推进到退避窗口之后：事件 1 重试成功（沿用同一事件 ID），随后事件 2 才发送
        self.db.tick += 10
        self.db.script("SUB-ALL", ["ok", "ok"])
        self.db.dispatch_once()  # 事件 1 重试成功（attempts=2）；本轮事件 2 尚未轮到
        self.assertEqual(self.db.sent[-1], ("SUB-ALL", "D1", 1, 2))
        self.db.dispatch_once()
        self.assertEqual(self.db.sent[-1], ("SUB-ALL", "D1", 2, 1))
        statuses = {d["event_id"]: d["status"] for d in self.db.deliveries_of("SUB-ALL")}
        self.assertEqual(statuses, {1: DELIVERY_SUCCEEDED, 2: DELIVERY_SUCCEEDED})

    def test_other_driver_delivery_not_blocked(self):
        # 两个订阅关注全部规则，制造 D1/D2 各一个事件
        self.db.subs["SUB-ALL"]["rules"] = validate_rules([RULE_DRIVING_STINT, RULE_ROLLING_24H])
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 事件 1（D1）
        self.db.add_segment("SEG-Y", "D2", 6, 12, "OTHER_DUTY")
        self.db.add_segment("SEG-Z", "D2", 13, 19, "OTHER_DUTY")  # 事件 2（D2）
        # D1 的回调持续失败：D2 同订阅不同司机的队列不得被阻塞
        self.db.script("SUB-ALL", [(500, "err"), "ok"])
        self.db.dispatch_once()
        drivers_sent = {row[1] for row in self.db.sent}
        self.assertEqual(drivers_sent, {"D1", "D2"})  # 两组队头互不阻塞、同轮各自发送
        d2 = [d for d in self.db.deliveries_of("SUB-ALL") if d["driver_id"] == "D2"][0]
        self.assertEqual(d2["status"], DELIVERY_SUCCEEDED)

    def test_network_error_retried_with_same_event_id(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 事件 1
        self.db.script("SUB-ALL", ["NETERR", (503, "busy"), "ok"])
        self.db.dispatch_once()
        d = self.db.deliveries_of("SUB-ALL")[0]
        self.assertEqual(d["status"], DELIVERY_FAILED)
        self.assertEqual(d["attempts"], 1)
        self.assertIsNone(d["last_response_code"])
        self.assertIn("URLError", d["last_error"])
        self.db.tick += 10
        self.db.dispatch_once()  # 第二次失败（503）
        self.assertEqual(d["attempts"], 2)
        self.db.tick += 10
        self.db.dispatch_once()  # 第三次成功
        self.assertEqual(d["status"], DELIVERY_SUCCEEDED)
        self.assertEqual(d["attempts"], 3)
        # 三次发送始终沿用同一稳定事件 ID
        self.assertEqual([row[2] for row in self.db.sent], [1, 1, 1])

    def test_disable_cancels_pending_and_stops_sending(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 事件 1，待投递
        d = self.db.deliveries_of("SUB-ALL")[0]
        self.assertEqual(d["status"], DELIVERY_PENDING)
        self.db.disable("SUB-ALL")
        self.assertEqual(self.db.subs["SUB-ALL"]["status"], SUB_DISABLED)
        self.assertEqual(d["status"], DELIVERY_CANCELLED)
        self.db.dispatch_once()  # 停用后不再发送
        self.assertEqual(self.db.sent, [])

        # 停用之后再发生的 D1 事件也不会为该订阅产生投递（扇出仅取 ACTIVE 订阅）
        self.db.add_subscription("SUB-OTHER")
        self.db.patch_segment("SEG-B", expected_revision=1, sh=12, eh=12, em=40, typ="REST")
        sub_all_rows = self.db.deliveries_of("SUB-ALL")
        self.assertTrue(all(x["status"] == DELIVERY_CANCELLED for x in sub_all_rows))
        # 其他订阅不受影响
        self.assertTrue(any(x["status"] == DELIVERY_PENDING for x in self.db.deliveries_of("SUB-OTHER")))

    def test_callback_body_is_signed_with_shared_secret(self):
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)  # 事件 1
        self.db.dispatch_once()
        secret, ts, body, signature = self.db.signed_bodies[0]
        self.assertEqual(secret, "shared-secret-00")
        self.assertTrue(verify_signature(secret, ts, body, signature))
        # 请求体即规范化事件 JSON，可解析出稳定事件 ID
        parsed = json.loads(body.decode("utf-8"))
        self.assertEqual(parsed["event_id"], 1)
        self.assertEqual(parsed["rules_after"], [RULE_DRIVING_STINT])


if __name__ == "__main__":
    unittest.main()
