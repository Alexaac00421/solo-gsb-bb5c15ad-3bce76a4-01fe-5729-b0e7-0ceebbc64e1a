"""合规引擎单元测试：仅依赖标准库，可用 `python3 -m unittest` 或 pytest 运行。"""

import unittest
from datetime import datetime, timedelta, timezone

from app.compliance import (
    RULE_DRIVING_STINT,
    RULE_ROLLING_24H,
    Seg,
    evaluate,
)

BASE = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def seg(sid, start_h, end_h, typ="DRIVING", day=0):
    return Seg(
        segment_id=sid,
        start=BASE + timedelta(days=day, hours=start_h),
        end=BASE + timedelta(days=day, hours=end_h),
        type=typ,
    )


def rules(violations):
    return {v.rule for v in violations}


class Rolling24hDutyLimitTest(unittest.TestCase):
    def test_empty_is_compliant(self):
        self.assertEqual(evaluate([]), [])

    def test_exactly_10h_is_compliant(self):
        # 等于上限合规
        self.assertEqual(evaluate([seg("a", 0, 10, "OTHER_DUTY")]), [])

    def test_10h_plus_1min_violates_at_limit_crossing(self):
        v = evaluate([seg("a", 0, 10 + 1 / 60, "OTHER_DUTY")])
        self.assertEqual(rules(v), {RULE_ROLLING_24H})
        self.assertEqual(v[0].first_violation_at, BASE + timedelta(hours=10))
        self.assertEqual(v[0].segment_ids, ["a"])

    def test_split_duty_accumulates_across_gap(self):
        # 6h + 5h，中间间隔 6h：窗口内合计 11h，越限时刻为第二段第 4h 处
        v = evaluate([seg("a", 0, 6, "OTHER_DUTY"), seg("b", 12, 17, "OTHER_DUTY")])
        self.assertEqual(rules(v), {RULE_ROLLING_24H})
        rolling = v[0]
        self.assertEqual(rolling.first_violation_at, BASE + timedelta(hours=16))
        self.assertEqual(rolling.segment_ids, ["a", "b"])

    def test_duty_older_than_24h_not_counted(self):
        # 两段各 7h，相隔超过 24h，任意窗口最多 7h
        v = evaluate([seg("a", 0, 7, "OTHER_DUTY"), seg("b", 26, 33, "OTHER_DUTY")])
        self.assertEqual(v, [])

    def test_rest_not_counted(self):
        v = evaluate([seg("r", 0, 23, "REST")])
        self.assertEqual(v, [])

    def test_exactly_24h_apart_segments_do_not_combine(self):
        # 前一段恰好完全滑出窗口后才开始的片段不叠加
        v = evaluate([seg("a", 0, 6, "OTHER_DUTY"), seg("b", 30, 36, "OTHER_DUTY")])
        self.assertEqual(v, [])


class DrivingStintLimitTest(unittest.TestCase):
    def test_exactly_4h_driving_is_compliant(self):
        self.assertEqual(evaluate([seg("a", 0, 4, "DRIVING")]), [])

    def test_over_4h_driving_violates(self):
        v = evaluate([seg("a", 0, 4.5, "DRIVING")])
        self.assertEqual(rules(v), {RULE_DRIVING_STINT})
        self.assertEqual(v[0].first_violation_at, BASE + timedelta(hours=4))

    def test_short_rest_does_not_reset(self):
        # 2h 驾驶 + 29 分钟休息 + 2.5h 驾驶 => 越限
        v = evaluate(
            [
                seg("a", 0, 2, "DRIVING"),
                Seg("r", BASE + timedelta(hours=2), BASE + timedelta(hours=2, minutes=29), "REST"),
                Seg("b", BASE + timedelta(hours=2, minutes=29), BASE + timedelta(hours=4, minutes=59), "DRIVING"),
            ]
        )
        self.assertEqual(rules(v), {RULE_DRIVING_STINT})
        stint = [x for x in v if x.rule == RULE_DRIVING_STINT][0]
        self.assertEqual(stint.first_violation_at, BASE + timedelta(hours=2, minutes=29) + timedelta(hours=2))
        self.assertEqual(stint.segment_ids, ["a", "b"])

    def test_30min_rest_resets(self):
        v = evaluate(
            [
                seg("a", 0, 2, "DRIVING"),
                seg("r", 2, 2.5, "REST"),
                seg("b", 2.5, 6.5, "DRIVING"),
            ]
        )
        self.assertEqual(v, [])

    def test_adjacent_rests_merge_to_qualify(self):
        # 相接的 15 分钟 + 15 分钟休息合并为 30 分钟连续休息 => 重置
        v = evaluate(
            [
                seg("a", 0, 2, "DRIVING"),
                Seg("r1", BASE + timedelta(hours=2), BASE + timedelta(hours=2, minutes=15), "REST"),
                Seg("r2", BASE + timedelta(hours=2, minutes=15), BASE + timedelta(hours=2, minutes=30), "REST"),
                Seg("b", BASE + timedelta(hours=2, minutes=30), BASE + timedelta(hours=6, minutes=30), "DRIVING"),
            ]
        )
        self.assertEqual(v, [])

    def test_other_duty_breaks_rest_continuity(self):
        # 20 分钟休息 + 其他值勤 + 20 分钟休息，不构成连续 30 分钟休息
        v = evaluate(
            [
                seg("a", 0, 2, "DRIVING"),
                Seg("r1", BASE + timedelta(hours=2), BASE + timedelta(hours=2, minutes=20), "REST"),
                Seg("o", BASE + timedelta(hours=2, minutes=20), BASE + timedelta(hours=2, minutes=30), "OTHER_DUTY"),
                Seg("r2", BASE + timedelta(hours=2, minutes=30), BASE + timedelta(hours=2, minutes=50), "REST"),
                Seg("b", BASE + timedelta(hours=2, minutes=50), BASE + timedelta(hours=5), "DRIVING"),
            ]
        )
        self.assertEqual(rules(v), {RULE_DRIVING_STINT})

    def test_other_duty_does_not_count_toward_driving_stint(self):
        v = evaluate([seg("a", 0, 2, "DRIVING"), seg("o", 2, 8, "OTHER_DUTY"), seg("b", 8, 10, "DRIVING")])
        self.assertNotIn(RULE_DRIVING_STINT, rules(v))

    def test_participants_only_since_last_reset(self):
        v = evaluate(
            [
                seg("x", 0, 1, "DRIVING"),
                seg("r", 1, 1.5, "REST"),  # 合格休息，清零
                seg("a", 1.5, 4, "DRIVING"),
                seg("b", 4, 6, "DRIVING"),  # 累计 4.5h，越限
            ]
        )
        stint = [x for x in v if x.rule == RULE_DRIVING_STINT][0]
        self.assertEqual(stint.segment_ids, ["a", "b"])
        self.assertEqual(stint.first_violation_at, BASE + timedelta(hours=5, minutes=30))


class BothRulesTest(unittest.TestCase):
    def test_both_rules_can_fire(self):
        # 其他值勤 6h + 驾驶 5h：滚动 24h 合计 11h 越限，且累计驾驶 5h 越限
        v = evaluate([seg("o", 0, 6, "OTHER_DUTY"), seg("d", 6, 11, "DRIVING")])
        self.assertEqual(rules(v), {RULE_ROLLING_24H, RULE_DRIVING_STINT})
        by_rule = {x.rule: x for x in v}
        self.assertEqual(by_rule[RULE_ROLLING_24H].first_violation_at, BASE + timedelta(hours=10))
        self.assertEqual(by_rule[RULE_DRIVING_STINT].first_violation_at, BASE + timedelta(hours=10))
        self.assertEqual(by_rule[RULE_ROLLING_24H].segment_ids, ["o", "d"])
        self.assertEqual(by_rule[RULE_DRIVING_STINT].segment_ids, ["d"])


if __name__ == "__main__":
    unittest.main()
