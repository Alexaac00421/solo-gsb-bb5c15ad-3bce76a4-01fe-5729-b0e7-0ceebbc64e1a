"""跨司机批量调整纯逻辑单元测试：仅依赖标准库（与合规引擎测试相同）。

可用 `python3 -m unittest tests.test_reassignment` 或 pytest 运行。
"""

import unittest
from datetime import datetime, timedelta, timezone

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


def t(h, m=0):
    return BASE + timedelta(hours=h, minutes=m)


def existing(sid, driver, sh, eh, typ="DRIVING"):
    return SegView(sid, driver, t(sh), t(eh), typ)


def move(sid, rev, driver, sh, eh, typ="DRIVING"):
    return Move(sid, rev, driver, t(sh), t(eh), typ)


class UniqueRefsTest(unittest.TestCase):
    def test_unique_ok(self):
        ensure_unique_segment_refs(["a", "b", "c"])

    def test_duplicate_rejected(self):
        with self.assertRaises(BatchRejection) as ctx:
            ensure_unique_segment_refs(["a", "b", "a"])
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.code, "duplicate_segment_in_batch")
        self.assertEqual(ctx.exception.extra["segment_id"], "a")


class FingerprintTest(unittest.TestCase):
    def test_order_independent(self):
        a = move("A", 1, "D2", 8, 10)
        b = move("B", 3, "D1", 12, 14, "REST")
        self.assertEqual(
            payload_fingerprint(canonical_payload([a, b])),
            payload_fingerprint(canonical_payload([b, a])),
        )

    def test_same_instant_different_offset_matches(self):
        # Z 写法与 +08:00 写法表示同一时刻 => 内容相同
        a = Move("A", 1, "D2", datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc),
                 datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc), "DRIVING")
        cst = timezone(timedelta(hours=8))
        a2 = Move("A", 1, "D2", datetime(2026, 10, 5, 16, 0, tzinfo=cst),
                  datetime(2026, 10, 5, 18, 0, tzinfo=cst), "DRIVING")
        self.assertEqual(
            payload_fingerprint(canonical_payload([a])),
            payload_fingerprint(canonical_payload([a2])),
        )

    def test_different_content_differs(self):
        a = payload_fingerprint(canonical_payload([move("A", 1, "D2", 8, 10)]))
        b = payload_fingerprint(canonical_payload([move("A", 1, "D2", 8, 11)]))
        self.assertNotEqual(a, b)


class FinalLayoutTest(unittest.TestCase):
    def test_swap_non_overlapping_segments_ok(self):
        # D1 上 SEG-A（次日）与 D2 上 SEG-B 互不重叠；互换司机后仍不重叠
        ex = [
            existing("SEG-A", "D1", 32, 34),
            existing("SEG-B", "D2", 8, 10),
        ]
        groups = final_layout(ex, [move("SEG-A", 1, "D2", 32, 34), move("SEG-B", 1, "D1", 8, 10)])
        self.assertEqual({s.segment_id for s in groups["D1"]}, {"SEG-B"})
        self.assertEqual({s.segment_id for s in groups["D2"]}, {"SEG-A"})

    def test_swap_adjacent_segments_ok(self):
        # 相接（end == start）的两段互换司机，最终排布仍只是相接，合法
        ex = [
            existing("SEG-A", "D1", 8, 10),
            existing("SEG-B", "D2", 10, 12),
        ]
        groups = final_layout(ex, [move("SEG-A", 1, "D2", 8, 10), move("SEG-B", 1, "D1", 10, 12)])
        self.assertEqual({s.segment_id for s in groups["D1"]}, {"SEG-B"})
        self.assertEqual({s.segment_id for s in groups["D2"]}, {"SEG-A"})

    def test_order_independence(self):
        # 同批调整，先动哪一条都不影响最终排布结论
        ex = [
            existing("SEG-A", "D1", 8, 10),
            existing("SEG-B", "D1", 10, 12),
            existing("SEG-C", "D2", 9, 11),
        ]
        ms = [move("SEG-A", 1, "D2", 8, 10), move("SEG-C", 2, "D1", 9, 11)]
        ms_rev = list(reversed(ms))
        with self.assertRaises(BatchRejection) as c1:
            final_layout(ex, ms)
        with self.assertRaises(BatchRejection) as c2:
            final_layout(ex, ms_rev)
        # SEG-C 移到 D1 后（09-11）与未调整的 SEG-B（10-12）重叠，
        # 无论提交顺序；SEG-A 落入 D2 空出的位置则不重叠
        self.assertEqual(c1.exception.code, "segment_overlap")
        self.assertEqual(c2.exception.code, "segment_overlap")
        self.assertEqual(c1.exception.extra["driver_id"], "D1")
        self.assertEqual(
            set(c1.exception.extra["conflicting_segment_ids"]), {"SEG-C", "SEG-B"}
        )

    def test_move_into_free_slot_ok(self):
        ex = [existing("SEG-A", "D1", 8, 10), existing("SEG-E", "D2", 0, 2)]
        groups = final_layout(ex, [move("SEG-A", 1, "D2", 32, 34)])
        self.assertEqual({s.segment_id for s in groups["D2"]}, {"SEG-E", "SEG-A"})
        self.assertNotIn("D1", groups)

    def test_untouched_segments_kept(self):
        ex = [existing("SEG-A", "D1", 8, 10), existing("SEG-E", "D1", 12, 14)]
        groups = final_layout(ex, [move("SEG-A", 1, "D2", 8, 10)])
        self.assertEqual({s.segment_id for s in groups["D1"]}, {"SEG-E"})
        self.assertEqual({s.segment_id for s in groups["D2"]}, {"SEG-A"})

    def test_collision_with_untouched_segment_rejected(self):
        # SEG-E 是 D2 上 09:00-09:30 的 REST（未在批次中）；
        # SEG-A 移到 D2 的 08-10 会在最终排布中与之重叠
        ex = [
            SegView("SEG-A", "D1", t(8), t(10), "DRIVING"),
            SegView("SEG-E", "D2", t(9), t(9, 30), "REST"),
        ]
        with self.assertRaises(BatchRejection) as ctx:
            final_layout(ex, [move("SEG-A", 1, "D2", 8, 10)])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(
            set(ctx.exception.extra["conflicting_segment_ids"]), {"SEG-A", "SEG-E"}
        )


if __name__ == "__main__":
    unittest.main()
