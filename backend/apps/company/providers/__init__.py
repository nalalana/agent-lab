from apps.company.providers.base import (
    EnterpriseVerificationProvider,
    VerificationProviderConfigurationError,
)
from apps.company.providers.manual import (
    ApplicantIdentityVerifier,
    AuthorizationDocumentReader,
    BusinessLicenseReader,
    ManualVerificationProvider,
    RegistryVerifier,
    SealReader,
    UnavailableApplicantIdentityVerifier,
    UnavailableManualVerificationProvider,
    UnavailableRegistryVerifier,
)
from apps.company.providers.tencent_ess import TencentEssVerificationProvider
from apps.company.providers.tencent_manual import TencentManualVerificationProvider


def get_enterprise_verification_provider(
    provider_name: str = "tencent_ess",
) -> EnterpriseVerificationProvider:
    if provider_name != "tencent_ess":
        raise VerificationProviderConfigurationError(
            f"Unsupported legacy verification provider: {provider_name}",
            code="unsupported_provider",
        )
    return TencentEssVerificationProvider()


def _manual_provider():
    from django.conf import settings

    provider = str(getattr(settings, "COMPANY_VERIFICATION_PROVIDER", "")).strip()
    environment = str(getattr(
        settings, "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT", "production"
    )).strip().lower()
    if provider != "tencent_manual_v1":
        raise VerificationProviderConfigurationError(
            "Unsupported manual verification provider", code="unsupported_provider"
        )
    if environment == "mock":
        raise VerificationProviderConfigurationError(
            "Mock providers must be explicitly injected", code="mock_provider_forbidden"
        )
    return TencentManualVerificationProvider()


def get_registry_verifier() -> RegistryVerifier:
    return _manual_provider()


def get_business_license_reader() -> BusinessLicenseReader:
    return _manual_provider()


def get_applicant_identity_verifier() -> ApplicantIdentityVerifier:
    return _manual_provider()


def get_authorization_document_reader() -> AuthorizationDocumentReader:
    return _manual_provider()


def get_seal_reader() -> SealReader:
    return _manual_provider()


def get_manual_verification_provider() -> ManualVerificationProvider:
    # Compatibility boundary for EV-2 callers. New code uses the narrow factories.
    return UnavailableManualVerificationProvider()


__all__ = [
    "ApplicantIdentityVerifier",
    "AuthorizationDocumentReader",
    "BusinessLicenseReader",
    "EnterpriseVerificationProvider",
    "ManualVerificationProvider",
    "RegistryVerifier",
    "SealReader",
    "TencentEssVerificationProvider",
    "TencentManualVerificationProvider",
    "UnavailableApplicantIdentityVerifier",
    "UnavailableManualVerificationProvider",
    "UnavailableRegistryVerifier",
    "get_applicant_identity_verifier",
    "get_authorization_document_reader",
    "get_business_license_reader",
    "get_enterprise_verification_provider",
    "get_manual_verification_provider",
    "get_registry_verifier",
    "get_seal_reader",
]
