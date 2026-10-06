"""端到端语义参考实现（不依赖 FastAPI/SQLAlchemy，仅用标准库）。

在无法安装 Web 依赖的环境中，用内存存储复刻 API 处理器的全部判定顺序
（见 app/api.py 的 _apply_reassignment_batch），覆盖需求中的关键场景：
原子性、最终排布重叠、幂等重放、异内容冲突、修订号过期、目标司机缺失、
修订号只加一、调整前后合规结论。
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.compliance import Seg, evaluate_driver
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


def t(h, m=0, day=0):
    return BASE + timedelta(days=day, hours=h, minutes=m)


class HttpError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class MemDB:
    """内存表 + 全局互斥（对应 pg_advisory_xact_lock 串行化写操作）。"""

    def __init__(self):
        self.drivers: dict[str, dict] = {}
        self.segments: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}

    def add_driver(self, d):
        self.drivers[d] = {"driver_id": d, "name": d}

    def add_segment(self, sid, driver, sh, eh, typ="DRIVING", day=0):
        self.segments[sid] = {
            "segment_id": sid,
            "driver_id": driver,
            "start": t(sh, day=day),
            "end": t(eh, day=day),
            "type": typ,
            "revision": 1,
        }

    def driver_segments(self, driver):
        return sorted(
            (s for s in self.segments.values() if s["driver_id"] == driver),
            key=lambda s: (s["start"], s["end"], s["segment_id"]),
        )

    def submit_batch(self, batch_id, moves: list[Move]) -> dict:
        try:
            ensure_unique_segment_refs(m.segment_id for m in moves)
        except BatchRejection as exc:
            raise HttpError(exc.status_code,
                            {"error": exc.code, "message": exc.message, **exc.extra})
        payload = canonical_payload(moves)
        fp = payload_fingerprint(payload)

        prior = self.batches.get(batch_id)
        if prior is not None:
            if prior["fingerprint"] != fp:
                raise HttpError(409, {"error": "batch_content_conflict"})
            if prior["status"] >= 400:
                raise HttpError(prior["status"], dict(prior["response"]))
            result = dict(prior["response"])
            result["replayed"] = True
            return result

        # 快照（保证原子性：全部校验通过才提交）
        snapshot = {sid: dict(s) for sid, s in self.segments.items()}
        try:
            result = self._apply(batch_id, moves)
        except BatchRejection as exc:
            body = {"error": exc.code, "message": exc.message, **exc.extra}
            self.batches[batch_id] = {"fingerprint": fp, "status": exc.status_code, "response": body}
            # 原子拒绝：不改任何记录
            for sid, s in snapshot.items():
                assert self.segments[sid]["driver_id"] == s["driver_id"]
                assert self.segments[sid]["revision"] == s["revision"]
            raise HttpError(exc.status_code, body)
        self.batches[batch_id] = {"fingerprint": fp, "status": 201, "response": result}
        return result

    def _apply(self, batch_id, moves):
        by_id = {}
        for m in moves:
            seg = self.segments.get(m.segment_id)
            if seg is None:
                raise BatchRejection(
                    status_code=404, code="segment_not_found",
                    message=f"片段 '{m.segment_id}' 不存在", segment_id=m.segment_id)
            if seg["revision"] != m.expected_revision:
                raise BatchRejection(
                    status_code=409, code="revision_conflict", message="修订号过期",
                    segment_id=m.segment_id,
                    current_revision=seg["revision"],
                    expected_revision=m.expected_revision,
                )
            by_id[m.segment_id] = seg

        for d in {m.driver_id for m in moves}:
            if d not in self.drivers:
                raise BatchRejection(
                    status_code=404, code="driver_not_found",
                    message=f"目标司机 '{d}' 不存在", driver_id=d)

        affected = sorted({s["driver_id"] for s in by_id.values()} | {m.driver_id for m in moves})

        def comp(d):
            return evaluate_driver([
                Seg(s["segment_id"], s["start"], s["end"], s["type"])
                for s in self.driver_segments(d)
            ])

        before = {d: comp(d) for d in affected}

        existing = []
        for d in affected:
            for s in self.driver_segments(d):
                existing.append(SegView(s["segment_id"], s["driver_id"],
                                        s["start"], s["end"], s["type"]))
        final_layout(existing, moves)

        for m in moves:
            seg = by_id[m.segment_id]
            seg["driver_id"] = m.driver_id
            seg["start"] = m.start
            seg["end"] = m.end
            seg["type"] = m.type
            seg["revision"] += 1

        after = {d: comp(d) for d in affected}
        return {
            "batch_id": batch_id,
            "replayed": False,
            "affected_drivers": [
                {"driver_id": d, "before": before[d], "after": after[d]} for d in affected
            ],
            "segment_changes": [
                {"segment_id": m.segment_id, "driver_id": m.driver_id,
                 "revision_before": m.expected_revision,
                 "revision_after": m.expected_revision + 1}
                for m in moves
            ],
        }


def mv(sid, rev, driver, sh, eh, typ="DRIVING", day=0):
    return Move(sid, rev, driver, t(sh, day=day), t(eh, day=day), typ)


class BatchEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")
        self.db.add_driver("D2")
        # D1: 次日 08-10 驾驶；D2: 当天 08-10 驾驶 —— 互不重叠
        self.db.add_segment("SEG-A", "D1", 8, 10, day=1)
        self.db.add_segment("SEG-B", "D2", 8, 10, day=0)

    def test_swap_non_overlapping_succeeds_and_increments_once(self):
        res = self.db.submit_batch(
            "B1",
            [mv("SEG-A", 1, "D2", 8, 10, day=1), mv("SEG-B", 1, "D1", 8, 10, day=0)],
        )
        self.assertFalse(res["replayed"])
        self.assertEqual(self.db.segments["SEG-A"]["driver_id"], "D2")
        self.assertEqual(self.db.segments["SEG-B"]["driver_id"], "D1")
        self.assertEqual(self.db.segments["SEG-A"]["revision"], 2)
        self.assertEqual(self.db.segments["SEG-B"]["revision"], 2)
        effects = {e["driver_id"]: e for e in res["affected_drivers"]}
        self.assertEqual(set(effects), {"D1", "D2"})
        # before/after 均按调整时点现存记录实时重算
        self.assertTrue(effects["D1"]["before"]["compliant"])
        self.assertTrue(effects["D1"]["after"]["compliant"])

    def test_identical_retry_replays_first_result(self):
        r1 = self.db.submit_batch("B2", [mv("SEG-A", 1, "D2", 8, 10, day=1)])
        r2 = self.db.submit_batch("B2", [mv("SEG-A", 1, "D2", 8, 10, day=1)])
        self.assertFalse(r1["replayed"])
        self.assertTrue(r2["replayed"])
        # 重放不重复加修订号
        self.assertEqual(self.db.segments["SEG-A"]["revision"], 2)

    def test_same_batch_other_content_conflicts(self):
        self.db.submit_batch("B3", [mv("SEG-A", 1, "D2", 8, 10, day=1)])
        with self.assertRaises(HttpError) as ctx:
            self.db.submit_batch("B3", [mv("SEG-A", 1, "D2", 8, 11, day=1)])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "batch_content_conflict")
        self.assertEqual(self.db.segments["SEG-A"]["revision"], 2)

    def test_stale_revision_rejects_whole_batch_and_changes_nothing(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.submit_batch(
                "B4",
                [mv("SEG-A", 7, "D2", 8, 10, day=1), mv("SEG-B", 1, "D1", 8, 10, day=0)],
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "revision_conflict")
        self.assertEqual(ctx.exception.detail["current_revision"], 1)
        # 整批拒绝：两条都没动
        self.assertEqual(self.db.segments["SEG-A"]["driver_id"], "D1")
        self.assertEqual(self.db.segments["SEG-B"]["driver_id"], "D2")
        self.assertEqual(self.db.segments["SEG-A"]["revision"], 1)

    def test_missing_target_driver_rejects(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.submit_batch("B5", [mv("SEG-A", 1, "GHOST", 8, 10, day=1)])
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail["error"], "driver_not_found")
        self.assertEqual(self.db.segments["SEG-A"]["driver_id"], "D1")

    def test_missing_segment_rejects(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.submit_batch("B6", [mv("NOPE", 1, "D2", 8, 10, day=1)])
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail["error"], "segment_not_found")

    def test_final_overlap_rejects_regardless_of_item_order(self):
        # SEG-A 移到 D2 当天 09-11，与 D2 上未调整的 SEG-B(08-10) 在最终排布重叠
        with self.assertRaises(HttpError) as ctx:
            self.db.submit_batch("B7", [mv("SEG-A", 1, "D2", 9, 11)])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "segment_overlap")
        self.assertEqual(self.db.segments["SEG-A"]["driver_id"], "D1")
        self.assertEqual(self.db.segments["SEG-A"]["revision"], 1)

    def test_swap_adjacent_segments_ok(self):
        # 相接两段互换：D1 的 08-10 与 D2 的 10-12（同一天），互换后仍相接
        self.db.add_segment("SEG-C", "D1", 10, 12, day=2)
        self.db.add_segment("SEG-D", "D2", 10, 12, day=2)
        self.db.submit_batch(
            "B8",
            [mv("SEG-C", 1, "D2", 10, 12, day=2), mv("SEG-D", 1, "D1", 10, 12, day=2)],
        )
        self.assertEqual(self.db.segments["SEG-C"]["driver_id"], "D2")
        self.assertEqual(self.db.segments["SEG-D"]["driver_id"], "D1")

    def test_duplicate_segment_ref_in_batch_rejected(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.submit_batch(
                "B9",
                [mv("SEG-A", 1, "D2", 8, 10, day=1), mv("SEG-A", 1, "D2", 9, 11, day=1)],
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "duplicate_segment_in_batch")

    def test_failed_batch_replays_failure(self):
        with self.assertRaises(HttpError) as c1:
            self.db.submit_batch("B10", [mv("NOPE", 1, "D2", 8, 10)])
        self.assertEqual(c1.exception.status_code, 404)
        with self.assertRaises(HttpError) as c2:
            self.db.submit_batch("B10", [mv("NOPE", 1, "D2", 8, 10)])
        self.assertEqual(c2.exception.status_code, 404)
        self.assertEqual(c2.exception.detail["error"], "segment_not_found")

    def test_before_after_compliance_reflects_move(self):
        # D2 当天：SEG-B 08-10 驾驶，SEG-L 10:00-14:10 其他值勤（相接）；
        # 把 D1 次日的 SEG-A 移到 D2 当天 14:10-18:20 驾驶后，
        # D2 累计驾驶 = 2h + 4h10m = 6h10m > 4h（其他值勤不是合格休息）-> after 不合规
        self.db.add_segment("SEG-L", "D2", 10, 14 + 10 / 60, "OTHER_DUTY")
        self.db.submit_batch("B11", [
            mv("SEG-A", 1, "D2", 14 + 10 / 60, 18 + 20 / 60),
        ])
        effects = {e["driver_id"]: e for e in
                   self.db.batches["B11"]["response"]["affected_drivers"]}
        self.assertTrue(effects["D2"]["before"]["compliant"])
        self.assertFalse(effects["D2"]["after"]["compliant"])


if __name__ == "__main__":
    unittest.main()
