from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Optional

from django.apps import apps
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Grant,
    Group,
    GroupMembership,
    PermissionAuditLog,
    Principal,
    Resource,
    Tenant,
    TenantMembership,
)

if TYPE_CHECKING:
    from apps.user.models import User


READ = Grant.PERM_READ
WRITE = Grant.PERM_WRITE
ADMIN = Grant.PERM_ADMIN
SUPER_ADMIN = Grant.PERM_SUPER_ADMIN

ROLE_NAMES = {
    0: 'none',
    Grant.ROLE_VIEWER: 'viewer',
    Grant.ROLE_CONTRIBUTOR: 'contributor',
    Grant.ROLE_EDITOR: 'editor',
    Grant.ROLE_MANAGER: 'manager',
}
ROLE_VALUES = {name: value for value, name in ROLE_NAMES.items() if value}

UNASSIGNED_GROUP_NAME = '暂未分组'
UNASSIGNED_GROUP_DESCRIPTION = '系统自动维护的未分组员工'

# 每种资源的动作都显式映射到最低角色，避免用“有权限”代替具体能力。
ACTION_MIN_ROLE = {
    Resource.TYPE_COMPANY: {
        'view': READ,
        'update': ADMIN,
        'manage_members': SUPER_ADMIN,
        'manage_permissions': SUPER_ADMIN,
    },
    Resource.TYPE_PROJECT: {
        'view': READ,
        'create_content': WRITE,
        'update_content': WRITE,
        'update': ADMIN,
        'manage_permissions': SUPER_ADMIN,
    },
    Resource.TYPE_TABLE: {
        'view': READ,
        'read_rows': READ,
        'insert_rows': WRITE,
        'update_rows': WRITE,
        'delete_rows': ADMIN,
        'update_schema': ADMIN,
        'manage_permissions': SUPER_ADMIN,
    },
    Resource.TYPE_AGENT: {
        'view': READ,
        'run': WRITE,
        'edit': ADMIN,
        'configure': ADMIN,
        'manage_permissions': SUPER_ADMIN,
    },
    Resource.TYPE_KNOWLEDGE_BASE: {
        'view': READ,
        'query': READ,
        'add_content': WRITE,
        'update_content': ADMIN,
        'delete_content': ADMIN,
        'manage_permissions': SUPER_ADMIN,
    },
}


@dataclass(frozen=True)
class EffectivePermission:
    principal: Principal
    resource: Resource
    permissions: int
    sources: tuple[dict, ...] = ()

    @property
    def role(self) -> str:
        return ROLE_NAMES.get(self.permissions, 'none')

    def can_read(self) -> bool:
        return self.permissions >= READ

    def can_write(self) -> bool:
        return self.permissions >= WRITE

    def can_admin(self) -> bool:
        return self.permissions >= ADMIN

    def can_super_admin(self) -> bool:
        return self.permissions >= SUPER_ADMIN


def get_resource_ancestors(resource: Resource) -> Iterable[Resource]:
    current = resource
    while current is not None:
        yield current
        if current == resource and current.permission_mode == Resource.PERMISSION_MODE_INDEPENDENT:
            break
        parent = current.parent
        if (
            parent is not None
            and resource.tenant_id is not None
            and parent.tenant_id != resource.tenant_id
        ):
            break
        current = parent


def get_or_create_resource(
    resource_type: str,
    external_id: str,
    parent: Resource | None = None,
    *,
    tenant: Tenant | None = None,
    creator: User | None = None,
    permission_mode: str | None = None,
) -> Resource:
    if (
        tenant is not None
        and tenant.type == Tenant.TYPE_PERSONAL
        and resource_type not in (Resource.TYPE_PROJECT, Resource.TYPE_TABLE)
    ):
        raise ValueError('个人空间仅支持项目和表格资源')
    defaults = {'parent': parent, 'tenant': tenant, 'creator': creator}
    if permission_mode:
        defaults['permission_mode'] = permission_mode
    resource, _ = Resource.objects.get_or_create(
        type=resource_type,
        external_id=external_id,
        defaults=defaults,
    )
    changed = []
    related_fields = {'parent', 'tenant', 'creator'}
    for field, value in (
        ('parent', parent), ('tenant', tenant), ('creator', creator),
        ('permission_mode', permission_mode),
    ):
        if value is None:
            continue
        differs = (
            getattr(resource, f'{field}_id') != value.id
            if field in related_fields
            else getattr(resource, field) != value
        )
        if differs:
            setattr(resource, field, value)
            changed.append(field)
    if changed:
        resource.save(update_fields=changed)
    return resource


def get_or_create_principal(principal_type: str, external_id: str) -> Principal:
    principal, _ = Principal.objects.get_or_create(
        type=principal_type,
        external_id=str(external_id),
    )
    return principal


def get_or_create_personal_tenant(user: User) -> Tenant:
    tenant, _ = Tenant.objects.get_or_create(
        type=Tenant.TYPE_PERSONAL,
        external_id=f'personal:{user.uid}',
        defaults={'name': '个人空间', 'owner_user': user},
    )
    if tenant.owner_user_id != user.id:
        tenant.owner_user = user
        tenant.save(update_fields=['owner_user', 'updated_at'])
    TenantMembership.objects.update_or_create(
        tenant=tenant,
        user=user,
        defaults={'role': TenantMembership.ROLE_SUPER_ADMIN, 'status': TenantMembership.STATUS_ACTIVE},
    )
    return tenant


def get_or_create_organization_tenant(
    company,
    super_user: User | None = None,
    *,
    sync_memberships: bool = True,
) -> Tenant:
    """为企业补齐 Tenant；读取路径可跳过已经完成的成员同步。"""

    tenant_external_id = f'organization:{company.cid}'
    tenant = Tenant.objects.filter(
        type=Tenant.TYPE_ORGANIZATION,
        external_id=tenant_external_id,
    ).select_related('super_admin_user').first()
    created = tenant is None

    if tenant is not None and super_user is None:
        super_user = tenant.super_admin_user
    if tenant is None and super_user is None:
        verified = getattr(company, 'verified_verification', None)
        super_user = getattr(verified, 'applicant_user', None) if verified else None
        company_resource = Resource.objects.filter(
            type=Resource.TYPE_COMPANY,
            external_id=company.cid,
        ).first()
        principal_id = None
        if super_user is None and company_resource:
            principal_id = Grant.objects.filter(
                resource=company_resource,
                permissions=Grant.PERM_SUPER_ADMIN,
                principal__type=Principal.TYPE_USER,
            ).order_by('created_at').values_list('principal__external_id', flat=True).first()
        if super_user is None:
            User = apps.get_model('user', 'User')
            super_user = User.objects.filter(uid=principal_id).first() if principal_id else company.account
    if tenant is None:
        tenant, created = Tenant.objects.get_or_create(
            type=Tenant.TYPE_ORGANIZATION,
            external_id=tenant_external_id,
            defaults={
                'name': company.company_name,
                'super_admin_user': super_user,
            },
        )
    changed = []
    if company.company_name and tenant.name != company.company_name:
        tenant.name = company.company_name
        changed.append('name')
    if tenant.super_admin_user_id is None and super_user is not None:
        tenant.super_admin_user = super_user
        changed.append('super_admin_user')
    if changed:
        changed.append('updated_at')
        tenant.save(update_fields=changed)
    if created or sync_memberships:
        CompanyMember = apps.get_model('company', 'CompanyMember')
        disabled_user_ids = CompanyMember.objects.filter(
            company=company,
            user__isnull=False,
        ).exclude(status=CompanyMember.STATUS_ACTIVE).values_list('user_id', flat=True)
        TenantMembership.objects.filter(
            tenant=tenant,
            user_id__in=disabled_user_ids,
        ).exclude(user_id=tenant.super_admin_user_id).update(status=TenantMembership.STATUS_DISABLED)
        for member_user in CompanyMember.objects.filter(
            company=company,
            status=CompanyMember.STATUS_ACTIVE,
            user__isnull=False,
        ).select_related('user'):
            role = (
                TenantMembership.ROLE_SUPER_ADMIN
                if member_user.user_id == tenant.super_admin_user_id
                else TenantMembership.ROLE_MEMBER
            )
            TenantMembership.objects.update_or_create(
                tenant=tenant,
                user=member_user.user,
                defaults={'role': role, 'status': TenantMembership.STATUS_ACTIVE},
            )
        if super_user is not None:
            TenantMembership.objects.update_or_create(
                tenant=tenant,
                user=super_user,
                defaults={
                    'role': TenantMembership.ROLE_SUPER_ADMIN,
                    'status': TenantMembership.STATUS_ACTIVE,
                },
            )
    return tenant


def get_or_create_unassigned_group(
    tenant: Tenant,
    *,
    created_by: User | None = None,
) -> Group:
    """返回系统保留的未分组员工分组。"""

    group, _ = Group.objects.get_or_create(
        tenant=tenant,
        name=UNASSIGNED_GROUP_NAME,
        defaults={
            'description': UNASSIGNED_GROUP_DESCRIPTION,
            'created_by': created_by,
        },
    )
    return group


def reconcile_unassigned_group(
    tenant: Tenant,
    *,
    created_by: User | None = None,
) -> Group:
    """把没有加入普通分组的有效员工归入系统未分组分组。"""

    group = get_or_create_unassigned_group(tenant, created_by=created_by)
    active_user_ids = set(TenantMembership.objects.filter(
        tenant=tenant,
        status=TenantMembership.STATUS_ACTIVE,
    ).exclude(user_id=tenant.super_admin_user_id).values_list('user_id', flat=True))
    assigned_user_ids = set(GroupMembership.objects.filter(
        group__tenant=tenant,
    ).exclude(group=group).values_list('user_id', flat=True))
    desired_user_ids = active_user_ids - assigned_user_ids

    GroupMembership.objects.filter(group=group).exclude(
        user_id__in=desired_user_ids,
    ).delete()
    existing_user_ids = set(GroupMembership.objects.filter(
        group=group,
        user_id__in=desired_user_ids,
    ).values_list('user_id', flat=True))
    GroupMembership.objects.bulk_create([
        GroupMembership(group=group, user_id=user_id, created_by=created_by)
        for user_id in desired_user_ids - existing_user_ids
    ], ignore_conflicts=True)
    return group


def ensure_default_read_access_for_user(
    tenant: Tenant,
    user: User,
    *,
    granter: User | None = None,
) -> None:
    """为新员工在企业已有资源上补齐可阅读直授，不覆盖更高权限。"""

    if user.id == tenant.super_admin_user_id:
        return
    resource_ids = list(Resource.objects.filter(tenant=tenant).values_list('id', flat=True))
    if not resource_ids:
        return
    principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
    existing_resource_ids = set(Grant.objects.filter(
        principal=principal,
        resource_id__in=resource_ids,
    ).values_list('resource_id', flat=True))
    Grant.objects.bulk_create([
        Grant(
            principal=principal,
            resource_id=resource_id,
            permissions=Grant.ROLE_VIEWER,
            granter=granter,
            inherit=True,
        )
        for resource_id in resource_ids
        if resource_id not in existing_resource_ids
    ], ignore_conflicts=True)


def ensure_default_read_access_for_resource(
    resource: Resource,
    *,
    granter: User | None = None,
) -> None:
    """新资源创建时为企业现有有效员工补齐可阅读权限。"""

    if resource.tenant_id is None:
        return
    users = list(TenantMembership.objects.filter(
        tenant_id=resource.tenant_id,
        status=TenantMembership.STATUS_ACTIVE,
    ).exclude(user_id=resource.tenant.super_admin_user_id).select_related('user'))
    principals = [
        get_or_create_principal(Principal.TYPE_USER, membership.user.uid)
        for membership in users
    ]
    existing_principal_ids = set(Grant.objects.filter(
        resource=resource,
        principal__in=principals,
    ).values_list('principal_id', flat=True))
    Grant.objects.bulk_create([
        Grant(
            principal=principal,
            resource=resource,
            permissions=Grant.ROLE_VIEWER,
            granter=granter,
            inherit=True,
        )
        for principal in principals
        if principal.id not in existing_principal_ids
    ], ignore_conflicts=True)


def sync_organization_tenant_member(company, member) -> None:
    """只同步一个企业成员，供成员变更接口避免下一次读取时全量扫描。"""

    if not getattr(member, 'user_id', None):
        return
    tenant = get_or_create_organization_tenant(company, sync_memberships=False)
    role = (
        TenantMembership.ROLE_SUPER_ADMIN
        if member.user_id == tenant.super_admin_user_id
        else TenantMembership.ROLE_MEMBER
    )
    status = (
        TenantMembership.STATUS_ACTIVE
        if member.status == member.STATUS_ACTIVE
        else TenantMembership.STATUS_DISABLED
    )
    TenantMembership.objects.update_or_create(
        tenant=tenant,
        user_id=member.user_id,
        defaults={'role': role, 'status': status},
    )
    if status == TenantMembership.STATUS_ACTIVE and role != TenantMembership.ROLE_SUPER_ADMIN:
        granter = tenant.super_admin_user
        reconcile_unassigned_group(tenant, created_by=granter)
        ensure_default_read_access_for_user(tenant, member.user, granter=granter)


def _active_grants(principals: list[Principal], resource: Resource):
    now = timezone.now()
    return Grant.objects.filter(
        principal__in=principals,
        resource=resource,
    ).filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))


def _user_for_principal(principal: Principal):
    if principal.type != Principal.TYPE_USER:
        return None
    User = apps.get_model('user', 'User')
    return User.objects.filter(uid=principal.external_id).first()


def _principal_set(principal: Principal, resource: Resource) -> list[Principal]:
    principals = [principal]
    user = _user_for_principal(principal)
    if user is None or resource.tenant_id is None:
        return principals
    group_ids = GroupMembership.objects.filter(
        user=user,
        group__tenant_id=resource.tenant_id,
    ).values_list('group__group_id', flat=True)
    principals.extend(Principal.objects.filter(
        type=Principal.TYPE_GROUP,
        external_id__in=[str(group_id) for group_id in group_ids],
    ))
    return principals


def _tenant_bypass(principal: Principal, resource: Resource) -> tuple[int, dict | None]:
    user = _user_for_principal(principal)
    tenant = resource.tenant
    if user is None or tenant is None:
        return 0, None
    if tenant.status != Tenant.STATUS_ACTIVE:
        return -1, None
    if tenant.type == Tenant.TYPE_PERSONAL and tenant.owner_user_id == user.id:
        return SUPER_ADMIN, {'kind': 'tenant_owner', 'tenant_id': tenant.external_id, 'role': 'manager'}
    if tenant.type == Tenant.TYPE_ORGANIZATION:
        Company = apps.get_model('company', 'Company')
        CompanyMember = apps.get_model('company', 'CompanyMember')
        cid = tenant.external_id.removeprefix('organization:')
        if not Company.objects.filter(
            cid=cid,
            verification_status='verified',
            verification_suspended_at__isnull=True,
            service_suspended_at__isnull=True,
        ).exists():
            return -1, None
        if tenant.super_admin_user_id == user.id:
            return SUPER_ADMIN, {'kind': 'tenant_super_admin', 'tenant_id': tenant.external_id, 'role': 'manager'}
        if not CompanyMember.objects.filter(
            company__cid=cid,
            user=user,
            status=CompanyMember.STATUS_ACTIVE,
        ).exists():
            return -1, None
    active_member = TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        status=TenantMembership.STATUS_ACTIVE,
    ).exists()
    if not active_member:
        return -1, None
    return 0, None


def compute_effective_permission(principal: Principal, resource: Resource) -> EffectivePermission:
    """合并用户直授、用户所在组授权和父资源继承，并严格限制 Tenant。"""

    bypass, bypass_source = _tenant_bypass(principal, resource)
    if bypass < 0:
        return EffectivePermission(principal, resource, 0, ())
    if bypass:
        return EffectivePermission(principal, resource, bypass, (bypass_source,))

    principals = _principal_set(principal, resource)
    max_permission = 0
    sources = []
    for node in get_resource_ancestors(resource):
        for grant in _active_grants(principals, node).select_related('principal'):
            if node != resource and not grant.inherit:
                continue
            max_permission = max(max_permission, grant.permissions)
            sources.append({
                'kind': 'group' if grant.principal.type == Principal.TYPE_GROUP else 'direct',
                'principal_type': grant.principal.type,
                'principal_external_id': grant.principal.external_id,
                'resource_type': node.type,
                'resource_external_id': node.external_id,
                'inherited': node.id != resource.id,
                'role': ROLE_NAMES.get(grant.permissions, 'none'),
            })
    return EffectivePermission(principal, resource, max_permission, tuple(sources))


def compute_effective_permissions(
    principal: Principal, resources: Iterable[Resource],
) -> dict[int, EffectivePermission]:
    """Batch the same tenant, group, inheritance and expiry rules as single checks."""
    resources = list(resources)
    contexts = {}
    results = {}
    nodes_by_resource = {}
    principal_ids = set()
    resource_ids = set()
    for resource in resources:
        if resource.tenant_id not in contexts:
            bypass, source = _tenant_bypass(principal, resource)
            principals = _principal_set(principal, resource) if bypass == 0 else []
            contexts[resource.tenant_id] = (bypass, source, {item.id for item in principals})
        bypass, source, allowed_principals = contexts[resource.tenant_id]
        if bypass != 0:
            results[resource.id] = EffectivePermission(
                principal, resource, max(bypass, 0), (source,) if source else (),
            )
            continue
        nodes = tuple(get_resource_ancestors(resource))
        nodes_by_resource[resource.id] = nodes
        resource_ids.update(node.id for node in nodes)
        principal_ids.update(allowed_principals)
    now = timezone.now()
    grants_by_resource = {}
    if resource_ids:
        grants = Grant.objects.filter(
            principal_id__in=principal_ids, resource_id__in=resource_ids,
        ).filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)).select_related('principal')
        for grant in grants:
            grants_by_resource.setdefault(grant.resource_id, []).append(grant)
    for resource in resources:
        if resource.id in results:
            continue
        allowed_principals = contexts[resource.tenant_id][2]
        permission = 0
        sources = []
        for node in nodes_by_resource[resource.id]:
            for grant in grants_by_resource.get(node.id, []):
                if grant.principal_id not in allowed_principals or (node.id != resource.id and not grant.inherit):
                    continue
                permission = max(permission, grant.permissions)
                sources.append({
                    'kind': 'group' if grant.principal.type == Principal.TYPE_GROUP else 'direct',
                    'principal_type': grant.principal.type,
                    'principal_external_id': grant.principal.external_id,
                    'resource_type': node.type, 'resource_external_id': node.external_id,
                    'inherited': node.id != resource.id,
                    'role': ROLE_NAMES.get(grant.permissions, 'none'),
                })
        results[resource.id] = EffectivePermission(principal, resource, permission, tuple(sources))
    return results


def user_effective_permission(user: User, resource: Resource) -> EffectivePermission:
    principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
    return compute_effective_permission(principal, resource)


def is_granted(principal: Principal, resource: Resource, permissions: int) -> bool:
    return compute_effective_permission(principal, resource).permissions >= permissions


def is_action_allowed(principal: Principal, resource: Resource, action: str) -> bool:
    required = ACTION_MIN_ROLE.get(resource.type, {}).get(action)
    return required is not None and is_granted(principal, resource, required)


def is_user_action_allowed(user: User, resource: Resource, action: str) -> bool:
    return is_action_allowed(get_or_create_principal(Principal.TYPE_USER, user.uid), resource, action)


def visible_resource_ids(user: User, resource_type: str, tenant: Tenant | None = None) -> list[str]:
    queryset = Resource.objects.filter(type=resource_type).select_related('tenant', 'parent')
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
    return [
        resource.external_id for resource in queryset
        if compute_effective_permission(principal, resource).can_read()
    ]


def _active_direct_agent_grant(principal: Principal, resource: Resource):
    return Grant.objects.filter(
        principal=principal,
        resource=resource,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
    ).first()


def agent_project_table_permission(principal: Principal, resource: Resource) -> int:
    """Resolve Agent project/table grants with per-table overrides.

    A direct table grant, including an explicit zero grant, overrides its
    project's grant. Tables configured as independent never inherit the
    project's Agent grant. This mirrors the knowledge panel's file exceptions
    without changing employee IAM inheritance behavior.
    """
    if principal.type != Principal.TYPE_AGENT:
        return 0
    if resource.type == Resource.TYPE_PROJECT:
        grant = _active_direct_agent_grant(principal, resource)
        return min(int(grant.permissions), WRITE) if grant else 0
    if resource.type != Resource.TYPE_TABLE:
        return 0
    table_grant = _active_direct_agent_grant(principal, resource)
    if table_grant is not None:
        return min(int(table_grant.permissions), WRITE)
    if resource.permission_mode == Resource.PERMISSION_MODE_INDEPENDENT or not resource.parent_id:
        return 0
    project_grant = _active_direct_agent_grant(principal, resource.parent)
    if project_grant is None or not project_grant.inherit:
        return 0
    return min(int(project_grant.permissions), WRITE)


def runtime_action_allowed(
    user: User,
    agent_external_id: str,
    target_resource: Resource,
    target_action: str,
) -> bool:
    """运行时取用户权限、Agent 身份权限和目标动作策略的交集。"""

    agent_resource = Resource.objects.filter(
        type=Resource.TYPE_AGENT,
        external_id=agent_external_id,
    ).first()
    if agent_resource is None or agent_resource.tenant_id != target_resource.tenant_id:
        return False
    user_principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
    if not is_action_allowed(user_principal, agent_resource, 'run'):
        return False
    if not is_action_allowed(user_principal, target_resource, target_action):
        return False
    agent_principal = get_or_create_principal(Principal.TYPE_AGENT, agent_external_id)
    if target_resource.type in (Resource.TYPE_PROJECT, Resource.TYPE_TABLE):
        required = ACTION_MIN_ROLE.get(target_resource.type, {}).get(target_action)
        return required is not None and agent_project_table_permission(
            agent_principal, target_resource,
        ) >= required
    return is_action_allowed(agent_principal, target_resource, target_action)


def grant_permissions(
    principal: Principal,
    resource: Resource,
    permissions: int,
    granter_user: Optional[User] = None,
    inherit: bool = True,
    expires_at: Optional[timezone.datetime] = None,
) -> Grant:
    grant_obj, _ = Grant.objects.update_or_create(
        principal=principal,
        resource=resource,
        defaults={
            'permissions': permissions,
            'granter': granter_user,
            'expires_at': expires_at,
            'inherit': inherit,
        },
    )
    return grant_obj


def revoke_permissions(principal: Principal, resource: Resource) -> None:
    Grant.objects.filter(principal=principal, resource=resource).delete()


def can_manage_permissions(user: User, resource: Resource) -> bool:
    return user_effective_permission(user, resource).permissions >= Grant.ROLE_MANAGER


def _validate_principal_tenant(principal: Principal, resource: Resource) -> None:
    tenant = resource.tenant
    if tenant is None:
        raise ValueError('资源尚未绑定权限空间')
    if principal.type == Principal.TYPE_GROUP:
        if not Group.objects.filter(group_id=principal.external_id, tenant=tenant).exists():
            raise ValueError('分组不属于当前权限空间')
        return
    if principal.type == Principal.TYPE_USER:
        user = _user_for_principal(principal)
        if user is None or not TenantMembership.objects.filter(
            tenant=tenant, user=user, status=TenantMembership.STATUS_ACTIVE,
        ).exists():
            raise ValueError('用户不是当前企业的有效成员')
        return
    raise ValueError('只允许给用户或分组授权')


@transaction.atomic
def set_resource_role(
    actor: User,
    resource: Resource,
    principal: Principal,
    role: str,
    *,
    inherit: bool = True,
    ip_address: str | None = None,
) -> Grant:
    if role not in ROLE_VALUES:
        raise ValueError('无效的资源角色')
    if not can_manage_permissions(actor, resource):
        _audit(actor, 'grant.update', resource, principal, {}, {}, PermissionAuditLog.RESULT_DENIED, ip_address)
        raise PermissionError('只有资源管理员可以分配权限')
    _validate_principal_tenant(principal, resource)
    if resource.tenant and resource.tenant.super_admin_user_id:
        target = _user_for_principal(principal)
        if target and target.id == resource.tenant.super_admin_user_id:
            raise ValueError('企业最高管理员权限由企业管理员交接流程维护')
    existing = Grant.objects.filter(principal=principal, resource=resource).first()
    before = _grant_state(existing)
    grant = grant_permissions(principal, resource, ROLE_VALUES[role], actor, inherit)
    _audit(actor, 'grant.update', resource, principal, before, _grant_state(grant), PermissionAuditLog.RESULT_SUCCESS, ip_address)
    return grant


@transaction.atomic
def remove_resource_role(
    actor: User,
    resource: Resource,
    principal: Principal,
    *,
    ip_address: str | None = None,
) -> bool:
    if not can_manage_permissions(actor, resource):
        _audit(actor, 'grant.revoke', resource, principal, {}, {}, PermissionAuditLog.RESULT_DENIED, ip_address)
        raise PermissionError('只有资源管理员可以撤销权限')
    if resource.tenant and resource.tenant.super_admin_user_id:
        target = _user_for_principal(principal)
        if target and target.id == resource.tenant.super_admin_user_id:
            raise ValueError('不能移除企业最高管理员')
    existing = Grant.objects.filter(principal=principal, resource=resource).first()
    if existing is None:
        return False
    before = _grant_state(existing)
    existing.delete()
    _audit(actor, 'grant.revoke', resource, principal, before, {}, PermissionAuditLog.RESULT_SUCCESS, ip_address)
    return True


def _grant_state(grant: Grant | None) -> dict:
    if grant is None:
        return {}
    return {
        'role': ROLE_NAMES.get(grant.permissions, 'none'),
        'inherit': grant.inherit,
        'expires_at': grant.expires_at.isoformat() if grant.expires_at else None,
    }


def _audit(actor, action, resource, principal, before, after, result, ip_address=None, context=None):
    PermissionAuditLog.objects.create(
        tenant=resource.tenant,
        actor=actor,
        action=action,
        resource=resource,
        target_principal=principal,
        before=before,
        after=after,
        result=result,
        context=context or {},
        ip_address=ip_address,
    )
