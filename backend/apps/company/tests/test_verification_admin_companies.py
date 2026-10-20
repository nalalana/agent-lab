from django.test import TestCase

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationOperation,
)
from apps.company.permissions import can_access_formal_company
from apps.company.tests.ev5_test_support import EV5FixtureMixin
from apps.company.verification_reviews import (
    get_review_application,
    list_registered_companies,
    list_verification_audit_events,
    restore_company_service,
    restore_company_verification,
    suspend_company_service,
    suspend_company_verification,
)
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions


class VerificationAdminCompanyTests(EV5FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.application.status = CompanyVerification.STATUS_VERIFIED
        self.application.stage = CompanyVerification.STAGE_COMPLETED
        self.application.finished_at = self.now
        self.application.save(update_fields=("status", "stage", "finished_at"))
        self.company.verification_status = Company.VERIFICATION_STATUS_VERIFIED
        self.company.verification_provider = Company.VERIFICATION_PROVIDER_TENCENT_MANUAL_V1
        self.company.verified_at = self.now
        self.company.verified_verification = self.application
        self.company.save(update_fields=(
            "verification_status", "verification_provider", "verified_at",
            "verified_verification",
        ))
        self.reviewer.can_suspend_verification = True
        self.reviewer.save(update_fields=("can_suspend_verification",))
        principal = get_or_create_principal(Principal.TYPE_USER, self.applicant.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.root)

    def test_lists_all_registered_companies_and_keeps_approved_history_readable(self):
        Company.objects.create(
            cid="registered-without-application",
            company_name="Registered draft",
            created_at="1",
            updated_at="1",
        )

        result = list_registered_companies(
            user=self.reviewer_user, status="all", now=self.now
        )

        self.assertEqual(result["total"], 2)
        rows = {item["company_id"]: item for item in result["items"]}
        self.assertEqual(
            rows[self.company.cid]["application_id"],
            str(self.application.public_id),
        )
        self.assertIsNone(
            rows["registered-without-application"]["application_id"]
        )
        detail = get_review_application(
            user=self.reviewer_user,
            application_id=str(self.application.public_id),
            now=self.now,
        )
        self.assertEqual(detail["status"], CompanyVerification.STATUS_VERIFIED)
        self.assertIn("revoke_verification", detail["administrative_actions"])
        self.assertIn("suspend_service", detail["administrative_actions"])

        self.company.verification_suspended_at = self.now
        self.company.verification_suspension_reason = "identity_dispute"
        self.company.save(update_fields=(
            "verification_suspended_at", "verification_suspension_reason"
        ))
        historical = list_registered_companies(
            user=self.reviewer_user, status="verified", now=self.now
        )
        self.assertEqual(historical["total"], 1)
        self.assertTrue(historical["items"][0]["verification_revoked"])

    def test_service_suspension_is_independent_and_audited(self):
        self.assertTrue(can_access_formal_company(self.company, self.applicant))

        result = suspend_company_service(
            user=self.reviewer_user,
            company_id=self.company.cid,
            reason_code="regulatory_request",
            ticket_reference="SERVICE-001",
            idempotency_key="service-suspend-001",
            expected_application_id=self.application.public_id,
            now=self.now,
        )

        self.company.refresh_from_db()
        self.assertTrue(result["service_suspended"])
        self.assertFalse(result["verification_revoked"])
        self.assertIsNotNone(self.company.service_suspended_at)
        self.assertIsNone(self.company.verification_suspended_at)
        self.assertEqual(
            self.company.verification_status,
            Company.VERIFICATION_STATUS_VERIFIED,
        )
        self.assertFalse(can_access_formal_company(self.company, self.applicant))
        self.assertTrue(VerificationAuditEvent.objects.filter(
            company=self.company, action="company_service_suspended"
        ).exists())
        self.assertTrue(VerificationOperation.objects.filter(
            company=self.company,
            operation_kind=VerificationOperation.KIND_SUSPEND_SERVICE,
        ).exists())
        self.assertEqual(Grant.objects.count(), 1)
    def test_service_restore_reenables_access_without_changing_grants(self):
        suspend_company_service(
            user=self.reviewer_user,
            company_id=self.company.cid,
            reason_code="regulatory_request",
            ticket_reference="SERVICE-002",
            idempotency_key="service-suspend-002",
            expected_application_id=self.application.public_id,
            now=self.now,
        )

        result = restore_company_service(
            user=self.reviewer_user,
            company_id=self.company.cid,
            reason_code="issue_resolved",
            ticket_reference="SERVICE-RESTORE-002",
            idempotency_key="service-restore-002",
            expected_application_id=self.application.public_id,
            now=self.now,
        )

        self.company.refresh_from_db()
        self.assertFalse(result["service_suspended"])
        self.assertIsNone(self.company.service_suspended_at)
        self.assertEqual(self.company.service_suspension_reason, "")
        self.assertTrue(can_access_formal_company(self.company, self.applicant))
        self.assertTrue(VerificationAuditEvent.objects.filter(
            company=self.company, action="company_service_restored"
        ).exists())
        self.assertTrue(VerificationOperation.objects.filter(
            company=self.company,
            operation_kind=VerificationOperation.KIND_RESTORE_SERVICE,
        ).exists())
        self.assertEqual(Grant.objects.count(), 1)

    def test_verification_restore_and_audit_timeline_are_preserved(self):
        self.application.submitted_at = self.now
        self.application.save(update_fields=("submitted_at",))
        suspend_company_verification(
            user=self.reviewer_user,
            company_id=self.company.cid,
            reason_code="identity_dispute",
            ticket_reference="VERIFY-003",
            idempotency_key="verification-suspend-003",
            expected_application_id=self.application.public_id,
            now=self.now,
        )
        self.company.refresh_from_db()
        self.assertFalse(can_access_formal_company(self.company, self.applicant))
        suspended_detail = get_review_application(
            user=self.reviewer_user,
            application_id=str(self.application.public_id),
            now=self.now,
        )
        self.assertIn(
            "restore_verification", suspended_detail["administrative_actions"]
        )

        result = restore_company_verification(
            user=self.reviewer_user,
            company_id=self.company.cid,
            reason_code="appeal_approved",
            ticket_reference="VERIFY-RESTORE-003",
            idempotency_key="verification-restore-003",
            expected_application_id=self.application.public_id,
            now=self.now,
        )

        self.company.refresh_from_db()
        self.assertFalse(result["verification_revoked"])
        self.assertIsNone(self.company.verification_suspended_at)
        self.assertTrue(can_access_formal_company(self.company, self.applicant))
        self.assertEqual(Grant.objects.count(), 1)
        detail = get_review_application(
            user=self.reviewer_user,
            application_id=str(self.application.public_id),
            now=self.now,
        )
        history_actions = {item["action"] for item in detail["review_history"]}
        self.assertIn("application_submitted_for_review", history_actions)
        self.assertIn("verification_suspended", history_actions)
        self.assertIn("verification_restored", history_actions)

        audit = list_verification_audit_events(
            user=self.reviewer_user,
            category="administration",
            now=self.now,
        )
        self.assertEqual(audit["total"], 2)
        self.assertEqual(
            {item["action"] for item in audit["items"]},
            {"verification_suspended", "verification_restored"},
        )
        self.assertTrue(all(item["category_label"] for item in audit["items"]))
