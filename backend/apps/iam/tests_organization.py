import json

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.company.models import CompanyMember
from apps.iam.models import Grant, Group, GroupMembership, PermissionAuditLog, Resource, Tenant
from apps.iam.services import UNASSIGNED_GROUP_NAME, get_or_create_principal
from apps.iam.models import Principal
from common.utils.Login_utils import sign_token
from . import tests_unified_permissions as permission_fixtures


class OrganizationManagementTests(TestCase):
    def setUp(self):
        permission_fixtures.UnifiedPermissionTests.setUp(self)
        self.company_resource = Resource.objects.create(
            tenant=self.tenant, type=Resource.TYPE_COMPANY, external_id=self.company.cid,
        )
        get_or_create_principal(Principal.TYPE_USER, self.owner.uid)

    def post(self, path, payload, actor=None):
        return self.client.post(
            '/api/iam/unified/' + path, json.dumps({'cid': self.company.cid, **payload}),
            content_type='application/json',
            HTTP_ACCESS_TOKEN=sign_token({'user_id': (actor or self.owner).id}),
        )

    def tree(self, actor=None):
        return self.client.get(
            '/api/iam/unified/organization', {'cid': self.company.cid},
            HTTP_ACCESS_TOKEN=sign_token({'user_id': (actor or self.owner).id}),
        )

    def create_department(self, name, parent=None):
        response = self.post('group/upsert', {'name': name, 'parent_group_id': parent, 'description': ''})
        self.assertEqual(response.status_code, 200)
        return response.json()['data']['group_id']

    def change_members(self, groups, operation='set', public_ids=None, actor=None):
        return self.post('organization/members/update', {
            'public_ids': public_ids or [self.member.public_id], 'group_ids': groups, 'operation': operation,
        }, actor=actor)

    def test_department_create_and_legacy_edit_preserve_parent(self):
        parent = self.create_department('研发部')
        child = self.create_department('开发组', parent)
        response = self.post('group/upsert', {'group_id': child, 'name': '软件组', 'description': '软件研发'})
        self.assertEqual(response.json()['data']['parent_group_id'], parent)
        audit = PermissionAuditLog.objects.filter(action='group.update').latest('id')
        self.assertEqual(audit.before['name'], '开发组')
        self.assertEqual(audit.after['name'], '软件组')
        self.assertEqual(audit.after['parent_group_id'], parent)

    def test_self_and_descendant_parent_rejected_without_changes(self):
        parent = self.create_department('研发部')
        child = self.create_department('开发组', parent)
        for new_parent in (parent, child):
            response = self.post('group/upsert', {
                'group_id': parent, 'name': '研发部', 'parent_group_id': new_parent,
            })
            self.assertEqual(response.status_code, 400)
        self.assertIsNone(Group.objects.get(group_id=parent).parent_id)

    def test_foreign_system_and_malformed_parent_rejected(self):
        other = Tenant.objects.create(type=Tenant.TYPE_ORGANIZATION, external_id='organization:other')
        foreign = Group.objects.create(tenant=other, name='外部部门')
        self.tree()
        system = Group.objects.get(tenant=self.tenant, name=UNASSIGNED_GROUP_NAME)
        for parent in (str(foreign.group_id), str(system.group_id), 'invalid-id', 123):
            response = self.post('group/upsert', {'name': '新部门', 'parent_group_id': parent})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Group.objects.filter(tenant=self.tenant, name='新部门').exists())

    def test_depth_limit_applies_to_subtree_reparenting(self):
        root = Group.objects.create(tenant=self.tenant, name='根部门')
        current = root
        for index in range(23):
            current = Group.objects.create(tenant=self.tenant, name=f'层级{index}', parent=current)
        branch = Group.objects.create(tenant=self.tenant, name='分支')
        Group.objects.create(tenant=self.tenant, name='分支子部门', parent=branch)
        response = self.post('group/upsert', {
            'group_id': str(branch.group_id), 'name': branch.name, 'parent_group_id': str(current.group_id),
        })
        self.assertEqual(response.status_code, 400)
        branch.refresh_from_db()
        self.assertIsNone(branch.parent_id)

    def test_delete_department_requires_empty_members_and_children(self):
        parent = self.create_department('研发部')
        child = self.create_department('开发组', parent)
        self.assertEqual(self.post('group/delete', {'group_id': parent}).status_code, 400)
        self.change_members([child])
        self.assertEqual(self.post('group/delete', {'group_id': child}).status_code, 400)
        self.change_members([])
        self.assertEqual(self.post('group/delete', {'group_id': child}).status_code, 200)
        self.assertEqual(self.post('group/delete', {'group_id': parent}).status_code, 200)
        self.assertEqual(PermissionAuditLog.objects.filter(action='group.delete').count(), 2)

    def test_tree_has_highest_admin_and_unassigned_members_without_private_profile(self):
        response = self.tree()
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertTrue(data['can_manage_departments'])
        self.assertEqual(len([row for row in data['members'] if row['is_super_admin']]), 1)
        self.assertEqual(len(data['members']), 2)
        self.assertNotIn('user', data['members'][0])
        self.assertNotIn('phone', data['members'][0])
        self.assertIn('no-store', response['Cache-Control'])
        system = next(group for group in data['groups'] if group['is_system'])
        self.assertEqual(system['members'][0]['public_id'], self.member.public_id)

    def test_company_manager_can_view_but_department_management_remains_highest_admin_only(self):
        self.assertEqual(self.tree(self.member).status_code, 403)
        Grant.objects.create(principal=self.member_principal, resource=self.company_resource, permissions=4)
        response = self.tree(self.member)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['can_manage_departments'])
        self.assertEqual(self.post('group/upsert', {'name': '非法部门'}, actor=self.member).status_code, 403)
        self.assertEqual(self.change_members([], actor=self.member).status_code, 403)
        self.assertEqual(self.tree(self.outsider).status_code, 403)

    def test_add_registered_employee_by_phone_into_department_atomically(self):
        group = self.create_department('市场部')
        self.outsider.phone = '13900000001'
        self.outsider.save(update_fields=['phone'])
        response = self.post('organization/member/add', {
            'mode': 'phone', 'identifier': self.outsider.phone, 'group_id': group,
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(CompanyMember.objects.filter(company=self.company, user=self.outsider).exists())
        self.assertTrue(GroupMembership.objects.filter(group__group_id=group, user=self.outsider).exists())
        principal = Principal.objects.get(type=Principal.TYPE_USER, external_id=self.outsider.uid)
        self.assertTrue(all(value == Grant.ROLE_VIEWER for value in Grant.objects.filter(
            principal=principal, resource__tenant=self.tenant,
        ).values_list('permissions', flat=True)))
        self.assertTrue(PermissionAuditLog.objects.filter(action='organization.member.add').exists())

    def test_invalid_department_does_not_partially_add_employee(self):
        response = self.post('organization/member/add', {
            'mode': 'user_id', 'identifier': self.outsider.public_id, 'group_id': 'invalid',
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CompanyMember.objects.filter(company=self.company, user=self.outsider).exists())
        self.assertFalse(PermissionAuditLog.objects.filter(action='organization.member.add').exists())

    def test_member_manager_can_add_unassigned_employee_not_assign_departments(self):
        Grant.objects.create(principal=self.member_principal, resource=self.company_resource, permissions=4)
        group = self.create_department('市场部')
        response = self.post('organization/member/add', {
            'mode': 'user_id', 'identifier': self.outsider.public_id, 'group_id': group,
        }, actor=self.member)
        self.assertEqual(response.status_code, 403)
        response = self.post('organization/member/add', {
            'mode': 'user_id', 'identifier': self.outsider.public_id, 'group_id': None,
        }, actor=self.member)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(GroupMembership.objects.filter(
            group__name=UNASSIGNED_GROUP_NAME, group__tenant=self.tenant, user=self.outsider,
        ).exists())

    def test_bulk_department_set_add_remove_keep_direct_grants_and_audit(self):
        first, second = self.create_department('研发部'), self.create_department('产品部')
        grant = Grant.objects.create(principal=self.member_principal, resource=self.project, permissions=3)
        self.assertEqual(self.change_members([first]).status_code, 200)
        self.assertEqual(self.change_members([second], 'add').status_code, 200)
        self.assertEqual(GroupMembership.objects.filter(user=self.member, group__tenant=self.tenant).count(), 2)
        self.assertEqual(self.change_members([first], 'remove').status_code, 200)
        self.assertFalse(GroupMembership.objects.filter(user=self.member, group__group_id=first).exists())
        self.assertTrue(GroupMembership.objects.filter(user=self.member, group__group_id=second).exists())
        self.assertEqual(self.change_members([]).status_code, 200)
        self.assertTrue(GroupMembership.objects.filter(user=self.member, group__name=UNASSIGNED_GROUP_NAME).exists())
        grant.refresh_from_db()
        self.assertEqual(grant.permissions, 3)
        audit = PermissionAuditLog.objects.filter(action='organization.members.set').latest('id')
        self.assertEqual(audit.before['members'][0]['department_names'], ['产品部'])
        self.assertEqual(audit.after['members'][0]['group_ids'], [])

    def test_bulk_invalid_or_disabled_member_is_all_or_nothing(self):
        group = self.create_department('研发部')
        response = self.change_members([group], public_ids=[self.member.public_id, self.outsider.public_id])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(GroupMembership.objects.filter(group__group_id=group).exists())
        CompanyMember.objects.filter(company=self.company, user=self.member).update(status='disabled')
        self.assertEqual(self.change_members([group]).status_code, 400)
        self.assertFalse(PermissionAuditLog.objects.filter(action='organization.members.set').exists())

    def test_foreign_system_and_malformed_batch_departments_rejected(self):
        foreign_tenant = Tenant.objects.create(type=Tenant.TYPE_ORGANIZATION, external_id='organization:foreign')
        foreign = Group.objects.create(tenant=foreign_tenant, name='外部')
        self.tree()
        system = Group.objects.get(tenant=self.tenant, name=UNASSIGNED_GROUP_NAME)
        for ids in ([str(foreign.group_id)], [str(system.group_id)], ['invalid'], [None], 'invalid'):
            response = self.change_members(ids)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(PermissionAuditLog.objects.filter(action='organization.members.set').exists())

    def test_bulk_limit_and_operation_are_validated(self):
        for payload in (
            {'public_ids': [self.member.public_id] * 101, 'group_ids': [], 'operation': 'set'},
            {'public_ids': [], 'group_ids': [], 'operation': 'set'},
            {'public_ids': [self.member.public_id], 'group_ids': [], 'operation': 'unknown'},
        ):
            self.assertEqual(self.post('organization/members/update', payload).status_code, 400)

    def test_disabled_members_do_not_appear_in_department_tree(self):
        group = self.create_department('研发部')
        self.change_members([group])
        CompanyMember.objects.filter(company=self.company, user=self.member).update(status='disabled')
        data = self.tree().json()['data']
        department = next(row for row in data['groups'] if row['group_id'] == group)
        self.assertEqual(department['members'], [])
        row = next(row for row in data['members'] if row['public_id'] == self.member.public_id)
        self.assertEqual(row['department_ids'], [group])
        self.assertEqual(next(row for row in data['members'] if row['public_id'] == self.member.public_id)['status'], 'disabled')

    def test_group_serialization_has_bounded_queries_not_n_plus_one(self):
        for index in range(20):
            group = Group.objects.create(tenant=self.tenant, name=f'部门{index}')
            GroupMembership.objects.create(group=group, user=self.member)
        with CaptureQueriesContext(connection) as captured:
            response = self.tree()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(captured), 24)
        self.assertEqual(len(response.json()['data']['groups']), 21)
