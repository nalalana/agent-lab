import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("touming")


def mask_phone(phone: str) -> str:
    raw = str(phone or "")
    if len(raw) <= 7:
        return "***"
    return f"{raw[:3]}****{raw[-4:]}"


def tencent_sms_error_message(error_code: str) -> str:
    code = str(error_code or "")
    if code == "LimitExceeded.PhoneNumberDailyLimit":
        return "该手机号今日验证码接收次数已达上限，请明天再试或更换手机号"
    if code in {
        "LimitExceeded.PhoneNumberThirtySecondLimit",
        "LimitExceeded.PhoneNumberOneHourLimit",
    }:
        return "验证码发送过于频繁，请稍后再试"
    if code.startswith("LimitExceeded."):
        return "验证码发送次数已达上限，请稍后再试"
    return "短信发送失败，请稍后重试"


@dataclass(frozen=True)
class SmsSendResult:
    success: bool
    message: str = ""


class BaseSmsProvider:
    def send_code(self, phone: str, code: str, ttl_seconds: int) -> SmsSendResult:
        raise NotImplementedError


class MockSmsProvider(BaseSmsProvider):
    def send_code(self, phone: str, code: str, ttl_seconds: int) -> SmsSendResult:
        logger.info(
            "[sms] mock provider accepted code phone=%s ttl_seconds=%s",
            mask_phone(phone),
            ttl_seconds,
        )
        return SmsSendResult(True, "mock sms sent")


class TencentSmsProvider(BaseSmsProvider):
    REQUIRED_SETTINGS = (
        "SECRET_ID",
        "SECRET_KEY",
        "SDK_APP_ID",
        "SIGN_NAME",
        "TEMPLATE_ID",
    )

    def __init__(self, config: dict | None = None):
        self.config = config or getattr(settings, "TENCENT_SMS", {})
        missing = [name for name in self.REQUIRED_SETTINGS if not self.config.get(name)]
        if missing:
            raise ImproperlyConfigured(f"Missing Tencent SMS settings: {', '.join(missing)}")

    def send_code(self, phone: str, code: str, ttl_seconds: int) -> SmsSendResult:
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
                TencentCloudSDKException,
            )
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.sms.v20210111 import models, sms_client
        except ImportError as exc:
            raise ImproperlyConfigured("tencentcloud-sdk-python is required for Tencent SMS") from exc

        expire_minutes = max(1, int(round(ttl_seconds / 60)))
        request = models.SendSmsRequest()
        request.SmsSdkAppId = str(self.config["SDK_APP_ID"])
        request.SignName = str(self.config["SIGN_NAME"])
        request.TemplateId = str(self.config["TEMPLATE_ID"])
        request.TemplateParamSet = [str(code), str(expire_minutes)]
        request.PhoneNumberSet = [self._normalize_phone(phone)]

        http_profile = HttpProfile()
        http_profile.endpoint = str(self.config.get("ENDPOINT") or "sms.tencentcloudapi.com")
        http_profile.reqTimeout = int(self.config.get("TIMEOUT_SECONDS") or 30)

        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile

        client = sms_client.SmsClient(
            credential.Credential(str(self.config["SECRET_ID"]), str(self.config["SECRET_KEY"])),
            str(self.config.get("REGION") or "ap-guangzhou"),
            client_profile,
        )

        masked_phone = mask_phone(phone)
        try:
            response = client.SendSms(request)
        except TencentCloudSDKException as exc:
            error_code = getattr(exc, "code", "")
            logger.warning(
                "[sms] tencent send failed phone=%s request_id=%s error_code=%s",
                masked_phone,
                getattr(exc, "requestId", ""),
                error_code,
            )
            return SmsSendResult(False, tencent_sms_error_message(error_code))
        except Exception:
            logger.exception("[sms] tencent send raised unexpected error phone=%s", masked_phone)
            return SmsSendResult(False, "短信发送失败，请稍后重试")

        statuses = list(getattr(response, "SendStatusSet", []) or [])
        failed_status = next(
            (status for status in statuses if getattr(status, "Code", "") != "Ok"),
            None,
        )
        if failed_status:
            error_code = getattr(failed_status, "Code", "")
            logger.warning(
                "[sms] tencent returned non-ok phone=%s request_id=%s code=%s",
                masked_phone,
                getattr(response, "RequestId", ""),
                error_code,
            )
            return SmsSendResult(False, tencent_sms_error_message(error_code))

        logger.info(
            "[sms] tencent send succeeded phone=%s request_id=%s",
            masked_phone,
            getattr(response, "RequestId", ""),
        )
        return SmsSendResult(True, "短信发送成功")

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        raw = str(phone or "").strip()
        if raw.startswith("+"):
            return raw
        return f"+86{raw}"


def get_sms_provider() -> BaseSmsProvider:
    provider = str(getattr(settings, "SMS_PROVIDER", "mock") or "mock").strip().lower()
    if provider == "mock":
        return MockSmsProvider()
    if provider == "tencent":
        return TencentSmsProvider()
    raise ImproperlyConfigured(f"Unsupported SMS_PROVIDER: {provider}")
