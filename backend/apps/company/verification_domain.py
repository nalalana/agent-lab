"""Provider-neutral enterprise verification application state services."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationFaceSession,
    VerificationOperation,
)
from apps.company.providers import get_applicant_identity_verifier
from apps.company.providers.base import VerificationProviderError
from apps.company.providers.manual import ApplicantIdentityPrecheckRequest
from apps.company.validators import validate_social_credit_code
from apps.company.verification_crypto import (
    VerificationCryptoError,
    get_identity_protector,
)
from apps.user.models import User


ACTIVE_STATUSES = (
    CompanyVerification.STATUS_INITIATING,
    CompanyVerification.STATUS_PENDING,
)
TERMINAL_STATUSES = (
    CompanyVerification.STATUS_VERIFIED,
    CompanyVerification.STATUS_REJECTED,
    CompanyVerification.STATUS_EXPIRED,
    CompanyVerification.STATUS_CANCELLED,
)
ALLOWED_STATUS_TRANSITIONS = {
    CompanyVerification.STATUS_INITIATING: {
        CompanyVerification.STATUS_PENDING,
        CompanyVerification.STATUS_CANCELLED,
        CompanyVerification.STATUS_EXPIRED,
    },
    CompanyVerification.STATUS_PENDING: {
        CompanyVerification.STATUS_REJECTED,
        CompanyVerification.STATUS_CANCELLED,
        CompanyVerification.STATUS_EXPIRED,
    },
}
ALLOWED_STAGE_TRANSITIONS = {
    CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD: {
        CompanyVerification.STAGE_COMPANY_CHECK,
    },
    CompanyVerification.STAGE_COMPANY_CHECK: {
        CompanyVerification.STAGE_APPLICANT_FACE,
    },
    CompanyVerification.STAGE_APPLICANT_FACE: {
        CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
        CompanyVerification.STAGE_READY_TO_SUBMIT,
    },
    CompanyVerification.STAGE_AUTHORIZATION_UPLOAD: {
        CompanyVerification.STAGE_DOCUMENT_CHECK,
    },
    CompanyVerification.STAGE_DOCUMENT_CHECK: {
        CompanyVerification.STAGE_READY_TO_SUBMIT,
        CompanyVerification.STAGE_SUPPLEMENT_REQUIRED,
    },
    CompanyVerification.STAGE_READY_TO_SUBMIT: {
        CompanyVerification.STAGE_MANUAL_REVIEW,
    },
    CompanyVerification.STAGE_MANUAL_REVIEW: {
        CompanyVerification.STAGE_SUPPLEMENT_REQUIRED,
    },
    CompanyVerification.STAGE_SUPPLEMENT_REQUIRED: {
        CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
    },
}

NEXT_ACTION_BY_STAGE = {
    CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD: "upload_business_license",
    CompanyVerification.STAGE_COMPANY_CHECK: "wait_company_check",
    CompanyVerification.STAGE_APPLICANT_FACE: "start_applicant_face",
    CompanyVerification.STAGE_AUTHORIZATION_UPLOAD: "upload_authorization",
    CompanyVerification.STAGE_DOCUMENT_CHECK: "wait_document_check",
    CompanyVerification.STAGE_READY_TO_SUBMIT: "submit_for_review",
    CompanyVerification.STAGE_MANUAL_REVIEW: "wait_manual_review",
    CompanyVerification.STAGE_SUPPLEMENT_REQUIRED: "upload_supplement",
    CompanyVerification.STAGE_COMPLETED: "none",
}

# These are user-requested navigation reversals between the five public flow
# steps. Evidence remains append-only; returning only changes the active stage.
PREVIOUS_STAGE_BY_STAGE = {
    CompanyVerification.STAGE_COMPANY_CHECK: CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD,
    CompanyVerification.STAGE_AUTHORIZATION_UPLOAD: CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD,
    CompanyVerification.STAGE_DOCUMENT_CHECK: CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
    CompanyVerification.STAGE_SUPPLEMENT_REQUIRED: CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
    CompanyVerification.STAGE_READY_TO_SUBMIT: CompanyVerification.STAGE_APPLICANT_FACE,
}


def _previous_stage(application: CompanyVerification) -> str | None:
    if application.stage == CompanyVerification.STAGE_APPLICANT_FACE:
        return (
            CompanyVerification.STAGE_AUTHORIZATION_UPLOAD
            if application.applicant_role == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
            else CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD
        )
    return PREVIOUS_STAGE_BY_STAGE.get(application.stage)


class VerificationDomainError(Exception):
    MESSAGES = {
        "feature_disabled": "企业认证功能尚未启用",
        "provider_unavailable": "企业认证服务暂时不可用",
        "face_rule_not_found": "腾讯实名核身业务流程ID无效，请联系管理员重新配置 RuleId",
        "face_rule_disabled": "腾讯实名核身业务流程已停用，请联系管理员启用或更换 RuleId",
        "face_service_not_activated": "腾讯云实名核身服务尚未正确开通",
        "company_not_found": "公司不存在",
        "not_draft_owner": "只有企业草稿创建者可以发起认证",
        "already_verified": "企业已完成认证",
        "company_identity_incomplete": "请先完善企业名称和统一社会信用代码",
        "application_active": "企业已有进行中的认证申请",
        "application_not_found": "企业认证申请不存在",
        "application_forbidden": "无权访问该认证申请",
        "invalid_request": "认证申请参数无效",
        "applicant_identity_required": "请先补充核验人的姓名和居民身份证号",
        "applicant_identity_mismatch": "核验人姓名与营业执照识别的法定代表人不一致",
        "identity_information_mismatch": "姓名与身份证号未通过权威二要素核验，请核对后重新填写",
        "identity_precheck_unavailable": "身份信息暂时无法核验，请稍后重试",
        "idempotency_conflict": "幂等键已用于不同请求",
        "stale_revision": "认证申请版本已变化，请刷新后重试",
        "transition_not_allowed": "当前状态不允许该操作",
        "manual_approval_required": "认证只能由合格的平台审核流程完成",
        "evidence_application_mismatch": "证据不属于当前认证申请",
        "unsupported_registry_mode": "当前版本仅支持企业二要素核验",
        "operation_in_progress": "认证任务正在处理中，请稍后刷新",
        "rate_limited": "操作过于频繁，请稍后重试",
        "face_session_unavailable": "当前没有可查询的实名核身会话",
        "face_session_expired": "实名核身会话已过期，请重新发起",
        "template_unavailable": "授权书模板暂时不可用",
        "document_invalid": "授权材料格式或安全检查未通过",
        "document_storage_unavailable": "授权材料存储暂时不可用",
        "document_not_found": "授权材料不存在",
        "document_unavailable": "授权材料尚不可查看",
        "business_license_required": "请先上传营业执照图片",
        "business_license_mismatch": "营业执照识别结果与企业或法人信息不一致",
        "business_license_not_recognized": "未识别到有效营业执照，请上传四边完整、文字清晰的营业执照原图",
        "business_license_authenticity_warning": "营业执照存在复印或翻拍风险，请上传原件扫描件或官方电子营业执照",
        "reviewer_management_forbidden": "只有平台根管理员可以管理认证审核资格",
        "reviewer_user_not_found": "审核员用户不存在",
        "reviewer_forbidden": "没有企业认证审核资格",
        "reviewer_expired": "企业认证审核资格已过期",
        "review_self_forbidden": "审核员与该企业或申请存在利益冲突",
        "review_conflict": "认证申请已变化或不再允许审核",
        "review_evidence_not_ready": "认证证据尚未满足提交或批准条件",
        "verification_not_verified": "企业当前没有可执行该操作的已通过认证",
        "verification_already_suspended": "企业认证已经撤销",
        "service_already_suspended": "企业服务已经停用",
        "verification_not_revoked": "企业认证当前处于有效状态，无需恢复",
        "service_not_suspended": "企业服务当前处于正常状态，无需恢复",
        "suspension_forbidden": "没有撤销认证或停用企业服务的资格",
        "evidence_refresh_pending": "企业证据已过期，已安排刷新",
        "transcription_not_allowed": "当前材料不允许人工转录修正",
        "transcription_invalid": "人工转录修正内容无效",
    }

    def __init__(self, code: str, *, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(self.MESSAGES.get(code, "企业认证操作失败"))


@dataclass(frozen=True)
class ManualVerificationStartInput:
    applicant_name: str
    applicant_id_type: str
    applicant_id_number: str
    consent_version: str
    idempotency_key: str
    applicant_role: str = ""


@dataclass(frozen=True)
class ManualVerificationIdentityInput:
    expected_revision: int
    idempotency_key: str
    applicant_name: str
    applicant_id_type: str
    applicant_id_number: str


@dataclass(frozen=True)
class ManualVerificationRoleInput:
    expected_revision: int
    idempotency_key: str
    applicant_role: str


def _configuration() -> tuple[str, str, int, str]:
    if not bool(getattr(settings, "COMPANY_VERIFICATION_ENABLED", False)):
        raise VerificationDomainError("feature_disabled", status_code=503)
    provider = str(
        getattr(
            settings,
            "COMPANY_VERIFICATION_PROVIDER",
            CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        )
    ).strip()
    if provider != CompanyVerification.PROVIDER_TENCENT_MANUAL_V1:
        raise VerificationDomainError("provider_unavailable", status_code=503)
    environment = str(
        getattr(settings, "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT", "production")
    ).strip().lower()
    if environment not in {
        CompanyVerification.ENVIRONMENT_SANDBOX,
        CompanyVerification.ENVIRONMENT_PRODUCTION,
        CompanyVerification.ENVIRONMENT_MOCK,
    }:
        raise VerificationDomainError("provider_unavailable", status_code=503)
    ttl = max(
        int(getattr(settings, "COMPANY_VERIFICATION_APPLICATION_TTL_SECONDS", 604800)),
        60,
    )
    mode = str(getattr(
        settings, "COMPANY_VERIFICATION_REGISTRY_MODE",
        CompanyVerification.MODE_TWO_ELEMENTS,
    )).strip().lower()
    if mode != CompanyVerification.MODE_TWO_ELEMENTS:
        raise VerificationDomainError("unsupported_registry_mode", status_code=503)
    return provider, environment, ttl, mode


def _request_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _valid_resident_identity_card(value: str) -> bool:
    if not re.fullmatch(r"\d{17}[\dX]", value):
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checksum = "10X98765432"
    return checksum[sum(int(value[i]) * weights[i] for i in range(17)) % 11] == value[-1]


def _validate_start_input(value: ManualVerificationStartInput) -> ManualVerificationStartInput:
    normalized = ManualVerificationStartInput(
        applicant_name=str(value.applicant_name or "").strip(),
        applicant_id_type=str(value.applicant_id_type or "").strip().lower(),
        applicant_id_number=str(value.applicant_id_number or "").strip().upper(),
        consent_version=str(value.consent_version or "").strip(),
        idempotency_key=str(value.idempotency_key or "").strip(),
        applicant_role=str(value.applicant_role or "").strip().lower(),
    )
    if (
        not normalized.consent_version
        or not normalized.idempotency_key
        or len(normalized.consent_version) > 64
        or len(normalized.idempotency_key) > 128
        or normalized.applicant_role not in {
            "",
            CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE,
            CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON,
        }
    ):
        raise VerificationDomainError("invalid_request")
    if normalized.applicant_role in {
        CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE,
        CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON,
    }:
        if (
            normalized.applicant_name
            or normalized.applicant_id_type
            or normalized.applicant_id_number
        ):
            raise VerificationDomainError("invalid_request")
        return normalized
    if (
        not normalized.applicant_name
        or not normalized.applicant_id_type
        or not normalized.applicant_id_number
        or len(normalized.applicant_name) > 128
        or len(normalized.applicant_id_type) > 32
        or normalized.applicant_id_type != "resident_identity_card"
        or not _valid_resident_identity_card(normalized.applicant_id_number)
    ):
        raise VerificationDomainError("invalid_request")
    return normalized


def _can_access(application: CompanyVerification, user: Optional[User]) -> bool:
    return bool(
        user
        and (
            application.applicant_user_id == user.id
            or (
                application.company.verification_status
                != Company.VERIFICATION_STATUS_VERIFIED
                and application.company.account_id == user.id
            )
        )
    )


def _mask_person_name(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return value[0] + "*" * max(len(value) - 1, 1)


def _identity_is_bound(application: CompanyVerification) -> bool:
    return bool(
        application.applicant_name_snapshot
        and application.applicant_id_type == "resident_identity_card"
        and application.applicant_id_encrypted
        and application.applicant_id_masked
        and application.applicant_id_hmac
        and application.applicant_id_key_version
    )


def _identity_precheck_is_current(application: CompanyVerification) -> bool:
    if not _identity_is_bound(application):
        return False
    operations = application.operations.filter(
        operation_kind=VerificationOperation.KIND_APPLICANT_IDENTITY,
        status=VerificationOperation.STATUS_COMPLETED,
    ).order_by("-completed_at", "-created_at")
    return any(
        isinstance(operation.result, dict)
        and operation.result.get("identity_precheck") == "passed"
        and operation.result.get("identity_hmac") == application.applicant_id_hmac
        for operation in operations
    )


def _verified_legal_representative_for_identity(
    application: CompanyVerification, *, now
) -> str:
    document = application.documents.filter(
        document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()
    if not document:
        return ""
    check = application.checks.filter(
        kind=VerificationCheck.KIND_BUSINESS_LICENSE,
        document=document,
        status=VerificationCheck.STATUS_PASSED,
        valid_until__gt=now,
    ).order_by("-checked_at", "-created_at").first()
    if not check or (check.result or {}).get("adopted") is not True:
        return ""
    return str((check.result or {}).get("legal_representative") or "").strip()


def _latest_check_status(application: CompanyVerification, kind: str) -> str:
    check = application.checks.filter(kind=kind).order_by("-created_at", "-id").first()
    return check.status if check else "not_started"


def _latest_document_check(application, document, kind):
    if document is None:
        return None
    return application.checks.filter(
        kind=kind,
        document=document,
        material_revision=document.material_revision,
    ).order_by("-checked_at", "-created_at", "-id").first()


def serialize_manual_verification(application: CompanyVerification) -> dict:
    if application.provider != CompanyVerification.PROVIDER_TENCENT_MANUAL_V1:
        raise VerificationDomainError("application_not_found", status_code=404)
    active = application.status in ACTIVE_STATUSES
    identity_bound = _identity_is_bound(application)
    identity_precheck = (
        "passed" if _identity_precheck_is_current(application) else "not_started"
    )
    identity_required = bool(
        active
        and application.applicant_role
        and application.stage in {
            CompanyVerification.STAGE_APPLICANT_FACE,
            CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
            CompanyVerification.STAGE_SUPPLEMENT_REQUIRED,
        }
        and (not identity_bound or identity_precheck != "passed")
    )
    continuation_stage = None
    allowed_actions = ["cancel"] if active else []
    if active:
        stage_action = (
            "provide_applicant_identity"
            if identity_required
            else NEXT_ACTION_BY_STAGE.get(application.stage)
        )
        if (
            application.stage == CompanyVerification.STAGE_COMPANY_CHECK
            and application.applicant_role
        ):
            allowed_actions.insert(0, "upload_business_license")
        if application.stage == CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD:
            allowed_actions.insert(0, "select_applicant_role")
        if stage_action and stage_action not in {"none", "wait_company_check", "wait_document_check", "wait_manual_review"}:
            allowed_actions.insert(0, stage_action)
        if not identity_required:
            continuation_stage = _next_stage_from_existing_evidence(
                application,
                now=timezone.now(),
            )
            if continuation_stage:
                allowed_actions.insert(0, "continue_to_next_step")
        if _previous_stage(application) is not None:
            allowed_actions.append("return_to_previous_step")
    face_session = application.face_sessions.order_by("-attempt_no").first()
    face_payload = None
    if face_session:
        face_payload = {
            "session_id": str(face_session.id),
            "attempt_no": face_session.attempt_no,
            "status": face_session.status,
            "provider_expires_at": face_session.provider_expires_at.isoformat() if face_session.provider_expires_at else None,
            "query_expires_at": face_session.query_expires_at.isoformat() if face_session.query_expires_at else None,
            "result_valid_until": face_session.result_valid_until.isoformat() if face_session.result_valid_until else None,
        }
    document = application.documents.filter(
        material_revision=application.material_revision,
        document_type=VerificationDocument.TYPE_AUTHORIZATION,
    ).order_by("-uploaded_at", "-id").first()
    business_license = application.documents.filter(
        document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
    ).order_by("-uploaded_at", "-id").first()
    business_license_payload = None
    business_license_check = "not_started"
    business_license_authenticity = "not_started"
    business_license_result = None
    legal_representative_payload = None
    if business_license:
        business_license_payload = {
            "document_id": str(business_license.id),
            "material_revision": business_license.material_revision,
            "template_version": business_license.template_version,
            "mime_type": business_license.mime_type,
            "size_bytes": business_license.size_bytes,
            "page_count": business_license.page_count,
            "scan_status": business_license.scan_status,
            "preview_available": bool(
                business_license.scan_status == VerificationDocument.SCAN_PASSED
                and business_license.preview_storage_key
            ),
            "uploaded_at": business_license.uploaded_at.isoformat()
            if business_license.uploaded_at else None,
        }
        license_check = _latest_document_check(
            application,
            business_license,
            VerificationCheck.KIND_BUSINESS_LICENSE,
        )
        business_license_check = license_check.status if license_check else "not_started"
        if license_check and isinstance(license_check.result, dict):
            result_payload = license_check.result
            business_license_authenticity = str(
                result_payload.get("authenticity") or "unknown"
            )
            legal_representative = str(
                result_payload.get("legal_representative") or ""
            ).strip()
            if legal_representative:
                legal_representative_payload = {
                    "name": _mask_person_name(legal_representative),
                    "source": "verified_business_license",
                }
            company_name = str(result_payload.get("company_name") or "").strip()
            registration_no = str(result_payload.get("registration_no") or "").strip()
            if not company_name and result_payload.get("company_name_match") is True:
                company_name = application.company_name_snapshot
            if not registration_no and result_payload.get("registration_no_match") is True:
                registration_no = application.registration_no_snapshot
            if result_payload.get("electronic") is True:
                material_type = "electronic"
            elif business_license_authenticity == "warning":
                material_type = "photo_or_copy"
            elif license_check.status == VerificationCheck.STATUS_PASSED:
                material_type = "image"
            else:
                material_type = "unknown"
            business_license_result = {
                "recognized": license_check.status == VerificationCheck.STATUS_PASSED,
                "company_name": company_name,
                "registration_no": registration_no,
                "legal_representative": _mask_person_name(legal_representative),
                "material_type": material_type,
                "copy_or_photo_warning": business_license_authenticity == "warning",
            }
    document_payload = None
    document_checks = {
        "security": "not_started",
        "text_ocr": "not_started",
        "seal_ocr": "not_started",
        "consistency": "not_started",
    }
    pre_review = {
        "status": "not_required",
        "reasons": [],
        "manual_transcription_allowed": False,
        "manual_transcription_is_final_approval": False,
    }
    if document:
        document_payload = {
            "document_id": str(document.id),
            "material_revision": document.material_revision,
            "template_version": document.template_version,
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
            "page_count": document.page_count,
            "scan_status": document.scan_status,
            "preview_available": bool(
                document.scan_status == VerificationDocument.SCAN_PASSED
                and document.preview_storage_key
            ),
            "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else None,
            "scanned_at": document.scanned_at.isoformat() if document.scanned_at else None,
        }
        check_kinds = {
            "security": VerificationCheck.KIND_DOCUMENT_SECURITY,
            "text_ocr": VerificationCheck.KIND_DOCUMENT_TEXT,
            "seal_ocr": VerificationCheck.KIND_SEAL,
            "consistency": VerificationCheck.KIND_CONSISTENCY,
        }
        latest_checks = {
            name: _latest_document_check(application, document, kind)
            for name, kind in check_kinds.items()
        }
        document_checks = {
            name: check.status if check else "not_started"
            for name, check in latest_checks.items()
        }
        consistency = latest_checks["consistency"]
        if consistency and isinstance(consistency.result, dict):
            pre_review = {
                "status": consistency.result.get("pre_review_status", "unknown"),
                "reasons": list(consistency.result.get("pre_review_reasons") or []),
                "manual_transcription_allowed": bool(
                    consistency.result.get("manual_transcription_allowed", False)
                ),
                "manual_transcription_is_final_approval": False,
            }
    next_action = NEXT_ACTION_BY_STAGE.get(application.stage, "none") if active else "none"
    if identity_required:
        next_action = "provide_applicant_identity"
    elif continuation_stage:
        next_action = "continue_to_next_step"
    if (
        active
        and application.stage == CompanyVerification.STAGE_DOCUMENT_CHECK
        and pre_review["status"] == "required"
    ):
        next_action = "wait_document_pre_review"
    latest_review = application.reviews.order_by("-created_at").first()
    review_payload = None
    if latest_review:
        review_payload = {
            "action": latest_review.action,
            "submitted_revision": latest_review.submitted_revision,
            "material_revision": latest_review.material_revision,
            "reason": latest_review.reason_public or None,
            "created_at": latest_review.created_at.isoformat(),
        }
    return {
        "application_id": str(application.public_id),
        "provider": application.provider,
        "provider_environment": application.provider_environment,
        "status": application.status,
        "stage": application.stage,
        "version": application.revision,
        "revision": application.revision,
        "material_revision": application.material_revision,
        "policy_version": application.policy_version,
        "mode": application.mode,
        "applicant_role": application.applicant_role or None,
        "identity_required": identity_required,
        "identity_bound": identity_bound,
        "identity_precheck": identity_precheck,
        "company_check": _latest_check_status(application, VerificationCheck.KIND_REGISTRY),
        "applicant_check": _latest_check_status(application, VerificationCheck.KIND_FACE),
        "face_session": face_payload,
        "business_license_document": business_license_payload,
        "business_license_check": business_license_check,
        "business_license_authenticity": business_license_authenticity,
        "business_license_result": business_license_result,
        "legal_representative": legal_representative_payload,
        "authorization_document": document_payload,
        "authorization_checks": document_checks,
        "pre_review": pre_review,
        "next_action": next_action,
        "allowed_actions": allowed_actions,
        "applicant": {
            "name": _mask_person_name(application.applicant_name_snapshot),
            "id_type": application.applicant_id_type,
            "id_number": application.applicant_id_masked,
        },
        "expires_at": application.expires_at.isoformat() if application.expires_at else None,
        "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None,
        "submitted_revision": application.submitted_revision,
        "submitted_material_revision": application.submitted_material_revision,
        "review": review_payload,
        "finished_at": application.finished_at.isoformat() if application.finished_at else None,
        "failure_code": application.failure_code or None,
        "reason": application.public_reason or None,
    }


def _identity_binding_context(
    application: CompanyVerification,
    *,
    user: User,
    applicant_name: str,
    now,
) -> tuple[str, str]:
    if application.applicant_user_id != user.id:
        raise VerificationDomainError("application_forbidden", status_code=403)
    if application.status not in ACTIVE_STATUSES:
        raise VerificationDomainError("transition_not_allowed", status_code=409)
    allowed_stage = (
        CompanyVerification.STAGE_APPLICANT_FACE
        if application.applicant_role
        == CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE
        else CompanyVerification.STAGE_AUTHORIZATION_UPLOAD
        if application.applicant_role
        == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
        else ""
    )
    if application.stage not in {
        allowed_stage,
        CompanyVerification.STAGE_SUPPLEMENT_REQUIRED
        if application.applicant_role
        == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
        else "",
    }:
        raise VerificationDomainError("transition_not_allowed", status_code=409)
    identity_source = "applicant"
    if (
        application.applicant_role
        == CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE
    ):
        verified_name = _verified_legal_representative_for_identity(
            application, now=now
        )
        if not verified_name:
            raise VerificationDomainError("business_license_required", status_code=409)
        if applicant_name and re.sub(r"\s+", "", applicant_name) != re.sub(
            r"\s+", "", verified_name
        ):
            raise VerificationDomainError(
                "applicant_identity_mismatch", status_code=409
            )
        applicant_name = verified_name
        identity_source = "verified_business_license"
    elif not applicant_name or len(applicant_name) > 128:
        raise VerificationDomainError("invalid_request")
    return applicant_name, identity_source


def _existing_identity_operation_result(
    operation: VerificationOperation,
    *,
    request_hash: str,
    application: CompanyVerification,
) -> dict:
    if operation.request_hash != request_hash:
        raise VerificationDomainError("idempotency_conflict", status_code=409)
    result = operation.result if isinstance(operation.result, dict) else {}
    if result.get("identity_precheck") == "failed":
        raise VerificationDomainError("identity_information_mismatch", status_code=422)
    if result.get("identity_precheck") != "passed":
        raise VerificationDomainError("identity_precheck_unavailable", status_code=503)
    return serialize_manual_verification(application)


def bind_manual_verification_identity(
    *, company_id: str, application_id: str, user: User,
    value: ManualVerificationIdentityInput, now=None, protector=None, verifier=None,
) -> dict:
    key = str(value.idempotency_key or "").strip()
    supplied_name = str(value.applicant_name or "").strip()
    identity_type = str(value.applicant_id_type or "").strip().lower()
    identity_number = str(value.applicant_id_number or "").strip().upper()
    if (
        not key
        or len(key) > 128
        or identity_type != "resident_identity_card"
        or not _valid_resident_identity_card(identity_number)
    ):
        raise VerificationDomainError("invalid_request")
    now = now or timezone.now()
    protector = protector or get_identity_protector()

    company = Company.objects.filter(cid=company_id).first()
    if not company:
        raise VerificationDomainError("company_not_found", status_code=404)
    application = CompanyVerification.objects.filter(
        company=company,
        public_id=application_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).first()
    if not application:
        raise VerificationDomainError("application_not_found", status_code=404)
    applicant_name, identity_source = _identity_binding_context(
        application, user=user, applicant_name=supplied_name, now=now
    )
    try:
        protected = protector.protect(
            identity_number, context=str(application.public_id)
        )
    except VerificationCryptoError as exc:
        raise VerificationDomainError("provider_unavailable", status_code=503) from exc
    request_hash = _request_hash({
        "application_id": str(application.public_id),
        "revision": int(value.expected_revision),
        "applicant_role": application.applicant_role,
        "applicant_name": applicant_name,
        "identity_hmac": protected.digest,
    })
    existing = VerificationOperation.objects.filter(
        company=company,
        operation_kind=VerificationOperation.KIND_APPLICANT_IDENTITY,
        idempotency_key=key,
    ).first()
    if existing:
        return _existing_identity_operation_result(
            existing, request_hash=request_hash, application=application
        )
    if application.revision != int(value.expected_revision):
        raise VerificationDomainError("stale_revision", status_code=409)
    if application.face_sessions.filter(status__in=(
        VerificationFaceSession.STATUS_CREATED,
        VerificationFaceSession.STATUS_IN_PROGRESS,
        VerificationFaceSession.STATUS_PASSED,
    )).exists():
        raise VerificationDomainError("transition_not_allowed", status_code=409)

    try:
        precheck = (verifier or get_applicant_identity_verifier()).precheck(
            ApplicantIdentityPrecheckRequest(
                name=applicant_name,
                identity_number=identity_number,
                identity_type=identity_type,
            )
        )
    except VerificationProviderError as exc:
        raise VerificationDomainError(
            "identity_precheck_unavailable", status_code=503
        ) from exc
    precheck_passed = precheck.outcome == "passed" and precheck.matched is True
    precheck_failed = precheck.outcome == "failed" and precheck.matched is False
    if not precheck_passed and not precheck_failed:
        raise VerificationDomainError("identity_precheck_unavailable", status_code=503)

    mismatch = False
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationDomainError("company_not_found", status_code=404)
        application = CompanyVerification.objects.select_for_update().filter(
            company=company,
            public_id=application_id,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        ).first()
        if not application:
            raise VerificationDomainError("application_not_found", status_code=404)
        locked_name, identity_source = _identity_binding_context(
            application, user=user, applicant_name=supplied_name, now=now
        )
        locked_request_hash = _request_hash({
            "application_id": str(application.public_id),
            "revision": int(value.expected_revision),
            "applicant_role": application.applicant_role,
            "applicant_name": locked_name,
            "identity_hmac": protected.digest,
        })
        if locked_request_hash != request_hash:
            raise VerificationDomainError("stale_revision", status_code=409)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_APPLICANT_IDENTITY,
            idempotency_key=key,
        ).first()
        if existing:
            return _existing_identity_operation_result(
                existing, request_hash=request_hash, application=application
            )
        if application.revision != int(value.expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        if application.face_sessions.filter(status__in=(
            VerificationFaceSession.STATUS_CREATED,
            VerificationFaceSession.STATUS_IN_PROGRESS,
            VerificationFaceSession.STATUS_PASSED,
        )).exists():
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        operation_result = {
            "identity_bound": precheck_passed,
            "identity_precheck": "passed" if precheck_passed else "failed",
            "identity_source": identity_source,
            "identity_hmac": protected.digest,
            "applicant_role": application.applicant_role,
            "provider_code": str(precheck.provider_code or "")[:128],
            "request_id": str(precheck.request_id or "")[:255],
        }
        if precheck_failed:
            mismatch = True
            VerificationOperation.objects.create(
                company=company,
                application=application,
                operation_kind=VerificationOperation.KIND_APPLICANT_IDENTITY,
                application_revision=application.revision,
                idempotency_key=key,
                request_hash=request_hash,
                status=VerificationOperation.STATUS_COMPLETED,
                last_error_code=str(precheck.provider_code or "identity_mismatch")[:64],
                result=operation_result,
                completed_at=now,
            )
            VerificationAuditEvent.objects.create(
                company=company,
                application=application,
                actor=user,
                action="applicant_identity_precheck_failed",
                revision=application.revision,
                detail={
                    "applicant_role": application.applicant_role,
                    "provider_code": operation_result["provider_code"],
                    "request_id": operation_result["request_id"],
                },
            )
        else:
            changed = not (
                application.applicant_name_snapshot == locked_name
                and application.applicant_id_type == identity_type
                and application.applicant_id_hmac
                and application.applicant_id_hmac == protected.digest
            )
            if changed:
                application.applicant_name_snapshot = locked_name
                application.applicant_id_type = identity_type
                application.applicant_id_encrypted = protected.ciphertext
                application.applicant_id_masked = protected.masked
                application.applicant_id_hmac = protected.digest
                application.applicant_id_key_version = protected.key_version
                application.revision += 1
                application.failure_code = ""
                application.public_reason = ""
                application.save(update_fields=(
                    "applicant_name_snapshot", "applicant_id_type",
                    "applicant_id_encrypted", "applicant_id_masked",
                    "applicant_id_hmac", "applicant_id_key_version", "revision",
                    "failure_code", "public_reason",
                ))
            VerificationOperation.objects.create(
                company=company,
                application=application,
                operation_kind=VerificationOperation.KIND_APPLICANT_IDENTITY,
                application_revision=application.revision,
                idempotency_key=key,
                request_hash=request_hash,
                status=VerificationOperation.STATUS_COMPLETED,
                result=operation_result,
                completed_at=now,
            )
            VerificationAuditEvent.objects.create(
                company=company,
                application=application,
                actor=user,
                action="applicant_identity_bound",
                revision=application.revision,
                detail={
                    "applicant_role": application.applicant_role,
                    "identity_source": identity_source,
                    "identity_type": identity_type,
                    "identity_masked": protected.masked,
                    "identity_precheck": "passed",
                    "provider_code": operation_result["provider_code"],
                    "request_id": operation_result["request_id"],
                },
            )
        payload = serialize_manual_verification(application)
    if mismatch:
        raise VerificationDomainError("identity_information_mismatch", status_code=422)
    return payload


def _company_evidence_is_current(application: CompanyVerification, *, now) -> bool:
    document = application.documents.filter(
        document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()
    if not document:
        return False
    license_check = application.checks.filter(
        kind=VerificationCheck.KIND_BUSINESS_LICENSE,
        document=document,
        status=VerificationCheck.STATUS_PASSED,
        valid_until__gt=now,
    ).order_by("-checked_at", "-created_at").first()
    registry_check = application.checks.filter(
        kind=VerificationCheck.KIND_REGISTRY,
        status=VerificationCheck.STATUS_PASSED,
        valid_until__gt=now,
    ).order_by("-checked_at", "-created_at").first()
    return bool(
        license_check
        and isinstance(license_check.result, dict)
        and license_check.result.get("adopted") is True
        and registry_check
    )


def _authorization_evidence_is_current(
    application: CompanyVerification, *, now
) -> bool:
    document = application.documents.filter(
        document_type=VerificationDocument.TYPE_AUTHORIZATION,
        material_revision=application.material_revision,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()
    if not document:
        return False
    check = application.checks.filter(
        kind=VerificationCheck.KIND_CONSISTENCY,
        document=document,
        material_revision=application.material_revision,
        status=VerificationCheck.STATUS_PASSED,
        valid_until__gt=now,
    ).order_by("-checked_at", "-created_at").first()
    return bool(
        check
        and isinstance(check.result, dict)
        and check.result.get("adopted") is True
    )


def _face_evidence_is_current(application: CompanyVerification, *, now) -> bool:
    return application.face_sessions.filter(
        status=VerificationFaceSession.STATUS_PASSED,
        identity_match=True,
        result_valid_until__gt=now,
    ).exists()


def _resume_stage_for_role(application: CompanyVerification, *, now) -> str:
    if not _company_evidence_is_current(application, now=now):
        has_license = application.documents.filter(
            document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
            scan_status=VerificationDocument.SCAN_PASSED,
        ).exists()
        return (
            CompanyVerification.STAGE_COMPANY_CHECK
            if has_license
            else CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD
        )
    if application.applicant_role == CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE:
        return (
            CompanyVerification.STAGE_READY_TO_SUBMIT
            if _identity_is_bound(application)
            and _face_evidence_is_current(application, now=now)
            else CompanyVerification.STAGE_APPLICANT_FACE
        )
    authorization_ready = (
        _identity_is_bound(application)
        and _authorization_evidence_is_current(application, now=now)
    )
    if authorization_ready and _face_evidence_is_current(application, now=now):
        return CompanyVerification.STAGE_READY_TO_SUBMIT
    return (
        CompanyVerification.STAGE_APPLICANT_FACE
        if authorization_ready
        else CompanyVerification.STAGE_AUTHORIZATION_UPLOAD
    )


def _next_stage_from_existing_evidence(
    application: CompanyVerification,
    *,
    now,
) -> str | None:
    """Return a forward stage only when retained evidence already supports it."""

    if not application.applicant_role:
        return None
    target_stage = _resume_stage_for_role(application, now=now)
    current_stage = application.stage
    if current_stage == CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD:
        return target_stage if target_stage != current_stage else None
    if current_stage == CompanyVerification.STAGE_COMPANY_CHECK:
        if _company_evidence_is_current(application, now=now):
            return target_stage if target_stage != current_stage else None
        return None
    if current_stage == CompanyVerification.STAGE_AUTHORIZATION_UPLOAD:
        if (
            application.applicant_role
            == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
            and _authorization_evidence_is_current(application, now=now)
            and target_stage in {
                CompanyVerification.STAGE_APPLICANT_FACE,
                CompanyVerification.STAGE_READY_TO_SUBMIT,
            }
        ):
            return target_stage
        return None
    if current_stage == CompanyVerification.STAGE_APPLICANT_FACE:
        if (
            _face_evidence_is_current(application, now=now)
            and target_stage == CompanyVerification.STAGE_READY_TO_SUBMIT
        ):
            return target_stage
    return None


def select_manual_verification_applicant_role(
    *,
    company_id: str,
    application_id: str,
    user: User,
    value: ManualVerificationRoleInput,
    now=None,
) -> dict:
    key = str(value.idempotency_key or "").strip()
    role = str(value.applicant_role or "").strip().lower()
    if (
        not key
        or len(key) > 128
        or role not in {
            CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE,
            CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON,
        }
    ):
        raise VerificationDomainError("invalid_request")
    now = now or timezone.now()
    request_hash = _request_hash({
        "company_id": company_id,
        "application_id": str(application_id),
        "expected_revision": int(value.expected_revision),
        "applicant_role": role,
        "actor": user.id,
        "action": "select_applicant_role",
    })
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationDomainError("company_not_found", status_code=404)
        application = CompanyVerification.objects.select_for_update().filter(
            company=company,
            public_id=application_id,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        ).first()
        if not application:
            raise VerificationDomainError("application_not_found", status_code=404)
        if application.applicant_user_id != user.id:
            raise VerificationDomainError("application_forbidden", status_code=403)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_TRANSITION,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            return serialize_manual_verification(application)
        if application.revision != int(value.expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        if application.status not in ACTIVE_STATUSES or application.stage not in {
            CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD,
            CompanyVerification.STAGE_COMPANY_CHECK,
        }:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        old_role = application.applicant_role
        role_changed = old_role != role
        application.applicant_role = role
        application.mode = CompanyVerification.MODE_THREE_ELEMENTS
        invalidated_route_evidence = False
        if role_changed:
            invalidated_route_evidence = bool(
                _identity_is_bound(application)
                or application.documents.filter(
                    document_type=VerificationDocument.TYPE_AUTHORIZATION
                ).exists()
                or application.face_sessions.exists()
            )
            application.applicant_name_snapshot = ""
            application.applicant_id_type = ""
            application.applicant_id_encrypted = ""
            application.applicant_id_masked = ""
            application.applicant_id_hmac = ""
            application.applicant_id_key_version = ""
            application.material_revision += 1
            application.face_sessions.filter(status__in=(
                VerificationFaceSession.STATUS_CREATED,
                VerificationFaceSession.STATUS_IN_PROGRESS,
                VerificationFaceSession.STATUS_PASSED,
            )).update(
                status=VerificationFaceSession.STATUS_CANCELLED,
                outcome_code="applicant_role_changed",
                identity_match=False,
            )
        application.stage = _resume_stage_for_role(application, now=now)
        application.revision += 1
        application.failure_code = ""
        application.public_reason = ""
        application.save(update_fields=(
            "applicant_role",
            "mode",
            "stage",
            "revision",
            "material_revision",
            "applicant_name_snapshot",
            "applicant_id_type",
            "applicant_id_encrypted",
            "applicant_id_masked",
            "applicant_id_hmac",
            "applicant_id_key_version",
            "failure_code",
            "public_reason",
        ))
        result = {
            "application_id": str(application.public_id),
            "revision": application.revision,
            "stage": application.stage,
            "applicant_role": role,
            "role_changed": role_changed,
        }
        VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_TRANSITION,
            application_revision=application.revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result=result,
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="applicant_role_selected",
            revision=application.revision,
            detail={
                "from_role": old_role,
                "to_role": role,
                "to_stage": application.stage,
                "route_evidence_invalidated": invalidated_route_evidence,
                "company_evidence_preserved": True,
            },
        )
    return serialize_manual_verification(application)


def create_manual_verification(
    *,
    company_id: str,
    user: User,
    value: ManualVerificationStartInput,
    now=None,
    protector=None,
) -> dict:
    provider, environment, ttl, mode = _configuration()
    value = _validate_start_input(value)
    now = now or timezone.now()
    public_id = uuid.uuid4()
    protected = None
    if value.applicant_id_number:
        try:
            protector = protector or get_identity_protector()
            protected = protector.protect(
                value.applicant_id_number, context=str(public_id)
            )
        except VerificationCryptoError as exc:
            raise VerificationDomainError("provider_unavailable", status_code=503) from exc

    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationDomainError("company_not_found", status_code=404)
        if company.verification_status == Company.VERIFICATION_STATUS_VERIFIED:
            raise VerificationDomainError("already_verified", status_code=409)
        if company.account_id != user.id:
            raise VerificationDomainError("not_draft_owner", status_code=403)
        try:
            registration_no = validate_social_credit_code(
                company.registration_no,
                allow_blank=False,
            )
        except ValidationError as exc:
            raise VerificationDomainError("company_identity_incomplete") from exc
        company_name = str(company.company_name or "").strip()
        if not company_name:
            raise VerificationDomainError("company_identity_incomplete")

        request_hash = _request_hash(
            {
                "company_id": company.cid,
                "applicant_user_id": user.id,
                "applicant_name": value.applicant_name,
                "applicant_id_type": value.applicant_id_type,
                "applicant_id_hmac": protected.digest if protected else "",
                "consent_version": value.consent_version,
                "applicant_role": value.applicant_role,
            }
        )
        existing_operation = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_START,
            idempotency_key=value.idempotency_key,
        ).first()
        if existing_operation:
            if existing_operation.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if not existing_operation.application_id:
                raise VerificationDomainError("application_active", status_code=409)
            return serialize_manual_verification(existing_operation.application)

        if CompanyVerification.objects.filter(
            company=company,
            status__in=ACTIVE_STATUSES,
        ).exists():
            raise VerificationDomainError("application_active", status_code=409)

        application = CompanyVerification.objects.create(
            public_id=public_id,
            company=company,
            applicant_user=user,
            provider=provider,
            provider_environment=environment,
            mode=(
                CompanyVerification.MODE_THREE_ELEMENTS
                if value.applicant_role
                else mode
            ),
            applicant_role=value.applicant_role,
            stage=(
                CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD
                if value.applicant_role
                else CompanyVerification.STAGE_COMPANY_CHECK
            ),
            policy_version=str(
                getattr(settings, "COMPANY_VERIFICATION_POLICY_VERSION", "manual_v1")
            ).strip(),
            revision=1,
            material_revision=0,
            expires_at=now + timedelta(seconds=ttl),
            company_name_snapshot=company_name,
            registration_no_snapshot=registration_no,
            establishment_date_snapshot=str(company.establishment_date or ""),
            address_snapshot=str(company.address or ""),
            applicant_name_snapshot=value.applicant_name,
            applicant_id_type=value.applicant_id_type,
            applicant_id_encrypted=protected.ciphertext if protected else "",
            applicant_id_masked=protected.masked if protected else "",
            applicant_id_hmac=protected.digest if protected else "",
            applicant_id_key_version=protected.key_version if protected else "",
            consent_version=value.consent_version,
            consented_at=now,
            status=CompanyVerification.STATUS_PENDING,
        )
        result = {"application_id": str(application.public_id), "revision": application.revision}
        VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_START,
            idempotency_key=value.idempotency_key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result=result,
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="application_started",
            revision=application.revision,
            detail={"provider": provider, "stage": application.stage},
        )
    return serialize_manual_verification(application)


def get_manual_verification(
    *,
    company_id: str,
    user: User,
    application_id: str | None = None,
) -> dict:
    company = Company.objects.filter(cid=company_id).first()
    if not company:
        raise VerificationDomainError("company_not_found", status_code=404)
    queryset = CompanyVerification.objects.select_related("company").filter(
        company=company,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    )
    if application_id:
        queryset = queryset.filter(public_id=application_id)
    application = queryset.order_by("-id").first()
    if not application:
        raise VerificationDomainError("application_not_found", status_code=404)
    if not _can_access(application, user):
        raise VerificationDomainError("application_forbidden", status_code=403)
    return serialize_manual_verification(application)


def cancel_manual_verification(
    *,
    company_id: str,
    application_id: str,
    user: User,
    expected_revision: int,
    idempotency_key: str,
    now=None,
) -> dict:
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash(
        {
            "company_id": company_id,
            "application_id": str(application_id),
            "expected_revision": expected_revision,
            "actor": user.id,
        }
    )
    now = now or timezone.now()
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationDomainError("company_not_found", status_code=404)
        application = CompanyVerification.objects.select_for_update().filter(
            company=company,
            public_id=application_id,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        ).first()
        if not application:
            raise VerificationDomainError("application_not_found", status_code=404)
        if application.applicant_user_id != user.id:
            raise VerificationDomainError("application_forbidden", status_code=403)

        existing_operation = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_CANCEL,
            idempotency_key=key,
        ).first()
        if existing_operation:
            if existing_operation.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            return serialize_manual_verification(existing_operation.application)
        if application.revision != int(expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        if application.status not in ACTIVE_STATUSES:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        application.status = CompanyVerification.STATUS_CANCELLED
        application.revision += 1
        application.finished_at = now
        application.failure_code = "cancelled_by_applicant"
        application.public_reason = "申请已取消"
        application.save(
            update_fields=(
                "status",
                "revision",
                "finished_at",
                "failure_code",
                "public_reason",
            )
        )
        application.face_sessions.filter(
            status__in=(VerificationFaceSession.STATUS_CREATED, VerificationFaceSession.STATUS_IN_PROGRESS)
        ).update(status=VerificationFaceSession.STATUS_CANCELLED, outcome_code="application_cancelled")
        VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_CANCEL,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result={"application_id": str(application.public_id), "revision": application.revision},
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="application_cancelled",
            revision=application.revision,
            detail={},
        )
    return serialize_manual_verification(application)


def continue_manual_verification_to_next_step(
    *,
    company_id: str,
    application_id: str,
    user: User,
    expected_revision: int,
    idempotency_key: str,
    now=None,
) -> dict:
    """Resume a backtracked application using still-valid retained evidence."""

    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash(
        {
            "company_id": company_id,
            "application_id": str(application_id),
            "expected_revision": expected_revision,
            "actor": user.id,
            "action": "continue_to_next_step",
        }
    )
    now = now or timezone.now()
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationDomainError("company_not_found", status_code=404)
        application = CompanyVerification.objects.select_for_update().filter(
            company=company,
            public_id=application_id,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        ).first()
        if not application:
            raise VerificationDomainError("application_not_found", status_code=404)
        if application.applicant_user_id != user.id:
            raise VerificationDomainError("application_forbidden", status_code=403)

        existing_operation = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_TRANSITION,
            idempotency_key=key,
        ).first()
        if existing_operation:
            if existing_operation.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            return serialize_manual_verification(existing_operation.application)
        if application.revision != int(expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        if application.status not in ACTIVE_STATUSES:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        next_stage = _next_stage_from_existing_evidence(application, now=now)
        if not next_stage:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        from_stage = application.stage
        application.stage = next_stage
        application.revision += 1
        application.failure_code = ""
        application.public_reason = ""
        application.save(update_fields=(
            "stage",
            "revision",
            "failure_code",
            "public_reason",
        ))
        result = {
            "application_id": str(application.public_id),
            "revision": application.revision,
            "status": application.status,
            "stage": application.stage,
        }
        VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_TRANSITION,
            application_revision=application.revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result=result,
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="application_continued_to_next_step",
            revision=application.revision,
            detail={"from_stage": from_stage, "to_stage": next_stage},
        )
    return serialize_manual_verification(application)


def return_manual_verification_to_previous_step(
    *,
    company_id: str,
    application_id: str,
    user: User,
    expected_revision: int,
    idempotency_key: str,
    now=None,
) -> dict:
    """Move an editable application to the preceding public workflow step.

    This does not erase evidence or review history. Applications already
    submitted for manual review, or in a terminal state, are intentionally
    immutable from the applicant side.
    """

    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash(
        {
            "company_id": company_id,
            "application_id": str(application_id),
            "expected_revision": expected_revision,
            "actor": user.id,
            "action": "return_to_previous_step",
        }
    )
    now = now or timezone.now()
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationDomainError("company_not_found", status_code=404)
        application = CompanyVerification.objects.select_for_update().filter(
            company=company,
            public_id=application_id,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        ).first()
        if not application:
            raise VerificationDomainError("application_not_found", status_code=404)
        if application.applicant_user_id != user.id:
            raise VerificationDomainError("application_forbidden", status_code=403)

        existing_operation = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_TRANSITION,
            idempotency_key=key,
        ).first()
        if existing_operation:
            if existing_operation.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            return serialize_manual_verification(existing_operation.application)
        if application.revision != int(expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        if application.status not in ACTIVE_STATUSES:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        previous_stage = _previous_stage(application)
        if previous_stage is None:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        from_stage = application.stage
        application.stage = previous_stage
        application.revision += 1
        application.save(update_fields=("stage", "revision"))
        result = {
            "application_id": str(application.public_id),
            "revision": application.revision,
            "status": application.status,
            "stage": application.stage,
        }
        VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_TRANSITION,
            application_revision=application.revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result=result,
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="application_returned_to_previous_step",
            revision=application.revision,
            detail={"from_stage": from_stage, "to_stage": previous_stage},
        )
    return serialize_manual_verification(application)


def transition_manual_verification(
    *,
    application_id: str,
    expected_revision: int,
    idempotency_key: str,
    new_status: str | None = None,
    new_stage: str | None = None,
    actor: Optional[User] = None,
    reason_code: str = "",
    now=None,
) -> CompanyVerification:
    """Apply idempotent non-approval changes; EV-5 owns the verified transition."""

    if new_status == CompanyVerification.STATUS_VERIFIED:
        raise VerificationDomainError("manual_approval_required", status_code=403)
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128 or (not new_status and not new_stage):
        raise VerificationDomainError("invalid_request")
    operation_kind = (
        VerificationOperation.KIND_EXPIRE
        if new_status == CompanyVerification.STATUS_EXPIRED
        else VerificationOperation.KIND_TRANSITION
    )
    request_hash = _request_hash(
        {
            "application_id": str(application_id),
            "expected_revision": expected_revision,
            "new_status": new_status or "",
            "new_stage": new_stage or "",
            "reason_code": str(reason_code or "")[:64],
            "actor": actor.id if actor else None,
        }
    )
    now = now or timezone.now()
    application_ref = CompanyVerification.objects.only("company_id").filter(
        public_id=application_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).first()
    if not application_ref:
        raise VerificationDomainError("application_not_found", status_code=404)
    with transaction.atomic():
        company = Company.objects.select_for_update().get(
            pk=application_ref.company_id
        )
        application = CompanyVerification.objects.select_for_update().get(
            pk=application_ref.pk
        )
        existing_operation = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=operation_kind,
            idempotency_key=key,
        ).first()
        if existing_operation:
            if existing_operation.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            return existing_operation.application
        if application.revision != int(expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        if application.status in TERMINAL_STATUSES:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        changed = False
        if new_status and new_status != application.status:
            if new_status not in ALLOWED_STATUS_TRANSITIONS.get(application.status, set()):
                raise VerificationDomainError("transition_not_allowed", status_code=409)
            application.status = new_status
            changed = True
        if new_stage and new_stage != application.stage:
            if new_stage not in ALLOWED_STAGE_TRANSITIONS.get(application.stage, set()):
                raise VerificationDomainError("transition_not_allowed", status_code=409)
            application.stage = new_stage
            changed = True
        if not changed:
            raise VerificationDomainError("transition_not_allowed", status_code=409)

        application.revision += 1
        update_fields = ["status", "stage", "revision"]
        if application.status in TERMINAL_STATUSES:
            application.finished_at = now
            application.failure_code = str(reason_code or "")[:64]
            update_fields.extend(["finished_at", "failure_code"])
        application.save(update_fields=update_fields)
        if application.status in TERMINAL_STATUSES:
            application.face_sessions.filter(
                status__in=(VerificationFaceSession.STATUS_CREATED, VerificationFaceSession.STATUS_IN_PROGRESS)
            ).update(status=(
                VerificationFaceSession.STATUS_EXPIRED
                if application.status == CompanyVerification.STATUS_EXPIRED
                else VerificationFaceSession.STATUS_CANCELLED
            ), outcome_code=f"application_{application.status}")
        operation_payload = {
            "application_id": str(application.public_id),
            "revision": application.revision,
            "status": application.status,
            "stage": application.stage,
        }
        VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=operation_kind,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result=operation_payload,
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=actor,
            action="application_transitioned",
            revision=application.revision,
            detail={"status": application.status, "stage": application.stage},
        )
        return application


def expire_due_manual_verifications(*, limit: int = 100, now=None) -> int:
    now = now or timezone.now()
    candidates = list(
        CompanyVerification.objects.filter(
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
            status__in=ACTIVE_STATUSES,
            expires_at__lte=now,
        )
        .order_by("expires_at", "id")
        .values_list("public_id", "revision")[:limit]
    )
    expired = 0
    for public_id, revision in candidates:
        try:
            transition_manual_verification(
                application_id=str(public_id),
                expected_revision=revision,
                idempotency_key=f"expire:{public_id}:{revision}",
                new_status=CompanyVerification.STATUS_EXPIRED,
                reason_code="application_expired",
                now=now,
            )
            expired += 1
        except VerificationDomainError as exc:
            if exc.code not in {"stale_revision", "transition_not_allowed"}:
                raise
    return expired


def assert_evidence_belongs_to_application(
    application: CompanyVerification,
    *,
    document: Optional[VerificationDocument] = None,
    check: Optional[VerificationCheck] = None,
):
    if document and document.application_id != application.id:
        raise VerificationDomainError("evidence_application_mismatch", status_code=409)
    if check:
        if check.application_id != application.id:
            raise VerificationDomainError("evidence_application_mismatch", status_code=409)
        if check.document_id and check.document.application_id != application.id:
            raise VerificationDomainError("evidence_application_mismatch", status_code=409)
        if check.face_session_id and check.face_session.application_id != application.id:
            raise VerificationDomainError("evidence_application_mismatch", status_code=409)
