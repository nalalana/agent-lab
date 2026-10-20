from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationNotification,
    VerificationOperation,
)
from apps.company.tasks import reconcile_verification_document_cleanups
from apps.company.tests.ev5_test_support import EV5FixtureMixin
from apps.company.verification_evidence import retry_due_manual_evidence
from apps.company.verification_reviews import (
    process_verification_notification,
    reconcile_verification_notifications,
)
from apps.company.verification_runtime import (
    enforce_verification_rate_limit,
    plan_company_draft_retention,
    plan_verification_document_retention,
    verification_job_health,
)
from apps.company.verification_domain import VerificationDomainError


class VerificationJobRecoveryTests(EV5FixtureMixin, TestCase):
    def _operation(self, *, kind, status, attempts=1, lease_until=None, next_run_at=None):
        document = self.application.documents.first()
        return VerificationOperation.objects.create(
            company=self.company,
            application=self.application,
            document=document if kind == VerificationOperation.KIND_DOCUMENT_CLEANUP else None,
            operation_kind=kind,
            application_revision=self.application.revision,
            material_revision=self.application.material_revision,
            idempotency_key=f"{kind}-{VerificationOperation.objects.count()}",
            request_hash="e" * 64,
            status=status,
            attempts=attempts,
            lease_until=lease_until,
            next_run_at=next_run_at,
        )

    @patch("apps.company.verification_evidence.run_registry_verification")
    def test_expired_running_evidence_lease_is_recovered(self, run_registry):
        self.application.stage = CompanyVerification.STAGE_COMPANY_CHECK
        self.application.save(update_fields=("stage",))
        self._operation(
            kind=VerificationOperation.KIND_REGISTRY_CHECK,
            status=VerificationOperation.STATUS_RUNNING,
            lease_until=self.now - timedelta(seconds=1),
        )
        run_registry.return_value = {"status": "pending"}

        result = retry_due_manual_evidence(limit=10, now=self.now)

        self.assertEqual(result, {"processed": 1, "failed": 0})
        run_registry.assert_called_once()

    @override_settings(COMPANY_VERIFICATION_ENABLED=True)
    @patch("apps.company.tasks.retry_document_cleanup")
    def test_expired_running_cleanup_lease_is_recovered(self, retry_cleanup):
        operation = self._operation(
            kind=VerificationOperation.KIND_DOCUMENT_CLEANUP,
            status=VerificationOperation.STATUS_RUNNING,
            lease_until=self.now - timedelta(seconds=1),
        )
        retry_cleanup.return_value = {"cleaned": True}

        result = reconcile_verification_document_cleanups(limit=10)

        self.assertEqual(result["processed"], 1)
        retry_cleanup.assert_called_once_with(operation_id=str(operation.id))

    @override_settings(COMPANY_VERIFICATION_NOTIFICATION_MAX_ATTEMPTS=3)
    def test_notification_retry_budget_has_terminal_condition(self):
        notification = VerificationNotification.objects.create(
            application=self.application,
            recipient=self.applicant,
            event_type=VerificationNotification.EVENT_SUBMITTED,
            application_revision=self.application.revision,
            payload={},
            status=VerificationNotification.STATUS_FAILED,
            attempts=3,
            next_run_at=self.now,
        )
        sender = Mock()

        result = process_verification_notification(
            notification_id=str(notification.id), sender=sender, now=self.now
        )

        self.assertEqual(result["reason"], "retry_exhausted")
        sender.assert_not_called()
        notification.refresh_from_db()
        self.assertIsNone(notification.next_run_at)
        self.assertEqual(notification.last_error_code, "retry_exhausted")

    @override_settings(COMPANY_VERIFICATION_NOTIFICATION_MAX_ATTEMPTS=3)
    def test_reconciler_terminalizes_historical_exhausted_notification(self):
        notification = VerificationNotification.objects.create(
            application=self.application,
            recipient=self.applicant,
            event_type=VerificationNotification.EVENT_SUBMITTED,
            application_revision=self.application.revision,
            payload={},
            status=VerificationNotification.STATUS_FAILED,
            attempts=3,
            next_run_at=self.now,
            last_error_code="legacy_delivery_error",
        )

        result = reconcile_verification_notifications(limit=10, now=self.now)

        self.assertEqual(result, {"processed": 0, "failed": 0})
        notification.refresh_from_db()
        self.assertEqual(notification.last_error_code, "retry_exhausted")
        self.assertIsNone(notification.next_run_at)

    def test_retention_planner_is_terminal_only_and_never_deletes(self):
        document = self.application.documents.first()
        old = self.now - timedelta(days=200)
        type(document).objects.filter(pk=document.pk).update(uploaded_at=old)
        self.application.status = CompanyVerification.STATUS_REJECTED
        self.application.stage = CompanyVerification.STAGE_COMPLETED
        self.application.save(update_fields=("status", "stage"))

        result = plan_verification_document_retention(
            older_than_seconds=180 * 86400, now=self.now
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["deleted_count"], 0)
        self.assertNotIn("storage_key", result["candidates"][0])
        self.assertTrue(type(document).objects.filter(pk=document.pk).exists())

        self.application.status = CompanyVerification.STATUS_VERIFIED
        self.application.save(update_fields=("status",))
        approved_plan = plan_verification_document_retention(
            older_than_seconds=180 * 86400, now=self.now
        )
        self.assertEqual(approved_plan["candidate_count"], 0)

    def test_draft_retention_uses_latest_activity_and_never_deletes(self):
        old = self.now - timedelta(days=120)
        draft = Company.objects.create(
            cid="old-unverified-draft",
            account=self.applicant,
            company_name="Draft",
            created_at=str(old.timestamp()),
            updated_at=str(old.timestamp()),
        )
        plan = plan_company_draft_retention(now=self.now)
        self.assertIn(
            draft.cid,
            {item["company_id"] for item in plan["candidates"]},
        )
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["deleted_count"], 0)
        self.assertTrue(Company.objects.filter(pk=draft.pk).exists())

        VerificationAuditEvent.objects.create(
            company=draft,
            actor=self.applicant,
            action="draft_activity",
            revision=0,
            detail={},
        )
        refreshed = plan_company_draft_retention(now=self.now)
        self.assertNotIn(
            draft.cid,
            {item["company_id"] for item in refreshed["candidates"]},
        )

    def test_health_snapshot_contains_aggregates_not_sensitive_payloads(self):
        self._operation(
            kind=VerificationOperation.KIND_DOCUMENT_CLEANUP,
            status=VerificationOperation.STATUS_RUNNING,
            lease_until=self.now - timedelta(seconds=1),
        )
        result = verification_job_health(now=self.now)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["stale_operation_leases"], 1)
        self.assertNotIn("candidates", result)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    COMPANY_VERIFICATION_RATE_LIMITS={"start": (1, 60)},
)
class VerificationRateLimitTests(TestCase):
    def test_fixed_window_returns_explicit_rate_limited_error(self):
        enforce_verification_rate_limit(scope="start", principal="42", resource="company")
        with self.assertRaises(VerificationDomainError) as captured:
            enforce_verification_rate_limit(scope="start", principal="42", resource="company")
        self.assertEqual(captured.exception.code, "rate_limited")
        self.assertEqual(captured.exception.status_code, 429)
