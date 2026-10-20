import time

from django.test import TestCase

from apps.company.models import Company, companyGate
from apps.company.services import CompanyService
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.opengate.models import Gates
from apps.user.models import User


class CompanyPermissionStopgapTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid='phase0-operator',
            username='phase0-operator',
            email='operator@example.com',
            created_at=now,
            updated_at=now,
        )
        self.target_user = User.objects.create(
            uid='phase0-target',
            username='phase0-target',
            email='target@example.com',
            created_at=now,
            updated_at=now,
        )
        self.company = Company.objects.create(
            account=self.user,
            cid='phase0-company',
            company_name='Phase 0 Company',
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.gate = Gates.objects.create(
            gid='phase0-gate',
            owner=self.user,
            gatename='Phase 0 Gate',
            created_at=now,
            updated_at=now,
        )
        self.principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        self.company_resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        self.gate_resource = get_or_create_resource(Resource.TYPE_PROJECT, self.gate.gid)

    def _bind(self):
        return CompanyService.bind_program(self.company.cid, self.gate.gid, self.user)

    def test_bind_without_permissions_does_not_create_grants(self):
        ok, message = self._bind()

        self.assertFalse(ok)
        self.assertEqual(message, '权限不足')
        self.assertFalse(Grant.objects.filter(principal=self.principal).exists())
        self.assertFalse(companyGate.objects.filter(company=self.company, opengate=self.gate).exists())

    def test_bind_requires_company_super_admin(self):
        grant_permissions(self.principal, self.gate_resource, Grant.PERM_SUPER_ADMIN)

        ok, message = self._bind()

        self.assertFalse(ok)
        self.assertEqual(message, '权限不足')
        self.assertFalse(Grant.objects.filter(principal=self.principal, resource=self.company_resource).exists())

    def test_bind_requires_gate_super_admin(self):
        grant_permissions(self.principal, self.company_resource, Grant.PERM_SUPER_ADMIN)

        ok, message = self._bind()

        self.assertFalse(ok)
        self.assertEqual(message, '权限不足')
        self.assertFalse(Grant.objects.filter(principal=self.principal, resource=self.gate_resource).exists())

    def test_bind_with_both_super_admin_permissions_succeeds(self):
        grant_permissions(self.principal, self.company_resource, Grant.PERM_SUPER_ADMIN)
        grant_permissions(self.principal, self.gate_resource, Grant.PERM_SUPER_ADMIN)

        ok, message = self._bind()

        self.assertTrue(ok)
        self.assertIsNone(message)
        self.assertTrue(companyGate.objects.filter(company=self.company, opengate=self.gate).exists())
        self.gate.refresh_from_db()
        self.assertEqual(self.gate.label, Gates.LABEL_COMPANY)

    def test_failed_bind_does_not_upgrade_existing_grants(self):
        company_grant = grant_permissions(self.principal, self.company_resource, Grant.PERM_READ)
        gate_grant = grant_permissions(self.principal, self.gate_resource, Grant.PERM_ADMIN)

        ok, message = self._bind()

        self.assertFalse(ok)
        self.assertEqual(message, '权限不足')
        company_grant.refresh_from_db()
        gate_grant.refresh_from_db()
        self.assertEqual(company_grant.permissions, Grant.PERM_READ)
        self.assertEqual(gate_grant.permissions, Grant.PERM_ADMIN)

    def test_deprecated_transfer_owner_does_not_modify_company_account(self):
        ok, message = CompanyService.transfer_owner(
            operator=self.user,
            company_id=self.company.cid,
            target_email=self.target_user.email,
        )

        self.assertFalse(ok)
        self.assertEqual(message, '企业所有权转移接口已停用')
        self.company.refresh_from_db()
        self.assertEqual(self.company.account, self.user)
