"""EV-5 reviewer qualification, transcription and atomic approval services."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.company.models import (
    Company,
    CompanyMember,
    CompanyVerification,
    CompanyVerificationReviewer,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationNotification,
    VerificationOperation,
    VerificationReview,
    VerificationReviewerEvent,
    VerificationTranscription,
)
from apps.company.providers import get_registry_verifier
from apps.company.providers.base import VerificationProviderError
from apps.company.providers.manual import RegistryVerificationRequest
from apps.company.verification_documents import (
    _normalize_text,
    authorization_expected_values,
)
from apps.company.verification_domain import (
    ACTIVE_STATUSES,
    VerificationDomainError,
    _request_hash,
    serialize_manual_verification,
)
from apps.company.verification_policy import (
    REGISTRATION_COMMON_CHECKS,
    REQUIRED_APPROVAL_CHECKS,
    evaluate_manual_approval,
    evaluate_manual_submission,
)
from apps.company.verification_runtime import retry_budget
from apps.company.verification_storage import VerificationStorageError, get_verification_storage
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import grant_permissions
from apps.user.models import User
from apps.user.permissions import get_platform_administrator, is_platform_administrator


logger = logging.getLogger("touming")

CAPABILITY_PRE_REVIEW = "pre_review"
CAPABILITY_FINAL_REVIEW = "final_review"
CAPABILITY_SUSPEND_VERIFICATION = "suspend_verification"
REVIEW_QUEUE_CAPABILITIES = {CAPABILITY_PRE_REVIEW, CAPABILITY_FINAL_REVIEW}
REVIEW_CAPABILITIES = REVIEW_QUEUE_CAPABILITIES | {CAPABILITY_SUSPEND_VERIFICATION}
REVIEW_ACTIONS = {
    VerificationReview.ACTION_APPROVE,
    VerificationReview.ACTION_REJECT,
    VerificationReview.ACTION_REQUEST_SUPPLEMENT,
}
REVIEW_CHECKLIST_KEYS = {
    "company_identity",
    "applicant_identity",
    "authorization_document",
    "evidence_versions",
    "conflict_of_interest",
}
TRANSCRIPTION_FIELDS = {
    "company_name",
    "registration_no",
    "applicant_name",
    "application_id",
    "template_version",
    "authorization_scope",
    "issued_on",
    "valid_until",
    "seal_company_name",
}
AUDIT_CATEGORY_ACTIONS = {
    "submission": {
        "application_started",
        "application_submitted_for_review",
    },
    "review": {
        "authorization_transcription_completed",
        "manual_review_approve",
        "manual_review_reject",
        "manual_review_request_supplement",
    },
    "administration": {
        "verification_suspended",
        "verification_restored",
        "company_service_suspended",
        "company_service_restored",
    },
    "material": {
        "business_license_uploaded",
        "business_license_check_passed",
        "authorization_document_uploaded",
        "authorization_document_checked",
        "review_business_license_previewed",
        "review_authorization_document_previewed",
    },
}
AUDIT_ACTION_LABELS = {
    "application_started": "发起认证申请",
    "application_submitted_for_review": "提交认证审核",
    "authorization_transcription_completed": "完成材料人工修正",
    "manual_review_approve": "审核通过",
    "manual_review_reject": "审核驳回",
    "manual_review_request_supplement": "要求补充材料",
    "verification_suspended": "撤销企业认证",
    "verification_restored": "恢复企业认证",
    "company_service_suspended": "停用企业服务",
    "company_service_restored": "恢复企业服务",
    "business_license_uploaded": "上传营业执照",
    "business_license_check_passed": "营业执照核验通过",
    "authorization_document_uploaded": "上传授权书",
    "authorization_document_checked": "完成授权书核验",
    "review_business_license_previewed": "查看营业执照",
    "review_authorization_document_previewed": "查看授权书",
}
AUDIT_CATEGORY_LABELS = {
    "all": "全部记录",
    "submission": "申请提交",
    "review": "审核决定",
    "administration": "认证与服务管理",
    "material": "材料操作",
}




def _require_feature_enabled():
    if not bool(getattr(settings, "COMPANY_VERIFICATION_ENABLED", False)):
        raise VerificationDomainError("feature_disabled", status_code=503)


def _allow_mock_for_internal_review() -> bool:
    return bool(getattr(settings, "COMPANY_VERIFICATION_ALLOW_MOCK", False)) and bool(
        getattr(settings, "COMPANY_VERIFICATION_REVIEW_ALLOW_MOCK", False)
    )


def _require_platform_reviewer_manager(user: User | None):
    if not is_platform_administrator(user):
        raise VerificationDomainError("reviewer_management_forbidden", status_code=403)


def _capabilities(reviewer: CompanyVerificationReviewer) -> list[str]:
    result = []
    if reviewer.can_pre_review:
        result.append(CAPABILITY_PRE_REVIEW)
    if reviewer.can_final_review:
        result.append(CAPABILITY_FINAL_REVIEW)
    if reviewer.can_suspend_verification:
        result.append(CAPABILITY_SUSPEND_VERIFICATION)
    return result


def _serialize_reviewer(reviewer: CompanyVerificationReviewer) -> dict:
    return {
        "reviewer_id": reviewer.id,
        "user_uid": reviewer.user.uid,
        "username": reviewer.user.username,
        "enabled": reviewer.enabled,
        "capabilities": _capabilities(reviewer),
        "expires_at": reviewer.expires_at.isoformat() if reviewer.expires_at else None,
        "revoked_at": reviewer.revoked_at.isoformat() if reviewer.revoked_at else None,
        "reason": reviewer.reason,
    }


def get_reviewer_context(*, user: User, now=None) -> dict:
    """Return the caller's effective reviewer capabilities without exposing grants."""
    now = now or timezone.now()
    if is_platform_administrator(user):
        return {
            "enabled": True,
            "capabilities": [
                CAPABILITY_PRE_REVIEW,
                CAPABILITY_FINAL_REVIEW,
                CAPABILITY_SUSPEND_VERIFICATION,
            ],
            "expires_at": None,
            "is_platform_admin": True,
        }
    reviewer = CompanyVerificationReviewer.objects.filter(user=user).first()
    active = bool(
        reviewer
        and reviewer.enabled
        and reviewer.revoked_at is None
        and (reviewer.expires_at is None or reviewer.expires_at > now)
    )
    return {
        "enabled": active,
        "capabilities": _capabilities(reviewer) if active else [],
        "expires_at": (
            reviewer.expires_at.isoformat()
            if active and reviewer.expires_at is not None
            else None
        ),
    }


def _normalize_capabilities(values) -> set[str]:
    capabilities = {str(value or "").strip() for value in (values or [])}
    if not capabilities or not capabilities.issubset(REVIEW_CAPABILITIES):
        raise VerificationDomainError("invalid_request")
    return capabilities


def grant_or_update_reviewer(
    *, actor: User, target_user_uid: str, capabilities, reason: str, expires_at=None, now=None
) -> dict:
    _require_platform_reviewer_manager(actor)
    now = now or timezone.now()
    target_uid = str(target_user_uid or "").strip()
    reason = str(reason or "").strip()
    capability_set = _normalize_capabilities(capabilities)
    if not target_uid or not reason or len(reason) > 255:
        raise VerificationDomainError("invalid_request")
    if expires_at is not None and expires_at <= now:
        raise VerificationDomainError("invalid_request")
    with transaction.atomic():
        target = User.objects.select_for_update().filter(uid=target_uid).first()
        if target is None:
            raise VerificationDomainError("reviewer_user_not_found", status_code=404)
        reviewer = CompanyVerificationReviewer.objects.select_for_update().filter(
            user=target
        ).first()
        was_active = bool(reviewer and reviewer.enabled and reviewer.revoked_at is None)
        if reviewer is None:
            reviewer = CompanyVerificationReviewer(user=target, granted_by=actor)
        reviewer.enabled = True
        reviewer.can_pre_review = CAPABILITY_PRE_REVIEW in capability_set
        reviewer.can_final_review = CAPABILITY_FINAL_REVIEW in capability_set
        reviewer.can_suspend_verification = CAPABILITY_SUSPEND_VERIFICATION in capability_set
        reviewer.reason = reason
        reviewer.granted_by = actor
        reviewer.granted_at = now
        reviewer.expires_at = expires_at
        reviewer.revoked_at = None
        reviewer.revoked_by = None
        reviewer.save()
        VerificationReviewerEvent.objects.create(
            reviewer=reviewer,
            actor=actor,
            action=(
                VerificationReviewerEvent.ACTION_UPDATED
                if was_active
                else VerificationReviewerEvent.ACTION_GRANTED
            ),
            capabilities=sorted(capability_set),
            reason=reason,
            expires_at=expires_at,
        )
    return _serialize_reviewer(reviewer)


def revoke_reviewer(*, actor: User, reviewer_id: int, reason: str, now=None) -> dict:
    _require_platform_reviewer_manager(actor)
    now = now or timezone.now()
    reason = str(reason or "").strip()
    if not reason or len(reason) > 255:
        raise VerificationDomainError("invalid_request")
    with transaction.atomic():
        reviewer = CompanyVerificationReviewer.objects.select_for_update().select_related(
            "user"
        ).filter(pk=reviewer_id).first()
        if reviewer is None:
            raise VerificationDomainError("reviewer_user_not_found", status_code=404)
        if not reviewer.enabled and reviewer.revoked_at is not None:
            return _serialize_reviewer(reviewer)
        reviewer.enabled = False
        reviewer.revoked_at = now
        reviewer.revoked_by = actor
        reviewer.reason = reason
        reviewer.save(update_fields=("enabled", "revoked_at", "revoked_by", "reason"))
        VerificationReviewerEvent.objects.create(
            reviewer=reviewer,
            actor=actor,
            action=VerificationReviewerEvent.ACTION_REVOKED,
            capabilities=_capabilities(reviewer),
            reason=reason,
            expires_at=reviewer.expires_at,
        )
    return _serialize_reviewer(reviewer)


def list_reviewers(*, actor: User, limit: int = 100, offset: int = 0) -> dict:
    _require_platform_reviewer_manager(actor)
    queryset = CompanyVerificationReviewer.objects.select_related("user").order_by("id")
    total = queryset.count()
    return {
        "total": total,
        "items": [_serialize_reviewer(item) for item in queryset[offset: offset + limit]],
    }


def _platform_administrator_reviewer(user, *, lock=False):
    """Use the platform role; persist only the identity needed by review audit FKs."""
    administrator = get_platform_administrator(user, lock=lock)
    if administrator is None:
        return None
    queryset = CompanyVerificationReviewer.objects.select_related("user")
    if lock:
        queryset = queryset.select_for_update()
    reviewer = queryset.filter(user=administrator).first()
    if reviewer is None:
        values = {
            "enabled": False,
            "reason": "平台管理员审核身份，权限由用户角色决定",
            "granted_by": administrator,
        }
        if lock:
            reviewer, _ = CompanyVerificationReviewer.objects.get_or_create(
                user=administrator, defaults=values,
            )
        else:
            # Reading capabilities or queues must not create a separate qualification.
            reviewer = CompanyVerificationReviewer(user=administrator, **values)
    # Do not save these effective flags: demotion must not leave an implicit grant.
    reviewer.enabled = True
    reviewer.can_pre_review = True
    reviewer.can_final_review = True
    reviewer.can_suspend_verification = True
    reviewer.expires_at = None
    reviewer.revoked_at = None
    return reviewer


def _require_reviewer(
    user: User | None, capability: str, *, now=None, lock: bool = False
) -> CompanyVerificationReviewer:
    now = now or timezone.now()
    if capability not in REVIEW_CAPABILITIES or not user:
        raise VerificationDomainError("reviewer_forbidden", status_code=403)
    administrator = _platform_administrator_reviewer(user, lock=lock)
    if administrator is not None:
        return administrator
    queryset = CompanyVerificationReviewer.objects.select_related("user")
    if lock:
        queryset = queryset.select_for_update()
    reviewer = queryset.filter(user=user).first()
    if reviewer is None or not reviewer.enabled or reviewer.revoked_at is not None:
        raise VerificationDomainError("reviewer_forbidden", status_code=403)
    if reviewer.expires_at is not None and reviewer.expires_at <= now:
        raise VerificationDomainError("reviewer_expired", status_code=403)
    allowed = {
        CAPABILITY_PRE_REVIEW: reviewer.can_pre_review,
        CAPABILITY_FINAL_REVIEW: reviewer.can_final_review,
        CAPABILITY_SUSPEND_VERIFICATION: reviewer.can_suspend_verification,
    }.get(capability, False)
    if not allowed:
        code = (
            "suspension_forbidden"
            if capability == CAPABILITY_SUSPEND_VERIFICATION
            else "reviewer_forbidden"
        )
        raise VerificationDomainError(code, status_code=403)
    return reviewer


def _require_any_reviewer(
    user: User | None, capabilities=REVIEW_CAPABILITIES, *, now=None, lock: bool = False
) -> CompanyVerificationReviewer:
    now = now or timezone.now()
    if not user:
        raise VerificationDomainError("reviewer_forbidden", status_code=403)
    capabilities = set(capabilities)
    if not capabilities.intersection(REVIEW_CAPABILITIES):
        raise VerificationDomainError("reviewer_forbidden", status_code=403)
    administrator = _platform_administrator_reviewer(user, lock=lock)
    if administrator is not None:
        return administrator
    queryset = CompanyVerificationReviewer.objects.select_related("user")
    if lock:
        queryset = queryset.select_for_update()
    reviewer = queryset.filter(user=user).first()
    if (
        reviewer is None
        or not reviewer.enabled
        or reviewer.revoked_at is not None
        or (reviewer.expires_at is not None and reviewer.expires_at <= now)
    ):
        raise VerificationDomainError("reviewer_forbidden", status_code=403)
    if not set(_capabilities(reviewer)).intersection(set(capabilities)):
        raise VerificationDomainError("reviewer_forbidden", status_code=403)
    return reviewer


def _audit_category(action: str) -> str:
    for category, actions in AUDIT_CATEGORY_ACTIONS.items():
        if action in actions:
            return category
    return "other"


def _safe_audit_detail(detail) -> dict:
    source = detail if isinstance(detail, dict) else {}
    keys = (
        "reason_code",
        "ticket_reference",
        "material_revision",
        "document_type",
        "review_id",
        "supplement_fields",
        "management_user_uid",
        "changed",
        "previous_reason_code",
        "previous_suspended_at",
    )
    return {key: source[key] for key in keys if key in source}


def _audit_event_payload(event: VerificationAuditEvent) -> dict:
    category = _audit_category(event.action)
    actor = event.actor
    return {
        "event_id": str(event.id),
        "category": category,
        "category_label": AUDIT_CATEGORY_LABELS.get(category, "其他操作"),
        "action": event.action,
        "action_label": AUDIT_ACTION_LABELS.get(event.action, "认证流程操作"),
        "company": {
            "company_id": event.company.cid,
            "company_name": event.company.company_name,
            "registration_no": event.company.registration_no or "",
        },
        "application_id": (
            str(event.application.public_id) if event.application_id else None
        ),
        "actor": {
            "user_uid": actor.uid if actor else "",
            "username": actor.username if actor else "系统任务",
        },
        "revision": event.revision,
        "detail": _safe_audit_detail(event.detail),
        "created_at": event.created_at.isoformat(),
    }


def suspend_company_verification(
    *,
    user: User,
    company_id: str,
    reason_code: str,
    ticket_reference: str,
    idempotency_key: str,
    expected_application_id=None,
    now=None,
) -> dict:
    """Revoke effective certification while preserving approval and IAM history."""

    now = now or timezone.now()
    reason_code = str(reason_code or "").strip().lower()
    ticket_reference = str(ticket_reference or "").strip()
    key = str(idempotency_key or "").strip()
    if not reason_code or not ticket_reference or not key:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash({
        "company_id": str(company_id),
        "reason_code": reason_code,
        "ticket_reference": ticket_reference,
        "expected_application_id": (
            str(expected_application_id) if expected_application_id else None
        ),
    })
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if company is None:
            raise VerificationDomainError("company_not_found", status_code=404)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_SUSPEND_VERIFICATION,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                return dict(existing.result or {})
            raise VerificationDomainError("operation_in_progress", status_code=409)
        application = CompanyVerification.objects.select_for_update().filter(
            pk=company.verified_verification_id
        ).first()
        if (
            company.verification_status != Company.VERIFICATION_STATUS_VERIFIED
            or application is None
            or application.status != CompanyVerification.STATUS_VERIFIED
        ):
            raise VerificationDomainError("verification_not_verified", status_code=409)
        if expected_application_id and str(application.public_id) != str(expected_application_id):
            raise VerificationDomainError("stale_revision", status_code=409)
        reviewer = _require_reviewer(
            user, CAPABILITY_SUSPEND_VERIFICATION, now=now, lock=True
        )
        _require_no_conflict(application, user)
        if company.verification_suspended_at is not None:
            raise VerificationDomainError("verification_already_suspended", status_code=409)
        operation = VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_SUSPEND_VERIFICATION,
            application_revision=application.revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_RUNNING,
            attempts=1,
            lease_until=now + timedelta(seconds=60),
        )
        company.verification_suspended_at = now
        company.verification_suspension_reason = reason_code
        company.save(update_fields=(
            "verification_suspended_at", "verification_suspension_reason"
        ))
        result = {
            "company_id": company.cid,
            "application_id": str(application.public_id),
            "verification_status": company.verification_status,
            "verification_suspended": True,
            "verification_revoked": True,
            "suspended_at": now.isoformat(),
            "revoked_at": now.isoformat(),
            "reason_code": reason_code,
            "ticket_reference": ticket_reference,
            "iam_grants_changed": False,
        }
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="verification_suspended",
            revision=application.revision,
            detail={
                "operation_id": str(operation.id),
                "reviewer_id": reviewer.id,
                "reason_code": reason_code,
                "ticket_reference": ticket_reference,
                "iam_grants_changed": False,
            },
        )
        operation.status = VerificationOperation.STATUS_COMPLETED
        operation.result = result
        operation.lease_until = None
        operation.completed_at = now
        operation.save(update_fields=(
            "status", "result", "lease_until", "completed_at", "updated_at"
        ))
    return result


def suspend_company_service(
    *, user: User, company_id: str, reason_code: str, ticket_reference: str,
    idempotency_key: str, expected_application_id=None, now=None
) -> dict:
    """Block formal and public company services without changing certification history."""

    now = now or timezone.now()
    reason_code = str(reason_code or "").strip().lower()
    ticket_reference = str(ticket_reference or "").strip()
    key = str(idempotency_key or "").strip()
    if not reason_code or not ticket_reference or not key:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash({
        "company_id": str(company_id),
        "reason_code": reason_code,
        "ticket_reference": ticket_reference,
        "expected_application_id": (
            str(expected_application_id) if expected_application_id else None
        ),
    })
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if company is None:
            raise VerificationDomainError("company_not_found", status_code=404)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_SUSPEND_SERVICE,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                return dict(existing.result or {})
            raise VerificationDomainError("operation_in_progress", status_code=409)
        application = CompanyVerification.objects.select_for_update().filter(
            pk=company.verified_verification_id
        ).first()
        if (
            company.verification_status != Company.VERIFICATION_STATUS_VERIFIED
            or application is None
            or application.status != CompanyVerification.STATUS_VERIFIED
        ):
            raise VerificationDomainError("verification_not_verified", status_code=409)
        if expected_application_id and str(application.public_id) != str(expected_application_id):
            raise VerificationDomainError("stale_revision", status_code=409)
        reviewer = _require_reviewer(
            user, CAPABILITY_SUSPEND_VERIFICATION, now=now, lock=True
        )
        _require_no_conflict(application, user)
        if company.service_suspended_at is not None:
            raise VerificationDomainError("service_already_suspended", status_code=409)
        operation = VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_SUSPEND_SERVICE,
            application_revision=application.revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_RUNNING,
            attempts=1,
            lease_until=now + timedelta(seconds=60),
        )
        company.service_suspended_at = now
        company.service_suspension_reason = reason_code
        company.save(update_fields=(
            "service_suspended_at", "service_suspension_reason"
        ))
        result = {
            "company_id": company.cid,
            "application_id": str(application.public_id),
            "verification_status": company.verification_status,
            "verification_revoked": company.verification_suspended_at is not None,
            "service_suspended": True,
            "service_suspended_at": now.isoformat(),
            "reason_code": reason_code,
            "ticket_reference": ticket_reference,
            "iam_grants_changed": False,
        }
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="company_service_suspended",
            revision=application.revision,
            detail={
                "operation_id": str(operation.id),
                "reviewer_id": reviewer.id,
                "reason_code": reason_code,
                "ticket_reference": ticket_reference,
                "iam_grants_changed": False,
            },
        )
        operation.status = VerificationOperation.STATUS_COMPLETED
        operation.result = result
        operation.lease_until = None
        operation.completed_at = now
        operation.save(update_fields=(
            "status", "result", "lease_until", "completed_at", "updated_at"
        ))
    return result


def _restore_company_state(
    *, user: User, company_id: str, reason_code: str, ticket_reference: str,
    idempotency_key: str, restore_target: str, expected_application_id=None,
    now=None,
) -> dict:
    now = now or timezone.now()
    reason_code = str(reason_code or "").strip().lower()
    ticket_reference = str(ticket_reference or "").strip()
    key = str(idempotency_key or "").strip()
    if not reason_code or not ticket_reference or not key:
        raise VerificationDomainError("invalid_request")
    if restore_target == "verification":
        timestamp_field = "verification_suspended_at"
        reason_field = "verification_suspension_reason"
        operation_kind = VerificationOperation.KIND_RESTORE_VERIFICATION
        audit_action = "verification_restored"
        missing_code = "verification_not_revoked"
    elif restore_target == "service":
        timestamp_field = "service_suspended_at"
        reason_field = "service_suspension_reason"
        operation_kind = VerificationOperation.KIND_RESTORE_SERVICE
        audit_action = "company_service_restored"
        missing_code = "service_not_suspended"
    else:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash({
        "company_id": str(company_id),
        "restore_target": restore_target,
        "reason_code": reason_code,
        "ticket_reference": ticket_reference,
        "expected_application_id": (
            str(expected_application_id) if expected_application_id else None
        ),
    })
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if company is None:
            raise VerificationDomainError("company_not_found", status_code=404)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=operation_kind,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                return dict(existing.result or {})
            raise VerificationDomainError("operation_in_progress", status_code=409)
        application = CompanyVerification.objects.select_for_update().filter(
            pk=company.verified_verification_id
        ).first()
        if (
            company.verification_status != Company.VERIFICATION_STATUS_VERIFIED
            or application is None
            or application.status != CompanyVerification.STATUS_VERIFIED
        ):
            raise VerificationDomainError("verification_not_verified", status_code=409)
        if expected_application_id and str(application.public_id) != str(expected_application_id):
            raise VerificationDomainError("stale_revision", status_code=409)
        reviewer = _require_reviewer(
            user, CAPABILITY_SUSPEND_VERIFICATION, now=now, lock=True
        )
        _require_no_conflict(application, user)
        previous_at = getattr(company, timestamp_field)
        previous_reason = getattr(company, reason_field)
        if previous_at is None:
            raise VerificationDomainError(missing_code, status_code=409)
        operation = VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=operation_kind,
            application_revision=application.revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_RUNNING,
            attempts=1,
            lease_until=now + timedelta(seconds=60),
        )
        setattr(company, timestamp_field, None)
        setattr(company, reason_field, "")
        company.save(update_fields=(timestamp_field, reason_field))
        result = {
            "company_id": company.cid,
            "application_id": str(application.public_id),
            "verification_status": company.verification_status,
            "verification_revoked": company.verification_suspended_at is not None,
            "service_suspended": company.service_suspended_at is not None,
            "restored_target": restore_target,
            "restored_at": now.isoformat(),
            "reason_code": reason_code,
            "ticket_reference": ticket_reference,
            "iam_grants_changed": False,
        }
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action=audit_action,
            revision=application.revision,
            detail={
                "operation_id": str(operation.id),
                "reviewer_id": reviewer.id,
                "reason_code": reason_code,
                "ticket_reference": ticket_reference,
                "previous_reason_code": previous_reason,
                "previous_suspended_at": previous_at.isoformat(),
                "iam_grants_changed": False,
            },
        )
        operation.status = VerificationOperation.STATUS_COMPLETED
        operation.result = result
        operation.lease_until = None
        operation.completed_at = now
        operation.save(update_fields=(
            "status", "result", "lease_until", "completed_at", "updated_at"
        ))
    return result


def restore_company_verification(**kwargs) -> dict:
    return _restore_company_state(restore_target="verification", **kwargs)


def restore_company_service(**kwargs) -> dict:
    return _restore_company_state(restore_target="service", **kwargs)



def _conflict_reason(application: CompanyVerification, user: User) -> str:
    if application.applicant_user_id == user.id:
        return "reviewer_is_applicant"
    if application.company.account_id == user.id:
        return "reviewer_is_company_creator"
    if CompanyMember.objects.filter(company=application.company, user=user).exists():
        return "reviewer_is_company_member"
    principal = Principal.objects.filter(type=Principal.TYPE_USER, external_id=user.uid).first()
    if principal and Grant.objects.filter(
        principal=principal,
        resource__type=Resource.TYPE_COMPANY,
        resource__external_id=application.company.cid,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())).exists():
        return "reviewer_has_company_grant"
    return ""


def _require_no_conflict(application: CompanyVerification, user: User):
    conflict = _conflict_reason(application, user)
    if conflict:
        raise VerificationDomainError("review_self_forbidden", status_code=403)


def _lock_application(application_id: str) -> tuple[Company, CompanyVerification]:
    reference = CompanyVerification.objects.filter(
        public_id=application_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).only("id", "company_id").first()
    if reference is None:
        raise VerificationDomainError("application_not_found", status_code=404)
    company = Company.objects.select_for_update().get(pk=reference.company_id)
    application = CompanyVerification.objects.select_for_update().select_related(
        "company"
    ).get(pk=reference.id)
    return company, application


def _grant_verified_company_management(
    *, company: Company, application: CompanyVerification, granter: User
) -> tuple[Grant, User, bool]:
    """Give the verified legal representative or delegate ultimate company control."""

    target_user = application.applicant_user
    if target_user is None:
        raise VerificationDomainError("review_evidence_not_ready", status_code=409)
    principal, _ = Principal.objects.select_for_update().get_or_create(
        type=Principal.TYPE_USER,
        external_id=target_user.uid,
    )
    resource, _ = Resource.objects.select_for_update().get_or_create(
        type=Resource.TYPE_COMPANY,
        external_id=company.cid,
    )
    existing = Grant.objects.select_for_update().filter(
        principal=principal,
        resource=resource,
    ).first()
    changed = bool(
        existing is None
        or existing.permissions != Grant.PERM_SUPER_ADMIN
        or existing.expires_at is not None
        or existing.inherit is not True
    )
    grant = grant_permissions(
        principal=principal,
        resource=resource,
        permissions=Grant.PERM_SUPER_ADMIN,
        granter_user=granter,
        inherit=True,
        expires_at=None,
    )
    return grant, target_user, changed


def _current_document(application: CompanyVerification) -> VerificationDocument | None:
    return application.documents.filter(
        material_revision=application.material_revision,
        document_type=VerificationDocument.TYPE_AUTHORIZATION,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()


def _business_license_document(
    application: CompanyVerification,
) -> VerificationDocument | None:
    return application.documents.filter(
        document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()


def _review_payload(application: CompanyVerification) -> dict:
    payload = serialize_manual_verification(application)
    payload["company"] = {
        "cid": application.company.cid,
        "company_name": application.company_name_snapshot,
        "registration_no": application.registration_no_snapshot,
        "verification_status": application.company.verification_status,
        "verification_revoked": application.company.verification_suspended_at is not None,
        "verification_revoked_at": (
            application.company.verification_suspended_at.isoformat()
            if application.company.verification_suspended_at else None
        ),
        "service_suspended": application.company.service_suspended_at is not None,
        "service_suspended_at": (
            application.company.service_suspended_at.isoformat()
            if application.company.service_suspended_at else None
        ),
    }
    payload["submitted_revision"] = application.submitted_revision
    payload["submitted_material_revision"] = application.submitted_material_revision
    payload["review_actions"] = (
        ["transcribe"]
        if application.stage == CompanyVerification.STAGE_DOCUMENT_CHECK
        else sorted(REVIEW_ACTIONS)
        if application.stage == CompanyVerification.STAGE_MANUAL_REVIEW
        else []
    )
    payload["administrative_actions"] = []
    reviews = {
        str(review.id): review
        for review in application.reviews.select_related("reviewer__user")
    }
    history_actions = set().union(
        AUDIT_CATEGORY_ACTIONS["submission"],
        AUDIT_CATEGORY_ACTIONS["review"],
        AUDIT_CATEGORY_ACTIONS["administration"],
    )
    history = []
    seen_review_ids = set()
    for event in application.audit_events.select_related(
        "company", "application", "actor"
    ).filter(action__in=history_actions).order_by("created_at", "id"):
        item = _audit_event_payload(event)
        review_id = str((event.detail or {}).get("review_id") or "")
        review = reviews.get(review_id)
        if review is not None:
            seen_review_ids.add(review_id)
            item.update({
                "review_id": review_id,
                "reason_public": review.reason_public,
                "reason_internal": review.reason_internal,
                "reviewer_uid": review.reviewer.user.uid,
                "reviewer_name": review.reviewer.user.username,
            })
        history.append(item)
    history_action_names = {item["action"] for item in history}
    if (
        application.submitted_at is not None
        and "application_submitted_for_review" not in history_action_names
    ):
        history.append({
            "event_id": f"submission-{application.public_id}",
            "category": "submission",
            "category_label": AUDIT_CATEGORY_LABELS["submission"],
            "action": "application_submitted_for_review",
            "action_label": AUDIT_ACTION_LABELS["application_submitted_for_review"],
            "actor": {
                "user_uid": application.applicant_user.uid,
                "username": application.applicant_user.username,
            },
            "created_at": application.submitted_at.isoformat(),
            "detail": {},
        })

    for review_id, review in reviews.items():
        if review_id in seen_review_ids:
            continue
        history.append({
            "event_id": f"review-{review_id}",
            "category": "review",
            "category_label": AUDIT_CATEGORY_LABELS["review"],
            "action": f"manual_review_{review.action}",
            "action_label": AUDIT_ACTION_LABELS.get(
                f"manual_review_{review.action}", "审核操作"
            ),
            "review_id": review_id,
            "reason_public": review.reason_public,
            "reason_internal": review.reason_internal,
            "reviewer_uid": review.reviewer.user.uid,
            "reviewer_name": review.reviewer.user.username,
            "actor": {
                "user_uid": review.reviewer.user.uid,
                "username": review.reviewer.user.username,
            },
            "created_at": review.created_at.isoformat(),
            "detail": {},
        })
    payload["review_history"] = sorted(
        history, key=lambda item: (item["created_at"], item["event_id"])
    )
    return payload


def list_registered_companies(
    *, user: User, status: str = "all", keyword: str = "", limit: int = 50,
    offset: int = 0, now=None
) -> dict:
    """List every registered company for the verification administration console."""

    now = now or timezone.now()
    _require_any_reviewer(user, now=now)
    applications = CompanyVerification.objects.select_related("applicant_user").filter(
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).order_by("-started_at", "-id")
    queryset = Company.objects.select_related(
        "verified_verification", "verified_verification__applicant_user",
    ).prefetch_related(
        Prefetch("verifications", queryset=applications, to_attr="admin_verifications")
    )
    keyword = str(keyword or "").strip()
    if keyword:
        queryset = queryset.filter(
            Q(company_name__icontains=keyword)
            | Q(registration_no__icontains=keyword)
            | Q(cid__icontains=keyword)
        )
    if status == "verified":
        queryset = queryset.filter(
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
        )
    elif status == "unverified":
        queryset = queryset.filter(
            verification_status=Company.VERIFICATION_STATUS_UNVERIFIED
        )
    elif status == "revoked":
        queryset = queryset.filter(verification_suspended_at__isnull=False)
    elif status == "service_suspended":
        queryset = queryset.filter(service_suspended_at__isnull=False)
    elif status != "all":
        raise VerificationDomainError("invalid_request")
    queryset = queryset.order_by("-id")
    total = queryset.count()
    items = []
    for company in queryset[offset: offset + limit]:
        application = company.verified_verification
        if application is None and company.admin_verifications:
            application = company.admin_verifications[0]
        items.append({
            "company_id": company.cid,
            "company_name": company.company_name,
            "registration_no": company.registration_no or "",
            "created_at": company.created_at,
            "verification_status": company.verification_status,
            "verification_effective": bool(
                company.verification_status == Company.VERIFICATION_STATUS_VERIFIED
                and company.verification_suspended_at is None
            ),
            "verification_revoked": company.verification_suspended_at is not None,
            "verification_revoked_at": (
                company.verification_suspended_at.isoformat()
                if company.verification_suspended_at else None
            ),
            "service_suspended": company.service_suspended_at is not None,
            "service_suspended_at": (
                company.service_suspended_at.isoformat()
                if company.service_suspended_at else None
            ),
            "verified_at": company.verified_at.isoformat() if company.verified_at else None,
            "application_id": str(application.public_id) if application else None,
            "application_status": application.status if application else None,
            "application_stage": application.stage if application else None,
            "applicant_name": application.applicant_name_snapshot if application else "",
        })
    return {"status": status, "total": total, "items": items}


def list_verification_audit_events(
    *, user: User, category: str = "all", keyword: str = "", limit: int = 50,
    offset: int = 0, now=None,
) -> dict:
    now = now or timezone.now()
    _require_any_reviewer(user, now=now)
    queryset = VerificationAuditEvent.objects.select_related(
        "company", "application", "actor"
    )
    if category != "all":
        actions = AUDIT_CATEGORY_ACTIONS.get(category)
        if actions is None:
            raise VerificationDomainError("invalid_request")
        queryset = queryset.filter(action__in=actions)
    keyword = str(keyword or "").strip()
    if keyword:
        queryset = queryset.filter(
            Q(company__company_name__icontains=keyword)
            | Q(company__registration_no__icontains=keyword)
            | Q(company__cid__icontains=keyword)
            | Q(actor__username__icontains=keyword)
            | Q(actor__uid__icontains=keyword)
            | Q(action__icontains=keyword)
        )
    queryset = queryset.order_by("-created_at", "-id")
    total = queryset.count()
    events = list(queryset[offset: offset + limit])
    return {
        "category": category,
        "category_label": AUDIT_CATEGORY_LABELS.get(category, "其他操作"),
        "total": total,
        "items": [_audit_event_payload(event) for event in events],
    }



def list_review_queue(
    *, user: User, queue: str, limit: int = 20, offset: int = 0, now=None
) -> dict:
    now = now or timezone.now()
    capability = (
        CAPABILITY_PRE_REVIEW if queue == CAPABILITY_PRE_REVIEW else CAPABILITY_FINAL_REVIEW
    )
    if queue not in REVIEW_QUEUE_CAPABILITIES:
        raise VerificationDomainError("invalid_request")
    _require_reviewer(user, capability, now=now)
    stage = (
        CompanyVerification.STAGE_DOCUMENT_CHECK
        if queue == CAPABILITY_PRE_REVIEW
        else CompanyVerification.STAGE_MANUAL_REVIEW
    )
    candidates = CompanyVerification.objects.select_related("company", "applicant_user").filter(
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        status=CompanyVerification.STATUS_PENDING,
        stage=stage,
        expires_at__gt=now,
    ).order_by("started_at", "id")
    eligible = []
    for application in candidates:
        if _conflict_reason(application, user):
            continue
        if queue == CAPABILITY_PRE_REVIEW:
            consistency = application.checks.filter(
                kind=VerificationCheck.KIND_CONSISTENCY,
                material_revision=application.material_revision,
            ).order_by("-checked_at", "-created_at").first()
            result = consistency.result if consistency and isinstance(consistency.result, dict) else {}
            if (
                result.get("pre_review_status") != "required"
                or result.get("manual_transcription_allowed") is not True
            ):
                continue
        eligible.append(application)
    return {
        "queue": queue,
        "total": len(eligible),
        "items": [_review_payload(item) for item in eligible[offset: offset + limit]],
    }


def get_review_application(*, user: User, application_id: str, now=None) -> dict:
    now = now or timezone.now()
    application = CompanyVerification.objects.select_related("company", "applicant_user").filter(
        public_id=application_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).first()
    if application is None:
        raise VerificationDomainError("application_not_found", status_code=404)
    is_terminal = application.status not in ACTIVE_STATUSES
    if is_terminal:
        reviewer = _require_any_reviewer(
            user, {CAPABILITY_FINAL_REVIEW, CAPABILITY_SUSPEND_VERIFICATION}, now=now
        )
    else:
        capability = (
            CAPABILITY_PRE_REVIEW
            if application.stage == CompanyVerification.STAGE_DOCUMENT_CHECK
            else CAPABILITY_FINAL_REVIEW
        )
        reviewer = _require_reviewer(user, capability, now=now)
        _require_no_conflict(application, user)
    payload = _review_payload(application)
    if (
        application.status == CompanyVerification.STATUS_VERIFIED
        and reviewer.can_suspend_verification
        and not _conflict_reason(application, user)
    ):
        payload["administrative_actions"].append(
            "restore_verification"
            if application.company.verification_suspended_at is not None
            else "revoke_verification"
        )
        payload["administrative_actions"].append(
            "restore_service"
            if application.company.service_suspended_at is not None
            else "suspend_service"
        )
    return payload


def get_review_document_preview(
    *, user: User, application_id: str, document_id: str, storage=None, now=None
) -> bytes:
    now = now or timezone.now()
    application = CompanyVerification.objects.select_related("company").filter(
        public_id=application_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).first()
    if application is None:
        raise VerificationDomainError("application_not_found", status_code=404)
    if application.status not in ACTIVE_STATUSES:
        _require_any_reviewer(
            user, {CAPABILITY_FINAL_REVIEW, CAPABILITY_SUSPEND_VERIFICATION}, now=now
        )
    else:
        capability = (
            CAPABILITY_PRE_REVIEW
            if application.stage == CompanyVerification.STAGE_DOCUMENT_CHECK
            else CAPABILITY_FINAL_REVIEW
        )
        _require_reviewer(user, capability, now=now)
        _require_no_conflict(application, user)
    authorization_document = _current_document(application)
    business_license_document = _business_license_document(application)
    document = next(
        (
            candidate
            for candidate in (authorization_document, business_license_document)
            if candidate is not None and str(candidate.id) == str(document_id)
        ),
        None,
    )
    if document is None or not document.preview_storage_key:
        raise VerificationDomainError("document_not_found", status_code=404)
    try:
        content = (storage or get_verification_storage()).get(
            document.preview_storage_key, max_bytes=12 * 1024 * 1024
        )
    except VerificationStorageError as exc:
        raise VerificationDomainError("document_storage_unavailable", status_code=503) from exc
    VerificationAuditEvent.objects.create(
        company=application.company,
        application=application,
        actor=user,
        action=(
            "review_business_license_previewed"
            if document.document_type == VerificationDocument.TYPE_BUSINESS_LICENSE
            else "review_authorization_document_previewed"
        ),
        revision=application.revision,
        detail={
            "document_id": str(document.id),
            "document_type": document.document_type,
            "material_revision": document.material_revision,
        },
    )
    return content


def _validate_corrections(application, mismatches, corrections):
    if not isinstance(corrections, list) or not corrections or len(corrections) > len(TRANSCRIPTION_FIELDS):
        raise VerificationDomainError("transcription_invalid")
    expected = authorization_expected_values(application)
    normalized = []
    fields = []
    for raw in corrections:
        if not isinstance(raw, dict):
            raise VerificationDomainError("transcription_invalid")
        field = str(raw.get("field") or "").strip()
        original_value = str(raw.get("original_value") or "").strip()
        corrected_value = str(raw.get("corrected_value") or "").strip()
        source_location = str(raw.get("source_location") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if (
            field not in TRANSCRIPTION_FIELDS
            or field in fields
            or not original_value
            or not corrected_value
            or not source_location
            or not reason
            or len(original_value) > 512
            or len(corrected_value) > 512
            or len(source_location) > 255
            or len(reason) > 500
            or _normalize_text(corrected_value) != _normalize_text(expected[field])
        ):
            raise VerificationDomainError("transcription_invalid")
        fields.append(field)
        normalized.append({
            "field": field,
            "original_value": original_value,
            "corrected_value": corrected_value,
            "source_location": source_location,
            "reason": reason,
        })
    if set(fields) != set(mismatches):
        raise VerificationDomainError("transcription_invalid")
    return normalized


def transcribe_authorization_document(
    *, user: User, application_id: str, expected_revision: int, idempotency_key: str,
    corrections, reason: str, now=None
) -> dict:
    _require_feature_enabled()
    now = now or timezone.now()
    key = str(idempotency_key or "").strip()
    reason = str(reason or "").strip()
    if not key or len(key) > 128 or not reason or len(reason) > 2000:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash({
        "application_id": str(application_id),
        "expected_revision": int(expected_revision),
        "actor": user.id,
        "corrections": corrections,
        "reason": reason,
    })
    with transaction.atomic():
        company, application = _lock_application(application_id)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_TRANSCRIPTION,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                return serialize_manual_verification(application)
            raise VerificationDomainError("operation_in_progress", status_code=409)
        reviewer = _require_reviewer(user, CAPABILITY_PRE_REVIEW, now=now, lock=True)
        _require_no_conflict(application, user)
        if (
            application.status != CompanyVerification.STATUS_PENDING
            or application.stage != CompanyVerification.STAGE_DOCUMENT_CHECK
            or application.revision != int(expected_revision)
            or not application.expires_at
            or application.expires_at <= now
        ):
            raise VerificationDomainError("stale_revision", status_code=409)
        document = _current_document(application)
        if document is None:
            raise VerificationDomainError("document_not_found", status_code=404)
        source = application.checks.filter(
            kind=VerificationCheck.KIND_CONSISTENCY,
            document=document,
            material_revision=application.material_revision,
        ).order_by("-checked_at", "-created_at").first()
        source_result = source.result if source and isinstance(source.result, dict) else {}
        if (
            source is None
            or source.status != VerificationCheck.STATUS_UNKNOWN
            or source.provider != "touming_rules"
            or source.action != "authorization_consistency_v1"
            or source.rule_version != application.policy_version
            or source.provider_environment != application.provider_environment
            or source_result.get("adopted") is not True
            or source_result.get("pre_review_status") != "required"
            or source_result.get("manual_transcription_allowed") is not True
        ):
            raise VerificationDomainError("transcription_not_allowed", status_code=409)
        evidence_checks = []
        for kind in (
            VerificationCheck.KIND_DOCUMENT_SECURITY,
            VerificationCheck.KIND_DOCUMENT_TEXT,
            VerificationCheck.KIND_SEAL,
        ):
            check = application.checks.filter(
                kind=kind,
                document=document,
                material_revision=application.material_revision,
            ).order_by("-checked_at", "-created_at").first()
            result = check.result if check and isinstance(check.result, dict) else {}
            if (
                check is None
                or check.status != VerificationCheck.STATUS_PASSED
                or not check.valid_until
                or check.valid_until <= now
                or result.get("adopted") is not True
            ):
                raise VerificationDomainError("transcription_not_allowed", status_code=409)
            evidence_checks.append(check)
        normalized = _validate_corrections(
            application, source_result.get("mismatches") or [], corrections
        )
        transcription = VerificationTranscription.objects.create(
            application=application,
            document=document,
            reviewer=reviewer,
            source_check=source,
            application_revision=application.revision,
            material_revision=application.material_revision,
            corrections=normalized,
            reason=reason,
        )
        fields = dict(source_result.get("fields") or {})
        fields.update({item["field"]: True for item in normalized})
        result_check = VerificationCheck.objects.create(
            application=application,
            kind=VerificationCheck.KIND_CONSISTENCY,
            attempt_no=application.checks.filter(
                kind=VerificationCheck.KIND_CONSISTENCY,
                document=document,
            ).count() + 1,
            input_digest=request_hash,
            material_revision=application.material_revision,
            document=document,
            provider="touming_rules",
            action="authorization_consistency_v1",
            provider_environment=application.provider_environment,
            status=VerificationCheck.STATUS_PASSED,
            provider_code="authorization_consistent_after_transcription",
            result={
                "outcome": "passed",
                "fields": fields,
                "mismatches": [],
                "pre_review_status": "completed",
                "pre_review_reasons": [],
                "manual_transcription_allowed": False,
                "manual_transcription_is_final_approval": False,
                "adopted": True,
                "source_check_ids": list(source_result.get("source_check_ids") or []),
                "source_consistency_check_id": str(source.id),
                "transcription_id": str(transcription.id),
            },
            rule_version=application.policy_version,
            checked_at=now,
            valid_until=min(check.valid_until for check in evidence_checks),
        )
        transcription.result_check = result_check
        transcription.save(update_fields=("result_check",))
        application.stage = CompanyVerification.STAGE_READY_TO_SUBMIT
        application.revision += 1
        application.failure_code = ""
        application.public_reason = ""
        application.save(update_fields=("stage", "revision", "failure_code", "public_reason"))
        VerificationOperation.objects.create(
            company=company,
            application=application,
            document=document,
            operation_kind=VerificationOperation.KIND_TRANSCRIPTION,
            application_revision=int(expected_revision),
            material_revision=application.material_revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result={
                "transcription_id": str(transcription.id),
                "check_id": str(result_check.id),
                "revision": application.revision,
            },
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="authorization_transcription_completed",
            revision=application.revision,
            detail={
                "transcription_id": str(transcription.id),
                "document_id": str(document.id),
                "material_revision": document.material_revision,
                "corrected_fields": sorted(item["field"] for item in normalized),
            },
        )
    return serialize_manual_verification(application)


def _enqueue_notification(application, event_type: str, payload: dict, now):
    VerificationNotification.objects.get_or_create(
        application=application,
        event_type=event_type,
        application_revision=application.revision,
        defaults={
            "recipient": application.applicant_user,
            "payload": payload,
            "status": VerificationNotification.STATUS_PENDING,
            "next_run_at": now,
        },
    )


def submit_manual_verification(
    *, company_id: str, application_id: str, user: User, expected_revision: int,
    idempotency_key: str, now=None
) -> dict:
    _require_feature_enabled()
    now = now or timezone.now()
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    request_hash = _request_hash({
        "company_id": company_id,
        "application_id": str(application_id),
        "expected_revision": int(expected_revision),
        "actor": user.id,
    })
    with transaction.atomic():
        company, application = _lock_application(application_id)
        if company.cid != company_id or application.applicant_user_id != user.id:
            raise VerificationDomainError("application_forbidden", status_code=403)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_SUBMIT,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                return serialize_manual_verification(application)
            raise VerificationDomainError("operation_in_progress", status_code=409)
        if application.revision != int(expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        decision = evaluate_manual_submission(
            application, now=now, allow_mock=_allow_mock_for_internal_review()
        )
        if not decision.allowed:
            raise VerificationDomainError("review_evidence_not_ready", status_code=409)
        application.stage = CompanyVerification.STAGE_MANUAL_REVIEW
        application.revision += 1
        application.submitted_revision = application.revision
        application.submitted_material_revision = application.material_revision
        application.submitted_at = now
        application.failure_code = ""
        application.public_reason = ""
        application.save(update_fields=(
            "stage", "revision", "submitted_revision", "submitted_material_revision",
            "submitted_at", "failure_code", "public_reason",
        ))
        VerificationOperation.objects.create(
            company=company,
            application=application,
            operation_kind=VerificationOperation.KIND_SUBMIT,
            application_revision=int(expected_revision),
            material_revision=application.material_revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_COMPLETED,
            result={"revision": application.revision, "stage": application.stage},
            completed_at=now,
        )
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action="application_submitted_for_review",
            revision=application.revision,
            detail={"material_revision": application.material_revision},
        )
        _enqueue_notification(
            application,
            VerificationNotification.EVENT_SUBMITTED,
            {"application_id": str(application.public_id), "stage": application.stage},
            now,
        )
    return serialize_manual_verification(application)


def _validate_review_input(action, reason_public, reason_internal, checklist, supplement_fields):
    if action not in REVIEW_ACTIONS:
        raise VerificationDomainError("invalid_request")
    public = str(reason_public or "").strip()
    internal = str(reason_internal or "").strip()
    if len(public) > 255 or len(internal) > 4000:
        raise VerificationDomainError("invalid_request")
    if action != VerificationReview.ACTION_APPROVE and not public:
        raise VerificationDomainError("invalid_request")
    if not isinstance(checklist, dict) or set(checklist) != REVIEW_CHECKLIST_KEYS:
        raise VerificationDomainError("invalid_request")
    if any(not isinstance(checklist[key], bool) for key in REVIEW_CHECKLIST_KEYS):
        raise VerificationDomainError("invalid_request")
    if action == VerificationReview.ACTION_APPROVE and not all(checklist.values()):
        raise VerificationDomainError("review_evidence_not_ready", status_code=409)
    fields = sorted({str(item or "").strip() for item in (supplement_fields or []) if str(item or "").strip()})
    if action == VerificationReview.ACTION_REQUEST_SUPPLEMENT:
        if not fields or not set(fields).issubset({
            "authorization_document",
            "business_license",
        }):
            raise VerificationDomainError("invalid_request")
    elif fields:
        raise VerificationDomainError("invalid_request")
    return public, internal, fields


def _schedule_registry_refresh_locked(application, now):
    source_check_id = application.checks.filter(
        kind=VerificationCheck.KIND_REGISTRY
    ).order_by("-checked_at", "-created_at").values_list("id", flat=True).first()
    key = f"review-refresh:{application.public_id}:{application.revision}:{source_check_id or 'missing'}"
    operation, created = VerificationOperation.objects.get_or_create(
        company=application.company,
        operation_kind=VerificationOperation.KIND_REVIEW_EVIDENCE_REFRESH,
        idempotency_key=key,
        defaults={
            "application": application,
            "application_revision": application.revision,
            "material_revision": application.material_revision,
            "request_hash": _request_hash({
                "application_id": str(application.public_id),
                "revision": application.revision,
                "company_name": application.company_name_snapshot,
                "registration_no": application.registration_no_snapshot,
                "mode": application.mode,
            }),
            "status": VerificationOperation.STATUS_PENDING,
            "next_run_at": now,
            "result": {"kind": VerificationCheck.KIND_REGISTRY},
        },
    )
    if created:
        VerificationAuditEvent.objects.create(
            company=application.company,
            application=application,
            action="review_evidence_refresh_scheduled",
            revision=application.revision,
            detail={"operation_id": str(operation.id), "kind": VerificationCheck.KIND_REGISTRY},
        )
    return operation


def decide_manual_verification(
    *, user: User, application_id: str, expected_revision: int, idempotency_key: str,
    action: str, reason_public: str, reason_internal: str, checklist: dict,
    supplement_fields=None, now=None
) -> dict:
    _require_feature_enabled()
    now = now or timezone.now()
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    public, internal, fields = _validate_review_input(
        action, reason_public, reason_internal, checklist, supplement_fields
    )
    request_hash = _request_hash({
        "application_id": str(application_id),
        "expected_revision": int(expected_revision),
        "actor": user.id,
        "action": action,
        "reason_public": public,
        "reason_internal": internal,
        "checklist": checklist,
        "supplement_fields": fields,
    })
    refresh_pending = False
    with transaction.atomic():
        company, application = _lock_application(application_id)
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_REVIEW_DECISION,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                return serialize_manual_verification(application)
            raise VerificationDomainError("operation_in_progress", status_code=409)
        reviewer = _require_reviewer(user, CAPABILITY_FINAL_REVIEW, now=now, lock=True)
        _require_no_conflict(application, user)
        if application.revision != int(expected_revision):
            raise VerificationDomainError("stale_revision", status_code=409)
        if (
            application.status != CompanyVerification.STATUS_PENDING
            or application.stage != CompanyVerification.STAGE_MANUAL_REVIEW
            or application.submitted_revision != application.revision
            or application.submitted_material_revision != application.material_revision
            or not application.expires_at
            or application.expires_at <= now
        ):
            raise VerificationDomainError("review_conflict", status_code=409)
        if action == VerificationReview.ACTION_REQUEST_SUPPLEMENT:
            expected_supplement = {
                "business_license"
                if application.applicant_role
                == CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE
                else "authorization_document"
            }
            if set(fields) != expected_supplement:
                raise VerificationDomainError("invalid_request")
        if action == VerificationReview.ACTION_APPROVE:
            policy = evaluate_manual_approval(
                application, now=now, allow_mock=_allow_mock_for_internal_review()
            )
            if not policy.allowed:
                if set(policy.reasons) == {"registry_check_stale"}:
                    _schedule_registry_refresh_locked(application, now)
                    refresh_pending = True
                else:
                    raise VerificationDomainError("review_evidence_not_ready", status_code=409)
        if not refresh_pending:
            before_revision = application.revision
            before_status = application.status
            document = _current_document(application)
            business_license_document = _business_license_document(application)
            management_grant = None
            management_user = None
            management_grant_changed = False
            if action == VerificationReview.ACTION_APPROVE:
                after_status = CompanyVerification.STATUS_VERIFIED
                event_type = VerificationNotification.EVENT_APPROVED
                application.status = CompanyVerification.STATUS_VERIFIED
                application.stage = CompanyVerification.STAGE_COMPLETED
                application.finished_at = now
                application.failure_code = ""
                application.public_reason = public
                company.verification_status = Company.VERIFICATION_STATUS_VERIFIED
                company.verification_provider = Company.VERIFICATION_PROVIDER_TENCENT_MANUAL_V1
                company.verified_at = now
                company.verified_verification = application
                company.save(update_fields=(
                    "verification_status", "verification_provider", "verified_at",
                    "verified_verification",
                ))
                (
                    management_grant,
                    management_user,
                    management_grant_changed,
                ) = _grant_verified_company_management(
                    company=company,
                    application=application,
                    granter=user,
                )
            elif action == VerificationReview.ACTION_REJECT:
                after_status = CompanyVerification.STATUS_REJECTED
                event_type = VerificationNotification.EVENT_REJECTED
                application.status = CompanyVerification.STATUS_REJECTED
                application.stage = CompanyVerification.STAGE_COMPLETED
                application.finished_at = now
                application.failure_code = "review_rejected"
                application.public_reason = public
            else:
                after_status = CompanyVerification.STATUS_PENDING
                event_type = VerificationNotification.EVENT_SUPPLEMENT_REQUIRED
                application.stage = (
                    CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD
                    if fields == ["business_license"]
                    else CompanyVerification.STAGE_SUPPLEMENT_REQUIRED
                )
                application.finished_at = None
                application.failure_code = "supplement_required"
                application.public_reason = public
            application.internal_reason = internal
            application.revision += 1
            application.save(update_fields=(
                "status", "stage", "finished_at", "failure_code", "public_reason",
                "internal_reason", "revision",
            ))
            review = VerificationReview.objects.create(
                application=application,
                reviewer=reviewer,
                action=action,
                submitted_revision=before_revision,
                material_revision=application.material_revision,
                reason_public=public,
                reason_internal=internal,
                checklist=checklist,
                status_before=before_status,
                status_after=after_status,
            )
            evidence = []
            if application.applicant_role:
                evidence_kinds = REGISTRATION_COMMON_CHECKS
                if (
                    application.applicant_role
                    == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
                ):
                    evidence_kinds += (
                    VerificationCheck.KIND_DOCUMENT_SECURITY,
                    VerificationCheck.KIND_DOCUMENT_TEXT,
                    VerificationCheck.KIND_SEAL,
                    VerificationCheck.KIND_CONSISTENCY,
                    )
            else:
                evidence_kinds = REQUIRED_APPROVAL_CHECKS
            for kind in evidence_kinds:
                query = application.checks.filter(kind=kind)
                if kind in {
                    VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY,
                    VerificationCheck.KIND_BUSINESS_LICENSE,
                    VerificationCheck.KIND_DOCUMENT_SECURITY,
                    VerificationCheck.KIND_DOCUMENT_TEXT,
                    VerificationCheck.KIND_SEAL,
                    VerificationCheck.KIND_CONSISTENCY,
                }:
                    evidence_document = (
                        business_license_document
                        if kind in {
                            VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY,
                            VerificationCheck.KIND_BUSINESS_LICENSE,
                        }
                        else document
                    )
                    if evidence_document is None:
                        continue
                    query = query.filter(
                        material_revision=evidence_document.material_revision
                    )
                check = query.order_by("-checked_at", "-created_at").first()
                if check:
                    evidence.append(check)
            review.checks.add(*evidence)
            if document:
                review.documents.add(document)
            if business_license_document:
                review.documents.add(business_license_document)
            operation_result = {
                "review_id": str(review.id),
                "action": action,
                "revision": application.revision,
            }
            if management_grant is not None and management_user is not None:
                operation_result["management_grant_id"] = management_grant.id
                operation_result["management_user_uid"] = management_user.uid
                operation_result["management_grant_changed"] = management_grant_changed
            VerificationOperation.objects.create(
                company=company,
                application=application,
                document=document or business_license_document,
                operation_kind=VerificationOperation.KIND_REVIEW_DECISION,
                application_revision=before_revision,
                material_revision=application.material_revision,
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
                action=f"manual_review_{action}",
                revision=application.revision,
                detail={
                    "review_id": str(review.id),
                    "submitted_revision": before_revision,
                    "material_revision": application.material_revision,
                    "supplement_fields": fields,
                    "management_grant_id": (
                        management_grant.id if management_grant is not None else None
                    ),
                    "management_user_uid": (
                        management_user.uid if management_user is not None else ""
                    ),
                    "management_grant_changed": management_grant_changed,
                },
            )
            if management_grant is not None and management_user is not None:
                VerificationAuditEvent.objects.create(
                    company=company,
                    application=application,
                    actor=user,
                    action="verified_company_management_granted",
                    revision=application.revision,
                    detail={
                        "grant_id": management_grant.id,
                        "user_uid": management_user.uid,
                        "permissions": Grant.PERM_ADMIN,
                        "changed": management_grant_changed,
                    },
                )
            _enqueue_notification(
                application,
                event_type,
                {
                    "application_id": str(application.public_id),
                    "status": application.status,
                    "stage": application.stage,
                    "reason": public,
                },
                now,
            )
    if refresh_pending:
        raise VerificationDomainError("evidence_refresh_pending", status_code=409)
    return serialize_manual_verification(application)


def process_review_evidence_refresh(*, operation_id: str, verifier=None, now=None) -> dict:
    now = now or timezone.now()
    operation_pk = operation_id
    with transaction.atomic():
        operation = VerificationOperation.objects.select_for_update().filter(
            pk=operation_pk,
            operation_kind=VerificationOperation.KIND_REVIEW_EVIDENCE_REFRESH,
        ).first()
        if operation is None:
            return {"processed": False, "reason": "not_found"}
        if operation.status == VerificationOperation.STATUS_COMPLETED:
            return {"processed": False, "reason": "completed"}
        if (
            operation.status == VerificationOperation.STATUS_RUNNING
            and operation.lease_until
            and operation.lease_until > now
        ):
            return {"processed": False, "reason": "leased"}
        if operation.attempts >= retry_budget():
            operation.status = VerificationOperation.STATUS_FAILED
            operation.lease_until = None
            operation.next_run_at = None
            operation.last_error_code = "retry_exhausted"
            operation.save(update_fields=(
                "status", "lease_until", "next_run_at", "last_error_code", "updated_at"
            ))
            return {"processed": False, "reason": "retry_exhausted"}
        application = CompanyVerification.objects.select_for_update().get(pk=operation.application_id)
        if (
            application.status != CompanyVerification.STATUS_PENDING
            or application.stage != CompanyVerification.STAGE_MANUAL_REVIEW
            or application.revision != operation.application_revision
            or application.submitted_material_revision != application.material_revision
        ):
            operation.status = VerificationOperation.STATUS_COMPLETED
            operation.completed_at = now
            operation.last_error_code = "stale_application"
            operation.next_run_at = None
            operation.save(update_fields=(
                "status", "completed_at", "last_error_code", "next_run_at", "updated_at"
            ))
            return {"processed": False, "reason": "stale_application"}
        attempt_no = (application.checks.filter(kind=VerificationCheck.KIND_REGISTRY)
                      .order_by("-attempt_no").values_list("attempt_no", flat=True).first() or 0) + 1
        check = VerificationCheck.objects.create(
            application=application,
            kind=VerificationCheck.KIND_REGISTRY,
            attempt_no=attempt_no,
            input_digest=operation.request_hash,
            provider=application.provider,
            action="VerifyBizLicenseEnterprise3",
            mode=application.mode,
            provider_environment=application.provider_environment,
            status=VerificationCheck.STATUS_RUNNING,
            rule_version=application.policy_version,
        )
        operation.status = VerificationOperation.STATUS_RUNNING
        operation.attempts += 1
        operation.lease_until = now + timedelta(seconds=60)
        operation.next_run_at = None
        operation.result = {"check_id": str(check.id), "kind": VerificationCheck.KIND_REGISTRY}
        operation.save(update_fields=(
            "status", "attempts", "lease_until", "next_run_at", "result", "updated_at"
        ))
        request = RegistryVerificationRequest(
            company_name=application.company_name_snapshot,
            registration_no=application.registration_no_snapshot,
            mode=application.mode,
        )
    try:
        result = (verifier or get_registry_verifier()).verify(request)
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__) or "provider_unavailable")[:64]
        with transaction.atomic():
            operation = VerificationOperation.objects.select_for_update().get(pk=operation_pk)
            exhausted = operation.attempts >= retry_budget()
            VerificationCheck.objects.filter(pk=(operation.result or {}).get("check_id")).update(
                status=VerificationCheck.STATUS_UNKNOWN,
                provider_code=code,
                checked_at=now,
                valid_until=None,
            )
            operation.status = VerificationOperation.STATUS_FAILED
            operation.lease_until = None
            operation.next_run_at = None if exhausted else now + timedelta(seconds=max(int(getattr(
                settings, "COMPANY_VERIFICATION_OPERATION_RETRY_SECONDS", 30
            )), 1))
            operation.last_error_code = "retry_exhausted" if exhausted else code
            operation.save(update_fields=(
                "status", "lease_until", "next_run_at", "last_error_code", "updated_at"
            ))
        if isinstance(exc, VerificationProviderError):
            return {"processed": False, "reason": "provider_unavailable"}
        logger.exception("review evidence refresh failed operation_id=%s", operation_pk)
        return {"processed": False, "reason": "provider_unavailable"}
    passed = result.outcome == "passed" and result.matched is True and result.operating_status == "1"
    with transaction.atomic():
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_pk)
        application = CompanyVerification.objects.select_for_update().select_related("company").get(
            pk=operation.application_id
        )
        check = VerificationCheck.objects.select_for_update().get(
            pk=(operation.result or {}).get("check_id")
        )
        current = (
            application.status == CompanyVerification.STATUS_PENDING
            and application.stage == CompanyVerification.STAGE_MANUAL_REVIEW
            and application.revision == operation.application_revision
            and application.submitted_material_revision == application.material_revision
        )
        check.status = (
            VerificationCheck.STATUS_PASSED
            if passed and current
            else VerificationCheck.STATUS_FAILED
            if result.outcome == "failed" and current
            else VerificationCheck.STATUS_UNKNOWN
        )
        check.provider_code = str(result.provider_code or "")[:128]
        check.request_id = str(result.request_id or "")[:255]
        evidence = dict(result.evidence or {})
        check.result = {
            "outcome": result.outcome,
            "matched": result.matched,
            "operating_status": result.operating_status,
            "status_code": evidence.get("status_code"),
            "verify_result": evidence.get("verify_result"),
        }
        check.checked_at = now
        check.valid_until = (
            now + timedelta(seconds=max(int(getattr(
                settings, "COMPANY_VERIFICATION_REGISTRY_MAX_AGE_SECONDS", 86400
            )), 1))
            if passed and current
            else None
        )
        check.save(update_fields=(
            "status", "provider_code", "request_id", "result", "checked_at", "valid_until"
        ))
        operation.status = (
            VerificationOperation.STATUS_FAILED
            if result.retryable
            else VerificationOperation.STATUS_COMPLETED
        )
        operation.lease_until = None
        exhausted = result.retryable and operation.attempts >= retry_budget()
        operation.next_run_at = (
            now + timedelta(seconds=max(int(getattr(
                settings, "COMPANY_VERIFICATION_OPERATION_RETRY_SECONDS", 30
            )), 1))
            if result.retryable and not exhausted
            else None
        )
        operation.last_error_code = (
            "retry_exhausted"
            if exhausted
            else str(result.provider_code or "")[:64]
            if result.retryable
            else ""
        )
        operation.completed_at = None if result.retryable else now
        operation.result = {
            "check_id": str(check.id),
            "kind": VerificationCheck.KIND_REGISTRY,
            "outcome": result.outcome,
            "adopted": current,
        }
        operation.save(update_fields=(
            "status", "lease_until", "next_run_at", "last_error_code", "completed_at",
            "result", "updated_at",
        ))
        VerificationAuditEvent.objects.create(
            company=application.company,
            application=application,
            action="review_evidence_refresh_completed",
            revision=application.revision,
            detail={"operation_id": str(operation.id), "check_id": str(check.id), "adopted": current},
        )
    return {"processed": True, "passed": passed and current, "check_id": str(check.id)}


def reconcile_review_evidence_refreshes(*, limit: int = 20, now=None) -> dict:
    now = now or timezone.now()
    VerificationOperation.objects.filter(
        operation_kind=VerificationOperation.KIND_REVIEW_EVIDENCE_REFRESH,
        status=VerificationOperation.STATUS_FAILED,
        attempts__gte=retry_budget(),
    ).exclude(last_error_code="retry_exhausted").update(
        next_run_at=None,
        lease_until=None,
        last_error_code="retry_exhausted",
    )
    ids = list(VerificationOperation.objects.filter(
        operation_kind=VerificationOperation.KIND_REVIEW_EVIDENCE_REFRESH,
        attempts__lt=retry_budget(),
    ).filter(
        Q(status=VerificationOperation.STATUS_PENDING, next_run_at__lte=now)
        | Q(status=VerificationOperation.STATUS_FAILED, next_run_at__lte=now)
        | Q(status=VerificationOperation.STATUS_RUNNING, lease_until__lte=now)
    ).order_by("next_run_at", "created_at").values_list("id", flat=True)[:limit])
    processed = failed = 0
    for operation_id in ids:
        result = process_review_evidence_refresh(operation_id=str(operation_id), now=now)
        if result.get("processed"):
            processed += 1
        else:
            failed += 1
    return {"processed": processed, "failed": failed}


def _notification_sender(sender=None):
    if sender is not None:
        return sender
    dotted_path = str(getattr(settings, "COMPANY_VERIFICATION_NOTIFICATION_SENDER", "") or "").strip()
    if not dotted_path:
        raise RuntimeError("notification_sender_not_configured")
    return import_string(dotted_path)


def send_in_app_verification_notification(delivery: dict):
    """Idempotently persist an applicant receipt and best-effort WebSocket hint."""

    from apps.notification.models import Notification
    from apps.notification.services import NotificationService

    recipient_uid = str(delivery.get("recipient_uid") or "").strip()
    event_type = str(delivery.get("event_type") or "").strip()
    payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
    recipient = User.objects.filter(uid=recipient_uid).first()
    if recipient is None:
        raise RuntimeError("notification_recipient_not_found")
    messages = {
        VerificationNotification.EVENT_SUBMITTED: (
            "企业认证已提交",
            "您的企业认证申请已提交平台审核。",
        ),
        VerificationNotification.EVENT_APPROVED: (
            "企业认证已通过",
            "您的企业认证申请已通过平台审核。",
        ),
        VerificationNotification.EVENT_REJECTED: (
            "企业认证未通过",
            f"您的企业认证申请未通过审核。{str(payload.get('reason') or '').strip()}",
        ),
        VerificationNotification.EVENT_SUPPLEMENT_REQUIRED: (
            "企业认证需要补充材料",
            f"您的企业认证申请需要补充材料。{str(payload.get('reason') or '').strip()}",
        ),
    }
    if event_type not in messages:
        raise RuntimeError("notification_event_unsupported")
    title, content = messages[event_type]
    notification = NotificationService.create_notification(
        user=recipient,
        type=Notification.TYPE_ENTERPRISE_VERIFICATION,
        title=title,
        content=content,
        extraData={
            "application_id": str(delivery.get("application_id") or ""),
            "application_revision": delivery.get("application_revision"),
            "event_type": event_type,
            "status": payload.get("status"),
            "stage": payload.get("stage"),
        },
        deduplication_key=f"enterprise-verification:{delivery.get('notification_id')}",
    )
    try:
        from apps.notification.consumers import send_notification_via_websocket

        send_notification_via_websocket(
            user_id=recipient.id,
            notification_data=notification.get_info(),
        )
    except Exception:
        logger.warning(
            "enterprise verification WebSocket hint failed notification_id=%s",
            notification.id,
        )
    return notification.id


def process_verification_notification(*, notification_id: str, sender=None, now=None) -> dict:
    now = now or timezone.now()
    with transaction.atomic():
        notification = VerificationNotification.objects.select_for_update().select_related(
            "application"
        ).filter(pk=notification_id).first()
        if notification is None:
            return {"processed": False, "reason": "not_found"}
        if notification.status == VerificationNotification.STATUS_SENT:
            return {"processed": False, "reason": "sent"}
        max_attempts = max(int(getattr(
            settings, "COMPANY_VERIFICATION_NOTIFICATION_MAX_ATTEMPTS", 8
        )), 1)
        if (
            notification.status == VerificationNotification.STATUS_SENDING
            and notification.lease_until
            and notification.lease_until > now
        ):
            return {"processed": False, "reason": "leased"}
        if notification.attempts >= max_attempts:
            notification.status = VerificationNotification.STATUS_FAILED
            notification.lease_until = None
            notification.next_run_at = None
            notification.last_error_code = "retry_exhausted"
            notification.save(update_fields=(
                "status", "lease_until", "next_run_at", "last_error_code", "updated_at"
            ))
            return {"processed": False, "reason": "retry_exhausted"}
        notification.status = VerificationNotification.STATUS_SENDING
        notification.attempts += 1
        notification.lease_until = now + timedelta(seconds=60)
        notification.next_run_at = None
        notification.save(update_fields=(
            "status", "attempts", "lease_until", "next_run_at", "updated_at"
        ))
        delivery = {
            "notification_id": str(notification.id),
            "recipient_uid": notification.recipient.uid if notification.recipient else None,
            "event_type": notification.event_type,
            "application_id": str(notification.application.public_id),
            "application_revision": notification.application_revision,
            "payload": dict(notification.payload or {}),
        }
    try:
        _notification_sender(sender)(delivery)
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__) or "notification_failed")[:64]
        with transaction.atomic():
            notification = VerificationNotification.objects.select_for_update().get(pk=notification_id)
            notification.status = VerificationNotification.STATUS_FAILED
            notification.lease_until = None
            notification.next_run_at = (
                None
                if notification.attempts >= max_attempts
                else now + timedelta(seconds=min(
                    3600, 30 * (2 ** min(notification.attempts, 6))
                ))
            )
            notification.last_error_code = code
            notification.save(update_fields=(
                "status", "lease_until", "next_run_at", "last_error_code", "updated_at"
            ))
        return {"processed": False, "reason": "delivery_failed"}
    with transaction.atomic():
        notification = VerificationNotification.objects.select_for_update().get(pk=notification_id)
        notification.status = VerificationNotification.STATUS_SENT
        notification.lease_until = None
        notification.next_run_at = None
        notification.last_error_code = ""
        notification.sent_at = now
        notification.save(update_fields=(
            "status", "lease_until", "next_run_at", "last_error_code", "sent_at", "updated_at"
        ))
    return {"processed": True, "notification_id": str(notification.id)}


def reconcile_verification_notifications(*, limit: int = 50, now=None) -> dict:
    now = now or timezone.now()
    max_attempts = max(int(getattr(
        settings, "COMPANY_VERIFICATION_NOTIFICATION_MAX_ATTEMPTS", 8
    )), 1)
    VerificationNotification.objects.filter(
        attempts__gte=max_attempts,
    ).exclude(status=VerificationNotification.STATUS_SENT).exclude(
        last_error_code="retry_exhausted"
    ).update(
        status=VerificationNotification.STATUS_FAILED,
        lease_until=None,
        next_run_at=None,
        last_error_code="retry_exhausted",
        updated_at=now,
    )
    ids = list(VerificationNotification.objects.filter(attempts__lt=max_attempts).filter(
        Q(status=VerificationNotification.STATUS_PENDING, next_run_at__lte=now)
        | Q(status=VerificationNotification.STATUS_FAILED, next_run_at__lte=now)
        | Q(status=VerificationNotification.STATUS_SENDING, lease_until__lte=now)
    ).order_by("next_run_at", "created_at").values_list("id", flat=True)[:limit])
    processed = failed = 0
    for notification_id in ids:
        result = process_verification_notification(notification_id=str(notification_id), now=now)
        if result.get("processed"):
            processed += 1
        else:
            failed += 1
    return {"processed": processed, "failed": failed}
