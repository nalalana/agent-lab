import time

from django.db.models.deletion import ProtectedError
from django.test import TestCase

from apps.agent_runtime.alerts import _alert_recipients
from apps.company.models import Company, CompanyClaim, CompanyMember, CompanyVerification
from apps.user.models import User


class CompanyVerificationSchemaTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid='phase1-schema-user',
            username='phase1-schema-user',
            created_at=now,
            updated_at=now,
        )
        self.company = Company.objects.create(
            account=self.user,
            cid='phase1-schema-company',
            company_name='Phase 1 Schema Company',
            created_at=now,
            updated_at=now,
        )

    def test_company_verification_fields_have_safe_defaults(self):
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)
        self.assertEqual(self.company.verification_provider, '')
        self.assertIsNone(self.company.proxy_organization_open_id)
        self.assertIsNone(self.company.verified_at)

    def test_deleting_created_by_preserves_company_and_get_info_is_null_safe(self):
        company_id = self.company.id

        self.user.delete()

        company = Company.objects.get(id=company_id)
        self.assertIsNone(company.account)
        self.assertIsNone(company.get_info()['account'])

    def test_verification_and_claim_defaults_and_statuses(self):
        verification = CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.user,
        )
        claim = CompanyClaim.objects.create(
            company=self.company,
            applicant_user=self.user,
            verification=verification,
        )

        self.assertEqual(verification.provider, CompanyVerification.PROVIDER_TENCENT_ESS)
        self.assertEqual(verification.status, CompanyVerification.STATUS_INITIATING)
        self.assertEqual(claim.status, CompanyClaim.STATUS_PENDING)
        self.assertEqual(
            {value for value, _ in CompanyVerification.STATUS_CHOICES},
            {'initiating', 'pending', 'verified', 'rejected', 'expired', 'cancelled'},
        )
        self.assertEqual(
            {value for value, _ in CompanyClaim.STATUS_CHOICES},
            {'pending', 'approved', 'rejected', 'expired', 'cancelled'},
        )

    def test_deleting_applicant_preserves_verification_and_claim_history(self):
        verification = CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.user,
        )
        claim = CompanyClaim.objects.create(
            company=self.company,
            applicant_user=self.user,
            verification=verification,
        )

        self.user.delete()

        verification.refresh_from_db()
        claim.refresh_from_db()
        self.assertIsNone(verification.applicant_user)
        self.assertIsNone(claim.applicant_user)

    def test_claim_protects_verification_history(self):
        verification = CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.user,
        )
        claim = CompanyClaim.objects.create(
            company=self.company,
            applicant_user=self.user,
            verification=verification,
        )

        with self.assertRaises(ProtectedError):
            verification.delete()

        claim.refresh_from_db()
        self.assertEqual(claim.verification_id, verification.id)

    def test_alert_recipients_handle_null_created_by_and_filter_members(self):
        active_admin = User.objects.create(
            uid='phase1-active-admin',
            username='phase1-active-admin',
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        inactive_owner = User.objects.create(
            uid='phase1-inactive-owner',
            username='phase1-inactive-owner',
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        active_member = User.objects.create(
            uid='phase1-active-member',
            username='phase1-active-member',
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        CompanyMember.objects.create(
            company=self.company,
            user=active_admin,
            memberUid=active_admin.uid,
            name=active_admin.username,
            email='active-admin@example.com',
            role='admin',
            status=CompanyMember.STATUS_ACTIVE,
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        CompanyMember.objects.create(
            company=self.company,
            user=inactive_owner,
            memberUid=inactive_owner.uid,
            name=inactive_owner.username,
            email='inactive-owner@example.com',
            role='owner',
            status=CompanyMember.STATUS_DISABLED,
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        CompanyMember.objects.create(
            company=self.company,
            user=active_member,
            memberUid=active_member.uid,
            name=active_member.username,
            email='active-member@example.com',
            role='member',
            status=CompanyMember.STATUS_ACTIVE,
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        CompanyMember.objects.create(
            company=self.company,
            user=None,
            name='Unbound owner invitation',
            email='unbound-owner@example.com',
            role='owner',
            status=CompanyMember.STATUS_ACTIVE,
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        self.company.account = None
        self.company.save(update_fields=['account'])

        self.assertEqual(_alert_recipients(self.company), (active_admin,))
