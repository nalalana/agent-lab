"""EV-6 runtime safeguards: rate limits, job health and retention dry runs."""

from __future__ import annotations

from calendar import monthrange
import logging
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationDocument,
    VerificationNotification,
    VerificationOperation,
)
from apps.company.retention_policy import DRAFT_INACTIVE_MONTHS
from apps.company.verification_domain import VerificationDomainError


logger = logging.getLogger("touming")

DEFAULT_RATE_LIMITS = {
    "start": (5, 60),
    "query": (60, 60),
    "upload": (10, 60),
    "submit": (10, 60),
    "review": (60, 60),
    "suspend": (5, 60),
}


def enforce_verification_rate_limit(*, scope: str, principal: str, resource: str = ""):
    """Apply a fail-open fixed-window limit without putting PII in cache keys."""

    configured = getattr(settings, "COMPANY_VERIFICATION_RATE_LIMITS", {}) or {}
    limit, window = configured.get(scope, DEFAULT_RATE_LIMITS.get(scope, (30, 60)))
    limit, window = max(int(limit), 0), max(int(window), 1)
    if not limit:
        return
    bucket = int(timezone.now().timestamp()) // window
    key = f"company-verification-rate:{scope}:{principal}:{resource}:{bucket}"
    try:
        if cache.add(key, 1, timeout=window + 2):
            count = 1
        else:
            count = cache.incr(key)
    except Exception:
        logger.warning("company verification rate-limit cache unavailable scope=%s", scope)
        return
    if count > limit:
        raise VerificationDomainError("rate_limited", status_code=429)


def retry_budget() -> int:
    return max(int(getattr(settings, "COMPANY_VERIFICATION_JOB_MAX_ATTEMPTS", 8)), 1)


def plan_verification_document_retention(*, older_than_seconds=None, limit=100, now=None):
    """Return deletion candidates only; this function never reads or deletes object data."""

    now = now or timezone.now()
    age = int(older_than_seconds or getattr(
        settings, "COMPANY_VERIFICATION_RETENTION_DRY_RUN_AGE_SECONDS", 180 * 86400
    ))
    age = max(age, 86400)
    cutoff = now - timedelta(seconds=age)
    candidates = VerificationDocument.objects.select_related("application").filter(
        uploaded_at__lte=cutoff,
        application__status__in=(
            CompanyVerification.STATUS_REJECTED,
            CompanyVerification.STATUS_EXPIRED,
            CompanyVerification.STATUS_CANCELLED,
        ),
    ).order_by("uploaded_at", "id")[: max(min(int(limit), 1000), 1)]
    items = [
        {
            "document_id": str(document.id),
            "application_id": str(document.application.public_id),
            "application_status": document.application.status,
            "uploaded_at": document.uploaded_at.isoformat(),
            "size_bytes": document.size_bytes,
        }
        for document in candidates
    ]
    return {
        "dry_run": True,
        "cutoff": cutoff.isoformat(),
        "candidate_count": len(items),
        "candidates": items,
        "deleted_count": 0,
    }


def _months_before(moment: datetime, months: int) -> datetime:
    year, zero_based_month = divmod(moment.year * 12 + moment.month - 1 - months, 12)
    month = zero_based_month + 1
    return moment.replace(
        year=year,
        month=month,
        day=min(moment.day, monthrange(year, month)[1]),
    )


def _as_utc(value: datetime) -> datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(value, datetime_timezone.utc)
    return value.astimezone(datetime_timezone.utc)


def _company_epoch(value: str | None) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=datetime_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def plan_company_draft_retention(*, limit=100, now=None):
    """List unverified drafts idle for three calendar months; never delete them."""

    now = _as_utc(now or timezone.now())
    cutoff = _months_before(now, DRAFT_INACTIVE_MONTHS)
    ranked = []
    unassessable_count = 0
    drafts = Company.objects.filter(
        verification_status=Company.VERIFICATION_STATUS_UNVERIFIED,
        verified_at__isnull=True,
        verified_verification_id__isnull=True,
        account_id__isnull=False,
    ).only("id", "cid", "created_at", "updated_at")
    for company in drafts.iterator(chunk_size=200):
        # A company with any successful verification is not a disposable draft,
        # even if a later state transition marked it unverified.
        if CompanyVerification.objects.filter(
            company_id=company.pk,
            status=CompanyVerification.STATUS_VERIFIED,
        ).exists():
            continue
        application_activity = CompanyVerification.objects.filter(
            company_id=company.pk,
        ).aggregate(
            started=Max("started_at"),
            refreshed=Max("last_refreshed_at"),
            submitted=Max("submitted_at"),
            finished=Max("finished_at"),
        )
        audit_activity = VerificationAuditEvent.objects.filter(
            company_id=company.pk,
        ).aggregate(latest=Max("created_at"))["latest"]
        operation_activity = VerificationOperation.objects.filter(
            company_id=company.pk,
        ).aggregate(latest=Max("updated_at"))["latest"]
        activities = [
            _company_epoch(company.created_at),
            _company_epoch(company.updated_at),
            *application_activity.values(),
            audit_activity,
            operation_activity,
        ]
        observed = [_as_utc(value) for value in activities if value is not None]
        if not observed:
            unassessable_count += 1
            continue
        last_activity = max(observed)
        if last_activity <= cutoff:
            ranked.append((last_activity, str(company.cid)))
    ranked.sort()
    items = [
        {"company_id": cid, "last_activity_at": last_activity.isoformat()}
        for last_activity, cid in ranked[: max(min(int(limit), 1000), 1)]
    ]
    return {
        "dry_run": True,
        "cutoff": cutoff.isoformat(),
        "inactive_months": DRAFT_INACTIVE_MONTHS,
        "candidate_count": len(items),
        "unassessable_count": unassessable_count,
        "candidates": items,
        "deleted_count": 0,
    }


def verification_job_health(*, now=None):
    """Expose aggregate operational metadata only; no identity or object keys."""

    now = now or timezone.now()
    operation_counts = {
        f"{item['operation_kind']}:{item['status']}": item["count"]
        for item in VerificationOperation.objects.values("operation_kind", "status")
        .annotate(count=Count("id"))
        .order_by("operation_kind", "status")
    }
    notification_counts = {
        item["status"]: item["count"]
        for item in VerificationNotification.objects.values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    }
    stale_operation_leases = VerificationOperation.objects.filter(
        status=VerificationOperation.STATUS_RUNNING, lease_until__lte=now
    ).count()
    stale_notification_leases = VerificationNotification.objects.filter(
        status=VerificationNotification.STATUS_SENDING, lease_until__lte=now
    ).count()
    exhausted_operations = VerificationOperation.objects.filter(
        status=VerificationOperation.STATUS_FAILED,
    ).filter(
        Q(attempts__gte=retry_budget()) | Q(last_error_code="retry_exhausted")
    ).count()
    exhausted_notifications = VerificationNotification.objects.filter(
        status=VerificationNotification.STATUS_FAILED,
        last_error_code="retry_exhausted",
    ).count()
    return {
        "observed_at": now.isoformat(),
        "operation_counts": operation_counts,
        "notification_counts": notification_counts,
        "stale_operation_leases": stale_operation_leases,
        "stale_notification_leases": stale_notification_leases,
        "exhausted_operations": exhausted_operations,
        "exhausted_notifications": exhausted_notifications,
        "healthy": not (
            stale_operation_leases
            or stale_notification_leases
            or exhausted_operations
            or exhausted_notifications
        ),
    }
