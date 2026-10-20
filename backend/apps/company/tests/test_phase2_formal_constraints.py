import time
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.company.models import Company, CompanyClaim, CompanyMember, CompanyVerification
from apps.company.services import CompanyService
from apps.company.validators import normalize_social_credit_code, validate_social_credit_code
from apps.user.models import User


VALID_REGISTRATION_NO = '91350211M000100Y46'


class FormalCompanyConstraintTests(TestCase):
    def setUp(self):
        self.now = str(time.time())[:20]
        self.user = self._user('phase2-user', 'phase2@example.com')
        self.company = self._company('phase2-company')

    def _user(self, uid, email=''):
        return User.objects.create(
            uid=uid,
            username=uid,
            email=email,
            created_at=self.now,
            updated_at=self.now,
        )

    def _company(self, cid, **kwargs):
        return Company.objects.create(
            cid=cid,
            company_name=cid,
            created_at=self.now,
            updated_at=self.now,
            **kwargs,
        )

    def assert_integrity_error(self, create):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create()

    def test_domain_validator_normalizes_and_checks_checksum(self):
        self.assertEqual(
            normalize_social_credit_code(' 91350211-m000100y46 '),
            VALID_REGISTRATION_NO,
        )
        self.assertEqual(
            validate_social_credit_code('91350211 m000100y46'),
            VALID_REGISTRATION_NO,
        )
        with self.assertRaises(ValidationError):
            validate_social_credit_code('91350211M000100Y45')

    def test_registration_no_database_format_and_global_uniqueness(self):
        self._company('registered-company', registration_no=VALID_REGISTRATION_NO)
        self.assert_integrity_error(
            lambda: self._company('duplicate-registration', registration_no=VALID_REGISTRATION_NO)
        )
        self.assert_integrity_error(
            lambda: self._company('noncanonical-registration', registration_no=VALID_REGISTRATION_NO.lower())
        )

    def test_database_format_does_not_claim_to_validate_checksum(self):
        company = self._company('format-only-registration', registration_no='91350211M000100Y45')
        self.assertEqual(company.registration_no, '91350211M000100Y45')

    def test_proxy_organization_open_id_is_unique_when_nonblank(self):
        self._company('proxy-company', proxy_organization_open_id='proxy-org-1')
        self.assert_integrity_error(
            lambda: self._company('duplicate-proxy', proxy_organization_open_id='proxy-org-1')
        )
        self._company('blank-proxy-one', proxy_organization_open_id='')
        self._company('blank-proxy-two', proxy_organization_open_id='')

    def test_only_one_active_verification_per_company(self):
        CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.user,
            status=CompanyVerification.STATUS_INITIATING,
        )
        self.assert_integrity_error(
            lambda: CompanyVerification.objects.create(
                company=self.company,
                applicant_user=self.user,
                status=CompanyVerification.STATUS_PENDING,
            )
        )
        CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.user,
            status=CompanyVerification.STATUS_REJECTED,
        )

    def test_only_one_pending_claim_per_company_and_applicant(self):
        CompanyClaim.objects.create(company=self.company, applicant_user=self.user)
        self.assert_integrity_error(
            lambda: CompanyClaim.objects.create(company=self.company, applicant_user=self.user)
        )
        CompanyClaim.objects.create(
            company=self.company,
            applicant_user=self.user,
            status=CompanyClaim.STATUS_REJECTED,
        )

    def test_member_identity_and_normalized_invitation_constraints(self):
        CompanyMember.objects.create(
            company=self.company,
            user=self.user,
            name='Bound member',
            email=' Bound@Example.COM ',
            created_at=self.now,
            updated_at=self.now,
        )
        self.assertEqual(
            CompanyMember.objects.get(company=self.company, user=self.user).email,
            'bound@example.com',
        )
        self.assert_integrity_error(
            lambda: CompanyMember.objects.create(
                company=self.company,
                user=self.user,
                name='Duplicate bound member',
                email='different@example.com',
                created_at=self.now,
                updated_at=self.now,
            )
        )

        CompanyMember.objects.create(
            company=self.company,
            name='Invitation',
            email=' Invite@Example.COM ',
            created_at=self.now,
            updated_at=self.now,
        )
        self.assert_integrity_error(
            lambda: CompanyMember.objects.create(
                company=self.company,
                name='Duplicate invitation',
                email='invite@example.com',
                created_at=self.now,
                updated_at=self.now,
            )
        )
        self.assert_integrity_error(
            lambda: CompanyMember.objects.create(
                company=self.company,
                name='Whitespace invitation',
                email='   ',
                created_at=self.now,
                updated_at=self.now,
            )
        )


class FormalCompanyServiceTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.owner = User.objects.create(
            uid='phase2-owner',
            username='phase2-owner',
            email='owner@example.com',
            created_at=now,
            updated_at=now,
        )

    @patch('apps.company.services.ImageModerationService.require_approved_public_image', return_value=(True, '', ''))
    @patch('apps.company.services._moderate_company_texts', return_value=(True, ''))
    def test_create_company_normalizes_and_uses_database_uniqueness(self, _moderate, _image):
        ok, company = CompanyService.create_company(
            company_name='Canonical company',
            owner=self.owner,
            registration_no='91350211-m000100y46',
        )
        duplicate_owner = User.objects.create(
            uid='phase2-other-owner',
            username='phase2-other-owner',
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        duplicate_ok, duplicate_error = CompanyService.create_company(
            company_name='Duplicate company',
            owner=duplicate_owner,
            registration_no=VALID_REGISTRATION_NO,
        )

        self.assertTrue(ok)
        self.assertEqual(company.registration_no, VALID_REGISTRATION_NO)
        self.assertFalse(duplicate_ok)
        self.assertEqual(duplicate_error, '企业已存在')

    def test_create_company_rejects_invalid_checksum_before_insert(self):
        ok, error = CompanyService.create_company(
            company_name='Invalid checksum company',
            owner=self.owner,
            registration_no='91350211M000100Y45',
        )

        self.assertFalse(ok)
        self.assertEqual(error, '请输入有效的18位统一社会信用代码')
        self.assertFalse(Company.objects.exists())

    @patch('apps.company.services.Company.objects.create', side_effect=IntegrityError('other constraint'))
    @patch('apps.company.services.ImageModerationService.require_approved_public_image', return_value=(True, '', ''))
    @patch('apps.company.services._moderate_company_texts', return_value=(True, ''))
    def test_create_company_does_not_misreport_other_integrity_errors(
        self,
        _moderate,
        _image,
        _create,
    ):
        ok, error = CompanyService.create_company(
            company_name='Other integrity failure',
            owner=self.owner,
            registration_no=VALID_REGISTRATION_NO,
        )

        self.assertFalse(ok)
        self.assertEqual(error, '创建失败')

    @patch('apps.company.services._moderate_company_texts', return_value=(True, ''))
    @patch('apps.company.services.is_granted', return_value=True)
    def test_update_company_normalizes_registration_no(self, _granted, _moderate):
        company = Company.objects.create(
            account=self.owner,
            cid='phase2-update-canonical',
            company_name='Update canonical',
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )

        ok, _ = CompanyService.update_company(
            cid=company.cid,
            user=self.owner,
            registration_no=' 91350211-m000100y46 ',
        )

        company.refresh_from_db()
        self.assertTrue(ok)
        self.assertEqual(company.registration_no, VALID_REGISTRATION_NO)

    @patch('apps.company.services._moderate_company_texts', return_value=(True, ''))
    @patch('apps.company.services.is_granted', return_value=True)
    def test_update_company_rejects_invalid_registration_checksum(self, _granted, moderate):
        company = Company.objects.create(
            account=self.owner,
            cid='phase2-update-invalid',
            company_name='Update invalid',
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )

        ok, error = CompanyService.update_company(
            cid=company.cid,
            user=self.owner,
            registration_no='91350211M000100Y45',
        )

        company.refresh_from_db()
        self.assertFalse(ok)
        self.assertEqual(error, '请输入有效的18位统一社会信用代码')
        self.assertIsNone(company.registration_no)
        moderate.assert_not_called()

    @patch('apps.company.services.CompanyService._get_company_for_admin')
    def test_member_upsert_uses_user_then_normalized_invitation_identity(self, get_company):
        company = Company.objects.create(
            cid='phase2-service-company',
            company_name='Service company',
            created_at='2026-09-01',
            updated_at='2026-09-01',
        )
        get_company.return_value = (True, company)

        CompanyService.upsert_company_member(
            cid=company.cid,
            user=self.owner,
            member_uid=self.owner.uid,
            name='Owner',
            role='owner',
            email=' First@Example.COM ',
        )
        CompanyService.upsert_company_member(
            cid=company.cid,
            user=self.owner,
            member_uid=self.owner.uid,
            name='Owner updated',
            role='owner',
            email='second@example.com',
        )
        CompanyService.upsert_company_member(
            cid=company.cid,
            user=self.owner,
            name='Invite',
            role='member',
            email=' Invite@Example.COM ',
        )
        CompanyService.upsert_company_member(
            cid=company.cid,
            user=self.owner,
            name='Invite updated',
            role='admin',
            email='invite@example.com',
        )

        self.assertEqual(CompanyMember.objects.filter(company=company).count(), 2)
        self.assertEqual(
            CompanyMember.objects.get(company=company, user=self.owner).email,
            'second@example.com',
        )
        invitation = CompanyMember.objects.get(company=company, user__isnull=True)
        self.assertEqual(invitation.email, 'invite@example.com')
        self.assertEqual(invitation.role, 'admin')
