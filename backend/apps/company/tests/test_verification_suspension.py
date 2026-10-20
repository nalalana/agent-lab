from unittest.mock import patch

from django.test import TestCase

from apps.company.models import (
    CompanyVerification,
    TransparentCompany,
    VerificationAuditEvent,
    VerificationOperation,
)
from apps.company.permissions import can_access_company_draft, can_access_formal_company
from apps.company.tests.ev5_test_support import EV5FixtureMixin
from apps.company.verification_domain import VerificationDomainError
from apps.company.verification_reviews import suspend_company_verification
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions


class VerificationSuspensionTests(EV5FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.application.status = CompanyVerification.STATUS_VERIFIED
        self.application.stage = CompanyVerification.STAGE_COMPLETED
        self.application.save(update_fields=("status", "stage"))
        self.company.verification_status = self.company.VERIFICATION_STATUS_VERIFIED
        self.company.verification_provider = self.company.VERIFICATION_PROVIDER_TENCENT_MANUAL_V1
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

    def _suspend(self, **overrides):
        values = {
            "user": self.reviewer_user,
            "company_id": self.company.cid,
            "reason_code": "identity_dispute",
            "ticket_reference": "EV6-TEST-001",
            "idempotency_key": "suspend-001",
            "expected_application_id": self.application.public_id,
            "now": self.now,
        }
        values.update(overrides)
        return suspend_company_verification(**values)

    def test_suspension_preserves_verified_fact_and_existing_grant_but_blocks_access(self):
        grant_count = Grant.objects.count()
        self.assertTrue(can_access_formal_company(self.company, self.applicant))

        result = self._suspend()

        self.company.refresh_from_db()
        self.application.refresh_from_db()
        self.assertTrue(result["verification_suspended"])
        self.assertFalse(result["iam_grants_changed"])
        self.assertEqual(self.company.verification_status, self.company.VERIFICATION_STATUS_VERIFIED)
        self.assertEqual(self.company.verified_verification_id, self.application.id)
        self.assertEqual(self.application.status, CompanyVerification.STATUS_VERIFIED)
        self.assertEqual(Grant.objects.count(), grant_count)
        self.assertFalse(can_access_formal_company(self.company, self.applicant))
        self.assertFalse(can_access_company_draft(self.company, self.applicant))
        audit = VerificationAuditEvent.objects.get(action="verification_suspended")
        self.assertEqual(audit.detail["ticket_reference"], "EV6-TEST-001")
        self.assertFalse(audit.detail["iam_grants_changed"])

    def test_suspension_is_idempotent_and_conflicting_payload_is_rejected(self):
        first = self._suspend()
        replay = self._suspend()
        self.assertEqual(replay, first)
        self.assertEqual(
            VerificationOperation.objects.filter(
                operation_kind=VerificationOperation.KIND_SUSPEND_VERIFICATION
            ).count(),
            1,
        )
        with self.assertRaises(VerificationDomainError) as captured:
            self._suspend(reason_code="fraud_report")
        self.assertEqual(captured.exception.code, "idempotency_conflict")

    def test_separate_capability_and_conflict_rules_are_enforced(self):
        self.reviewer.can_suspend_verification = False
        self.reviewer.save(update_fields=("can_suspend_verification",))
        with self.assertRaises(VerificationDomainError) as captured:
            self._suspend()
        self.assertEqual(captured.exception.code, "suspension_forbidden")

        self.reviewer.can_suspend_verification = True
        self.reviewer.save(update_fields=("can_suspend_verification",))
        self.application.applicant_user = self.reviewer_user
        self.application.save(update_fields=("applicant_user",))
        with self.assertRaises(VerificationDomainError) as captured:
            self._suspend(idempotency_key="suspend-conflict")
        self.assertEqual(captured.exception.code, "review_self_forbidden")

    @patch("apps.company.services.CompanyService._build_client_entry_payload")
    def test_suspended_company_is_not_available_to_public_agent_listing(self, _payload):
        from apps.company.services import CompanyService

        TransparentCompany.objects.create(company=self.company, prompts="test")
        self._suspend()
        ok, message = CompanyService.list_transparent_company_agents(
            cid=self.company.cid,
            public_only=True,
        )
        self.assertFalse(ok)
        self.assertEqual(message, "透明公司不存在")
        _payload.assert_not_called()
