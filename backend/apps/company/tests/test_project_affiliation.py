from unittest.mock import patch

from django.test import TestCase
from django.db import IntegrityError, transaction

from apps.company.models import Company, companyGate
from apps.company.services import CompanyService
from apps.iam.models import Resource, Tenant
from apps.opengate.models import Gates
from apps.opengate.services import OpenGateService
from apps.user.models import User


class ProjectAffiliationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(
            uid='affiliation-user', username='Affiliation User',
            created_at='1', updated_at='1',
        )
        self.company_a = Company.objects.create(
            cid='affiliation-a', company_name='Company A', account=self.user,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
        )
        self.company_b = Company.objects.create(
            cid='affiliation-b', company_name='Company B', account=self.user,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
        )
        tenant_a = Tenant.objects.create(
            type=Tenant.TYPE_ORGANIZATION, external_id='organization:affiliation-a',
        )
        tenant_b = Tenant.objects.create(
            type=Tenant.TYPE_ORGANIZATION, external_id='organization:affiliation-b',
        )
        personal_tenant = Tenant.objects.create(
            type=Tenant.TYPE_PERSONAL, external_id='personal:affiliation-user',
            owner_user=self.user,
        )
        self.owned = self._project('affiliation-owned', tenant_a)
        self.moved = self._project('affiliation-moved', tenant_b)
        self.stale = self._project('affiliation-stale', tenant_b)
        self.unbound = self._project('affiliation-unbound', tenant_b)
        self.personal = self._project('affiliation-personal', personal_tenant)
        companyGate.objects.create(company=self.company_a, opengate=self.owned)
        companyGate.objects.create(company=self.company_a, opengate=self.stale)
        companyGate.objects.create(company=self.company_b, opengate=self.moved)
        companyGate.objects.create(company=self.company_a, opengate=self.personal)
        self.visible_gids = [self.owned.gid, self.moved.gid, self.stale.gid, self.unbound.gid, self.personal.gid]

    def _project(self, gid, tenant):
        gate = Gates.objects.create(
            gid=gid, gatename=gid, owner=self.user,
            label=Gates.LABEL_COMPANY, created_at='1', updated_at='1',
        )
        Resource.objects.create(
            type=Resource.TYPE_PROJECT, external_id=gid, tenant=tenant,
        )
        return gate

    def test_company_list_excludes_stale_bindings_and_personal_projects(self):
        with patch('apps.company.services.visible_resource_ids', return_value=self.visible_gids):
            ok_a, result_a = CompanyService.list_company_programs(
                self.user, self.company_a.cid, 20, 0,
            )
            ok_b, result_b = CompanyService.list_company_programs(
                self.user, self.company_b.cid, 20, 0,
            )

        self.assertTrue(ok_a)
        self.assertTrue(ok_b)
        self.assertEqual(result_a['total'], 1)
        self.assertEqual([item['gid'] for item in result_a['items']], [self.owned.gid])
        self.assertEqual(result_b['total'], 1)
        self.assertEqual([item['gid'] for item in result_b['items']], [self.moved.gid])

    def test_personal_project_list_labels_only_current_company(self):
        with patch('apps.opengate.services.visible_resource_ids', return_value=self.visible_gids):
            result = OpenGateService.list_gates_by_owner(self.user.uid)

        by_gid = {item['gid']: item for item in result['gates']}
        self.assertEqual(result['total'], 5)
        self.assertEqual(by_gid[self.owned.gid]['cid'], self.company_a.cid)
        self.assertEqual(by_gid[self.owned.gid]['company_name'], self.company_a.company_name)
        self.assertEqual(by_gid[self.moved.gid]['cid'], self.company_b.cid)
        self.assertEqual(by_gid[self.moved.gid]['company_name'], self.company_b.company_name)
        self.assertEqual(by_gid[self.stale.gid]['company_name'], self.company_b.company_name)
        self.assertEqual(by_gid[self.unbound.gid]['cid'], self.company_b.cid)
        self.assertEqual(by_gid[self.unbound.gid]['company_name'], self.company_b.company_name)
        self.assertIsNone(by_gid[self.personal.gid]['cid'])
        self.assertIsNone(by_gid[self.personal.gid]['company_name'])

    def test_cannot_bind_project_to_second_company(self):
        with patch('apps.company.services.can_access_formal_company', return_value=True), patch(
            'apps.company.services.has_iam_permission', return_value=True,
        ):
            ok, message = CompanyService.bind_program(
                self.company_b.cid, self.owned.gid, self.user,
            )

        self.assertFalse(ok)
        self.assertEqual(message, '项目已归属其他公司')
        self.assertFalse(companyGate.objects.filter(company=self.company_b, opengate=self.owned).exists())
        self.assertEqual(Resource.objects.get(type=Resource.TYPE_PROJECT, external_id=self.owned.gid).tenant.external_id, 'organization:affiliation-a')

    def test_database_rejects_second_company_binding(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            companyGate.objects.create(company=self.company_b, opengate=self.owned)

    def test_stale_binding_cannot_unbind_current_company_owner(self):
        with patch('apps.company.services.can_access_formal_company', return_value=True), patch(
            'apps.company.services.has_iam_permission', return_value=True,
        ):
            ok, message = CompanyService.delete_company_program(
                self.user, self.company_a.cid, self.stale.gid,
            )

        self.assertFalse(ok)
        self.assertEqual(message, '项目已归属其他公司')
        self.assertEqual(Resource.objects.get(type=Resource.TYPE_PROJECT, external_id=self.stale.gid).tenant.external_id, 'organization:affiliation-b')

    def test_create_project_in_company_sets_affiliation_atomically(self):
        with patch('apps.company.permissions.can_access_formal_company', return_value=True), patch(
            'apps.opengate.services._moderate_opengate_texts', return_value=(True, ''),
        ), patch('apps.opengate.services.ImmudbService') as immudb, patch(
            'apps.opengate.services.ImageModerationService.require_approved_public_image', return_value=(True, '', ''),
        ):
            immudb.return_value.create_db.return_value = (True, '')
            gate, message = OpenGateService.create_gate(
                self.user, 'affiliation-new', 'New Project', company_id=self.company_a.cid,
            )

        self.assertEqual(message, '创建成功')
        self.assertEqual(gate.label, Gates.LABEL_COMPANY)
        self.assertTrue(companyGate.objects.filter(company=self.company_a, opengate=gate).exists())
        self.assertEqual(Resource.objects.get(type=Resource.TYPE_PROJECT, external_id=gate.gid).tenant.external_id, 'organization:affiliation-a')

    def test_company_create_checks_permission_before_external_db(self):
        with patch('apps.company.permissions.can_access_formal_company', return_value=False), patch(
            'apps.opengate.services.ImmudbService.create_db',
        ) as create_db:
            gate, message = OpenGateService.create_gate(
                self.user, 'affiliation-denied', 'Denied', company_id=self.company_a.cid,
            )

        self.assertIsNone(gate)
        self.assertEqual(message, '公司不存在或无权限创建项目')
        create_db.assert_not_called()
