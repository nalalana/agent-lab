from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Tags, register

from apps.company.retention_policy import (
    RETENTION_POLICY_ID,
    TERMINAL_DOCUMENT_AGE_SECONDS,
)


@register(Tags.security)
def enterprise_verification_configuration_check(app_configs, **kwargs):
    if not getattr(settings, "COMPANY_VERIFICATION_ENABLED", False):
        return []

    errors = []
    provider = str(getattr(settings, "COMPANY_VERIFICATION_PROVIDER", "")).strip()
    environment = str(
        getattr(settings, "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT", "")
    ).strip()
    mode = str(getattr(settings, "COMPANY_VERIFICATION_REGISTRY_MODE", "")).strip()
    if provider != "tencent_manual_v1":
        errors.append(Error("Unsupported enterprise-verification provider.", id="company.EV2001"))
    if environment not in {"sandbox", "production", "mock"}:
        errors.append(Error("Invalid enterprise-verification provider environment.", id="company.EV2002"))
    if mode != "two_elements":
        errors.append(Error("Only two-element enterprise registry verification is implemented.", id="company.EV3001"))
    if environment != "mock":
        if getattr(settings, "COMPANY_VERIFICATION_ALLOW_MOCK", False):
            errors.append(Error("Mock enterprise-verification evidence is forbidden outside mock mode.", id="company.EV2004"))
        load_error = str(
            getattr(settings, "COMPANY_VERIFICATION_SECRETS_LOAD_ERROR", "")
        ).strip()
        if load_error:
            errors.append(Error(
                f"Enterprise-verification secret file is unavailable ({load_error}).",
                id="company.EV3004",
            ))
        if (
            not str(getattr(settings, "COMPANY_VERIFICATION_PII_ENCRYPTION_KEY", "")).strip()
            or not str(getattr(settings, "COMPANY_VERIFICATION_PII_HMAC_KEY", "")).strip()
            or not str(getattr(settings, "COMPANY_VERIFICATION_PII_KEY_VERSION", "")).strip()
        ):
            errors.append(Error(
                "Enterprise verification requires local PII encryption and HMAC keys.",
                id="company.EV2005",
            ))
        if (
            not str(getattr(settings, "TENCENT_VERIFICATION_SECRET_ID", "")).strip()
            or not str(getattr(settings, "TENCENT_VERIFICATION_SECRET_KEY", "")).strip()
        ):
            errors.append(Error("Enterprise verification requires runtime Tencent credentials.", id="company.EV3002"))
        if (
            not str(getattr(settings, "TENCENT_FACEID_RULE_ID", "")).strip()
            or not str(getattr(settings, "TENCENT_FACEID_REDIRECT_URL", "")).strip()
        ):
            errors.append(Error("Enterprise verification requires FaceID RuleId and redirect URL.", id="company.EV3003"))
    if environment == "production":
        if (
            not str(getattr(settings, "VERIFICATION_PRIVATE_COS_BUCKET", "")).strip()
            or not str(getattr(settings, "VERIFICATION_PRIVATE_COS_REGION", "")).strip()
        ):
            errors.append(Error("Production enterprise verification requires private evidence storage.", id="company.EV2006"))
        storage_error = str(
            getattr(settings, "VERIFICATION_PRIVATE_COS_SECRETS_LOAD_ERROR", "")
        ).strip()
        if storage_error:
            errors.append(Error(
                f"Private verification storage credentials are unavailable ({storage_error}).",
                id="company.EV4001",
            ))
        if (
            not str(getattr(settings, "VERIFICATION_PRIVATE_COS_SECRET_ID", "")).strip()
            or not str(getattr(settings, "VERIFICATION_PRIVATE_COS_SECRET_KEY", "")).strip()
        ):
            errors.append(Error(
                "Production enterprise verification requires private-COS credentials.",
                id="company.EV4002",
            ))
        retention_policy = str(
            getattr(settings, "VERIFICATION_RETENTION_POLICY", "")
        ).strip()
        if not retention_policy:
            errors.append(Error("Production enterprise verification requires an approved retention policy.", id="company.EV2010"))
        elif retention_policy != RETENTION_POLICY_ID:
            errors.append(Error(
                "Unsupported enterprise-verification retention policy.",
                id="company.EV2011",
            ))
        if int(getattr(
            settings,
            "COMPANY_VERIFICATION_RETENTION_DRY_RUN_AGE_SECONDS",
            TERMINAL_DOCUMENT_AGE_SECONDS,
        )) != TERMINAL_DOCUMENT_AGE_SECONDS:
            errors.append(Error(
                "Enterprise-verification document inventory must use 180 days.",
                id="company.EV2012",
            ))
        redirect_url = str(
            getattr(settings, "TENCENT_FACEID_REDIRECT_URL", "")
        ).strip()
        if urlsplit(redirect_url).scheme.lower() != "https":
            errors.append(Error(
                "Production FaceID redirect URL must use HTTPS.", id="company.EV3005"
            ))
    elif environment == "mock":
        if not getattr(settings, "COMPANY_VERIFICATION_ALLOW_MOCK", False):
            errors.append(Error("Mock verification must be explicitly enabled.", id="company.EV2007"))
        if not str(getattr(settings, "COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY", "")).strip():
            errors.append(Error("Mock verification requires a test encryption key.", id="company.EV2008"))
        if not str(getattr(settings, "COMPANY_VERIFICATION_TEST_HMAC_KEY", "")).strip():
            errors.append(Error("Mock verification requires a separate test HMAC key.", id="company.EV2009"))
    return errors
