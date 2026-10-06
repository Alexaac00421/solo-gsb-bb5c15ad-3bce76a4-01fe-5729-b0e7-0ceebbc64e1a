"""告警确认纯逻辑单元测试：仅依赖标准库（与合规引擎测试相同）。

可用 `python3 -m unittest tests.test_acknowledgment` 或 pytest 运行。
"""

import unittest

from app.acknowledgment import (
    ack_fingerprint,
    invalidation_diff,
    revision_set,
)


class RevisionSetTest(unittest.TestCase):
    def test_sorted_by_segment_id(self):
        rs = revision_set([("SEG-B", 2), ("SEG-A", 1), ("SEG-C", 5)])
        self.assertEqual(
            rs,
            [
                {"segment_id": "SEG-A", "revision": 1},
                {"segment_id": "SEG-B", "revision": 2},
                {"segment_id": "SEG-C", "revision": 5},
            ],
        )

    def test_empty(self):
        self.assertEqual(revision_set([]), [])


class FingerprintTest(unittest.TestCase):
    def test_same_set_same_fingerprint_regardless_of_order(self):
        a = ack_fingerprint("D1", "RULE_X", revision_set([("A", 1), ("B", 2)]))
        b = ack_fingerprint("D1", "RULE_X", revision_set([("B", 2), ("A", 1)]))
        self.assertEqual(a, b)

    def test_revision_change_changes_fingerprint(self):
        a = ack_fingerprint("D1", "RULE_X", revision_set([("A", 1)]))
        b = ack_fingerprint("D1", "RULE_X", revision_set([("A", 2)]))
        self.assertNotEqual(a, b)

    def test_added_or_removed_segment_changes_fingerprint(self):
        base = ack_fingerprint("D1", "RULE_X", revision_set([("A", 1)]))
        added = ack_fingerprint("D1", "RULE_X", revision_set([("A", 1), ("B", 1)]))
        removed = ack_fingerprint("D1", "RULE_X", revision_set([]))
        self.assertNotEqual(base, added)
        self.assertNotEqual(base, removed)

    def test_driver_and_rule_are_part_of_fingerprint(self):
        rev = revision_set([("A", 1)])
        base = ack_fingerprint("D1", "RULE_X", rev)
        self.assertNotEqual(base, ack_fingerprint("D2", "RULE_X", rev))
        self.assertNotEqual(base, ack_fingerprint("D1", "RULE_Y", rev))


class InvalidationDiffTest(unittest.TestCase):
    def test_identical_sets_mean_still_valid(self):
        rev = revision_set([("A", 1), ("B", 2)])
        self.assertIsNone(invalidation_diff(rev, list(rev)))

    def test_added_segment(self):
        stored = revision_set([("A", 1)])
        current = revision_set([("A", 1), ("B", 1)])
        diff = invalidation_diff(stored, current)
        self.assertEqual(diff["added"], ["B"])
        self.assertEqual(diff["removed"], [])
        self.assertEqual(diff["revision_changed"], [])
        self.assertIn("B", diff["reason"])

    def test_removed_segment(self):
        stored = revision_set([("A", 1), ("B", 1)])
        current = revision_set([("A", 1)])
        diff = invalidation_diff(stored, current)
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["removed"], ["B"])
        self.assertIn("B", diff["reason"])

    def test_revision_changed(self):
        stored = revision_set([("A", 1), ("B", 1)])
        current = revision_set([("A", 1), ("B", 2)])
        diff = invalidation_diff(stored, current)
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["removed"], [])
        self.assertEqual(
            diff["revision_changed"],
            [{"segment_id": "B", "acknowledged_revision": 1, "current_revision": 2}],
        )
        self.assertIn("B", diff["reason"])
        self.assertIn("1→2", diff["reason"])

    def test_combined_changes(self):
        stored = revision_set([("A", 1), ("B", 1), ("C", 3)])
        current = revision_set([("A", 2), ("C", 3), ("D", 1)])
        diff = invalidation_diff(stored, current)
        self.assertEqual(diff["added"], ["D"])
        self.assertEqual(diff["removed"], ["B"])
        self.assertEqual(
            diff["revision_changed"],
            [{"segment_id": "A", "acknowledged_revision": 1, "current_revision": 2}],
        )
        # 摘要同时覆盖三类变化
        self.assertIn("新增片段 D", diff["reason"])
        self.assertIn("B", diff["reason"])
        self.assertIn("A", diff["reason"])

    def test_input_order_does_not_matter(self):
        stored = [{"segment_id": "B", "revision": 1}, {"segment_id": "A", "revision": 1}]
        current = [{"segment_id": "A", "revision": 1}, {"segment_id": "B", "revision": 1}]
        self.assertIsNone(invalidation_diff(stored, current))


if __name__ == "__main__":
    unittest.main()
