"""违规事件订阅通知纯逻辑单元测试：仅依赖标准库（与合规引擎测试相同）。

可用 `python3 -m unittest tests.test_notification` 或 pytest 运行。
"""

import unittest
from datetime import datetime, timezone

from app.compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H
from app.notification import (
    build_payload,
    canonical_rules,
    changed_rules,
    encode_payload,
    is_valid_callback_url,
    retry_delay_seconds,
    rule_set,
    sign_body,
    subscription_matches,
)


class RuleSetTest(unittest.TestCase):
    def test_extracts_sorted_unique_rules(self):
        result = {
            "compliant": False,
            "violations": [
                {"rule": RULE_DRIVING_STINT},
                {"rule": RULE_ROLLING_24H},
                {"rule": RULE_DRIVING_STINT},
            ],
        }
        self.assertEqual(
            rule_set(result), sorted([RULE_ROLLING_24H, RULE_DRIVING_STINT])
        )

    def test_empty_when_compliant(self):
        self.assertEqual(rule_set({"compliant": True, "violations": []}), [])
        self.assertEqual(rule_set({}), [])


class ChangedRulesTest(unittest.TestCase):
    def test_symmetric_difference(self):
        self.assertEqual(changed_rules([], ["A"]), ["A"])  # 新增
        self.assertEqual(changed_rules(["A"], []), ["A"])  # 消除
        self.assertEqual(changed_rules(["A"], ["B"]), ["A", "B"])  # 一新一消
        self.assertEqual(changed_rules(["A"], ["A"]), [])  # 集合未变
        self.assertEqual(changed_rules([], []), [])


class CanonicalRulesTest(unittest.TestCase):
    def test_dedup_and_sort(self):
        self.assertEqual(canonical_rules(["B", "A", "B"]), ["A", "B"])
        self.assertEqual(canonical_rules([]), [])


class SubscriptionMatchesTest(unittest.TestCase):
    def test_all_drivers_subscription_matches_any_driver(self):
        self.assertTrue(subscription_matches(None, ["A"], "D1", [], ["A"]))

    def test_driver_scope(self):
        self.assertTrue(subscription_matches("D1", ["A"], "D1", [], ["A"]))
        self.assertFalse(subscription_matches("D2", ["A"], "D1", [], ["A"]))

    def test_rule_must_be_in_changed_set(self):
        # 关注规则 A：A 新增 -> 命中；A 消除 -> 命中；变化的是别的规则 -> 不命中
        self.assertTrue(subscription_matches(None, ["A"], "D1", [], ["A"]))
        self.assertTrue(subscription_matches(None, ["A"], "D1", ["A"], []))
        self.assertFalse(subscription_matches(None, ["A"], "D1", [], ["B"]))

    def test_rule_present_before_and_after_does_not_match(self):
        # A 前后都在违规集合中（状态未变），仅 B 新增：只关注 A 的订阅不命中
        self.assertFalse(subscription_matches(None, ["A"], "D1", ["A"], ["A", "B"]))
        # 同时关注 B 则命中
        self.assertTrue(subscription_matches(None, ["A", "B"], "D1", ["A"], ["A", "B"]))

    def test_no_set_change_never_matches(self):
        self.assertFalse(subscription_matches(None, ["A"], "D1", ["A"], ["A"]))


class CallbackUrlTest(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(is_valid_callback_url("http://localhost:9000/callback"))
        self.assertTrue(is_valid_callback_url("https://ops.example.com/hooks/duty"))

    def test_invalid(self):
        for url in (
            "",
            "localhost:9000",  # 无 scheme
            "ftp://example.com/x",  # 非 http(s)
            "http://",  # 无主机名
            "http:///path",
            "/relative/path",
        ):
            self.assertFalse(is_valid_callback_url(url), url)


class SignBodyTest(unittest.TestCase):
    def test_known_vector(self):
        body = b'{"event_id":"evt_1","driver_id":"DRV-1"}'
        self.assertEqual(
            sign_body("shared-secret-1", body),
            "sha256=e651707b1c91ed79f03bbaf8b776608a9652c76a81219bb2579efc5db83c03cc",
        )

    def test_secret_changes_signature(self):
        body = b"{}"
        self.assertNotEqual(sign_body("secret-one", body), sign_body("secret-two", body))


class BuildPayloadTest(unittest.TestCase):
    def test_shape_and_tz_normalization(self):
        payload = build_payload(
            event_id="evt_1",
            subscription_id="SUB-1",
            driver_id="D1",
            rules_before=[],
            rules_after=[RULE_ROLLING_24H],
            committed_at=datetime(2026, 10, 6, 3, 10),  # naive 视为 UTC
        )
        self.assertEqual(
            payload,
            {
                "event_id": "evt_1",
                "subscription_id": "SUB-1",
                "driver_id": "D1",
                "rules_before": [],
                "rules_after": [RULE_ROLLING_24H],
                "committed_at": "2026-10-06T03:10:00+00:00",
            },
        )

    def test_encode_is_stable(self):
        payload = build_payload(
            event_id="evt_1",
            subscription_id="SUB-1",
            driver_id="D1",
            rules_before=["A"],
            rules_after=[],
            committed_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
        )
        self.assertEqual(encode_payload(payload), encode_payload(payload))
        self.assertIsInstance(encode_payload(payload), bytes)


class RetryDelayTest(unittest.TestCase):
    def test_exponential_backoff(self):
        self.assertEqual(retry_delay_seconds(1, 2, 60), 2)
        self.assertEqual(retry_delay_seconds(2, 2, 60), 4)
        self.assertEqual(retry_delay_seconds(3, 2, 60), 8)
        self.assertEqual(retry_delay_seconds(4, 2, 60), 16)

    def test_capped_at_max(self):
        self.assertEqual(retry_delay_seconds(10, 2, 60), 60)
        self.assertEqual(retry_delay_seconds(100, 2, 60), 60)

    def test_attempts_floor(self):
        # attempts 异常地 <= 0 时按第一次失败处理
        self.assertEqual(retry_delay_seconds(0, 2, 60), 2)


if __name__ == "__main__":
    unittest.main()
