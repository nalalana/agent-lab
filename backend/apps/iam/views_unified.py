"""Tenant-aware resource permission management APIs."""

import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyMember,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    companyGate,
)
from apps.company.permissions import is_company_verified_for_formal_access
from apps.company.services import CompanyService
from apps.opengate.models import Gates, GatesTable
from apps.user.models import User
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request

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
from .services import (
    ROLE_NAMES,
    ROLE_VALUES,
    UNASSIGNED_GROUP_NAME,
    compute_effective_permission,
    ensure_default_read_access_for_resource,
    get_or_create_organization_tenant,
    get_or_create_principal,
    get_or_create_resource,
    reconcile_unassigned_group,
    remove_resource_role,
    set_resource_role,
    _audit,
    _grant_state,
    grant_permissions,
)


def _json_payload(request):
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError('请求体必须是 JSON 对象')
    if not isinstance(payload, dict):
        raise ValueError('请求体必须是 JSON 对象')
    return payload


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR')) or None


def _ensure_company_resources(company, tenant):
    bindings = list(companyGate.objects.filter(company=company).select_related('opengate', 'opengate__owner'))
    projects = [binding.opengate for binding in bindings]
    project_ids = [project.id for project in projects]
    tables = list(GatesTable.objects.filter(gate_id__in=project_ids).select_related('gate', 'gate__owner')) if project_ids else []
    transparent = TransparentCompany.objects.filter(company=company).first()
    agents = []
    if transparent:
        agents = list(TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany=transparent,
        ).exclude(status=TransparentCompanyAgentDefinition.STATUS_DEPRECATED))

    external_ids = [company.cid]
    external_ids.extend(project.gid for project in projects)
    external_ids.extend(table.tid for table in tables)
    external_ids.extend(f'agent:{company.cid}:{agent.agentId}' for agent in agents)
    if transparent:
        external_ids.append(f'knowledge:{company.cid}')
    known_resources = {
        resource.external_id: resource
        for resource in Resource.objects.filter(external_id__in=external_ids)
    }

    def ensure(resource_type, external_id, *, parent=None, creator=None):
        resource = known_resources.get(external_id)
        if resource is None:
            resource = get_or_create_resource(
                resource_type,
                external_id,
                parent=parent,
                tenant=tenant,
                creator=creator,
            )
            ensure_default_read_access_for_resource(
                resource,
                granter=tenant.super_admin_user,
            )
            known_resources[external_id] = resource
            return resource
        changed = []
        for field, value in (
            ('type', resource_type),
            ('parent_id', parent.id if parent else None),
            ('tenant_id', tenant.id),
            ('creator_id', creator.id if creator else None),
        ):
            if getattr(resource, field) != value:
                setattr(resource, field, value)
                changed.append(field.removesuffix('_id'))
        if changed:
            resource.save(update_fields=changed)
        return resource

    company_resource = ensure(
        Resource.TYPE_COMPANY,
        company.cid,
        creator=company.account,
    )
    project_resources = {
        project.id: ensure(
            Resource.TYPE_PROJECT,
            project.gid,
            creator=project.owner,
        )
        for project in projects
    }
    for table in tables:
        ensure(
            Resource.TYPE_TABLE,
            table.tid,
            parent=project_resources[table.gate_id],
            creator=table.gate.owner,
        )
    for agent in agents:
        ensure(
            Resource.TYPE_AGENT,
            f'agent:{company.cid}:{agent.agentId}',
            creator=company.account,
        )
    if transparent:
        ensure(
            Resource.TYPE_KNOWLEDGE_BASE,
            f'knowledge:{company.cid}',
            creator=company.account,
        )
    return company_resource


def _context(request, cid):
    user = get_user_from_request(request)
    if not user:
        return None, None, None, JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    company = Company.objects.filter(cid=cid).first() if isinstance(cid, str) else None
    if not company or not is_company_verified_for_formal_access(company):
        return None, None, None, JsonResponse({'result': 0, 'message': '企业不存在或尚未认证'}, status=404)
    tenant = get_or_create_organization_tenant(company, sync_memberships=False)
    membership = TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        status=TenantMembership.STATUS_ACTIVE,
    ).first()
    is_super_admin = tenant.super_admin_user_id == user.id
    is_active_company_member = is_super_admin or CompanyMember.objects.filter(
        company=company,
        user=user,
        status=CompanyMember.STATUS_ACTIVE,
    ).exists()
    if membership is None and is_active_company_member:
        tenant = get_or_create_organization_tenant(company, sync_memberships=True)
        membership = TenantMembership.objects.filter(
            tenant=tenant,
            user=user,
            status=TenantMembership.STATUS_ACTIVE,
        ).first()
    if membership is None or not is_active_company_member:
        return None, None, None, JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=403)
    return company, tenant, user, None


def _is_tenant_admin(tenant, user):
    return tenant.super_admin_user_id == user.id


def _resource_labels(resources, company):
    project_ids = [resource.external_id for resource in resources if resource.type == Resource.TYPE_PROJECT]
    table_ids = [resource.external_id for resource in resources if resource.type == Resource.TYPE_TABLE]
    agent_ids = [resource.external_id.rsplit(':', 1)[-1] for resource in resources if resource.type == Resource.TYPE_AGENT]
    projects = Gates.objects.filter(gid__in=project_ids).only('gid', 'gatename').in_bulk(field_name='gid')
    tables = GatesTable.objects.filter(tid__in=table_ids).only('tid', 'name').in_bulk(field_name='tid')
    agents = {
        agent.agentId: agent
        for agent in TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany__company=company,
            agentId__in=agent_ids,
        ).only('agentId', 'name')
    }
    labels = {}
    for resource in resources:
        if resource.type == Resource.TYPE_PROJECT:
            obj = projects.get(resource.external_id)
            labels[resource.external_id] = obj.gatename if obj else resource.external_id
        elif resource.type == Resource.TYPE_TABLE:
            obj = tables.get(resource.external_id)
            labels[resource.external_id] = obj.name if obj else resource.external_id
        elif resource.type == Resource.TYPE_AGENT:
            raw_id = resource.external_id.rsplit(':', 1)[-1]
            obj = agents.get(raw_id)
            labels[resource.external_id] = obj.name if obj else raw_id
        elif resource.type == Resource.TYPE_KNOWLEDGE_BASE:
            labels[resource.external_id] = '企业知识空间'
        else:
            labels[resource.external_id] = resource.external_id
    return labels


def _effective_resource_roles(tenant, user, resources):
    if _is_tenant_admin(tenant, user):
        return {resource.id: 'manager' for resource in resources}
    principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
    group_ids = GroupMembership.objects.filter(
        user=user,
        group__tenant=tenant,
    ).values_list('group__group_id', flat=True)
    principals = [principal, *Principal.objects.filter(
        type=Principal.TYPE_GROUP,
        external_id__in=[str(group_id) for group_id in group_ids],
    )]
    resource_ids = {resource.id for resource in resources}
    resource_ids.update(resource.parent_id for resource in resources if resource.parent_id)
    grants_by_resource = {}
    for grant in Grant.objects.filter(
        principal__in=principals,
        resource_id__in=resource_ids,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
    ):
        grants_by_resource.setdefault(grant.resource_id, []).append(grant)
    roles = {}
    for resource in resources:
        permission = 0
        nodes = [resource]
        if resource.permission_mode != Resource.PERMISSION_MODE_INDEPENDENT and resource.parent_id:
            nodes.append(resource.parent)
        for node in nodes:
            for grant in grants_by_resource.get(node.id, []):
                if node.id != resource.id and not grant.inherit:
                    continue
                permission = max(permission, grant.permissions)
        roles[resource.id] = ROLE_NAMES.get(permission, 'none')
    return roles


def _serialize_resource(resource, effective_role, labels):
    return {
        'type': resource.type,
        'external_id': resource.external_id,
        'name': labels.get(resource.external_id, resource.external_id),
        'parent_external_id': resource.parent.external_id if resource.parent else None,
        'permission_mode': resource.permission_mode,
        'effective_role': effective_role,
        'can_manage': effective_role == 'manager',
    }


def _serialize_group(group):
    memberships = getattr(group, '_prefetched_objects_cache', {}).get('memberships')
    if memberships is None:
        memberships = group.memberships.select_related('user').order_by('id')
    return {
        'group_id': str(group.group_id),
        'name': group.name,
        'description': group.description,
        'is_system': group.name == UNASSIGNED_GROUP_NAME,
        'parent_group_id': str(group.parent.group_id) if group.parent else None,
        'members': [
            {
                'public_id': membership.user.public_id,
                'uid': membership.user.uid,
                'name': membership.user.username,
            }
            for membership in memberships
        ],
    }


def _users_by_uid(user_ids):
    """User.uid 不是数据库唯一字段，避免对它使用 in_bulk。"""

    users = {}
    for target in User.objects.filter(uid__in=set(user_ids)).order_by('id'):
        users.setdefault(target.uid, target)
    return users


def _get_resource(tenant, resource_type, external_id):
    return Resource.objects.filter(
        tenant=tenant,
        type=resource_type,
        external_id=external_id,
    ).select_related('parent', 'tenant').first()


def _get_group(tenant, group_id):
    try:
        return Group.objects.filter(tenant=tenant, group_id=group_id).first()
    except (ValidationError, ValueError, TypeError):
        return None


@login_checker
def unified_context(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    company, tenant, user, error = _context(request, request.GET.get('cid'))
    if error:
        return error
    membership = TenantMembership.objects.get(tenant=tenant, user=user)
    return JsonResponse({'result': 1, 'data': {
        'tenant': {
            'id': tenant.external_id,
            'type': tenant.type,
            'name': tenant.name or company.company_name,
        },
        'membership_role': membership.role,
        'is_super_admin': _is_tenant_admin(tenant, user),
        'roles': [
            {'value': role, 'label': label}
            for role, label in (
                ('manager', '可管理'),
                ('editor', '可编辑'),
                ('contributor', '可使用'),
                ('viewer', '可阅读'),
            )
        ],
    }})


def _agent_project_table_context(request, *, cid, agent_id):
    user = get_user_from_request(request)
    if user is None:
        return None, None, None, JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    ok, company_or_message = CompanyService._get_company_for_admin(
        str(cid or ''), user, _client_ip(request),
    )
    if not ok:
        return None, None, None, JsonResponse({'result': 0, 'message': company_or_message}, status=403)
    company = company_or_message
    transparent = TransparentCompany.objects.filter(company=company).first()
    agent = TransparentCompanyAgentDefinition.objects.filter(
        transparentCompany=transparent,
        agentId=str(agent_id or '').strip(),
    ).exclude(status=TransparentCompanyAgentDefinition.STATUS_DEPRECATED).first() if transparent else None
    if agent is None:
        return None, None, None, JsonResponse({'result': 0, 'message': 'Agent 不存在'}, status=404)
    tenant = get_or_create_organization_tenant(company, sync_memberships=False)
    _ensure_company_resources(company, tenant)
    return company, tenant, (user, agent), None


def _agent_permission_name(value):
    return {0: 'none', 1: 'read', 2: 'write'}.get(min(int(value or 0), 2), 'none')


@login_checker
def unified_agent_project_tables(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    company, tenant, actor_agent, error = _agent_project_table_context(
        request, cid=request.GET.get('cid'), agent_id=request.GET.get('agent_id'),
    )
    if error:
        return error
    _user, agent = actor_agent
    projects = list(Gates.objects.filter(
        companygate__company=company,
        label=Gates.LABEL_COMPANY,
    ).distinct().order_by('id'))
    project_ids = [project.id for project in projects]
    tables = list(GatesTable.objects.filter(gate_id__in=project_ids).only('id', 'tid', 'name', 'gate_id', 'permission').order_by('id')) if project_ids else []
    external_ids = [project.gid for project in projects] + [table.tid for table in tables]
    resources = {
        resource.external_id: resource
        for resource in Resource.objects.filter(
            tenant=tenant,
            external_id__in=external_ids,
            type__in=(Resource.TYPE_PROJECT, Resource.TYPE_TABLE),
        ).select_related('parent')
    }
    principal = Principal.objects.filter(
        type=Principal.TYPE_AGENT,
        external_id=f'agent:{company.cid}:{agent.agentId}',
    ).first()
    resource_ids = [resource.id for resource in resources.values()]
    active_grants = {}
    if principal and resource_ids:
        active_grants = {
            grant.resource_id: grant
            for grant in Grant.objects.filter(principal=principal, resource_id__in=resource_ids).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
            )
        }
    tables_by_gate = {}
    for table in tables:
        tables_by_gate.setdefault(table.gate_id, []).append(table)
    project_items = []
    for project in projects:
        project_resource = resources.get(project.gid)
        project_grant = active_grants.get(project_resource.id) if project_resource else None
        project_permission = _agent_permission_name(project_grant.permissions if project_grant else 0)
        table_items = []
        for table in tables_by_gate.get(project.id, []):
            table_resource = resources.get(table.tid)
            table_grant = active_grants.get(table_resource.id) if table_resource else None
            inherited = (
                project_permission
                if table_resource
                and table_resource.permission_mode != Resource.PERMISSION_MODE_INDEPENDENT
                and project_grant
                and project_grant.inherit
                else 'none'
            )
            direct = _agent_permission_name(table_grant.permissions) if table_grant else 'none'
            effective = (
                min(int(table_grant.permissions), Grant.PERM_WRITE)
                if table_grant
                else min(int(project_grant.permissions), Grant.PERM_WRITE)
                if inherited != 'none' and project_grant
                else 0
            )
            table_items.append({
                'tid': table.tid,
                'name': table.name,
                'scope': {0: 'private', 1: 'public_read', 2: 'public_write'}.get(table.permission, 'private'),
                'explicit': table_grant is not None,
                'permission': direct,
                'effective_permission': _agent_permission_name(effective),
                'inherited_permission': inherited,
                'permission_mode': table_resource.permission_mode if table_resource else Resource.PERMISSION_MODE_INHERIT,
            })
        project_items.append({
            'gid': project.gid,
            'name': project.gatename,
            'public_access': project.public_access,
            'permission': project_permission,
            'tables': table_items,
        })
    return JsonResponse({'result': 1, 'data': {'projects': project_items}})


@login_checker
@transaction.atomic
def unified_agent_project_table_permission_upsert(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    company, tenant, actor_agent, error = _agent_project_table_context(
        request, cid=payload.get('cid'), agent_id=payload.get('agent_id'),
    )
    if error:
        return error
    user, agent = actor_agent
    resource_type = payload.get('resource_type')
    external_id = str(payload.get('external_id') or '').strip()
    permission = str(payload.get('permission') or '').strip().lower()
    if resource_type not in (Resource.TYPE_PROJECT, Resource.TYPE_TABLE) or permission not in {'none', 'read', 'write', 'inherit'} or (permission == 'inherit' and resource_type != Resource.TYPE_TABLE):
        return JsonResponse({'result': 0, 'message': '资源类型或权限值无效'}, status=400)
    company_projects = Gates.objects.filter(
        companygate__company=company,
        label=Gates.LABEL_COMPANY,
    ).distinct()
    if resource_type == Resource.TYPE_PROJECT:
        belongs = company_projects.filter(gid=external_id).exists()
    else:
        belongs = GatesTable.objects.filter(tid=external_id, gate__in=company_projects).exists()
    if not belongs:
        return JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=404)
    resource = Resource.objects.filter(
        tenant=tenant,
        type=resource_type,
        external_id=external_id,
    ).select_related('parent').first()
    if resource is None:
        return JsonResponse({'result': 0, 'message': '资源目录尚未同步'}, status=409)
    principal = get_or_create_principal(Principal.TYPE_AGENT, f'agent:{company.cid}:{agent.agentId}')
    grant = Grant.objects.filter(principal=principal, resource=resource).first()
    if resource_type == Resource.TYPE_PROJECT:
        child_grants = list(Grant.objects.filter(
            principal=principal,
            resource__tenant=tenant,
            resource__type=Resource.TYPE_TABLE,
            resource__parent=resource,
        ).select_related('resource__tenant'))
        for child_grant in child_grants:
            before = _grant_state(child_grant)
            child_resource = child_grant.resource
            child_grant.delete()
            _audit(
                user, 'grant.revoke', child_resource, principal, before, {},
                PermissionAuditLog.RESULT_SUCCESS, _client_ip(request),
                context={'source': 'agent_project_table_folder_update'},
            )
    if permission == 'inherit':
        if grant is not None:
            before = _grant_state(grant)
            grant.delete()
            _audit(
                user, 'grant.revoke', resource, principal, before, {},
                PermissionAuditLog.RESULT_SUCCESS, _client_ip(request),
                context={'source': 'agent_project_table_override_reset'},
            )
    elif permission == 'none':
        inherited = (
            resource_type == Resource.TYPE_TABLE
            and resource.permission_mode != Resource.PERMISSION_MODE_INDEPENDENT
            and resource.parent_id
            and (parent_grant := Grant.objects.filter(principal=principal, resource_id=resource.parent_id).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
            ).first()) is not None
            and parent_grant.inherit
            and parent_grant.permissions > 0
        )
        if inherited:
            before = _grant_state(grant)
            updated = grant_permissions(principal, resource, 0, user, True)
            _audit(
                user, 'grant.update', resource, principal, before, _grant_state(updated),
                PermissionAuditLog.RESULT_SUCCESS, _client_ip(request),
                context={'source': 'agent_project_table_override'},
            )
        elif grant is not None:
            before = _grant_state(grant)
            grant.delete()
            _audit(
                user, 'grant.revoke', resource, principal, before, {},
                PermissionAuditLog.RESULT_SUCCESS, _client_ip(request),
                context={'source': 'agent_project_table_override'},
            )
    else:
        before = _grant_state(grant)
        updated = grant_permissions(
            principal,
            resource,
            Grant.PERM_READ if permission == 'read' else Grant.PERM_WRITE,
            user,
            True,
        )
        _audit(
            user, 'grant.update', resource, principal, before, _grant_state(updated),
            PermissionAuditLog.RESULT_SUCCESS, _client_ip(request),
            context={'source': 'agent_project_table_permission'},
        )
    return JsonResponse({'result': 1, 'message': 'Agent 项目表格权限已保存', 'data': {'permission': permission}})


@login_checker
def unified_resources(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    company, tenant, user, error = _context(request, request.GET.get('cid'))
    if error:
        return error
    _ensure_company_resources(company, tenant)
    queryset = Resource.objects.filter(tenant=tenant).exclude(type=Resource.TYPE_COMPANY).select_related('parent')
    requested_type = request.GET.get('type')
    if requested_type:
        queryset = queryset.filter(type=requested_type)
    resources = list(queryset.order_by('type', 'id'))
    roles = _effective_resource_roles(tenant, user, resources)
    labels = _resource_labels(resources, company)
    resources = [
        _serialize_resource(resource, roles[resource.id], labels)
        for resource in resources
        if roles[resource.id] != 'none'
    ]
    return JsonResponse({'result': 1, 'data': resources})


@login_checker
def unified_resource_grants(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    company, tenant, user, error = _context(request, request.GET.get('cid'))
    if error:
        return error
    resource = _get_resource(tenant, request.GET.get('resource_type'), request.GET.get('external_id'))
    if not resource:
        return JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=404)
    effective = compute_effective_permission(get_or_create_principal(Principal.TYPE_USER, user.uid), resource)
    if effective.permissions < Grant.ROLE_MANAGER:
        return JsonResponse({'result': 0, 'message': '无权管理该资源'}, status=403)
    grant_rows = list(Grant.objects.filter(resource=resource).select_related('principal', 'granter').order_by('id'))
    user_ids = [grant.principal.external_id for grant in grant_rows if grant.principal.type == Principal.TYPE_USER]
    group_ids = [grant.principal.external_id for grant in grant_rows if grant.principal.type == Principal.TYPE_GROUP]
    users = _users_by_uid(user_ids)
    groups = {
        str(group.group_id): group
        for group in Group.objects.filter(group_id__in=group_ids)
    }
    grants = []
    for grant in grant_rows:
        display_name = grant.principal.external_id
        public_id = None
        if grant.principal.type == Principal.TYPE_USER:
            target = users.get(grant.principal.external_id)
            if target:
                display_name = target.username or target.public_id
                public_id = target.public_id
        elif grant.principal.type == Principal.TYPE_GROUP:
            group = groups.get(grant.principal.external_id)
            if group:
                display_name = group.name
        grants.append({
            'principal_type': grant.principal.type,
            'principal_external_id': grant.principal.external_id,
            'public_id': public_id,
            'display_name': display_name,
            'role': ROLE_NAMES.get(grant.permissions, 'none'),
            'inherit': grant.inherit,
            'granter_public_id': grant.granter.public_id if grant.granter else None,
            'created_at': grant.created_at.isoformat(),
        })
    return JsonResponse({'result': 1, 'data': {
        'resource': _serialize_resource(
            resource,
            effective.role,
            _resource_labels([resource], company),
        ),
        'grants': grants,
    }})


@login_checker
def unified_grant_upsert(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    _company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    resource = _get_resource(tenant, payload.get('resource_type'), payload.get('external_id'))
    if not resource:
        return JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=404)
    principal_type = payload.get('principal_type')
    principal_external_id = payload.get('principal_external_id')
    if principal_type == Principal.TYPE_USER:
        target = User.objects.filter(public_id=str(principal_external_id).strip().upper()).first()
        if not target:
            return JsonResponse({'result': 0, 'message': '用户 ID 不存在'}, status=400)
        principal_external_id = target.uid
    elif principal_type == Principal.TYPE_GROUP:
        group = _get_group(tenant, principal_external_id)
        if not group:
            return JsonResponse({'result': 0, 'message': '分组不存在'}, status=400)
        principal_external_id = str(group.group_id)
    else:
        return JsonResponse({'result': 0, 'message': '授权对象只能是用户或分组'}, status=400)
    principal = get_or_create_principal(principal_type, principal_external_id)
    try:
        grant = set_resource_role(
            user,
            resource,
            principal,
            payload.get('role'),
            inherit=payload.get('inherit', True) is True,
            ip_address=_client_ip(request),
        )
    except PermissionError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    return JsonResponse({'result': 1, 'message': '权限已保存', 'data': {
        'role': ROLE_NAMES[grant.permissions],
        'inherit': grant.inherit,
    }})


@login_checker
def unified_grant_revoke(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    _company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    resource = _get_resource(tenant, payload.get('resource_type'), payload.get('external_id'))
    if not resource:
        return JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=404)
    principal_type = payload.get('principal_type')
    external_id = payload.get('principal_external_id')
    if principal_type == Principal.TYPE_USER:
        target = User.objects.filter(public_id=str(external_id).strip().upper()).first()
        external_id = target.uid if target else external_id
    principal = Principal.objects.filter(type=principal_type, external_id=external_id).first()
    if not principal:
        return JsonResponse({'result': 1, 'message': '权限已撤销', 'data': {'changed': False}})
    try:
        changed = remove_resource_role(user, resource, principal, ip_address=_client_ip(request))
    except PermissionError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    return JsonResponse({'result': 1, 'message': '权限已撤销', 'data': {'changed': changed}})


@login_checker
def unified_groups(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    _company, tenant, user, error = _context(request, request.GET.get('cid'))
    if error:
        return error
    if not _is_tenant_admin(tenant, user):
        return JsonResponse({'result': 0, 'message': '只有企业最高管理员可以管理分组'}, status=403)
    reconcile_unassigned_group(tenant, created_by=user)
    groups = Group.objects.filter(tenant=tenant).select_related('parent').prefetch_related(
        Prefetch('memberships', queryset=GroupMembership.objects.select_related('user').order_by('id')),
    )
    return JsonResponse({'result': 1, 'data': [_serialize_group(group) for group in groups.order_by('name')]})


@login_checker
@transaction.atomic
def unified_group_upsert(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    _company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    if not _is_tenant_admin(tenant, user):
        return JsonResponse({'result': 0, 'message': '只有企业最高管理员可以管理部门'}, status=403)
    Tenant.objects.select_for_update().get(pk=tenant.pk)
    name = str(payload.get('name') or '').strip()
    if not name or len(name) > 100:
        return JsonResponse({'result': 0, 'message': '部门名称不能为空且不能超过100字'}, status=400)
    requested_group_id = payload.get('group_id')
    group = _get_group(tenant, requested_group_id) if requested_group_id else None
    if requested_group_id and group is None:
        return JsonResponse({'result': 0, 'message': '部门不存在'}, status=404)
    if name == UNASSIGNED_GROUP_NAME or (group and group.name == UNASSIGNED_GROUP_NAME):
        return JsonResponse({'result': 0, 'message': '“暂未分组”为系统节点，不能手动创建或修改'}, status=400)
    parent = group.parent if group else None
    if 'parent_group_id' in payload:
        parent_id = payload['parent_group_id']
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
            return JsonResponse({'result': 0, 'message': '上级部门格式不正确'}, status=400)
        parent = _get_group(tenant, parent_id) if parent_id else None
        if parent_id and parent is None:
            return JsonResponse({'result': 0, 'message': '上级部门不存在或不属于该企业'}, status=400)
    ancestors, current = set(), parent
    while current:
        if current.tenant_id != tenant.id or current.name == UNASSIGNED_GROUP_NAME:
            return JsonResponse({'result': 0, 'message': '请选择该企业的普通部门作为上级部门'}, status=400)
        if current.pk in ancestors or (group and current.pk == group.pk):
            return JsonResponse({'result': 0, 'message': '不能将部门移动至自身或其下级部门'}, status=400)
        ancestors.add(current.pk)
        if len(ancestors) >= 25:
            return JsonResponse({'result': 0, 'message': '部门层级不能超过25层'}, status=400)
        current = current.parent
    # Check descendant height too, so moving an existing subtree cannot bypass the depth limit.
    if group:
        links = {}
        for group_id, parent_id in Group.objects.filter(tenant=tenant).values_list('id', 'parent_id'):
            links.setdefault(parent_id, []).append(group_id)
        pending, visited = [(group.pk, 1)], set()
        while pending:
            node, depth = pending.pop()
            if node in visited or len(ancestors) + depth > 25:
                return JsonResponse({'result': 0, 'message': '部门层级无效或超过25层'}, status=400)
            visited.add(node)
            pending.extend((child, depth + 1) for child in links.get(node, []))
    before = {
        'group_id': str(group.group_id), 'name': group.name, 'description': group.description,
        'parent_group_id': str(group.parent.group_id) if group.parent else None,
    } if group else {}
    if group is None:
        group = Group(tenant=tenant, created_by=user)
    if Group.objects.filter(tenant=tenant, name=name).exclude(pk=group.pk).exists():
        return JsonResponse({'result': 0, 'message': '部门名称已存在'}, status=400)
    group.name = name
    group.description = str(payload.get('description') or '').strip()[:500]
    group.parent = parent
    group.save()
    get_or_create_principal(Principal.TYPE_GROUP, str(group.group_id))
    PermissionAuditLog.objects.create(
        tenant=tenant, actor=user, action='group.update' if before else 'group.create', before=before,
        after={
            'group_id': str(group.group_id), 'name': group.name, 'description': group.description,
            'parent_group_id': str(parent.group_id) if parent else None,
        }, ip_address=_client_ip(request),
    )
    return JsonResponse({'result': 1, 'message': '部门已保存', 'data': _serialize_group(group)})

@login_checker
@transaction.atomic
def unified_group_delete(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    _company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    if not _is_tenant_admin(tenant, user):
        return JsonResponse({'result': 0, 'message': '只有企业最高管理员可以管理分组'}, status=403)
    Tenant.objects.select_for_update().get(pk=tenant.pk)
    group = _get_group(tenant, payload.get('group_id'))
    if not group:
        return JsonResponse({'result': 1, 'message': '分组已删除', 'data': {'changed': False}})
    if group.name == UNASSIGNED_GROUP_NAME:
        return JsonResponse({'result': 0, 'message': '系统分组不能删除'}, status=400)
    if group.children.exists():
        return JsonResponse({'result': 0, 'message': '部门仍有下级部门，请先调整下级部门'}, status=400)
    active_ids = CompanyMember.objects.filter(
        company=_company, status=CompanyMember.STATUS_ACTIVE, user__isnull=False,
    ).values_list('user_id', flat=True)
    if group.memberships.filter(Q(user_id__in=active_ids) | Q(user_id=tenant.super_admin_user_id)).exists():
        return JsonResponse({'result': 0, 'message': '部门仍有成员，请先调整成员所属部门'}, status=400)
    before = {'group_id': str(group.group_id), 'name': group.name,
              'parent_group_id': str(group.parent.group_id) if group.parent else None}
    group_id = str(group.group_id)
    Principal.objects.filter(type=Principal.TYPE_GROUP, external_id=group_id).delete()
    group.delete()
    reconcile_unassigned_group(tenant, created_by=user)
    PermissionAuditLog.objects.create(
        tenant=tenant,
        actor=user,
        action='group.delete',
        before=before,
        ip_address=_client_ip(request),
    )
    return JsonResponse({'result': 1, 'message': '分组已删除', 'data': {'changed': True}})


@login_checker
@transaction.atomic
def unified_group_member(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    _company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    if not _is_tenant_admin(tenant, user):
        return JsonResponse({'result': 0, 'message': '只有企业最高管理员可以管理分组'}, status=403)
    Tenant.objects.select_for_update().get(pk=tenant.pk)
    group = _get_group(tenant, payload.get('group_id'))
    target = User.objects.filter(public_id=str(payload.get('public_id') or '').strip().upper()).first()
    if not group or not target:
        return JsonResponse({'result': 0, 'message': '分组或用户不存在'}, status=404)
    if group.name == UNASSIGNED_GROUP_NAME:
        return JsonResponse({'result': 0, 'message': '系统分组由系统自动维护'}, status=400)
    if not TenantMembership.objects.filter(
        tenant=tenant, user=target, status=TenantMembership.STATUS_ACTIVE,
    ).exists():
        return JsonResponse({'result': 0, 'message': '请先将该用户添加为企业员工'}, status=400)
    operation = payload.get('operation', 'add')
    if operation == 'add':
        _, changed = GroupMembership.objects.get_or_create(
            group=group,
            user=target,
            defaults={'created_by': user},
        )
    elif operation == 'remove':
        changed, _ = GroupMembership.objects.filter(group=group, user=target).delete()
        changed = bool(changed)
    else:
        return JsonResponse({'result': 0, 'message': '无效的分组成员操作'}, status=400)
    reconcile_unassigned_group(tenant, created_by=user)
    PermissionAuditLog.objects.create(
        tenant=tenant,
        actor=user,
        action=f'group.member.{operation}',
        after={'group_id': str(group.group_id), 'public_id': target.public_id},
        ip_address=_client_ip(request),
    )
    group.refresh_from_db()
    return JsonResponse({'result': 1, 'message': '分组成员已更新', 'data': {
        'changed': changed,
        'group': _serialize_group(group),
    }})


@login_checker
def unified_permission_mode(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    _company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    resource = _get_resource(tenant, payload.get('resource_type'), payload.get('external_id'))
    if not resource or resource.type != Resource.TYPE_TABLE:
        return JsonResponse({'result': 0, 'message': '表格资源不存在'}, status=404)
    effective = compute_effective_permission(get_or_create_principal(Principal.TYPE_USER, user.uid), resource)
    if effective.permissions < Grant.ROLE_MANAGER:
        return JsonResponse({'result': 0, 'message': '只有资源管理员可以修改继承方式'}, status=403)
    mode = payload.get('permission_mode')
    if mode not in dict(Resource.PERMISSION_MODE_CHOICES):
        return JsonResponse({'result': 0, 'message': '无效的继承方式'}, status=400)
    before = resource.permission_mode
    resource.permission_mode = mode
    resource.save(update_fields=['permission_mode'])
    PermissionAuditLog.objects.create(
        tenant=tenant,
        actor=user,
        action='resource.permission_mode.update',
        resource=resource,
        before={'permission_mode': before},
        after={'permission_mode': mode},
        ip_address=_client_ip(request),
    )
    return JsonResponse({'result': 1, 'message': '继承方式已更新', 'data': {'permission_mode': mode}})


@login_checker
def unified_explain(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    _company, tenant, user, error = _context(request, request.GET.get('cid'))
    if error:
        return error
    resource = _get_resource(tenant, request.GET.get('resource_type'), request.GET.get('external_id'))
    if not resource:
        return JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=404)
    target = user
    public_id = request.GET.get('public_id')
    if public_id:
        actor_permission = compute_effective_permission(
            get_or_create_principal(Principal.TYPE_USER, user.uid), resource,
        )
        if actor_permission.permissions < Grant.ROLE_MANAGER:
            return JsonResponse({'result': 0, 'message': '无权查看其他用户的权限来源'}, status=403)
        target = User.objects.filter(public_id=public_id.strip().upper()).first()
        if not target:
            return JsonResponse({'result': 0, 'message': '用户 ID 不存在'}, status=404)
    effective = compute_effective_permission(
        get_or_create_principal(Principal.TYPE_USER, target.uid), resource,
    )
    if target.id == user.id and not effective.can_read():
        return JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=404)
    return JsonResponse({'result': 1, 'data': {
        'public_id': target.public_id,
        'effective_role': effective.role,
        'sources': list(effective.sources),
    }})
