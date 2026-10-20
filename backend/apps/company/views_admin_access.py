"""Highest-admin-only delegation endpoints."""

import json

from django.http import JsonResponse
from django.utils import timezone

from apps.company.admin_access import scope_for, serialize_scope, upsert_scope
from apps.company.models import Company, CompanyMember, CompanyScopedAdmin
from apps.company.permissions import can_access_formal_company, is_company_verified_for_formal_access
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import compute_effective_permission
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request


def _company(request):
    cid = request.GET.get('cid') if request.method == 'GET' else None
    if request.method == 'POST':
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, None, JsonResponse({'result': 0, 'message': '请求体必须是 JSON 对象'}, status=400)
        if not isinstance(payload, dict):
            return None, None, JsonResponse({'result': 0, 'message': '请求体必须是 JSON 对象'}, status=400)
        cid = payload.get('cid')
    else:
        payload = {}
    user = get_user_from_request(request)
    if not user:
        return None, None, JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    company = Company.objects.filter(cid=cid).first() if isinstance(cid, str) else None
    if not company or not is_company_verified_for_formal_access(company):
        return None, None, JsonResponse({'result': 0, 'message': '企业不存在或尚未认证'}, status=404)
    return company, payload, None


@login_checker
def admin_access_me(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    company, _payload, error = _company(request)
    if error:
        return error
    user = get_user_from_request(request)
    is_super = can_access_formal_company(company, user, Grant.PERM_SUPER_ADMIN)
    principal = Principal.objects.filter(type=Principal.TYPE_USER, external_id=user.uid).first()
    resource = Resource.objects.filter(type=Resource.TYPE_COMPANY, external_id=company.cid).select_related("tenant").first()
    permissions = compute_effective_permission(principal, resource).permissions if principal and resource else 0
    if is_super:
        permissions = Grant.ROLE_MANAGER
    is_admin = permissions >= Grant.ROLE_EDITOR
    scope = scope_for(company, user)
    is_member = CompanyMember.objects.filter(company=company, user=user, status=CompanyMember.STATUS_ACTIVE).exists()
    if not is_admin and not scope and not is_member:
        return JsonResponse({'result': 0, 'message': '无权访问'}, status=403)
    return JsonResponse({'result': 1, 'data': {
        'is_super_admin': is_super,
        'is_admin': is_admin,
        'is_member': is_member,
        'permissions': permissions,
        'can_manage_members': permissions >= Grant.ROLE_MANAGER,
        'scope': serialize_scope(scope) if scope else None,
    }})


@login_checker
def admin_access_list(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '仅支持 GET'}, status=405)
    company, _payload, error = _company(request)
    if error:
        return error
    if not can_access_formal_company(company, get_user_from_request(request), Grant.PERM_SUPER_ADMIN):
        return JsonResponse({'result': 0, 'message': '只有最高管理员可以管理授权'}, status=403)
    scopes = CompanyScopedAdmin.objects.filter(company=company).select_related('user').order_by('id')
    return JsonResponse({'result': 1, 'data': [serialize_scope(scope) for scope in scopes]})


@login_checker
def admin_access_upsert(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    company, payload, error = _company(request)
    if error:
        return error
    public_id = payload.get('public_id')
    if not isinstance(public_id, str) or not public_id.strip():
        return JsonResponse({'result': 0, 'message': '请填写用户 ID'}, status=400)
    try:
        data = upsert_scope(
            company, get_user_from_request(request), public_id.strip(),
            payload.get('role_name', '受限管理员'),
            payload.get('file_rules', []), payload.get('agent_ids', []),
            payload.get('active', True),
        )
    except PermissionError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({'result': 0, 'message': str(exc)}, status=400)
    return JsonResponse({'result': 1, 'message': '授权已保存', 'data': data})


@login_checker
def admin_access_revoke(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    company, payload, error = _company(request)
    if error:
        return error
    if not can_access_formal_company(company, get_user_from_request(request), Grant.PERM_SUPER_ADMIN):
        return JsonResponse({'result': 0, 'message': '只有最高管理员可以管理授权'}, status=403)
    public_id = payload.get('public_id')
    if not isinstance(public_id, str) or not public_id.strip():
        return JsonResponse({'result': 0, 'message': '请填写用户 ID'}, status=400)
    changed = CompanyScopedAdmin.objects.filter(
        company=company, user__public_id=public_id.strip().upper(), active=True,
    ).update(
        active=False, granted_by=get_user_from_request(request), updated_at=timezone.now(),
    )
    return JsonResponse({'result': 1, 'message': '授权已撤销', 'data': {'changed': bool(changed)}})
