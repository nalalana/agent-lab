"""Human-scoped company administration, separate from broad IAM grants."""

from types import SimpleNamespace

from django.db import transaction

from apps.company.models import Company, CompanyMember, CompanyScopedAdmin, TransparentCompany, TransparentCompanyAgentDefinition
from apps.company.permissions import can_access_formal_company, is_company_verified_for_formal_access
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import compute_effective_permission, compute_effective_permissions, is_user_action_allowed
from apps.user.models import User


FILE_ACTIONS = {'read', 'write', 'delete'}


def scope_for(company, user):
    if not user or not is_company_verified_for_formal_access(company):
        return None
    if not CompanyMember.objects.filter(company=company, user=user, status=CompanyMember.STATUS_ACTIVE).exists():
        return None
    return CompanyScopedAdmin.objects.filter(company=company, user=user, active=True).first()


def file_allowed(company, user, action, logical_path):
    if can_access_formal_company(company, user, Grant.PERM_ADMIN):
        return True
    knowledge_resource = Resource.objects.filter(
        type=Resource.TYPE_KNOWLEDGE_BASE,
        external_id=f'knowledge:{company.cid}',
    ).first()
    unified_action = {
        'read': 'query',
        'write': 'add_content',
        'delete': 'delete_content',
    }.get(action)
    if knowledge_resource and unified_action and is_user_action_allowed(user, knowledge_resource, unified_action):
        return True
    scope = scope_for(company, user)
    if not scope or action not in FILE_ACTIONS:
        return False
    from apps.agent_runtime.knowledge.workspace import normalize_path

    try:
        path = normalize_path(logical_path)
    except ValueError:
        return False
    return any(
        action in rule.get('actions', [])
        and (path == rule.get('path') or path.startswith(f"{rule.get('path')}/"))
        for rule in scope.file_rules
        if isinstance(rule, dict)
    )


def filter_readable_tree(company, user, data):
    if can_access_formal_company(company, user, Grant.PERM_ADMIN):
        return data
    knowledge_resource = Resource.objects.filter(
        type=Resource.TYPE_KNOWLEDGE_BASE,
        external_id=f'knowledge:{company.cid}',
    ).first()
    if knowledge_resource and is_user_action_allowed(user, knowledge_resource, 'query'):
        return data
    scope = scope_for(company, user)
    readable = [
        rule['path'] for rule in (scope.file_rules if scope else [])
        if isinstance(rule, dict) and isinstance(rule.get('path'), str)
        and 'read' in rule.get('actions', [])
    ]

    def permitted(node):
        path = node['logical_path']
        if node['kind'] == 'file':
            return any(path == base or path.startswith(f'{base}/') for base in readable)
        children = node.get('children')
        if children is not None:
            node['children'] = [child for child in children if permitted(child)]
        return bool(node.get('children')) or any(
            path == base or path.startswith(f'{base}/') or base.startswith(f'{path}/')
            for base in readable
        )

    return {**data, 'items': [node for node in data['items'] if permitted(node)]}


def agent_permissions(company, user, agent_id):
    """Read, invoke and configure remain separate capabilities."""
    if not user or not is_company_verified_for_formal_access(company):
        return 0
    if can_access_formal_company(company, user, Grant.PERM_ADMIN):
        return Grant.ROLE_MANAGER
    principal = Principal.objects.filter(type=Principal.TYPE_USER, external_id=user.uid).first()
    resource = Resource.objects.filter(
        type=Resource.TYPE_AGENT,
        external_id=f'agent:{company.cid}:{agent_id}',
    ).select_related('tenant', 'parent').first()
    permission = compute_effective_permission(principal, resource).permissions if principal and resource else 0
    scope = scope_for(company, user)
    if scope and agent_id in scope.agent_ids:
        permission = max(permission, Grant.ROLE_CONTRIBUTOR)
    return permission


def agent_visible(company, user, agent_id):
    return agent_permissions(company, user, agent_id) >= Grant.ROLE_VIEWER


def agent_allowed(company, user, agent_id):
    return agent_permissions(company, user, agent_id) >= Grant.ROLE_CONTRIBUTOR


def agent_access(cid, user):
    company = Company.objects.filter(cid=cid).first()
    if not company or not is_company_verified_for_formal_access(company):
        return False, '无权访问或资源不存在', None
    broad_admin = can_access_formal_company(company, user, Grant.PERM_ADMIN)
    scope = None if broad_admin else scope_for(company, user)
    transparent = TransparentCompany.objects.filter(company=company).first()
    if transparent is None:
        return False, '透明公司不存在', None
    if broad_admin:
        return True, transparent, None
    agent_ids = list(TransparentCompanyAgentDefinition.objects.filter(
        transparentCompany=transparent,
    ).values_list('agentId', flat=True))
    permissions = {agent_id: 0 for agent_id in agent_ids}
    principal = Principal.objects.filter(type=Principal.TYPE_USER, external_id=user.uid).first() if user else None
    if principal:
        resources = list(Resource.objects.filter(
            type=Resource.TYPE_AGENT,
            external_id__in=[f'agent:{cid}:{agent_id}' for agent_id in agent_ids],
        ).select_related('tenant', 'parent'))
        for effective in compute_effective_permissions(principal, resources).values():
            permissions[effective.resource.external_id.removeprefix(f'agent:{cid}:')] = effective.permissions
    if scope:
        for agent_id in scope.agent_ids:
            if agent_id in permissions:
                permissions[agent_id] = max(permissions[agent_id], Grant.ROLE_CONTRIBUTOR)
    allowed_ids = [agent_id for agent_id, permission in permissions.items() if permission >= Grant.ROLE_VIEWER]
    if not allowed_ids and not scope:
        return False, '无权访问或资源不存在', None
    # Always return a restricted scope for delegated users, including unified
    # IAM grants. A missing legacy scope must never mean company-wide access.
    return True, transparent, SimpleNamespace(agent_ids=allowed_ids, permissions=permissions)


def serialize_scope(scope):
    return {
        'public_id': scope.user.public_id,
        'role_name': scope.role_name,
        'file_rules': scope.file_rules,
        'agent_ids': scope.agent_ids,
        'active': scope.active,
        'updated_at': scope.updated_at.isoformat(),
    }


def validate_rules(company, rules, agent_ids):
    from apps.agent_runtime.knowledge.workspace import normalize_path

    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError('文件权限格式不正确或数量过多')
    normalized = []
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get('path'), str):
            raise ValueError('文件权限格式不正确')
        actions = rule.get('actions')
        if not isinstance(actions, list) or not actions or any(not isinstance(action, str) or action not in FILE_ACTIONS for action in actions):
            raise ValueError('文件操作权限必须为 read、write、delete')
        path = normalize_path(rule['path'])
        if path in seen:
            raise ValueError('文件路径不能重复')
        seen.add(path)
        normalized.append({'path': path, 'actions': sorted(set(actions))})
    if not isinstance(agent_ids, list) or len(agent_ids) > 100 or any(not isinstance(item, str) or not item.strip() for item in agent_ids):
        raise ValueError('Agent 权限格式不正确')
    normalized_agents = list(dict.fromkeys(agent_ids))
    if normalized_agents:
        valid = set(TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany__company=company,
            agentId__in=normalized_agents,
        ).exclude(status=TransparentCompanyAgentDefinition.STATUS_DEPRECATED).values_list('agentId', flat=True))
        if valid != set(normalized_agents):
            raise ValueError('Agent 不属于该企业或已停用')
    return normalized, normalized_agents


@transaction.atomic
def upsert_scope(company, actor, public_id, role_name, file_rules, agent_ids, active=True):
    if not can_access_formal_company(company, actor, Grant.PERM_SUPER_ADMIN):
        raise PermissionError('只有最高管理员可以分配权限')
    target = User.objects.filter(public_id=public_id.strip().upper()).first()
    if not target:
        raise ValueError('用户 ID 不存在')
    if not CompanyMember.objects.filter(company=company, user=target, status=CompanyMember.STATUS_ACTIVE).exists():
        raise ValueError('请先通过用户 ID 将员工添加至企业')
    if target.id == actor.id or can_access_formal_company(company, target, Grant.PERM_ADMIN):
        raise ValueError('该用户已有企业全局管理权限，不能作为受限管理员授权')
    if not isinstance(role_name, str) or not role_name.strip() or len(role_name.strip()) > 100:
        raise ValueError('管理员名称不能为空且不能超过100字')
    if not isinstance(active, bool):
        raise ValueError('active 必须为布尔值')
    rules, agents = validate_rules(company, file_rules, agent_ids)
    scope, _ = CompanyScopedAdmin.objects.update_or_create(
        company=company, user=target,
        defaults={
            'role_name': role_name.strip(),
            'file_rules': rules,
            'agent_ids': agents,
            'active': active,
            'granted_by': actor,
        },
    )
    return serialize_scope(scope)
