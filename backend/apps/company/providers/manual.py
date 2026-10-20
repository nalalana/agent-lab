"""Typed contracts for manual enterprise-verification evidence providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from apps.company.providers.base import VerificationProviderUnavailableError


@dataclass(frozen=True)
class RegistryVerificationRequest:
    company_name: str
    registration_no: str
    mode: str
    legal_representative: str = ""


@dataclass(frozen=True)
class RegistryVerificationResult:
    outcome: str
    retryable: bool
    matched: bool | None = None
    operating_status: str = ""
    provider_code: str = ""
    request_id: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BusinessLicenseExtractionResult:
    outcome: str
    retryable: bool
    company_name: str = ""
    registration_no: str = ""
    legal_representative: str = ""
    warning_codes: tuple[int, ...] = ()
    is_duplication: int | None = None
    electronic: bool | None = None
    provider_code: str = ""
    request_id: str = ""


@dataclass(frozen=True)
class ApplicantIdentityPrecheckRequest:
    name: str
    identity_number: str
    identity_type: str


@dataclass(frozen=True)
class ApplicantIdentityPrecheckResult:
    outcome: str
    retryable: bool
    matched: bool | None = None
    provider_code: str = ""
    request_id: str = ""


@dataclass(frozen=True)
class ApplicantIdentityStartRequest:
    name: str
    identity_number: str
    identity_type: str
    rule_id: str
    redirect_url: str
    correlation_id: str


@dataclass(frozen=True)
class ApplicantIdentityStartResult:
    outcome: str
    retryable: bool
    token: str = ""
    verification_url: str = ""
    provider_code: str = ""
    request_id: str = ""
    expires_in_seconds: int = 0


@dataclass(frozen=True)
class ApplicantIdentityQueryRequest:
    token: str
    rule_id: str


@dataclass(frozen=True)
class ApplicantIdentityQueryResult:
    outcome: str
    retryable: bool
    name: str = ""
    identity_number: str = ""
    identity_type: str = ""
    correlation_id: str = ""
    provider_code: str = ""
    request_id: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentExtractionRequest:
    content: bytes
    mime_type: str
    page_number: int = 1


@dataclass(frozen=True)
class DocumentTextExtractionResult:
    outcome: str
    retryable: bool
    lines: tuple[dict, ...] = ()
    provider_code: str = ""
    request_id: str = ""


@dataclass(frozen=True)
class SealExtractionResult:
    outcome: str
    retryable: bool
    seals: tuple[dict, ...] = ()
    provider_code: str = ""
    request_id: str = ""


# Backwards-compatible result names retained for callers created in EV-2.
ProviderEvidenceResult = RegistryVerificationResult
FaceSessionStartResult = ApplicantIdentityStartResult


class RegistryVerifier(ABC):
    @abstractmethod
    def verify(self, request: RegistryVerificationRequest) -> RegistryVerificationResult:
        raise NotImplementedError


class BusinessLicenseReader(ABC):
    @abstractmethod
    def extract_business_license(
        self, request: DocumentExtractionRequest
    ) -> BusinessLicenseExtractionResult:
        raise NotImplementedError


class ApplicantIdentityVerifier(ABC):
    @abstractmethod
    def precheck(
        self, request: ApplicantIdentityPrecheckRequest
    ) -> ApplicantIdentityPrecheckResult:
        raise NotImplementedError

    @abstractmethod
    def start(self, request: ApplicantIdentityStartRequest) -> ApplicantIdentityStartResult:
        raise NotImplementedError

    @abstractmethod
    def query(self, request: ApplicantIdentityQueryRequest) -> ApplicantIdentityQueryResult:
        raise NotImplementedError


class AuthorizationDocumentReader(ABC):
    @abstractmethod
    def extract_document_text(
        self, request: DocumentExtractionRequest
    ) -> DocumentTextExtractionResult:
        raise NotImplementedError


class SealReader(ABC):
    @abstractmethod
    def extract_seal(self, request: DocumentExtractionRequest) -> SealExtractionResult:
        raise NotImplementedError


class ManualVerificationProvider(RegistryVerifier, ApplicantIdentityVerifier):
    """Compatibility umbrella; new services depend on the two narrow contracts."""

    def verify_company(self, *, snapshot: dict, mode: str) -> RegistryVerificationResult:
        return self.verify(RegistryVerificationRequest(
            company_name=str(snapshot.get("company_name", "")),
            registration_no=str(snapshot.get("registration_no", "")),
            mode=mode,
        ))

    def start_applicant_face(self, *, identity: dict, context: dict) -> ApplicantIdentityStartResult:
        return self.start(ApplicantIdentityStartRequest(
            name=str(identity.get("name", "")),
            identity_number=str(identity.get("identity_number", "")),
            identity_type=str(identity.get("identity_type", "")),
            rule_id=str(context.get("rule_id", "")),
            redirect_url=str(context.get("redirect_url", "")),
            correlation_id=str(context.get("correlation_id", "")),
        ))

    def query_applicant_face(self, *, token: str, context: dict) -> ApplicantIdentityQueryResult:
        return self.query(ApplicantIdentityQueryRequest(
            token=token, rule_id=str(context.get("rule_id", ""))
        ))

    def extract_document_text(self, *, document: dict):
        raise VerificationProviderUnavailableError("Document OCR is not implemented in EV-3", code="not_implemented")

    def extract_seal(self, *, document: dict):
        raise VerificationProviderUnavailableError("Seal OCR is not implemented in EV-3", code="not_implemented")


class UnavailableRegistryVerifier(RegistryVerifier):
    def verify(self, request: RegistryVerificationRequest) -> RegistryVerificationResult:
        raise VerificationProviderUnavailableError(
            "Registry verifier is not configured", code="provider_not_configured"
        )


class UnavailableApplicantIdentityVerifier(ApplicantIdentityVerifier):
    def precheck(
        self, request: ApplicantIdentityPrecheckRequest
    ) -> ApplicantIdentityPrecheckResult:
        raise VerificationProviderUnavailableError(
            "Applicant identity verifier is not configured", code="provider_not_configured"
        )

    def start(self, request: ApplicantIdentityStartRequest) -> ApplicantIdentityStartResult:
        raise VerificationProviderUnavailableError(
            "Applicant identity verifier is not configured", code="provider_not_configured"
        )

    def query(self, request: ApplicantIdentityQueryRequest) -> ApplicantIdentityQueryResult:
        raise VerificationProviderUnavailableError(
            "Applicant identity verifier is not configured", code="provider_not_configured"
        )


class UnavailableManualVerificationProvider(
    ManualVerificationProvider, UnavailableRegistryVerifier, UnavailableApplicantIdentityVerifier
):
    pass
