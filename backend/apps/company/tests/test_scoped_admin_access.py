import importlib
import json
from datetime import timedelta

from django.apps import apps
from django.test import TestCase
from django.utils import timezone
from common.utils.Login_utils import sign_token

from apps.company.admin_access import (
    agent_access, file_allowed, filter_readable_tree, scope_for, upsert_scope,
)
from apps.company.models import (
    Company, CompanyMember, CompanyScopedAdmin, CompanyVerification, TransparentCompany,
    TransparentCompanyAgentDefinition, TransparentCompanyAgentTestSession,
)
from apps.company.services import CompanyService
from apps.iam.models import Grant, Principal, Resource
from apps.user.models import User
from apps.user.serializers import UserProfileSerializer, UserUpdateSerializer


class ScopedAdminAccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(uid='owner', username='Owner', created_at='1', updated_at='1')
        self.delegate = User.objects.create(uid='delegate', username='Delegate', created_at='1', updated_at='1')
        self.other = User.objects.create(
            uid='other', username='Other', phone='13800138000',
            created_at='1', updated_at='1',
        )
        self.company = Company.objects.create(
            cid='company-one', company_name='测试企业', account=self.owner,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            verification_provider=Company.VERIFICATION_PROVIDER_LEGACY_IMPORT,
        )
        principal = Principal.objects.create(type=Principal.TYPE_USER, external_id=self.owner.uid)
        resource = Resource.objects.create(type=Resource.TYPE_COMPANY, external_id=self.company.cid)
        Grant.objects.create(principal=principal, resource=resource, permissions=Grant.PERM_SUPER_ADMIN)
        CompanyMember.objects.create(
            company=self.company, user=self.delegate, memberUid=self.delegate.uid,
            name=self.delegate.username, status=CompanyMember.STATUS_ACTIVE,
            created_at='1', updated_at='1',
        )
        self.transparent = TransparentCompany.objects.create(company=self.company, prompts='')
        self.agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent, agentId='agent-a', name='Agent A',
            created_at='1', updated_at='1',
        )
        self.other_agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent, agentId='agent-b', name='Agent B',
            created_at='1', updated_at='1',
        )

    def authorize(self):
        return upsert_scope(
            self.company, self.owner, self.delegate.public_id, '文件管理员',
            [{'path': 'knowledge/contracts', 'actions': ['read', 'write']}],
            ['agent-a'],
        )

    def test_only_super_admin_may_delegate_and_invalid_agent_is_rejected(self):
        with self.assertRaises(PermissionError):
            upsert_scope(self.company, self.delegate, self.other.public_id, '管理员', [], [])
        with self.assertRaises(ValueError):
            upsert_scope(self.company, self.owner, self.delegate.public_id, '管理员', [], ['foreign-agent'])
        with self.assertRaises(ValueError):
            upsert_scope(self.company, self.owner, self.delegate.public_id, '管理员',
                         [{'path': 'knowledge/other', 'actions': ['admin']}], [])
        with self.assertRaises(ValueError):
            upsert_scope(self.company, self.owner, self.delegate.public_id, '管理员',
                         [{'path': 'knowledge/other', 'actions': [{}]}], [])
        principal = Principal.objects.create(type=Principal.TYPE_USER, external_id=self.other.uid)
        resource = Resource.objects.get(type=Resource.TYPE_COMPANY, external_id=self.company.cid)
        Grant.objects.create(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN)
        with self.assertRaises(ValueError):
            upsert_scope(self.company, self.owner, self.other.public_id, '受限管理员', [], [])
        self.assertFalse(CompanyScopedAdmin.objects.exists())

    def test_file_actions_are_path_scoped_and_revocation_is_immediate(self):
        self.authorize()
        self.assertTrue(file_allowed(self.company, self.delegate, 'read', 'knowledge/contracts/a.pdf'))
        self.assertTrue(file_allowed(self.company, self.delegate, 'write', 'knowledge/contracts/new.pdf'))
        self.assertFalse(file_allowed(self.company, self.delegate, 'delete', 'knowledge/contracts/a.pdf'))
        self.assertFalse(file_allowed(self.company, self.delegate, 'read', 'knowledge/contracts-private/a.pdf'))
        self.assertFalse(file_allowed(self.company, self.delegate, 'read', 'knowledge/finance/a.pdf'))
        self.assertFalse(file_allowed(self.company, self.other, 'read', 'knowledge/contracts/a.pdf'))
        tree = {'items': [
            {'kind': 'directory', 'logical_path': 'knowledge/contracts', 'children': [
                {'kind': 'file', 'logical_path': 'knowledge/contracts/a.pdf'},
            ]},
            {'kind': 'file', 'logical_path': 'knowledge/finance.pdf'},
        ]}
        filtered = filter_readable_tree(self.company, self.delegate, tree)
        self.assertEqual(len(filtered['items']), 1)
        CompanyScopedAdmin.objects.filter(company=self.company, user=self.delegate).update(active=False)
        self.assertIsNone(scope_for(self.company, self.delegate))
        self.assertFalse(file_allowed(self.company, self.delegate, 'read', 'knowledge/contracts/a.pdf'))

    def test_agent_access_filters_inventory_and_isolates_sessions(self):
        self.authorize()
        ok, transparent, scope = agent_access(self.company.cid, self.delegate)
        self.assertTrue(ok)
        self.assertEqual(transparent.pk, self.transparent.pk)
        self.assertEqual(scope.agent_ids, ['agent-a'])
        ok, agents = CompanyService.list_transparent_company_test_agents(self.company.cid, self.delegate)
        self.assertTrue(ok)
        self.assertEqual([agent['agentId'] for agent in agents], ['agent-a'])
        self.assertNotIn('systemPrompt', agents[0])
        ok, message = CompanyService.create_transparent_company_test_session(
            self.company.cid, 'agent-b', self.delegate,
        )
        self.assertFalse(ok)
        self.assertIn('无权', message)
        own = TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=self.transparent, agent=self.agent, operator=self.delegate,
            conversationId='own-conversation', created_at='1', updated_at='1',
        )
        TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=self.transparent, agent=self.agent, operator=self.other,
            conversationId='other-conversation', created_at='1', updated_at='1',
        )
        ok, sessions = CompanyService.list_transparent_company_test_sessions(self.company.cid, self.delegate)
        self.assertTrue(ok)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(CompanyService._scoped_agent_test_send_allowed(self.transparent, self.delegate, '', own.conversationId))
        self.assertFalse(CompanyService._scoped_agent_test_send_allowed(self.transparent, self.delegate, '', 'other-conversation'))
        self.assertFalse(CompanyService._scoped_agent_test_send_allowed(self.transparent, self.delegate, 'agent-b', ''))
        ok, _ = CompanyService.get_transparent_company_test_messages(self.company.cid, 'other-conversation', self.delegate)
        self.assertFalse(ok)

    def test_scoped_admin_is_not_broad_iam_admin(self):
        self.authorize()
        ok, message = CompanyService._get_company_for_admin(self.company.cid, self.delegate)
        self.assertFalse(ok)
        self.assertEqual(message, '权限不足')
        ok, companies = CompanyService.list_companies_by_user(self.delegate, 20, 0)
        self.assertTrue(ok)
        self.assertIn(self.company.cid, [item['cid'] for item in companies['items']])

    def test_suspended_enterprise_disables_scoped_access(self):
        self.authorize()
        self.company.verification_suspended_at = timezone.now()
        self.company.verification_suspension_reason = 'test'
        self.company.save(update_fields=['verification_suspended_at', 'verification_suspension_reason'])
        self.assertFalse(file_allowed(self.company, self.delegate, 'read', 'knowledge/contracts/a.pdf'))
        ok, _, _ = agent_access(self.company.cid, self.delegate)
        self.assertFalse(ok)

    def test_http_delegation_requires_super_admin(self):
        self.authorize()
        url = f'/api/company/admin-access/list?cid={self.company.cid}'
        denied = self.client.get(url, HTTP_ACCESS_TOKEN=sign_token({'user_id': self.delegate.id}))
        self.assertEqual(denied.status_code, 403)
        permitted = self.client.get(url, HTTP_ACCESS_TOKEN=sign_token({'user_id': self.owner.id}))
        self.assertEqual(permitted.status_code, 200)
        self.assertEqual(permitted.json()['data'][0]['public_id'], self.delegate.public_id)
        self.assertEqual(self.client.post(
            '/api/company/admin-access/upsert',
            data=json.dumps({'cid': 'company-one', 'public_id': self.other.public_id, 'role_name': 'x', 'file_rules': [], 'agent_ids': []}),
            content_type='application/json',
            HTTP_ACCESS_TOKEN=sign_token({'user_id': self.delegate.id}),
        ).status_code, 403)

    def test_public_id_is_short_unique_and_read_only(self):
        self.assertEqual(len(self.owner.public_id), 13)
        self.assertNotEqual(self.owner.public_id, self.delegate.public_id)
        self.assertEqual(UserProfileSerializer(self.owner).data['public_id'], self.owner.public_id)
        serializer = UserUpdateSerializer(self.owner, data={'public_id': self.delegate.public_id}, partial=True)
        self.assertTrue(serializer.is_valid())
        serializer.save()
        self.owner.refresh_from_db()
        self.assertNotEqual(self.owner.public_id, self.delegate.public_id)

    def test_admin_adds_employee_by_public_id_without_granting_admin(self):
        owner_token = sign_token({'user_id': self.owner.id})
        delegate_token = sign_token({'user_id': self.delegate.id})
        payload = {'cid': self.company.cid, 'public_id': self.other.public_id}
        denied = self.client.post('/api/company/members/add-by-user-id',
            data=json.dumps(payload), content_type='application/json', HTTP_ACCESS_TOKEN=delegate_token)
        self.assertNotEqual(denied.json()['result'], 1)
        added = self.client.post('/api/company/members/add-by-user-id',
            data=json.dumps(payload), content_type='application/json', HTTP_ACCESS_TOKEN=owner_token)
        self.assertEqual(added.json()['result'], 1)
        self.assertEqual(added.json()['data']['public_id'], self.other.public_id)
        self.assertEqual(added.json()['data']['status'], CompanyMember.STATUS_ACTIVE)
        self.assertFalse(CompanyService._get_company_for_admin(self.company.cid, self.other)[0])
        me = self.client.get(f'/api/company/admin-access/me?cid={self.company.cid}', HTTP_ACCESS_TOKEN=sign_token({'user_id': self.other.id}))
        self.assertEqual(me.json()['data']['is_member'], True)
        self.assertEqual(me.json()['data']['is_admin'], False)
        ok, companies = CompanyService.list_companies_by_user(self.other, 20, 0)
        self.assertTrue(ok)
        self.assertIn(self.company.cid, [item['cid'] for item in companies['items']])
        duplicate = self.client.post('/api/company/members/add-by-user-id',
            data=json.dumps(payload), content_type='application/json', HTTP_ACCESS_TOKEN=owner_token)
        self.assertEqual(duplicate.json()['result'], 0)

    def test_admin_adds_employee_by_phone_without_granting_admin(self):
        owner_token = sign_token({'user_id': self.owner.id})
        delegate_token = sign_token({'user_id': self.delegate.id})
        payload = {'cid': self.company.cid, 'phone': self.other.phone}

        denied = self.client.post(
            '/api/company/members/add-by-phone',
            data=json.dumps(payload), content_type='application/json', HTTP_ACCESS_TOKEN=delegate_token,
        )
        self.assertNotEqual(denied.json()['result'], 1)

        added = self.client.post(
            '/api/company/members/add-by-phone',
            data=json.dumps(payload), content_type='application/json', HTTP_ACCESS_TOKEN=owner_token,
        )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json()['result'], 1)
        self.assertEqual(added.json()['data']['public_id'], self.other.public_id)
        self.assertEqual(added.json()['data']['status'], CompanyMember.STATUS_ACTIVE)
        self.assertFalse(CompanyService._get_company_for_admin(self.company.cid, self.other)[0])

        duplicate = self.client.post(
            '/api/company/members/add-by-phone',
            data=json.dumps(payload), content_type='application/json', HTTP_ACCESS_TOKEN=owner_token,
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.json()['result'], 0)

    def test_add_employee_by_phone_rejects_invalid_or_unknown_phone(self):
        owner_token = sign_token({'user_id': self.owner.id})
        for phone in ('not-a-phone', '13900139000'):
            response = self.client.post(
                '/api/company/members/add-by-phone',
                data=json.dumps({'cid': self.company.cid, 'phone': phone}),
                content_type='application/json', HTTP_ACCESS_TOKEN=owner_token,
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['result'], 0)

        User.objects.create(
            uid='duplicate-phone', username='Duplicate Phone', phone=self.other.phone,
            created_at='1', updated_at='1',
        )
        ambiguous = self.client.post(
            '/api/company/members/add-by-phone',
            data=json.dumps({'cid': self.company.cid, 'phone': self.other.phone}),
            content_type='application/json', HTTP_ACCESS_TOKEN=owner_token,
        )
        self.assertEqual(ambiguous.status_code, 400)
        self.assertIn('多个账号', ambiguous.json()['message'])

    def test_disabled_employee_loses_scoped_access(self):
        self.authorize()
        CompanyMember.objects.filter(company=self.company, user=self.delegate).update(status=CompanyMember.STATUS_DISABLED)
        self.assertIsNone(scope_for(self.company, self.delegate))
        self.assertFalse(file_allowed(self.company, self.delegate, 'read', 'knowledge/contracts/a.pdf'))
        ok, companies = CompanyService.list_companies_by_user(self.delegate, 20, 0)
        self.assertTrue(ok)
        self.assertNotIn(self.company.cid, [item['cid'] for item in companies['items']])

    def test_historical_migration_upgrades_only_verified_applicants(self):
        verification = CompanyVerification.objects.create(
            company=self.company, applicant_user=self.delegate,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
            status=CompanyVerification.STATUS_VERIFIED,
            applicant_role=CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE,
            company_name_snapshot=self.company.company_name,
            registration_no_snapshot='91350211M000100Y46',
            consent_version='test-v1', consented_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.company.verification_provider = Company.VERIFICATION_PROVIDER_TENCENT_MANUAL_V1
        self.company.verified_verification = verification
        self.company.save(update_fields=['verification_provider', 'verified_verification'])
        principal = Principal.objects.create(type=Principal.TYPE_USER, external_id=self.delegate.uid)
        resource = Resource.objects.get(type=Resource.TYPE_COMPANY, external_id=self.company.cid)
        grant = Grant.objects.create(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN)
        ess = Company.objects.create(
            cid='legacy-ess', company_name='历史电子签',
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            verification_provider=Company.VERIFICATION_PROVIDER_TENCENT_ESS,
        )
        ess_verification = CompanyVerification.objects.create(
            company=ess, applicant_user=self.other,
            provider=CompanyVerification.PROVIDER_TENCENT_ESS,
            status=CompanyVerification.STATUS_VERIFIED,
        )
        ess.verified_verification = ess_verification
        ess.save(update_fields=['verified_verification'])
        migrate = importlib.import_module('apps.company.migrations.0040_scoped_admin_and_verified_super_admin')
        migrate.upgrade_verified_applicants(apps, None)
        grant.refresh_from_db()
        self.assertEqual(grant.permissions, Grant.PERM_SUPER_ADMIN)
        self.assertEqual(
            Grant.objects.get(resource__external_id=ess.cid, principal__external_id=self.other.uid).permissions,
            Grant.PERM_SUPER_ADMIN,
        )
