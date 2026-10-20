from django.test import TestCase
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyMember,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
)
from apps.company.permissions import can_access_formal_company
from apps.company.services import CompanyService
from apps.agent_runtime.runtime import UnifiedRuntimeService
from apps.iam.models import (
    Grant,
    Group,
    GroupMembership,
    PermissionAuditLog,
    Principal,
    Resource,
    Tenant,
    TenantMembership,
)
from apps.iam.services import (
    UNASSIGNED_GROUP_NAME,
    compute_effective_permission,
    get_or_create_organization_tenant,
    get_or_create_principal,
    is_action_allowed,
    reconcile_unassigned_group,
    runtime_action_allowed,
    set_resource_role,
    sync_organization_tenant_member,
)
from apps.iam.views_unified import _effective_resource_roles, _users_by_uid
from apps.user.models import User


class UnifiedPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(uid='owner', username='Owner', created_at='1', updated_at='1')
        self.member = User.objects.create(uid='member', username='Member', created_at='1', updated_at='1')
        self.outsider = User.objects.create(uid='outsider', username='Outsider', created_at='1', updated_at='1')
        self.company = Company.objects.create(
            cid='company-acl',
            company_name='统一权限测试企业',
            account=self.owner,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            verification_provider=Company.VERIFICATION_PROVIDER_LEGACY_IMPORT,
        )
        CompanyMember.objects.create(
            company=self.company,
            user=self.member,
            memberUid=self.member.uid,
            name=self.member.username,
            status=CompanyMember.STATUS_ACTIVE,
            created_at='1',
            updated_at='1',
        )
        self.tenant = Tenant.objects.create(
            type=Tenant.TYPE_ORGANIZATION,
            external_id='organization:company-acl',
            name='统一权限测试企业',
            super_admin_user=self.owner,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.owner,
            role=TenantMembership.ROLE_SUPER_ADMIN,
        )
        TenantMembership.objects.create(tenant=self.tenant, user=self.member)
        self.project = Resource.objects.create(
            type=Resource.TYPE_PROJECT,
            external_id='project-acl',
            tenant=self.tenant,
            creator=self.owner,
        )
        self.table = Resource.objects.create(
            type=Resource.TYPE_TABLE,
            external_id='table-acl',
            tenant=self.tenant,
            creator=self.owner,
            parent=self.project,
        )
        self.agent = Resource.objects.create(
            type=Resource.TYPE_AGENT,
            external_id='agent:company-acl:analyst',
            tenant=self.tenant,
            creator=self.owner,
        )
        self.knowledge = Resource.objects.create(
            type=Resource.TYPE_KNOWLEDGE_BASE,
            external_id='knowledge:company-acl',
            tenant=self.tenant,
            creator=self.owner,
        )
        self.member_principal = get_or_create_principal(Principal.TYPE_USER, self.member.uid)

    def test_super_admin_bypass_and_default_deny(self):
        owner_principal = get_or_create_principal(Principal.TYPE_USER, self.owner.uid)
        outsider_principal = get_or_create_principal(Principal.TYPE_USER, self.outsider.uid)
        self.assertEqual(compute_effective_permission(owner_principal, self.project).role, 'manager')
        self.assertEqual(compute_effective_permission(self.member_principal, self.project).role, 'none')
        self.assertEqual(compute_effective_permission(outsider_principal, self.project).role, 'none')

    def test_tenant_has_one_canonical_super_admin(self):
        company_resource = Resource.objects.create(
            type=Resource.TYPE_COMPANY,
            external_id=self.company.cid,
            tenant=self.tenant,
        )
        Grant.objects.create(
            principal=self.member_principal,
            resource=company_resource,
            permissions=Grant.PERM_SUPER_ADMIN,
        )
        self.assertFalse(can_access_formal_company(
            self.company, self.member, Grant.PERM_SUPER_ADMIN,
        ))
        self.assertTrue(can_access_formal_company(
            self.company, self.member, Grant.PERM_ADMIN,
        ))

    def test_suspended_company_disables_all_resource_permissions(self):
        owner_principal = get_or_create_principal(Principal.TYPE_USER, self.owner.uid)
        self.company.verification_suspended_at = timezone.now()
        self.company.verification_suspension_reason = 'test'
        self.company.save(update_fields=['verification_suspended_at', 'verification_suspension_reason'])
        self.assertEqual(compute_effective_permission(owner_principal, self.project).role, 'none')

    def test_project_grant_inherits_to_table_and_independent_mode_stops_it(self):
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.project,
            permissions=Grant.ROLE_CONTRIBUTOR,
            inherit=True,
        )
        inherited = compute_effective_permission(self.member_principal, self.table)
        self.assertEqual(inherited.role, 'contributor')
        self.assertTrue(inherited.sources[0]['inherited'])
        self.table.permission_mode = Resource.PERMISSION_MODE_INDEPENDENT
        self.table.save(update_fields=['permission_mode'])
        self.assertEqual(compute_effective_permission(self.member_principal, self.table).role, 'none')

    def test_bulk_resource_roles_match_direct_and_inherited_permissions(self):
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.project,
            permissions=Grant.ROLE_CONTRIBUTOR,
            inherit=True,
        )
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.agent,
            permissions=Grant.ROLE_EDITOR,
        )

        roles = _effective_resource_roles(
            self.tenant,
            self.member,
            [self.project, self.table, self.agent, self.knowledge],
        )
        self.assertEqual(roles[self.project.id], 'contributor')
        self.assertEqual(roles[self.table.id], 'contributor')
        self.assertEqual(roles[self.agent.id], 'editor')
        self.assertEqual(roles[self.knowledge.id], 'none')

        owner_roles = _effective_resource_roles(
            self.tenant,
            self.owner,
            [self.project, self.table, self.agent, self.knowledge],
        )
        self.assertTrue(all(role == 'manager' for role in owner_roles.values()))

    def test_read_path_skips_full_member_sync_and_single_member_sync_repairs_state(self):
        membership = TenantMembership.objects.get(tenant=self.tenant, user=self.member)
        membership.status = TenantMembership.STATUS_DISABLED
        membership.save(update_fields=['status'])

        get_or_create_organization_tenant(self.company, sync_memberships=False)
        membership.refresh_from_db()
        self.assertEqual(membership.status, TenantMembership.STATUS_DISABLED)

        company_member = CompanyMember.objects.get(company=self.company, user=self.member)
        sync_organization_tenant_member(self.company, company_member)
        membership.refresh_from_db()
        self.assertEqual(membership.status, TenantMembership.STATUS_ACTIVE)

    def test_member_sync_adds_unassigned_group_and_default_read_without_downgrade(self):
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.project,
            permissions=Grant.ROLE_EDITOR,
        )
        company_member = CompanyMember.objects.get(company=self.company, user=self.member)

        sync_organization_tenant_member(self.company, company_member)

        unassigned = Group.objects.get(tenant=self.tenant, name=UNASSIGNED_GROUP_NAME)
        self.assertTrue(GroupMembership.objects.filter(group=unassigned, user=self.member).exists())
        self.assertEqual(
            Grant.objects.get(principal=self.member_principal, resource=self.project).permissions,
            Grant.ROLE_EDITOR,
        )
        for resource in (self.table, self.agent, self.knowledge):
            self.assertEqual(
                Grant.objects.get(principal=self.member_principal, resource=resource).permissions,
                Grant.ROLE_VIEWER,
            )

    def test_unassigned_group_tracks_members_without_normal_group(self):
        unassigned = reconcile_unassigned_group(self.tenant, created_by=self.owner)
        self.assertTrue(GroupMembership.objects.filter(group=unassigned, user=self.member).exists())

        team = Group.objects.create(tenant=self.tenant, name='研发组', created_by=self.owner)
        GroupMembership.objects.create(group=team, user=self.member, created_by=self.owner)
        reconcile_unassigned_group(self.tenant, created_by=self.owner)
        self.assertFalse(GroupMembership.objects.filter(group=unassigned, user=self.member).exists())

        GroupMembership.objects.filter(group=team, user=self.member).delete()
        reconcile_unassigned_group(self.tenant, created_by=self.owner)
        self.assertTrue(GroupMembership.objects.filter(group=unassigned, user=self.member).exists())

    def test_resource_grant_user_lookup_handles_non_unique_uid_field(self):
        duplicate = User.objects.create(
            uid=self.member.uid,
            username='Duplicate',
            created_at='1',
            updated_at='1',
        )
        users = _users_by_uid([self.member.uid])
        self.assertEqual(users[self.member.uid].id, min(self.member.id, duplicate.id))

    def test_permission_never_inherits_across_tenants(self):
        other_tenant = Tenant.objects.create(
            type=Tenant.TYPE_ORGANIZATION,
            external_id='organization:isolated',
            super_admin_user=self.outsider,
        )
        TenantMembership.objects.create(tenant=other_tenant, user=self.member)
        foreign_table = Resource.objects.create(
            type=Resource.TYPE_TABLE,
            external_id='foreign-table',
            tenant=other_tenant,
            parent=self.project,
        )
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.project,
            permissions=Grant.ROLE_MANAGER,
        )
        self.assertEqual(
            compute_effective_permission(self.member_principal, foreign_table).role,
            'none',
        )

    def test_group_grant_and_membership_revocation_are_immediate(self):
        group = Group.objects.create(tenant=self.tenant, name='财务组', created_by=self.owner)
        GroupMembership.objects.create(group=group, user=self.member, created_by=self.owner)
        group_principal = get_or_create_principal(Principal.TYPE_GROUP, str(group.group_id))
        Grant.objects.create(
            principal=group_principal,
            resource=self.project,
            permissions=Grant.ROLE_VIEWER,
        )
        effective = compute_effective_permission(self.member_principal, self.project)
        self.assertEqual(effective.role, 'viewer')
        self.assertEqual(effective.sources[0]['kind'], 'group')
        GroupMembership.objects.filter(group=group, user=self.member).delete()
        self.assertEqual(compute_effective_permission(self.member_principal, self.project).role, 'none')

    def test_disabled_tenant_member_is_denied_even_with_direct_grant(self):
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.project,
            permissions=Grant.ROLE_MANAGER,
        )
        CompanyMember.objects.filter(company=self.company, user=self.member).update(status='disabled')
        self.assertEqual(compute_effective_permission(self.member_principal, self.project).role, 'none')

    def test_agent_use_and_configuration_are_separate(self):
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.agent,
            permissions=Grant.ROLE_VIEWER,
        )
        self.assertTrue(is_action_allowed(self.member_principal, self.agent, 'view'))
        self.assertFalse(is_action_allowed(self.member_principal, self.agent, 'run'))
        Grant.objects.filter(principal=self.member_principal, resource=self.agent).update(
            permissions=Grant.ROLE_CONTRIBUTOR,
        )
        self.assertTrue(is_action_allowed(self.member_principal, self.agent, 'run'))
        self.assertFalse(is_action_allowed(self.member_principal, self.agent, 'configure'))

    def test_agent_editor_sees_configuration_but_runner_does_not(self):
        transparent = TransparentCompany.objects.create(company=self.company, prompts='')
        TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=transparent,
            agentId='analyst',
            name='分析 Agent',
            created_at='1',
            updated_at='1',
        )
        TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=transparent,
            agentId='runner',
            name='调用 Agent',
            created_at='1',
            updated_at='1',
        )
        runner_resource = Resource.objects.create(
            type=Resource.TYPE_AGENT,
            external_id='agent:company-acl:runner',
            tenant=self.tenant,
        )
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.agent,
            permissions=Grant.ROLE_EDITOR,
        )
        Grant.objects.create(
            principal=self.member_principal,
            resource=runner_resource,
            permissions=Grant.ROLE_CONTRIBUTOR,
        )
        ok, agents = CompanyService.list_transparent_company_agents(
            self.company.cid,
            user=self.member,
        )
        self.assertTrue(ok)
        self.assertEqual([agent['agentId'] for agent in agents], ['analyst'])

    def test_agent_runtime_requires_user_and_agent_permissions(self):
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.agent,
            permissions=Grant.ROLE_CONTRIBUTOR,
        )
        Grant.objects.create(
            principal=self.member_principal,
            resource=self.knowledge,
            permissions=Grant.ROLE_VIEWER,
        )
        self.assertFalse(runtime_action_allowed(
            self.member,
            self.agent.external_id,
            self.knowledge,
            'query',
        ))
        agent_principal = get_or_create_principal(Principal.TYPE_AGENT, self.agent.external_id)
        Grant.objects.create(
            principal=agent_principal,
            resource=self.knowledge,
            permissions=Grant.ROLE_VIEWER,
        )
        self.assertTrue(runtime_action_allowed(
            self.member,
            self.agent.external_id,
            self.knowledge,
            'query',
        ))

    def test_runtime_workspace_policy_is_capped_by_user_resource_role(self):
        policy = {'knowledge': {
            'read': ['knowledge'],
            'write': ['knowledge'],
            'delete': ['knowledge'],
        }}
        denied = UnifiedRuntimeService._apply_user_resource_ceiling(
            policy,
            company=self.company,
            user=self.member,
        )
        self.assertEqual(denied['knowledge']['read'], [])
        self.assertEqual(denied['knowledge']['write'], [])
        self.assertEqual(denied['knowledge']['delete'], [])

        Grant.objects.create(
            principal=self.member_principal,
            resource=self.knowledge,
            permissions=Grant.ROLE_CONTRIBUTOR,
        )
        contributor = UnifiedRuntimeService._apply_user_resource_ceiling(
            policy,
            company=self.company,
            user=self.member,
        )
        self.assertEqual(contributor['knowledge']['read'], ['knowledge'])
        self.assertEqual(contributor['knowledge']['write'], ['knowledge'])
        self.assertEqual(contributor['knowledge']['delete'], [])

    def test_resource_manager_can_grant_only_inside_tenant_and_is_audited(self):
        set_resource_role(
            self.owner,
            self.project,
            self.member_principal,
            'editor',
            ip_address='127.0.0.1',
        )
        self.assertEqual(compute_effective_permission(self.member_principal, self.project).role, 'editor')
        audit = PermissionAuditLog.objects.get(action='grant.update')
        self.assertEqual(audit.actor, self.owner)
        self.assertEqual(audit.after['role'], 'editor')

        other_tenant = Tenant.objects.create(
            type=Tenant.TYPE_ORGANIZATION,
            external_id='organization:other',
            super_admin_user=self.outsider,
        )
        group = Group.objects.create(tenant=other_tenant, name='其他企业分组')
        group_principal = get_or_create_principal(Principal.TYPE_GROUP, str(group.group_id))
        with self.assertRaises(ValueError):
            set_resource_role(self.owner, self.project, group_principal, 'viewer')
