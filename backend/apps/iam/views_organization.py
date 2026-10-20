"""Company organization tree and atomic department membership operations."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import JsonResponse

from apps.company.models import CompanyMember
from apps.company.permissions import has_company_iam_permission
from apps.company.services import CompanyService
from apps.user.models import User
from common.utils.Login_utils import login_checker

from .models import Grant, Group, GroupMembership, PermissionAuditLog, Tenant, TenantMembership
from .services import UNASSIGNED_GROUP_NAME, reconcile_unassigned_group
from .views_unified import _client_ip, _context, _get_group, _is_tenant_admin, _json_payload, _serialize_group


def _can_read_organization(company, tenant, user):
    return _is_tenant_admin(tenant, user) or has_company_iam_permission(company, user, Grant.ROLE_MANAGER)


def _organization_data(company, tenant, actor):
    reconcile_unassigned_group(tenant, created_by=actor)
    rows = list(CompanyMember.objects.filter(company=company).select_related('user').order_by('id'))
    department_ids = {}
    for user_id, group_id in GroupMembership.objects.filter(
        group__tenant=tenant,
    ).exclude(group__name=UNASSIGNED_GROUP_NAME).values_list('user_id', 'group__group_id'):
        department_ids.setdefault(user_id, []).append(str(group_id))
    active_ids = {row.user_id for row in rows if row.user_id and row.status == CompanyMember.STATUS_ACTIVE}
    if tenant.super_admin_user_id:
        active_ids.add(tenant.super_admin_user_id)
    groups = Group.objects.filter(tenant=tenant).select_related('parent').prefetch_related(
        Prefetch('memberships', queryset=GroupMembership.objects.filter(
            user_id__in=active_ids,
        ).select_related('user').order_by('id')),
    ).order_by('name')
    members = [{
        'id': row.id, 'public_id': row.user.public_id if row.user else None,
        'memberUid': row.user.uid if row.user else row.memberUid,
        'name': row.name or (row.user.username if row.user else ''),
        'status': row.status, 'is_super_admin': row.user_id == tenant.super_admin_user_id,
        'department_ids': department_ids.get(row.user_id, []),
    } for row in rows]
    if tenant.super_admin_user_id and tenant.super_admin_user_id not in {row.user_id for row in rows}:
        owner = User.objects.get(pk=tenant.super_admin_user_id)
        members.insert(0, {
            'id': None, 'public_id': owner.public_id, 'memberUid': owner.uid,
            'name': owner.username, 'status': CompanyMember.STATUS_ACTIVE, 'is_super_admin': True,
            'department_ids': department_ids.get(owner.id, []),
        })
    return {
        'company': {'cid': company.cid, 'name': company.company_name},
        'can_manage_departments': _is_tenant_admin(tenant, actor),
        'groups': [_serialize_group(group) for group in groups], 'members': members,
    }


def _response(company, tenant, actor, message='组织架构已更新'):
    response = JsonResponse({'result': 1, 'message': message, 'data': _organization_data(company, tenant, actor)})
    response['Cache-Control'] = 'private, no-store'
    return response


@login_checker
def organization_tree(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    company, tenant, user, error = _context(request, request.GET.get('cid'))
    if error:
        return error
    if not _can_read_organization(company, tenant, user):
        return JsonResponse({'result': 0, 'message': '无权查看企业组织架构'}, status=403)
    return _response(company, tenant, user, '组织架构已加载')


@login_checker
@transaction.atomic
def organization_member_add(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    if not _can_read_organization(company, tenant, user):
        return JsonResponse({'result': 0, 'message': '无权添加企业成员'}, status=403)
    Tenant.objects.select_for_update().get(pk=tenant.pk)
    group_id = payload.get('group_id')
    group = None
    if group_id:
        if not _is_tenant_admin(tenant, user):
            return JsonResponse({'result': 0, 'message': '只有企业最高管理员可以调整所属部门'}, status=403)
        group = _get_group(tenant, group_id)
        if not group or group.name == UNASSIGNED_GROUP_NAME:
            return JsonResponse({'result': 0, 'message': '请选择有效的部门'}, status=400)
    mode, identifier = payload.get('mode'), payload.get('identifier')
    if not isinstance(identifier, str):
        return JsonResponse({'result': 0, 'message': '请填写手机号或用户 ID'}, status=400)
    identifier = identifier.strip()
    if mode == 'phone' and identifier.isdigit() and 6 <= len(identifier) <= 20:
        ok, result = CompanyService.add_company_member_by_phone(
            company.cid, user, identifier, client_ip=_client_ip(request),
        )
    elif mode == 'user_id' and len(identifier) == 13:
        ok, result = CompanyService.add_company_member_by_public_id(
            company.cid, user, identifier.upper(), client_ip=_client_ip(request),
        )
    else:
        return JsonResponse({'result': 0, 'message': '手机号或用户 ID 格式不正确'}, status=400)
    if not ok:
        return JsonResponse({'result': 0, 'message': result}, status=400)
    target = User.objects.get(public_id=result['public_id'])
    if group:
        GroupMembership.objects.get_or_create(group=group, user=target, defaults={'created_by': user})
    PermissionAuditLog.objects.create(
        tenant=tenant, actor=user, action='organization.member.add',
        after={'public_id': target.public_id, 'group_id': str(group.group_id) if group else None},
        ip_address=_client_ip(request),
    )
    return _response(company, tenant, user, '员工已加入企业')


@login_checker
@transaction.atomic
def organization_members_update(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    try:
        payload = _json_payload(request)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    company, tenant, user, error = _context(request, payload.get('cid'))
    if error:
        return error
    if not _is_tenant_admin(tenant, user):
        return JsonResponse({'result': 0, 'message': '只有企业最高管理员可以调整所属部门'}, status=403)
    Tenant.objects.select_for_update().get(pk=tenant.pk)
    public_ids, group_ids = payload.get('public_ids'), payload.get('group_ids')
    operation = payload.get('operation')
    if (
        operation not in ('add', 'set', 'remove')
        or not isinstance(public_ids, list) or not 1 <= len(public_ids) <= 100
        or any(not isinstance(value, str) or not value.strip() for value in public_ids)
        or not isinstance(group_ids, list) or len(group_ids) > 100
        or any(not isinstance(value, str) or not value.strip() for value in group_ids)
        or (operation != 'set' and not group_ids)
    ):
        return JsonResponse({'result': 0, 'message': '请选择成员及目标部门，每次最多调整100名成员'}, status=400)
    public_ids = sorted({value.strip().upper() for value in public_ids})
    try:
        groups = list(Group.objects.filter(tenant=tenant, group_id__in=group_ids))
    except (ValueError, TypeError, ValidationError):
        return JsonResponse({'result': 0, 'message': '部门格式不正确'}, status=400)
    if len(groups) != len(set(group_ids)) or any(group.name == UNASSIGNED_GROUP_NAME for group in groups):
        return JsonResponse({'result': 0, 'message': '部门不存在或不属于该企业'}, status=400)
    active_ids = CompanyMember.objects.filter(
        company=company, status=CompanyMember.STATUS_ACTIVE, user__isnull=False,
    ).values_list('user_id', flat=True)
    targets = list(User.objects.filter(public_id__in=public_ids).filter(
        Q(pk__in=active_ids) | Q(pk=tenant.super_admin_user_id),
    ).filter(iam_tenant_memberships__tenant=tenant, iam_tenant_memberships__status=TenantMembership.STATUS_ACTIVE))
    if len(targets) != len(public_ids):
        return JsonResponse({'result': 0, 'message': '成员不存在、已停用或不属于该企业'}, status=400)
    desired_ids = {group.id for group in groups}
    existing = list(GroupMembership.objects.filter(
        group__tenant=tenant, user__in=targets,
    ).exclude(group__name=UNASSIGNED_GROUP_NAME).select_related('group'))
    by_user, groups_by_id = {}, {group.id: group for group in groups}
    for membership in existing:
        by_user.setdefault(membership.user_id, set()).add(membership.group_id)
        groups_by_id[membership.group_id] = membership.group
    before, after, additions, removals = [], [], [], []
    for target in targets:
        previous_ids = by_user.get(target.id, set())
        next_ids = desired_ids if operation == 'set' else (
            previous_ids | desired_ids if operation == 'add' else previous_ids - desired_ids
        )
        def snapshot(ids):
            return {
                'public_id': target.public_id,
                'group_ids': sorted(str(groups_by_id[group_id].group_id) for group_id in ids),
                'department_names': sorted(groups_by_id[group_id].name for group_id in ids),
            }
        before.append(snapshot(previous_ids))
        after.append(snapshot(next_ids))
        removals.extend(membership.pk for membership in existing if (
            membership.user_id == target.id and membership.group_id not in next_ids
        ))
        additions.extend(
            GroupMembership(group_id=group_id, user=target, created_by=user)
            for group_id in next_ids - previous_ids
        )
    GroupMembership.objects.filter(pk__in=removals).delete()
    GroupMembership.objects.bulk_create(additions, ignore_conflicts=True)
    PermissionAuditLog.objects.create(
        tenant=tenant, actor=user, action=f'organization.members.{operation}',
        before={'members': before}, after={'members': after}, ip_address=_client_ip(request),
    )
    return _response(company, tenant, user)
