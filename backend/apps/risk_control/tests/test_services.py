from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from apps.risk_control.models import RiskAllowListEntry, RiskBlockListEntry
from apps.risk_control.services import RiskControlService


@override_settings(
    RISK_CONTROL_ENABLED=True,
    RISK_CONTROL_FAIL_POLICY="deny",
    RISK_CONTROL_REDIS_PREFIX="test:risk",
    RISK_CONTROL_HASH_SALT="test-salt",
)
class RiskControlServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_rate_limit_blocks_after_scope_rule_limit(self):
        rules = {
            "unit.scope": [
                {"dimension": "ip", "limit": 2, "window_seconds": 60},
            ]
        }
        with patch.object(RiskControlService, "DEFAULT_RULES", rules):
            first = RiskControlService.evaluate("unit.scope", ip="203.0.113.7")
            second = RiskControlService.evaluate("unit.scope", ip="203.0.113.7")
            third = RiskControlService.evaluate("unit.scope", ip="203.0.113.7")

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(third.allowed)
        self.assertEqual(third.status_code, 429)
        self.assertIn("unit.scope:rate_limit:ip:2/60", third.matched_rule)

    def test_hash_value_does_not_return_raw_value(self):
        value = "13800138000"

        hashed = RiskControlService.hash_value(value)

        self.assertNotEqual(hashed, value)
        self.assertEqual(len(hashed), 64)

    def test_allow_list_takes_precedence_over_block_list(self):
        def active_entry(model, scope, dimension, value_hash):
            if model is RiskAllowListEntry and dimension == "ip":
                return SimpleNamespace(reason="trusted operator")
            if model is RiskBlockListEntry and dimension == "ip":
                return SimpleNamespace(reason="blocked ip")
            return None

        with patch.object(RiskControlService, "_active_list_entry", side_effect=active_entry):
            decision = RiskControlService.evaluate("unit.scope", ip="203.0.113.8")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.matched_rule, "allow_list:ip")

    def test_api_key_verify_preflight_does_not_consume_key_failure_budget(self):
        rules = {
            "api_key.verify": [
                {"dimension": "ip", "limit": 100, "window_seconds": 60},
                {"dimension": "api_key", "limit": 1, "window_seconds": 60, "action": "failure_limit"},
            ]
        }
        with patch.object(RiskControlService, "DEFAULT_RULES", rules):
            decisions = [
                RiskControlService.evaluate("api_key.verify", ip="203.0.113.9", api_key_id="skid_good")
                for _ in range(11)
            ]

        self.assertTrue(all(decision.allowed for decision in decisions))

    def test_api_key_verify_failure_budget_blocks_invalid_key_attempts(self):
        rules = {
            "api_key.verify": [
                {"dimension": "ip", "limit": 100, "window_seconds": 60},
                {"dimension": "api_key", "limit": 1, "window_seconds": 60, "action": "failure_limit"},
            ]
        }
        with patch.object(RiskControlService, "DEFAULT_RULES", rules):
            first = RiskControlService.record_failure("api_key.verify", api_key_id="skid_bad")
            second = RiskControlService.record_failure("api_key.verify", api_key_id="skid_bad")

        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)
        self.assertEqual(second.status_code, 429)
