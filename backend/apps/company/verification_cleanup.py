"""Narrow purge support for verification data owned by draft companies."""

from __future__ import annotations

from apps.company.models import (
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationFaceSession,
    VerificationNotification,
    VerificationOperation,
    VerificationReview,
    VerificationTranscription,
)


class DraftVerificationPurgeError(Exception):
    pass


def _raw_delete(queryset) -> int:
    """Delete a protected history queryset after the rollback guard has passed."""

    return queryset._raw_delete(queryset.db)


def purge_draft_verification_history(*, company) -> tuple[str, ...]:
    """Remove every verification attempt before its draft company is deleted.

    Verification history is append-only during normal operation. This is the one
    deliberate exception: the creator may delete an enterprise that never became
    verified, in which case no orphaned application or identity evidence remains.
    """

    if company.verification_status == company.VERIFICATION_STATUS_VERIFIED:
        raise DraftVerificationPurgeError(
            "verified_company_history_cannot_be_purged"
        )

    applications = list(
        CompanyVerification.objects.select_for_update().filter(company=company)
    )
    if not applications:
        return ()

    application_ids = [application.pk for application in applications]
    documents = list(
        VerificationDocument.objects.filter(application_id__in=application_ids)
    )
    storage_keys = tuple(dict.fromkeys(
        key
        for document in documents
        for key in (document.storage_key, document.preview_storage_key)
        if key
    ))
    reviews = VerificationReview.objects.filter(
        application_id__in=application_ids
    )
    review_ids = list(reviews.values_list("pk", flat=True))
    if review_ids:
        _raw_delete(
            VerificationReview.checks.through.objects.filter(
                verificationreview_id__in=review_ids
            )
        )
        _raw_delete(
            VerificationReview.documents.through.objects.filter(
                verificationreview_id__in=review_ids
            )
        )

    _raw_delete(
        VerificationNotification.objects.filter(application_id__in=application_ids)
    )
    _raw_delete(
        VerificationTranscription.objects.filter(application_id__in=application_ids)
    )
    _raw_delete(reviews)
    _raw_delete(VerificationOperation.objects.filter(company=company))
    _raw_delete(VerificationAuditEvent.objects.filter(company=company))
    _raw_delete(VerificationCheck.objects.filter(application_id__in=application_ids))
    _raw_delete(
        VerificationDocument.objects.filter(application_id__in=application_ids)
    )
    _raw_delete(
        VerificationFaceSession.objects.filter(application_id__in=application_ids)
    )
    _raw_delete(CompanyVerification.objects.filter(pk__in=application_ids))
    return storage_keys
