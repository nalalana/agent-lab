"""Asynchronous maintenance for company verification workflows."""

import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.company.models import CompanyVerification, VerificationDocument, VerificationOperation
from apps.company.providers import get_enterprise_verification_provider
from apps.company.providers.base import VerificationProviderError
from apps.company.verification import (
    VerificationFlowError,
    expire_stale_initiating_verifications,
    pending_verifications_for_reconciliation,
    refresh_company_verification,
)
from apps.company.verification_domain import expire_due_manual_verifications
from apps.company.verification_evidence import retry_due_manual_evidence
from apps.company.verification_documents import (
    process_authorization_document,
    retry_document_cleanup,
)
from apps.company.verification_domain import VerificationDomainError
from apps.company.verification_reviews import (
    process_review_evidence_refresh,
    process_verification_notification,
    reconcile_review_evidence_refreshes,
    reconcile_verification_notifications,
)
from apps.company.verification_runtime import (
    plan_company_draft_retention,
    plan_verification_document_retention,
    retry_budget,
    verification_job_health,
)


logger = logging.getLogger("touming")


@shared_task(name="apps.company.tasks.expire_stale_company_verifications")
def expire_stale_company_verifications(limit: int = 100):
    return expire_stale_initiating_verifications(limit=limit)


@shared_task(name="apps.company.tasks.expire_manual_company_verifications")
def expire_manual_company_verifications(limit: int = 100):
    return expire_due_manual_verifications(limit=limit)


@shared_task(name="apps.company.tasks.reconcile_manual_verification_evidence")
def reconcile_manual_verification_evidence(limit: int = 50):
    if not getattr(settings, "COMPANY_VERIFICATION_ENABLED", False):
        return {"processed": 0, "failed": 0, "disabled": True}
    result = retry_due_manual_evidence(limit=limit)
    result["disabled"] = False
    return result


@shared_task(name="apps.company.tasks.process_verification_document")
def process_verification_document_task(document_id: str):
    if not getattr(settings, "COMPANY_VERIFICATION_ENABLED", False):
        return {"document_id": str(document_id), "disabled": True}
    try:
        result = process_authorization_document(document_id=str(document_id))
        result["disabled"] = False
        return result
    except VerificationDomainError as exc:
        logger.warning(
            "[company-verification-document] document_id=%s error_code=%s",
            document_id,
            exc.code,
        )
        return {"document_id": str(document_id), "error_code": exc.code, "disabled": False}


@shared_task(name="apps.company.tasks.reconcile_pending_verification_documents")
def reconcile_pending_verification_documents(limit: int = 20):
    if not getattr(settings, "COMPANY_VERIFICATION_ENABLED", False):
        return {"processed": 0, "failed": 0, "disabled": True}
    candidates = VerificationDocument.objects.filter(
        application__provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        application__status=CompanyVerification.STATUS_PENDING,
        application__stage=CompanyVerification.STAGE_DOCUMENT_CHECK,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("uploaded_at").values_list("id", flat=True)[:limit]
    processed = failed = 0
    for document_id in candidates:
        try:
            process_authorization_document(document_id=str(document_id))
            processed += 1
        except VerificationDomainError:
            failed += 1
    return {"processed": processed, "failed": failed, "disabled": False}


@shared_task(name="apps.company.tasks.reconcile_verification_document_cleanups")
def reconcile_verification_document_cleanups(limit: int = 20):
    if not getattr(settings, "COMPANY_VERIFICATION_ENABLED", False):
        return {"processed": 0, "failed": 0, "disabled": True}
    maximum = retry_budget()
    VerificationOperation.objects.filter(
        operation_kind=VerificationOperation.KIND_DOCUMENT_CLEANUP,
        status=VerificationOperation.STATUS_FAILED,
        attempts__gte=maximum,
    ).exclude(last_error_code="retry_exhausted").update(
        next_run_at=None,
        lease_until=None,
        last_error_code="retry_exhausted",
    )
    candidates = list(VerificationOperation.objects.filter(
        operation_kind=VerificationOperation.KIND_DOCUMENT_CLEANUP,
        attempts__lt=maximum,
    ).filter(
        Q(status=VerificationOperation.STATUS_FAILED, next_run_at__lte=timezone.now())
        | Q(status=VerificationOperation.STATUS_RUNNING, lease_until__lte=timezone.now())
    ).order_by("next_run_at", "created_at").values_list("id", flat=True)[:limit])
    processed = failed = 0
    for operation_id in candidates:
        result = retry_document_cleanup(operation_id=str(operation_id))
        if result.get("cleaned"):
            processed += 1
        else:
            failed += 1
    return {"processed": processed, "failed": failed, "disabled": False}


@shared_task(name="apps.company.tasks.process_review_evidence_refresh")
def process_review_evidence_refresh_task(operation_id: str):
    if not getattr(settings, "COMPANY_VERIFICATION_ENABLED", False):
        return {"processed": False, "disabled": True}
    result = process_review_evidence_refresh(operation_id=str(operation_id))
    result["disabled"] = False
    return result


@shared_task(name="apps.company.tasks.reconcile_review_evidence_refreshes")
def reconcile_review_evidence_refreshes_task(limit: int = 20):
    if not getattr(settings, "COMPANY_VERIFICATION_ENABLED", False):
        return {"processed": 0, "failed": 0, "disabled": True}
    result = reconcile_review_evidence_refreshes(limit=limit)
    result["disabled"] = False
    return result


@shared_task(name="apps.company.tasks.process_verification_notification")
def process_verification_notification_task(notification_id: str):
    result = process_verification_notification(notification_id=str(notification_id))
    result["disabled"] = False
    return result


@shared_task(name="apps.company.tasks.reconcile_verification_notifications")
def reconcile_verification_notifications_task(limit: int = 50):
    result = reconcile_verification_notifications(limit=limit)
    result["disabled"] = False
    return result


@shared_task(name="apps.company.tasks.plan_verification_document_retention")
def plan_verification_document_retention_task(limit: int = 100):
    """Scheduled document and draft inventories; neither path deletes data."""

    result = plan_verification_document_retention(limit=limit)
    drafts = plan_company_draft_retention(limit=limit)
    logger.info(
        "[company-verification-retention] dry_run=true document_count=%s draft_count=%s",
        result["candidate_count"],
        drafts["candidate_count"],
    )
    return {
        "dry_run": True,
        "candidate_count": result["candidate_count"],
        "draft_candidate_count": drafts["candidate_count"],
        "draft_candidates": drafts["candidates"],
        "deleted_count": 0,
    }


@shared_task(name="apps.company.tasks.snapshot_verification_job_health")
def snapshot_verification_job_health_task():
    result = verification_job_health()
    logger.info(
        "[company-verification-health] healthy=%s stale_operation_leases=%s "
        "stale_notification_leases=%s exhausted_operations=%s",
        result["healthy"],
        result["stale_operation_leases"],
        result["stale_notification_leases"],
        result["exhausted_operations"],
    )
    return result


@shared_task(name="apps.company.tasks.reconcile_pending_company_verifications")
def reconcile_pending_company_verifications(limit: int = 50):
    if not getattr(settings, "COMPANY_VERIFICATION_ESS_RECONCILE_ENABLED", False):
        return {"processed": 0, "failed": 0, "disabled": True}
    try:
        provider = get_enterprise_verification_provider(
            CompanyVerification.PROVIDER_TENCENT_ESS
        )
    except VerificationProviderError as exc:
        logger.error(
            "[company-verification-reconcile] provider_error_code=%s",
            exc.code,
        )
        return {"processed": 0, "failed": 0, "provider_unavailable": True}

    processed = 0
    failed = 0
    for verification in pending_verifications_for_reconciliation(limit=limit):
        try:
            refresh_company_verification(
                company=verification.company,
                verification=verification,
                provider=provider,
            )
            processed += 1
        except VerificationFlowError as exc:
            failed += 1
            logger.warning(
                "[company-verification-reconcile] company_id=%s verification_id=%s "
                "failure_reason=%s",
                verification.company.cid,
                verification.id,
                exc.code,
            )
    return {"processed": processed, "failed": failed, "provider_unavailable": False}
