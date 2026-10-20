import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.user.services import SMSService, TencentCaptchaService
from apps.user.views import send_sms_code
from common.utils.Login_utils import get_client_ip


@override_settings(
    TRUSTED_PROXY_IPS=["10.0.0.1"],
    SMS_RATE_LIMIT_HASH_SALT="test-salt",
)
class SMSRateLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.captcha_patch = patch.object(TencentCaptchaService, "consume_token", return_value=(True, "success"))
        self.captcha_patch.start()

    def tearDown(self):
        self.captcha_patch.stop()
        cache.clear()

    def _post_send_sms(self, phone: str, remote_addr: str):
        request = self.factory.post(
            "/api/user/send_sms_code",
            {"phone": phone, "captcha_token": "captcha-token", "scene": "login_sms"},
            content_type="application/json",
            REMOTE_ADDR=remote_addr,
        )
        return send_sms_code(request)

    def test_same_phone_is_rate_limited_with_429(self):
        payload = {"phone": "13800138000"}

        first = self._post_send_sms(payload["phone"], "203.0.113.10")
        second = self._post_send_sms(payload["phone"], "203.0.113.10")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(json.loads(second.content)["code"], 429)

    def test_same_ip_different_phones_is_rate_limited(self):
        responses = []
        for index in range(6):
            responses.append(self._post_send_sms(f"13800138{index:03d}", "203.0.113.20"))

        self.assertEqual(responses[-1].status_code, 429)
        self.assertEqual(json.loads(responses[-1].content)["code"], 429)

    def test_same_ip_same_phone_combination_is_rate_limited(self):
        responses = []
        for _ in range(4):
            responses.append(self._post_send_sms("13800138001", "203.0.113.30"))

        self.assertEqual(responses[-1].status_code, 429)
        self.assertEqual(json.loads(responses[-1].content)["code"], 429)

    def test_limit_keys_do_not_store_raw_phone_or_ip(self):
        ok, _, _ = SMSService.send_code(
            "13800138002",
            client_ip="203.0.113.40",
            captcha_token="captcha-token",
            scene="login_sms",
        )

        self.assertTrue(ok)
        self.assertIsNone(cache.get("sms_send_count:13800138002"))
        self.assertIsNone(cache.get("sms:ip:count:60s:203.0.113.40"))

    def test_forwarded_for_is_used_only_from_trusted_proxy(self):
        factory = RequestFactory()

        trusted_request = factory.get(
            "/api/user/send_sms_code",
            HTTP_X_FORWARDED_FOR="198.51.100.9, 10.0.0.1",
            REMOTE_ADDR="10.0.0.1",
        )
        untrusted_request = factory.get(
            "/api/user/send_sms_code",
            HTTP_X_FORWARDED_FOR="198.51.100.9",
            REMOTE_ADDR="203.0.113.99",
        )

        self.assertEqual(get_client_ip(trusted_request), "198.51.100.9")
        self.assertEqual(get_client_ip(untrusted_request), "203.0.113.99")

