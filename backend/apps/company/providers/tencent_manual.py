"""Tencent registry, FaceID, document-text and seal adapters."""

from __future__ import annotations

import base64

from django.conf import settings
from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.faceid.v20180301 import faceid_client, models as face_models
from tencentcloud.ocr.v20181119 import models as ocr_models, ocr_client

from apps.company.models import CompanyVerification
from apps.company.providers.base import (
    VerificationProviderConfigurationError,
    VerificationProviderProtocolError,
    VerificationProviderUnavailableError,
)
from apps.company.providers.manual import (
    ApplicantIdentityPrecheckRequest,
    ApplicantIdentityPrecheckResult,
    ApplicantIdentityQueryRequest,
    ApplicantIdentityQueryResult,
    ApplicantIdentityStartRequest,
    ApplicantIdentityStartResult,
    ApplicantIdentityVerifier,
    AuthorizationDocumentReader,
    BusinessLicenseExtractionResult,
    BusinessLicenseReader,
    DocumentExtractionRequest,
    DocumentTextExtractionResult,
    RegistryVerificationRequest,
    RegistryVerificationResult,
    RegistryVerifier,
    SealExtractionResult,
    SealReader,
)


class TencentManualVerificationProvider(
    RegistryVerifier,
    ApplicantIdentityVerifier,
    BusinessLicenseReader,
    AuthorizationDocumentReader,
    SealReader,
):
    provider_name = CompanyVerification.PROVIDER_TENCENT_MANUAL_V1

    def __init__(self, *, registry_client=None, face_client=None, ocr_client_instance=None):
        self.environment = str(getattr(
            settings, "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT", "production"
        )).strip().lower()
        self.rule_id = str(getattr(settings, "TENCENT_FACEID_RULE_ID", "")).strip()
        self.redirect_url = str(getattr(settings, "TENCENT_FACEID_REDIRECT_URL", "")).strip()
        if registry_client is not None and face_client is not None:
            self._registry_client = registry_client
            self._face_client = face_client
            self._ocr_client = ocr_client_instance or registry_client
            return
        secret_id = str(getattr(settings, "TENCENT_VERIFICATION_SECRET_ID", "")).strip()
        secret_key = str(getattr(settings, "TENCENT_VERIFICATION_SECRET_KEY", "")).strip()
        if not secret_id or not secret_key or not self.rule_id or not self.redirect_url:
            raise VerificationProviderConfigurationError(
                "Tencent verification configuration is incomplete", code="provider_not_configured"
            )
        timeout = max(int(getattr(settings, "TENCENT_VERIFICATION_TIMEOUT_SECONDS", 10)), 1)
        cred = credential.Credential(secret_id, secret_key)
        self._registry_client = registry_client or ocr_client.OcrClient(
            cred, str(getattr(settings, "TENCENT_OCR_REGION", "ap-guangzhou")).strip(),
            self._profile(str(getattr(settings, "TENCENT_OCR_ENDPOINT", "ocr.tencentcloudapi.com")).strip(), timeout),
        )
        self._ocr_client = ocr_client_instance or self._registry_client
        self._face_client = face_client or faceid_client.FaceidClient(
            cred, str(getattr(settings, "TENCENT_FACEID_REGION", "ap-guangzhou")).strip(),
            self._profile(str(getattr(settings, "TENCENT_FACEID_ENDPOINT", "faceid.tencentcloudapi.com")).strip(), timeout),
        )

    @staticmethod
    def _profile(endpoint: str, timeout: int):
        http = HttpProfile()
        http.endpoint = endpoint
        http.reqTimeout = timeout
        profile = ClientProfile()
        profile.httpProfile = http
        return profile

    @staticmethod
    def _raise_provider(exc):
        if isinstance(exc, TencentCloudSDKException):
            raise VerificationProviderUnavailableError(
                "Tencent verification request failed",
                code=str(getattr(exc, "code", "") or "sdk_error"),
                request_id=str(getattr(exc, "requestId", "") or ""),
            ) from exc
        raise VerificationProviderUnavailableError(
            "Tencent verification request failed", code=type(exc).__name__
        ) from exc

    def verify(self, request: RegistryVerificationRequest) -> RegistryVerificationResult:
        if request.mode not in {
            CompanyVerification.MODE_TWO_ELEMENTS,
            CompanyVerification.MODE_THREE_ELEMENTS,
        }:
            raise VerificationProviderConfigurationError(
                "Unsupported registry verification mode", code="unsupported_registry_mode"
            )
        if (
            request.mode == CompanyVerification.MODE_THREE_ELEMENTS
            and not str(request.legal_representative or "").strip()
        ):
            raise VerificationProviderConfigurationError(
                "Three-element verification requires a legal representative",
                code="missing_legal_representative",
            )
        request_type = getattr(ocr_models, "VerifyBizLicenseEnterprise3Request", None)
        method = getattr(self._registry_client, "VerifyBizLicenseEnterprise3", None)
        if request_type is None or method is None:
            raise VerificationProviderConfigurationError(
                "Installed Tencent OCR SDK lacks VerifyBizLicenseEnterprise3",
                code="sdk_contract_unavailable",
            )
        sdk_request = request_type()
        sdk_request.CreditCode = request.registration_no
        sdk_request.EntName = request.company_name
        if request.mode == CompanyVerification.MODE_THREE_ELEMENTS:
            sdk_request.LrName = request.legal_representative
            sdk_request.VerifyType = "ENT_3META"
        else:
            sdk_request.VerifyType = "ENT_2META"
        try:
            response = method(sdk_request)
        except Exception as exc:
            self._raise_provider(exc)
        request_id = str(getattr(response, "RequestId", "") or "").strip()
        status_code = getattr(response, "StatusCode", None)
        verify_result = getattr(response, "VerifyResult", None)
        operating_status = str(getattr(response, "OperatingStatus", "") or "").strip()
        if status_code == 0:
            matched = verify_result == 1
            passed = matched and operating_status == "1"
            return RegistryVerificationResult(
                outcome="passed" if passed else "failed", retryable=False, matched=matched,
                operating_status=operating_status,
                provider_code="registry_match" if passed else (
                    "registry_mismatch" if not matched else "enterprise_not_operating"
                ), request_id=request_id,
                evidence={"status_code": 0, "verify_result": verify_result, "operating_status": operating_status},
            )
        if status_code in {1, 2}:
            return RegistryVerificationResult(
                outcome="unknown", retryable=status_code == 1, matched=None,
                operating_status=operating_status,
                provider_code="registry_system_error" if status_code == 1 else "registry_no_result",
                request_id=request_id, evidence={"status_code": status_code},
            )
        raise VerificationProviderProtocolError(
            "Tencent registry response has an unknown status", code="invalid_registry_response",
            request_id=request_id,
        )

    def extract_business_license(
        self, request: DocumentExtractionRequest
    ) -> BusinessLicenseExtractionResult:
        if request.mime_type not in {"image/jpeg", "image/png"}:
            raise VerificationProviderConfigurationError(
                "Business license OCR requires a JPEG or PNG image",
                code="unsupported_business_license_type",
            )
        sdk_request = ocr_models.BizLicenseOCRRequest()
        sdk_request.ImageBase64 = self._encoded_document(request)
        sdk_request.EnableCopyWarn = True
        sdk_request.EnablePeriodComplete = True
        try:
            response = self._ocr_client.BizLicenseOCR(sdk_request)
        except Exception as exc:
            self._raise_provider(exc)
        request_id = str(getattr(response, "RequestId", "") or "").strip()
        company_name = str(getattr(response, "Name", "") or "").strip()
        registration_no = str(getattr(response, "RegNum", "") or "").strip().upper()
        legal_representative = str(getattr(response, "Person", "") or "").strip()
        warning_codes = tuple(
            int(value) for value in (getattr(response, "RecognizeWarnCode", None) or [])
        )
        complete = bool(company_name and registration_no and legal_representative)
        return BusinessLicenseExtractionResult(
            outcome="passed" if complete else "unknown",
            retryable=not complete,
            company_name=company_name,
            registration_no=registration_no,
            legal_representative=legal_representative,
            warning_codes=warning_codes,
            is_duplication=getattr(response, "IsDuplication", None),
            electronic=getattr(response, "Electronic", None),
            provider_code="business_license_extracted" if complete else "business_license_incomplete",
            request_id=request_id,
        )

    def precheck(
        self, request: ApplicantIdentityPrecheckRequest
    ) -> ApplicantIdentityPrecheckResult:
        if request.identity_type != "resident_identity_card":
            raise VerificationProviderConfigurationError(
                "Unsupported identity type", code="unsupported_identity_type"
            )
        sdk_request = face_models.IdCardVerificationRequest()
        sdk_request.IdCard = request.identity_number
        sdk_request.Name = request.name
        try:
            response = self._face_client.IdCardVerification(sdk_request)
        except Exception as exc:
            self._raise_provider(exc)
        request_id = str(getattr(response, "RequestId", "") or "").strip()
        result_code = str(getattr(response, "Result", "") or "").strip()
        if result_code == "0":
            return ApplicantIdentityPrecheckResult(
                outcome="passed",
                retryable=False,
                matched=True,
                provider_code="identity_match",
                request_id=request_id,
            )
        if result_code in {"-1", "-2", "-3", "-5"}:
            provider_codes = {
                "-1": "identity_mismatch",
                "-2": "identity_number_invalid",
                "-3": "identity_name_invalid",
                "-5": "identity_record_not_found",
            }
            return ApplicantIdentityPrecheckResult(
                outcome="failed",
                retryable=False,
                matched=False,
                provider_code=provider_codes[result_code],
                request_id=request_id,
            )
        if result_code in {"-4", "-6", "-7"}:
            provider_codes = {
                "-4": "identity_registry_unavailable",
                "-6": "identity_registry_upgrading",
                "-7": "identity_daily_limit_reached",
            }
            return ApplicantIdentityPrecheckResult(
                outcome="unknown",
                retryable=result_code in {"-4", "-6"},
                matched=None,
                provider_code=provider_codes[result_code],
                request_id=request_id,
            )
        raise VerificationProviderProtocolError(
            "Tencent identity precheck returned an unknown result",
            code="invalid_identity_precheck_response",
            request_id=request_id,
        )

    def start(self, request: ApplicantIdentityStartRequest) -> ApplicantIdentityStartResult:
        if request.identity_type and request.identity_type != "resident_identity_card":
            raise VerificationProviderConfigurationError(
                "Unsupported identity type", code="unsupported_identity_type"
            )
        sdk_request = face_models.DetectAuthRequest()
        sdk_request.RuleId = request.rule_id
        sdk_request.TerminalType = str(getattr(settings, "TENCENT_FACEID_TERMINAL_TYPE", "0"))
        if request.identity_number:
            sdk_request.IdCard = request.identity_number
        if request.name:
            sdk_request.Name = request.name
        sdk_request.RedirectUrl = request.redirect_url
        sdk_request.Extra = request.correlation_id
        try:
            response = self._face_client.DetectAuth(sdk_request)
        except Exception as exc:
            self._raise_provider(exc)
        token = str(getattr(response, "BizToken", "") or "").strip()
        url = str(getattr(response, "Url", "") or "").strip()
        request_id = str(getattr(response, "RequestId", "") or "").strip()
        if not token or not url:
            raise VerificationProviderProtocolError(
                "Tencent FaceID returned no token or URL", code="missing_face_session",
                request_id=request_id,
            )
        return ApplicantIdentityStartResult(
            outcome="started", retryable=False, token=token, verification_url=url,
            provider_code="face_started", request_id=request_id, expires_in_seconds=7200,
        )

    def query(self, request: ApplicantIdentityQueryRequest) -> ApplicantIdentityQueryResult:
        sdk_request = face_models.GetDetectInfoEnhancedRequest()
        sdk_request.BizToken = request.token
        sdk_request.RuleId = request.rule_id
        sdk_request.InfoType = "1"
        sdk_request.BestFramesCount = 0
        sdk_request.IsCutIdCardImage = False
        sdk_request.IsNeedIdCardAvatar = False
        sdk_request.IsReturnAllVideo = False
        try:
            response = self._face_client.GetDetectInfoEnhanced(sdk_request)
        except Exception as exc:
            self._raise_provider(exc)
        request_id = str(getattr(response, "RequestId", "") or "").strip()
        text = getattr(response, "Text", None)
        if text is None or getattr(text, "ErrCode", None) is None:
            return ApplicantIdentityQueryResult(
                outcome="in_progress", retryable=True, provider_code="face_result_pending",
                request_id=request_id,
            )
        err_code = int(text.ErrCode)
        live_status = getattr(text, "LiveStatus", None)
        compare_status = getattr(text, "Comparestatus", None)
        passed = err_code == 0 and live_status == 0 and compare_status == 0
        if passed:
            provider_code = "face_0"
        elif err_code == 0:
            provider_code = "face_incomplete_result"
        else:
            provider_code = f"face_{err_code}"
        provider_name = str(
            getattr(text, "Name", "") or getattr(text, "OcrName", "") or ""
        ).strip()
        provider_identity_number = str(
            getattr(text, "IdCard", "") or getattr(text, "OcrIdCard", "") or ""
        ).strip().upper()
        return ApplicantIdentityQueryResult(
            outcome="passed" if passed else "failed", retryable=False,
            name=provider_name,
            identity_number=provider_identity_number,
            identity_type=("resident_identity_card" if getattr(text, "UseIDType", None) == 0 else "unsupported"),
            correlation_id=str(getattr(text, "Extra", "") or "").strip(),
            provider_code=provider_code, request_id=request_id,
            evidence={
                "err_code": err_code,
                "live_status": live_status,
                "compare_status": compare_status,
            },
        )

    @staticmethod
    def _encoded_document(request: DocumentExtractionRequest) -> str:
        if request.mime_type not in {"image/jpeg", "image/png", "application/pdf"}:
            raise VerificationProviderConfigurationError(
                "Unsupported authorization document type", code="unsupported_document_type"
            )
        encoded = base64.b64encode(request.content).decode("ascii")
        maximum = int(getattr(settings, "TENCENT_OCR_MAX_BASE64_BYTES", 10 * 1024 * 1024))
        if not encoded or len(encoded.encode("ascii")) > maximum:
            raise VerificationProviderConfigurationError(
                "Authorization document exceeds Tencent OCR limits", code="ocr_payload_too_large"
            )
        if request.page_number != 1:
            raise VerificationProviderConfigurationError(
                "Only the first page is supported", code="unsupported_pdf_page"
            )
        return encoded

    @staticmethod
    def _polygon(value) -> list[dict]:
        return [
            {"x": int(getattr(point, "X", 0) or 0), "y": int(getattr(point, "Y", 0) or 0)}
            for point in list(value or [])[:4]
        ]

    @staticmethod
    def _rect(value) -> dict:
        if value is None:
            return {}
        return {
            "x": int(getattr(value, "X", 0) or 0),
            "y": int(getattr(value, "Y", 0) or 0),
            "width": int(getattr(value, "Width", 0) or 0),
            "height": int(getattr(value, "Height", 0) or 0),
        }

    def extract_document_text(
        self, request: DocumentExtractionRequest
    ) -> DocumentTextExtractionResult:
        sdk_request = ocr_models.GeneralAccurateOCRRequest()
        sdk_request.ImageBase64 = self._encoded_document(request)
        sdk_request.IsPdf = request.mime_type == "application/pdf"
        sdk_request.PdfPageNumber = 1
        try:
            response = self._ocr_client.GeneralAccurateOCR(sdk_request)
        except Exception as exc:
            self._raise_provider(exc)
        request_id = str(getattr(response, "RequestId", "") or "").strip()
        detections = list(getattr(response, "TextDetections", None) or [])
        lines = tuple(
            {
                "text": str(getattr(item, "DetectedText", "") or "").strip()[:512],
                "confidence": int(getattr(item, "Confidence", 0) or 0),
                "polygon": self._polygon(getattr(item, "Polygon", None)),
            }
            for item in detections[:300]
            if str(getattr(item, "DetectedText", "") or "").strip()
        )
        return DocumentTextExtractionResult(
            outcome="passed" if lines else "unknown",
            retryable=not lines,
            lines=lines,
            provider_code="document_text_extracted" if lines else "document_text_empty",
            request_id=request_id,
        )

    def extract_seal(self, request: DocumentExtractionRequest) -> SealExtractionResult:
        sdk_request = ocr_models.SealOCRRequest()
        sdk_request.ImageBase64 = self._encoded_document(request)
        sdk_request.EnablePdf = request.mime_type == "application/pdf"
        sdk_request.PdfPageNumber = 1
        try:
            response = self._ocr_client.SealOCR(sdk_request)
        except Exception as exc:
            self._raise_provider(exc)
        request_id = str(getattr(response, "RequestId", "") or "").strip()
        raw_seals = list(getattr(response, "SealInfos", None) or [])
        if not raw_seals and str(getattr(response, "SealBody", "") or "").strip():
            raw_seals = [response]
        seals = tuple(
            {
                "text": str(getattr(item, "SealBody", "") or "").strip()[:255],
                "shape": str(getattr(item, "SealShape", "") or "").strip()[:16],
                "location": self._rect(getattr(item, "Location", None)),
            }
            for item in raw_seals[:20]
            if str(getattr(item, "SealBody", "") or "").strip()
        )
        return SealExtractionResult(
            outcome="passed" if seals else "unknown",
            retryable=not seals,
            seals=seals,
            provider_code="seal_extracted" if seals else "seal_not_detected",
            request_id=request_id,
        )
