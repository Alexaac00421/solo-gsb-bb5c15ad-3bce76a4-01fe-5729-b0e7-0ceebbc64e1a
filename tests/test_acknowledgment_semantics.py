"""告警确认端到端语义参考实现（不依赖 FastAPI/SQLAlchemy，仅用标准库）。

在无法安装 Web 依赖的环境中，用内存存储复刻 app/api.py 中告警确认相关
处理器的判定顺序，覆盖需求中的关键场景：

- 只能针对当前实时结果中存在的违规创建确认（否则 409 violation_not_found）；
- 司机不存在返回 404 driver_not_found；
- 创建时固化覆盖的片段修订集合（司机全部现存片段）；
- 同一司机、同一规则且修订集合未变时重复提交返回原记录，备注不同也不新增；
- 任一片段增删或修订后原确认失效，查询返回失效原因（新增/移除/修订）；
- 修订集合变化后可再次提交生成新记录，旧记录保持失效。
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.acknowledgment import (
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    ack_fingerprint,
    invalidation_diff,
    revision_set,
)
from app.compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H, Seg, evaluate_driver

BASE = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def t(h, m=0, day=0):
    return BASE + timedelta(days=day, hours=h, minutes=m)


class HttpError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class MemDB:
    """内存表 + 失效扫描（对应 api.py 写路径同事务调用的 _invalidate_stale_acknowledgments）。"""

    def __init__(self):
        self.drivers: dict[str, dict] = {}
        self.segments: dict[str, dict] = {}
        self.acks: dict[int, dict] = {}
        self._ack_seq = 0

    # ------------------------------------------------------------ 基础数据与写路径
    def add_driver(self, d):
        self.drivers[d] = {"driver_id": d, "name": d}

    def add_segment(self, sid, driver, sh, eh, typ="DRIVING", day=0):
        """对应 POST /segments 的实际插入路径：落库后做失效扫描。"""
        self.segments[sid] = {
            "segment_id": sid,
            "driver_id": driver,
            "start": t(sh, day=day),
            "end": t(eh, day=day),
            "type": typ,
            "revision": 1,
        }
        self._invalidate(driver)

    def patch_segment(self, sid, *, driver_id=None, sh=None, eh=None, typ=None):
        """对应 PATCH /segments/{id}：修订号 +1，新/旧司机都做失效扫描。"""
        seg = self.segments[sid]
        old_driver = seg["driver_id"]
        if driver_id is not None:
            seg["driver_id"] = driver_id
        if sh is not None:
            seg["start"] = t(sh)
        if eh is not None:
            seg["end"] = t(eh)
        if typ is not None:
            seg["type"] = typ
        seg["revision"] += 1
        self._invalidate(seg["driver_id"])
        if old_driver != seg["driver_id"]:
            self._invalidate(old_driver)

    def driver_segments(self, driver):
        return sorted(
            (s for s in self.segments.values() if s["driver_id"] == driver),
            key=lambda s: (s["start"], s["end"], s["segment_id"]),
        )

    def _revision_set(self, driver):
        return revision_set(
            (s["segment_id"], s["revision"]) for s in self.driver_segments(driver)
        )

    def _invalidate(self, driver):
        """与 api._invalidate_stale_acknowledgments 同逻辑：集合不匹配即失效并记录原因。"""
        current = self._revision_set(driver)
        for ack in self.acks.values():
            if ack["driver_id"] != driver or ack["status"] != STATUS_ACTIVE:
                continue
            diff = invalidation_diff(ack["segment_revisions"], current)
            if diff is not None:
                ack["status"] = STATUS_INVALIDATED
                ack["invalidation"] = diff

    # ------------------------------------------------------------ 告警确认（复刻 create_acknowledgment 判定顺序）
    def create_acknowledgment(self, driver_id, rule, note):
        if driver_id not in self.drivers:
            raise HttpError(404, {"error": "driver_not_found", "driver_id": driver_id})

        revisions = self._revision_set(driver_id)
        fp = ack_fingerprint(driver_id, rule, revisions)

        # 幂等：同司机同规则且修订集合未变 -> 返回原记录（备注不同也不新增）
        for ack in self.acks.values():
            if (ack["driver_id"], ack["rule"], ack["fingerprint"]) == (driver_id, rule, fp):
                return {"created_new": False, "acknowledgment": ack}

        # 只能确认当前实时结果中存在的违规
        result = evaluate_driver(
            Seg(s["segment_id"], s["start"], s["end"], s["type"])
            for s in self.driver_segments(driver_id)
        )
        violation = next((v for v in result["violations"] if v["rule"] == rule), None)
        if violation is None:
            raise HttpError(
                409,
                {
                    "error": "violation_not_found",
                    "driver_id": driver_id,
                    "rule": rule,
                    "current_violations": [v["rule"] for v in result["violations"]],
                },
            )

        self._ack_seq += 1
        ack = {
            "ack_id": self._ack_seq,
            "driver_id": driver_id,
            "rule": rule,
            "note": note,
            "fingerprint": fp,
            "segment_revisions": revisions,
            "violation_snapshot": violation,
            "status": STATUS_ACTIVE,
            "invalidation": None,
        }
        self.acks[ack["ack_id"]] = ack
        return {"created_new": True, "acknowledgment": ack}

    def list_acknowledgments(self, driver_id):
        return [a for a in self.acks.values() if a["driver_id"] == driver_id]


class AcknowledgmentSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")
        # D1：驾驶 08-12 + 12-13（相接，中间无合格休息）-> 累计驾驶 5h > 4h，越限于 12:00
        self.db.add_segment("SEG-A", "D1", 8, 12)
        self.db.add_segment("SEG-B", "D1", 12, 13)

    def test_create_only_for_existing_violation(self):
        res = self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "已通知司机")
        self.assertTrue(res["created_new"])
        ack = res["acknowledgment"]
        self.assertEqual(ack["status"], STATUS_ACTIVE)
        self.assertIsNone(ack["invalidation"])
        # 固化提交时覆盖的片段修订集合（司机全部现存片段，按 ID 排序）
        self.assertEqual(
            ack["segment_revisions"],
            [{"segment_id": "SEG-A", "revision": 1}, {"segment_id": "SEG-B", "revision": 1}],
        )
        # 固化提交时命中的违规明细
        self.assertEqual(ack["violation_snapshot"]["rule"], RULE_DRIVING_STINT)
        self.assertEqual(ack["violation_snapshot"]["segment_ids"], ["SEG-A", "SEG-B"])

    def test_compliant_rule_rejected_with_current_violations(self):
        # D1 当前只有累计驾驶越限，滚动 24h 未越限
        with self.assertRaises(HttpError) as ctx:
            self.db.create_acknowledgment("D1", RULE_ROLLING_24H, "并无该违规")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "violation_not_found")
        self.assertEqual(ctx.exception.detail["current_violations"], [RULE_DRIVING_STINT])
        self.assertEqual(self.db.list_acknowledgments("D1"), [])

    def test_compliant_driver_rejected(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.create_acknowledgment("D2", RULE_DRIVING_STINT, "无任何违规")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["current_violations"], [])

    def test_missing_driver_404(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.create_acknowledgment("GHOST", RULE_DRIVING_STINT, "x")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail["error"], "driver_not_found")

    def test_repeat_submission_returns_original_even_with_different_note(self):
        r1 = self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "首次确认")
        r2 = self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "换个备注也不新增")
        self.assertFalse(r2["created_new"])
        self.assertIs(r2["acknowledgment"], r1["acknowledgment"])
        # 备注保持首次提交内容，且只有一条记录
        self.assertEqual(r2["acknowledgment"]["note"], "首次确认")
        self.assertEqual(len(self.db.list_acknowledgments("D1")), 1)

    def test_segment_addition_invalidates_with_reason(self):
        self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "确认")
        self.db.add_segment("SEG-C", "D1", 14, 15, "OTHER_DUTY")
        ack = self.db.list_acknowledgments("D1")[0]
        self.assertEqual(ack["status"], STATUS_INVALIDATED)
        self.assertEqual(ack["invalidation"]["added"], ["SEG-C"])
        self.assertIn("SEG-C", ack["invalidation"]["reason"])

    def test_segment_revision_invalidates_with_reason(self):
        self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "确认")
        self.db.patch_segment("SEG-B", eh=12.5)
        ack = self.db.list_acknowledgments("D1")[0]
        self.assertEqual(ack["status"], STATUS_INVALIDATED)
        self.assertEqual(
            ack["invalidation"]["revision_changed"],
            [{"segment_id": "SEG-B", "acknowledged_revision": 1, "current_revision": 2}],
        )
        self.assertIn("SEG-B", ack["invalidation"]["reason"])

    def test_segment_moved_to_other_driver_invalidates_both_sides(self):
        # D2 也造出违规并确认：其他值勤 06-12 + 13-19，滚动 24h 合计 12h > 10h
        self.db.add_segment("SEG-X", "D2", 6, 12, "OTHER_DUTY")
        self.db.add_segment("SEG-Y", "D2", 13, 19, "OTHER_DUTY")
        self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "D1 确认")
        self.db.create_acknowledgment("D2", RULE_ROLLING_24H, "D2 确认")

        # SEG-B 从 D1 调到 D2：对 D1 是移除，对 D2 是新增
        self.db.patch_segment("SEG-B", driver_id="D2")

        ack1 = self.db.list_acknowledgments("D1")[0]
        self.assertEqual(ack1["status"], STATUS_INVALIDATED)
        self.assertEqual(ack1["invalidation"]["removed"], ["SEG-B"])

        ack2 = self.db.list_acknowledgments("D2")[0]
        self.assertEqual(ack2["status"], STATUS_INVALIDATED)
        self.assertEqual(ack2["invalidation"]["added"], ["SEG-B"])

    def test_unrelated_driver_not_affected(self):
        # D2：其他值勤 06-12 + 13-19，滚动 24h 合计 12h > 10h
        self.db.add_segment("SEG-X", "D2", 6, 12, "OTHER_DUTY")
        self.db.add_segment("SEG-Y", "D2", 13, 19, "OTHER_DUTY")
        self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "D1 确认")
        self.db.create_acknowledgment("D2", RULE_ROLLING_24H, "D2 确认")

        self.db.patch_segment("SEG-A", eh=11)  # 只动 D1 的片段

        self.assertEqual(self.db.list_acknowledgments("D1")[0]["status"], STATUS_INVALIDATED)
        self.assertEqual(self.db.list_acknowledgments("D2")[0]["status"], STATUS_ACTIVE)

    def test_resubmit_after_change_creates_new_record_and_old_stays_invalid(self):
        r1 = self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "首次确认")
        self.db.patch_segment("SEG-B", eh=12.5)  # 修订集合变化 -> 原确认失效（违规仍存在）
        r2 = self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "修订后再次确认")
        self.assertTrue(r2["created_new"])
        self.assertNotEqual(r2["acknowledgment"]["fingerprint"], r1["acknowledgment"]["fingerprint"])
        self.assertEqual(
            r2["acknowledgment"]["segment_revisions"],
            [{"segment_id": "SEG-A", "revision": 1}, {"segment_id": "SEG-B", "revision": 2}],
        )
        acks = self.db.list_acknowledgments("D1")
        self.assertEqual(len(acks), 2)
        self.assertEqual(acks[0]["status"], STATUS_INVALIDATED)
        self.assertEqual(acks[1]["status"], STATUS_ACTIVE)

    def test_resubmit_after_violation_resolved_rejected(self):
        self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "确认")
        # 把 SEG-B 改成 40 分钟合格休息：违规消除，同时修订集合变化使原确认失效
        self.db.patch_segment("SEG-B", eh=12.666666666666666, typ="REST")
        with self.assertRaises(HttpError) as ctx:
            self.db.create_acknowledgment("D1", RULE_DRIVING_STINT, "违规已消除")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "violation_not_found")
        acks = self.db.list_acknowledgments("D1")
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]["status"], STATUS_INVALIDATED)


if __name__ == "__main__":
    unittest.main()
