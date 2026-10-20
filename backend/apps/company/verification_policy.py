"""Fail-closed evidence policy for manual enterprise verification."""

from __future__ import annotations

from dataclasses import dataclass
import re

from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationCheck,
    VerificationDocument,
)


@dataclass(frozen=True)
class VerificationPolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


REQUIRED_APPROVAL_CHECKS = (
    VerificationCheck.KIND_REGISTRY,
    VerificationCheck.KIND_FACE,
    VerificationCheck.KIND_DOCUMENT_SECURITY,
    VerificationCheck.KIND_DOCUMENT_TEXT,
    VerificationCheck.KIND_SEAL,
    VerificationCheck.KIND_CONSISTENCY,
)

REGISTRATION_COMMON_CHECKS = (
    VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY,
    VerificationCheck.KIND_BUSINESS_LICENSE,
    VerificationCheck.KIND_REGISTRY,
    VerificationCheck.KIND_FACE,
)

_EXPECTED_EVIDENCE = {
    VerificationCheck.KIND_REGISTRY: (
        CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        "VerifyBizLicenseEnterprise3",
    ),
    VerificationCheck.KIND_FACE: (
        CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        "GetDetectInfoEnhanced",
    ),
    VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY: (
        "local_file_security",
        "inspect_and_scan",
    ),
    VerificationCheck.KIND_BUSINESS_LICENSE: (
        CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        "BizLicenseOCR",
    ),
    VerificationCheck.KIND_DOCUMENT_SECURITY: ("local_file_security", "inspect_and_scan"),
    VerificationCheck.KIND_DOCUMENT_TEXT: (
        CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        "GeneralAccurateOCR",
    ),
    VerificationCheck.KIND_SEAL: (
        CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        "SealOCR",
    ),
    VerificationCheck.KIND_CONSISTENCY: ("touming_rules", "authorization_consistency_v1"),
}

_DOCUMENT_CHECKS = {
    VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY,
    VerificationCheck.KIND_BUSINESS_LICENSE,
    VerificationCheck.KIND_DOCUMENT_SECURITY,
    VerificationCheck.KIND_DOCUMENT_TEXT,
    VerificationCheck.KIND_SEAL,
    VerificationCheck.KIND_CONSISTENCY,
}


def _validate_document_storage(application, document, *, label: str, reasons: list[str]):
    if document is None:
        reasons.append(f"{label}_document_missing")
        return
    expected_prefix = (
        f"enterprise-verification/{application.provider_environment}/"
        f"{application.public_id}/{document.material_revision}/{document.id}/"
    )
    if not document.storage_key.startswith(expected_prefix):
        reasons.append(f"{label}_document_storage_key_invalid")
    if not document.preview_storage_key.startswith(expected_prefix):
        reasons.append(f"{label}_document_preview_key_invalid")
    if not document.storage_version or not document.preview_storage_version:
        reasons.append(f"{label}_document_storage_version_missing")
    if not re.fullmatch(r"[0-9a-f]{64}", document.sha256 or ""):
        reasons.append(f"{label}_document_digest_invalid")


def evaluate_manual_approval(
    application: CompanyVerification,
    *,
    now=None,
    allow_mock: bool = False,
    expected_stage: str | None = None,
    require_submitted_binding: bool = True,
) -> VerificationPolicyDecision:
    now = now or timezone.now()
    reasons: list[str] = []
    if application.provider != CompanyVerification.PROVIDER_TENCENT_MANUAL_V1:
        reasons.append("wrong_provider")
    if application.status != CompanyVerification.STATUS_PENDING:
        reasons.append("application_not_pending")
    expected_stage = expected_stage or CompanyVerification.STAGE_MANUAL_REVIEW
    if application.stage != expected_stage:
        reasons.append(
            "application_not_in_manual_review"
            if expected_stage == CompanyVerification.STAGE_MANUAL_REVIEW
            else "application_not_ready_to_submit"
        )
    if require_submitted_binding and (
        not application.submitted_at
        or application.submitted_revision != application.revision
        or application.submitted_material_revision != application.material_revision
    ):
        reasons.append("submitted_version_mismatch")
    if not application.expires_at or application.expires_at <= now:
        reasons.append("application_expired")
    if application.company.verification_status != Company.VERIFICATION_STATUS_UNVERIFIED:
        reasons.append("company_not_unverified")
    if (
        application.company.company_name != application.company_name_snapshot
        or application.company.registration_no != application.registration_no_snapshot
        or str(application.company.establishment_date or "")
        != application.establishment_date_snapshot
        or str(application.company.address or "") != application.address_snapshot
    ):
        reasons.append("company_snapshot_changed")
    if application.provider_environment == CompanyVerification.ENVIRONMENT_MOCK and not allow_mock:
        reasons.append("mock_application")

    authorization_document = application.documents.filter(
        material_revision=application.material_revision,
        document_type=VerificationDocument.TYPE_AUTHORIZATION,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).first()
    business_license_document = application.documents.filter(
        document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()
    is_registration_flow = bool(application.applicant_role)
    requires_authorization = (
        not is_registration_flow
        or application.applicant_role
        == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
    )
    if requires_authorization:
        _validate_document_storage(
            application,
            authorization_document,
            label="authorization",
            reasons=reasons,
        )
    if is_registration_flow:
        _validate_document_storage(
            application,
            business_license_document,
            label="business_license",
            reasons=reasons,
        )

    required_checks = (
        REGISTRATION_COMMON_CHECKS
        + (
            (
                VerificationCheck.KIND_DOCUMENT_SECURITY,
                VerificationCheck.KIND_DOCUMENT_TEXT,
                VerificationCheck.KIND_SEAL,
                VerificationCheck.KIND_CONSISTENCY,
            )
            if requires_authorization else ()
        )
        if is_registration_flow else REQUIRED_APPROVAL_CHECKS
    )
    document_by_kind = {
        VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY: business_license_document,
        VerificationCheck.KIND_BUSINESS_LICENSE: business_license_document,
        VerificationCheck.KIND_DOCUMENT_SECURITY: authorization_document,
        VerificationCheck.KIND_DOCUMENT_TEXT: authorization_document,
        VerificationCheck.KIND_SEAL: authorization_document,
        VerificationCheck.KIND_CONSISTENCY: authorization_document,
    }

    for kind in required_checks:
        evidence_document = document_by_kind.get(kind)
        checks = application.checks.filter(kind=kind).order_by("-checked_at", "-created_at")
        if kind in _DOCUMENT_CHECKS:
            if evidence_document is None:
                reasons.append(f"{kind}_check_missing")
                continue
            checks = checks.filter(material_revision=evidence_document.material_revision)
        check = checks.first()
        if check is None:
            reasons.append(f"{kind}_check_missing")
            continue
        if check.status != VerificationCheck.STATUS_PASSED:
            reasons.append(f"{kind}_check_not_passed")
        if not check.checked_at or not check.valid_until or check.valid_until <= now:
            reasons.append(f"{kind}_check_stale")
        if check.provider_environment != application.provider_environment:
            reasons.append(f"{kind}_environment_mismatch")
        expected_provider, expected_action = _EXPECTED_EVIDENCE[kind]
        if check.provider != expected_provider:
            reasons.append(f"{kind}_provider_mismatch")
        if check.action != expected_action:
            reasons.append(f"{kind}_action_mismatch")
        if check.rule_version != application.policy_version:
            reasons.append(f"{kind}_rule_version_mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", check.input_digest or ""):
            reasons.append(f"{kind}_input_digest_invalid")
        if check.mode and check.mode != application.mode:
            reasons.append(f"{kind}_mode_mismatch")
        if check.provider_environment == CompanyVerification.ENVIRONMENT_MOCK and not allow_mock:
            reasons.append(f"{kind}_mock_evidence")
        if kind == VerificationCheck.KIND_FACE:
            if not check.face_session_id:
                reasons.append("face_session_missing")
            else:
                face_session = check.face_session
                if face_session.application_id != application.id:
                    reasons.append("face_session_mismatch")
                latest_attempt = application.face_sessions.order_by("-attempt_no").values_list("attempt_no", flat=True).first()
                if latest_attempt != face_session.attempt_no:
                    reasons.append("face_session_not_current")
                if face_session.provider_environment != application.provider_environment:
                    reasons.append("face_session_environment_mismatch")
                if (
                    face_session.status != face_session.STATUS_PASSED
                    or face_session.identity_match is not True
                    or not face_session.result_valid_until
                    or face_session.result_valid_until <= now
                ):
                    reasons.append("face_session_not_valid")
        result = check.result if isinstance(check.result, dict) else {}
        if kind == VerificationCheck.KIND_REGISTRY:
            if not (
                result.get("outcome") == "passed"
                and result.get("matched") is True
                and result.get("operating_status") == "1"
            ):
                reasons.append("registry_evidence_invalid")
        elif kind == VerificationCheck.KIND_FACE:
            if not (
                result.get("outcome") == "passed"
                and result.get("identity_match") is True
            ):
                reasons.append("face_evidence_invalid")
        else:
            if result.get("adopted") is not True:
                reasons.append(f"{kind}_evidence_not_adopted")
            if kind in {
                VerificationCheck.KIND_DOCUMENT_SECURITY,
                VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY,
            }:
                if (
                    evidence_document is None
                    or result.get("sha256") != evidence_document.sha256
                ):
                    reasons.append("document_security_digest_mismatch")
            elif result.get("outcome") != "passed":
                reasons.append(f"{kind}_evidence_invalid")
            if kind == VerificationCheck.KIND_CONSISTENCY and result.get("transcription_id"):
                transcription = application.transcriptions.filter(
                    pk=result.get("transcription_id"),
                    document=authorization_document,
                    material_revision=authorization_document.material_revision,
                    result_check=check,
                    source_check_id=result.get("source_consistency_check_id"),
                ).first()
                if transcription is None:
                    reasons.append("consistency_transcription_invalid")
        if kind in _DOCUMENT_CHECKS:
            if not check.document_id:
                reasons.append(f"{kind}_document_missing")
            elif evidence_document is None or check.document_id != evidence_document.id:
                reasons.append(f"{kind}_document_mismatch")
        elif check.document_id and check.document.application_id != application.id:
            reasons.append(f"{kind}_document_mismatch")
        if check.face_session_id and check.face_session.application_id != application.id:
            reasons.append(f"{kind}_face_session_mismatch")

    return VerificationPolicyDecision(allowed=not reasons, reasons=tuple(dict.fromkeys(reasons)))


def evaluate_manual_submission(
    application: CompanyVerification,
    *,
    now=None,
    allow_mock: bool = False,
) -> VerificationPolicyDecision:
    return evaluate_manual_approval(
        application,
        now=now,
        allow_mock=allow_mock,
        expected_stage=CompanyVerification.STAGE_READY_TO_SUBMIT,
        require_submitted_binding=False,
    )
