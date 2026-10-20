import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from apps.user import sms_providers
from apps.user.services import SMSService


TENCENT_SMS_CONFIG = {
    "SECRET_ID": "sid",
    "SECRET_KEY": "skey",
    "SDK_APP_ID": "1400000000",
    "SIGN_NAME": "TestSign",
    "TEMPLATE_ID": "123456",
    "REGION": "ap-beijing",
    "ENDPOINT": "sms.tencentcloudapi.com",
    "TIMEOUT_SECONDS": 30,
}


class FakeTencentCloudSDKException(Exception):
    def __init__(self, code="FailedOperation", request_id="req-error"):
        super().__init__(code)
        self.code = code
        self.requestId = request_id


@contextmanager
def fake_tencent_sdk(client_cls):
    module_names = [
        "tencentcloud",
        "tencentcloud.common",
        "tencentcloud.common.credential",
        "tencentcloud.common.exception",
        "tencentcloud.common.exception.tencent_cloud_sdk_exception",
        "tencentcloud.common.profile",
        "tencentcloud.common.profile.client_profile",
        "tencentcloud.common.profile.http_profile",
        "tencentcloud.sms",
        "tencentcloud.sms.v20210111",
        "tencentcloud.sms.v20210111.models",
        "tencentcloud.sms.v20210111.sms_client",
    ]
    previous = {name: sys.modules.get(name) for name in module_names}

    credential_module = types.ModuleType("tencentcloud.common.credential")
    credential_module.Credential = lambda secret_id, secret_key: SimpleNamespace(
        secret_id=secret_id,
        secret_key=secret_key,
    )

    exception_module = types.ModuleType("tencentcloud.common.exception.tencent_cloud_sdk_exception")
    exception_module.TencentCloudSDKException = FakeTencentCloudSDKException

    client_profile_module = types.ModuleType("tencentcloud.common.profile.client_profile")
    client_profile_module.ClientProfile = lambda: SimpleNamespace(httpProfile=None)

    http_profile_module = types.ModuleType("tencentcloud.common.profile.http_profile")
    http_profile_module.HttpProfile = lambda: SimpleNamespace(endpoint="", reqTimeout=0)

    models_module = types.ModuleType("tencentcloud.sms.v20210111.models")
    models_module.SendSmsRequest = lambda: SimpleNamespace()

    sms_client_module = types.ModuleType("tencentcloud.sms.v20210111.sms_client")
    sms_client_module.SmsClient = client_cls

    sys.modules.update(
        {
            "tencentcloud": types.ModuleType("tencentcloud"),
            "tencentcloud.common": types.ModuleType("tencentcloud.common"),
            "tencentcloud.common.credential": credential_module,
            "tencentcloud.common.exception": types.ModuleType("tencentcloud.common.exception"),
            "tencentcloud.common.exception.tencent_cloud_sdk_exception": exception_module,
            "tencentcloud.common.profile": types.ModuleType("tencentcloud.common.profile"),
            "tencentcloud.common.profile.client_profile": client_profile_module,
            "tencentcloud.common.profile.http_profile": http_profile_module,
            "tencentcloud.sms": types.ModuleType("tencentcloud.sms"),
            "tencentcloud.sms.v20210111": types.ModuleType("tencentcloud.sms.v20210111"),
            "tencentcloud.sms.v20210111.models": models_module,
            "tencentcloud.sms.v20210111.sms_client": sms_client_module,
        }
    )
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class TencentSmsProviderTests(SimpleTestCase):
    def test_get_sms_provider_defaults_to_mock(self):
        with override_settings(SMS_PROVIDER="mock"):
            self.assertIsInstance(sms_providers.get_sms_provider(), sms_providers.MockSmsProvider)

    def test_tencent_send_success(self):
        class SuccessClient:
            last_request = None

            def __init__(self, credential, region, client_profile):
                self.region = region
                self.client_profile = client_profile

            def SendSms(self, request):
                SuccessClient.last_request = request
                return SimpleNamespace(
                    RequestId="req-ok",
                    SendStatusSet=[SimpleNamespace(Code="Ok")],
                )

        with fake_tencent_sdk(SuccessClient), override_settings(TENCENT_SMS=TENCENT_SMS_CONFIG):
            result = sms_providers.TencentSmsProvider().send_code("13800138000", "654321", 300)

        self.assertTrue(result.success)
        self.assertEqual(SuccessClient.last_request.PhoneNumberSet, ["+8613800138000"])
        self.assertEqual(SuccessClient.last_request.TemplateParamSet, ["654321", "5"])

    def test_tencent_send_non_ok_status_fails(self):
        class FailureClient:
            def __init__(self, credential, region, client_profile):
                pass

            def SendSms(self, request):
                return SimpleNamespace(
                    RequestId="req-fail",
                    SendStatusSet=[SimpleNamespace(Code="FailedOperation.SignatureIncorrect")],
                )

        with fake_tencent_sdk(FailureClient), override_settings(TENCENT_SMS=TENCENT_SMS_CONFIG):
            result = sms_providers.TencentSmsProvider().send_code("13800138000", "654321", 300)

        self.assertFalse(result.success)
        self.assertIn("短信发送失败", result.message)

    def test_tencent_send_sdk_exception_fails(self):
        class ExceptionClient:
            def __init__(self, credential, region, client_profile):
                pass

            def SendSms(self, request):
                raise FakeTencentCloudSDKException()

        with fake_tencent_sdk(ExceptionClient), override_settings(TENCENT_SMS=TENCENT_SMS_CONFIG):
            result = sms_providers.TencentSmsProvider().send_code("13800138000", "654321", 300)

        self.assertFalse(result.success)
        self.assertIn("短信发送失败", result.message)

    def test_tencent_missing_config_raises(self):
        with override_settings(TENCENT_SMS={**TENCENT_SMS_CONFIG, "SECRET_KEY": ""}):
            with self.assertRaises(ImproperlyConfigured):
                sms_providers.TencentSmsProvider()


class SMSServiceTests(SimpleTestCase):
    def tearDown(self):
        cache.clear()

    @override_settings(SMS_PROVIDER="mock")
    @patch("apps.user.services.TencentCaptchaService.consume_token", return_value=(True, "success"))
    @patch("apps.user.services.get_sms_provider")
    def test_send_code_does_not_cache_when_provider_fails(self, get_provider, _consume_token):
        get_provider.return_value.send_code.return_value = sms_providers.SmsSendResult(
            False,
            "短信发送失败，请稍后重试",
        )

        ok, message, rate_limited = SMSService.send_code("13800138000", captcha_token="captcha-token")

        self.assertFalse(ok)
        self.assertFalse(rate_limited)
        self.assertIn("短信发送失败", message)
        self.assertIsNone(cache.get(SMSService.CACHE_KEY.format(phone="13800138000")))
