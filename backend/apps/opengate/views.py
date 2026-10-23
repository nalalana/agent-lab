"""
/backend/apps/opengate/views.py
OpenGate视图函数
"""
import uuid
import json
import time
import os
from json import JSONDecodeError

from django.http import JsonResponse
from rest_framework.decorators import api_view
from rest_framework import status
from properties import db_local_base_url, db_local_base_port, USER, PASSWORD
from apps.opengate.models import Gates, GatesTable, APIKey, TableAuditEvent
from apps.company.models import CompanyMember, companyGate
from apps.company.permissions import is_company_verified_for_formal_access
import logging

from apps.iam.models import Grant, Principal, Resource, Tenant
from apps.iam.services import compute_effective_permission, get_or_create_principal, get_or_create_resource, is_granted
from apps.risk_control.services import RiskControlService
from common.utils.common_utils import get_user_from_request
logger = logging.getLogger('touming')
from apps.user.models import User
from apps.moderation.models import ImageModerationAsset
from apps.moderation.services import ImageModerationService, moderation_payload
from common.utils.Login_utils import login_checker, api_key_checker, get_client_ip, check_token, make_api_bearer_token
from apps.opengate.serializers import CreateGateSerializer, DeleteGateKeySerializer, DeleteGateSerializer, ListGateKeySerializer, ListGateSerializer, ListTableSerializer, ProjectPublicAccessSerializer, SearchGateSerializer, ToggleGateKeySerializer, TableDetailsSerializer, TableDataSerializer, TableAuditSerializer, CreateGateKeySerializer, SimpleUpsertTableDataSerializer, DeleteTableRowSerializer
from apps.opengate.services import OpenGateService, TableCellValidationError, TableWriteConflict
from common.utils.Tengcentcos_utils import put_object, get_moderated_image_presigned_url

db_url = db_local_base_url
db_port = db_local_base_port

# Create your views here.


def _optional_user_from_request(request):
    token = request.META.get('HTTP_ACCESS_TOKEN')
    if not token:
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
    if not token:
        token = request.GET.get('access_token') or request.GET.get('token')
    if not token:
        return None

    payload = check_token(token)
    if not payload:
        return None
    try:
        user_id = payload.get('user_id', 0)
        if user_id <= 0:
            return None
        return User.objects.get(id=user_id)
    except User.DoesNotExist:
        return None


def _read_json_body(request):
    try:
        return json.loads(request.body.decode() or '{}'), None
    except (JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({'result': 0, 'message': 'invalid json payload'}, status=400)


def _api_key_principal(request):
    user = getattr(request, 'api_key_user', None)
    if user is None:
        return None, JsonResponse({'result': 0, 'message': 'API key owner is unavailable'}, status=403)
    return user, None


def _external_table_payload(table):
    return {
        'tid': table.tid,
        'gid': table.gate.gid,
        'tableName': table.name,
        'description': table.description or '',
        'permission': table.permission,
        'numColumns': table.num_columns,
        'columnTitles': table.get_column_titles(),
        'columns_config': table.columns_config,
        'created_at': table.created_at,
        'updated_at': table.updated_at,
    }


def _integrity_error_response(message):
    if message == 'authentication required':
        return JsonResponse({'result': 0, 'message': message}, status=401)
    if message == 'no read permission':
        return JsonResponse({'result': 0, 'message': message}, status=403)
    if message in ['table not found', 'row data not found']:
        return JsonResponse({'result': 0, 'message': message}, status=404)
    return JsonResponse({'result': 0, 'message': message}, status=400)


@api_view(['POST'])
@login_checker
def create_gate(request):
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在!'}, status=403)
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = CreateGateSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)
    gate, message = OpenGateService.create_gate(
        owner=user,
        gid='gate' + str(uuid.uuid4()).replace('-', ''),
        gatename=serializer.validated_data['gatename'],
        company_id=serializer.validated_data.get('cid'),
        logo=serializer.validated_data.get('logo', ''),
        description=serializer.validated_data.get('description', ''),
        background_image=serializer.validated_data.get('background_image', ''),
        client_ip=get_client_ip(request)
    )
    if not gate:
        status = 403 if message in ('公司不存在或无权限创建项目', '权限不足') else 500
        return JsonResponse({'result': 0, 'message': message}, status=status)
    return JsonResponse({'result': 1, 'message': 'success', 'data': gate.get_info()})


def validate_sql_type(sql_type):
    """验证SQL类型是否合法（已迁移到服务层）"""
    from apps.opengate.services import OpenGateService
    return OpenGateService.is_valid_sql_type(sql_type)



@login_checker
def create_table(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在!'})

    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    from apps.opengate.serializers import CreateTableSerializer
    cs = CreateTableSerializer(data=data_json)
    if not cs.is_valid():
        return JsonResponse({'result': 0, 'message': cs.errors})
    ok, msg = OpenGateService.create_table_full(
        owner=user,
        gid=cs.validated_data['gid'],
        table_name=cs.validated_data['tableName'],
        description=cs.validated_data.get('description', ''),
        permission=cs.validated_data['permission'],
        column_titles=cs.validated_data['columnTitles'],
        num_columns=cs.validated_data['numColumns'],
        client_ip=get_client_ip(request)
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg}, status=400)
    return JsonResponse({'result': 1, 'message': msg})

    # 删除遗留返回


@login_checker
def update_member(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id, is_activate=1)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在!'})
    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import UpdateMemberSerializer
    sm = UpdateMemberSerializer(data=data_json)
    if not sm.is_valid():
        return JsonResponse({'result': 0, 'message': sm.errors})
    ok, msg = OpenGateService.update_members(
        user,
        sm.validated_data['gatename'],
        sm.validated_data['table_name'],
        sm.validated_data['change_members'],
        client_ip=get_client_ip(request)
    )
    if not ok:
        logger.error(f"[update_member] {msg} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': msg})
    logger.info(f"[update_member] {msg} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': msg})


@login_checker
def check_invitation(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    user_id = request.user_id
    data_json = json.loads(request.body.decode())
    limit = data_json.get('limit', 20)
    offset = data_json.get('offset', 0)
    try:
        user = User.objects.get(id=user_id, is_activate=1)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在!'})
    
    from apps.opengate.models import GatesMember
    queryset = GatesMember.objects.filter(member=user, is_valid=0)
    gms = list(queryset.order_by('id')[int(offset): int(offset)+int(limit)])
    invitation_info = []
    for gm in gms:
        invitation_info.append({
            'invitation_id': gm.id,
            'gate_name': gm.table.gate.gatename,
            'table_name': gm.table.name,
            'permission': gm.permission,
            'updated_at': gm.updated_at,
        })
    return JsonResponse({'result': 1, 'message':'success', 'data': invitation_info})

@login_checker
def operate_invitation(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id, is_activate=1)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在!'})
    
    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import OperateInvitationSerializer
    s = OperateInvitationSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    ok, msg = OpenGateService.operate_invitation(
        s.validated_data['invitation_id'],
        s.validated_data['operation'],
        client_ip=get_client_ip(request)
    )
    if not ok:
        logger.error(f"[operate_invitation] {msg} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': msg})
    logger.info(f"[operate_invitation] {msg} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': msg})


@login_checker
def update_table(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在!'})

    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import UpdateTableSerializer
    s = UpdateTableSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    
    ok, msg = OpenGateService.update_table_full(
        owner=user,
        tid=s.validated_data['tid'],
        table_name=s.validated_data['tableName'],
        description=s.validated_data.get('description'),
        permission=s.validated_data.get('permission'),
        column_titles=s.validated_data.get('columnTitles'),
        num_columns=s.validated_data.get('numColumns'),
        client_ip=get_client_ip(request)
    )
    
    if not ok:
        return JsonResponse({'result': 0, 'message': msg}, status=400)
    return JsonResponse({'result': 1, 'message': msg})


@login_checker
def update_gate(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误！'})
    
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在！'})
    

    data, error_response = _read_json_body(request)
    if error_response:
        return error_response
    from apps.opengate.serializers import UpdateGateSerializer
    s = UpdateGateSerializer(data=data)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors}, status=400)
    gid = s.validated_data['gid']
    gatename = s.validated_data.get('gatename')
    logo = s.validated_data.get('logo')
    description = s.validated_data.get('description')
    background_image = s.validated_data.get('background_image')
    try:
        gate = Gates.objects.get(gid=gid, owner=user)
    except Gates.DoesNotExist:
        logger.error(f"[update_gate] 应用不存在 gatename={gatename} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '应用不存在！'})
    ok, msg = OpenGateService.update_gate(user=user, gate=gate, gatename=gatename, logo=logo, description=description, background_image=background_image, client_ip=get_client_ip(request))
    if not ok:
        if msg:
            return JsonResponse({'result': 0, 'message': msg})
        return JsonResponse({'result': 0, 'message': '更新失败'})
    logger.info(f"[update_gate] 更新成功 gatename={gatename} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': '更新成功！'})


@api_view(['POST'])
@login_checker
def update_gate_public_access(request):
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    data, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = ProjectPublicAccessSerializer(data=data)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)
    try:
        gate = Gates.objects.get(gid=serializer.validated_data['gid'])
    except Gates.DoesNotExist:
        return JsonResponse({'result': 0, 'message': '项目不存在'}, status=404)
    ok, result = OpenGateService.update_public_access(
        user=user,
        gate=gate,
        public_access=serializer.validated_data['public_access'],
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': result}, status=403)
    return JsonResponse({
        'result': 1,
        'message': '项目公开范围已更新',
        'data': {'gid': gate.gid, 'public_access': result},
    })


@api_view(['POST'])
@login_checker
def delete_gate(request):
    try:
        user = User.objects.get(id=request.user_id)
    except User.DoesNotExist:
        return JsonResponse({'result': 0, 'message': '用户不存在！'}, status=403)

    data, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = DeleteGateSerializer(data=data)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    gid = serializer.validated_data['gid']
    try:
        gate = Gates.objects.get(gid=gid)
    except Gates.DoesNotExist:
        return JsonResponse({'result': 0, 'message': '项目不存在'}, status=404)

    ok, message = OpenGateService.delete_gate(
        user=user,
        gate=gate,
        client_ip=get_client_ip(request),
    )
    if not ok:
        response_status = 403 if message == '用户无权限删除该项目' else 500
        return JsonResponse({'result': 0, 'message': message}, status=response_status)

    logger.info(
        f"[delete_gate] 删除成功 gid={gid} user={user.username} "
        f"ip={get_client_ip(request)} path={request.path}"
    )
    return JsonResponse({'result': 1, 'message': '项目已删除'})


@login_checker
def upload_gate_image(request):
    """上传项目相关图片（logo/background_image）到 COS，返回 file_key 和 presigned url。"""
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    upload_file = request.FILES.get('file') or request.FILES.get('image')
    if not upload_file:
        return JsonResponse({'result': 0, 'message': '缺少文件字段 file'}, status=400)

    content_type = (upload_file.content_type or '').lower()
    allowed_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    if content_type not in allowed_types:
        return JsonResponse({'result': 0, 'message': '仅支持 JPG/PNG/GIF/WEBP 图片'}, status=400)

    max_size = 10 * 1024 * 1024
    if upload_file.size > max_size:
        return JsonResponse({'result': 0, 'message': '图片大小不能超过 10MB'}, status=400)

    field = (request.POST.get('field') or request.GET.get('field') or 'logo').strip()
    if field not in {'logo', 'background_image'}:
        return JsonResponse({'result': 0, 'message': 'field 仅支持 logo/background_image'}, status=400)

    ext = os.path.splitext(upload_file.name or '')[1].lower()
    if not ext:
        ext = '.jpg'
    file_name = f'gate_{field}_{uuid.uuid4().hex[:12]}{ext}'

    try:
        file_key = put_object(upload_file.read(), file_name)
        asset = ImageModerationService.register_uploaded_image(
            file_key,
            source=ImageModerationAsset.SOURCE_OPENGATE_IMAGE,
            source_field=field,
            owner_type="user",
            owner_id=str(user.uid),
            mime_type=content_type,
            size=upload_file.size or 0,
        )
        file_url = (
            get_moderated_image_presigned_url(file_key, expires_in=3600)
            if ImageModerationService.is_publicly_viewable(file_key, content_type)
            else None
        )
        return JsonResponse({
            'result': 1,
            'message': '上传成功',
            'data': {
                'field': field,
                'file_key': file_key,
                'url': file_url,
                'file_url': file_url,
                'moderation': moderation_payload(asset),
            }
        })
    except Exception as e:
        logger.error(f"[gate-upload-image] 上传失败 user={user.uid} ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({'result': 0, 'message': '上传失败'}, status=500)

@login_checker
def search_gate(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    serializer = SearchGateSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    keyword = serializer.validated_data.get('keyword', '')
    limit = serializer.validated_data['limit']
    offset = serializer.validated_data['offset']
    try:
        user = get_user_from_request(request)
        if not user:
            return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
        result = OpenGateService.search_gates(user, keyword, limit, offset)
        return JsonResponse({'result':1, 'message': 'success', 'data': result})
    except Exception as e:
        logger.error(f"[search_gate] 查找失败 keyword={keyword} ip={get_client_ip(request)} path={request.path} err={e}")
        return JsonResponse({'result':0, 'message':'查找失败'})

def list_table_by_gate(request):
    if request.method != 'GET':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    gid = request.GET.get('gid')
    limit = request.GET.get('limit', 20)
    offset = request.GET.get('offset', 0)
    user = _optional_user_from_request(request)
    ok, table_info = OpenGateService.list_tables(user, gid, int(limit), int(offset))
    if not ok:
        logger.error(f"[list_table_by_gate] {table_info} gid={gid} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': table_info}, status=404)
    logger.info(f"[list_table_by_gate] 查找成功 gid={gid} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': table_info})

def view_table_details(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    user = _optional_user_from_request(request)

    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = TableDetailsSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    tid = serializer.validated_data['tid']
    ok, table_info = OpenGateService.get_table_full_details(user, tid)
    username = user.username if user else 'anonymous'
    if not ok:
        logger.error(f"[view_table_details] {table_info} tid={tid} user={username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result': 0, 'message': table_info}, status=404)
    logger.info(f"[view_table_details] 查看表格信息 tid={tid} user={username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
    return JsonResponse({'result': 1, 'message': 'success', 'data': table_info})


def view_table_data(request):
    user = _optional_user_from_request(request)
    
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    s = TableDataSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    # 权限检查：通过 IAM 系统或表格权限
    tid = s.validated_data['tid']
    try:
        table = GatesTable.objects.get(tid=tid)
    except GatesTable.DoesNotExist:
        username = user.username if user else 'anonymous'
        logger.error(f"[view_table_data] 表格不存在 tid={tid} user={username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不存在'})

    
    ok, data = OpenGateService.get_table_data(
        user=user,
        table=table,
        what_to_select=s.validated_data.get('what_to_select'),
        params=s.validated_data.get('params'),
        limit=s.validated_data.get('limit'),
        offset=s.validated_data.get('offset'),
        client_ip=get_client_ip(request)
    )
    
    if not ok:
        username = user.username if user else 'anonymous'
        logger.error(f"[view_table_data] {data} tid={s.validated_data['tid']} user={username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': data}, status=404)
    
    username = user.username if user else 'anonymous'
    logger.info(f"[view_table_data] 查看表格数据 tid={s.validated_data['tid']} user={username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


@api_view(['POST'])
def table_audit_events(request):
    user = _optional_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = TableAuditSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)
    tid = serializer.validated_data['tid']
    try:
        table = GatesTable.objects.get(tid=tid)
    except GatesTable.DoesNotExist:
        return JsonResponse({'result': 0, 'message': '表格不存在'}, status=404)
    if not OpenGateService._has_iam_permission(user, Resource.TYPE_TABLE, tid, Grant.PERM_READ):
        return JsonResponse({'result': 0, 'message': '没有查看权限'}, status=403)

    query = TableAuditEvent.objects.filter(table=table).select_related('actor')
    source = serializer.validated_data.get('source')
    if source:
        query = query.filter(source=source)
    total = query.count()
    offset = serializer.validated_data['offset']
    limit = serializer.validated_data['limit']
    items = [{
        'id': event.id,
        'action': event.action,
        'result': event.result,
        'source': event.source,
        'actor_name': event.actor.username if event.actor else '',
        'api_key_name': event.api_key_name,
        'row_index': event.row_index,
        'column_index': event.column_index,
        'details': event.details,
        'ip_address': event.ip_address,
        'created_at': event.created_at.isoformat(),
    } for event in query[offset:offset + limit]]
    return JsonResponse({'result': 1, 'message': 'success', 'data': {
        'total': total, 'items': items, 'hasMore': offset + limit < total,
    }})


@api_view(['GET'])
def table_integrity(request, table_id):
    user = _optional_user_from_request(request)
    ok, data = OpenGateService.get_table_integrity(
        user=user,
        tid=table_id,
        client_ip=get_client_ip(request)
    )
    if not ok:
        return _integrity_error_response(data)
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


@api_view(['GET'])
def row_integrity(request, table_id, row_id):
    user = _optional_user_from_request(request)
    ok, data = OpenGateService.get_row_integrity(
        user=user,
        tid=table_id,
        row_id=row_id,
        client_ip=get_client_ip(request)
    )
    if not ok:
        return _integrity_error_response(data)
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


@api_view(['POST'])
def verify_table_integrity(request, table_id):
    user = _optional_user_from_request(request)
    try:
        data_json = json.loads(request.body.decode() or '{}')
    except (JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'result': 0, 'message': 'invalid json payload'}, status=400)

    row_ids = data_json.get('row_ids', [])
    if not isinstance(row_ids, list) or any(not isinstance(row_id, str) for row_id in row_ids):
        return JsonResponse({'result': 0, 'message': 'row_ids must be a string array'}, status=400)

    ok, data = OpenGateService.verify_table_rows(
        user=user,
        tid=table_id,
        row_ids=row_ids,
        client_ip=get_client_ip(request)
    )
    if not ok:
        return _integrity_error_response(data)
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


def upsert_table_data(request):
    user = _optional_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    from apps.opengate.serializers import UpsertTableDataSerializer
    s = UpsertTableDataSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    
    tid = s.validated_data['tid']
    try:
        table = GatesTable.objects.get(tid=tid)
    except GatesTable.DoesNotExist:
        logger.error(f"[upsert_table_data] 表格不存在 tid={tid} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不存在'})

    ok, msg = OpenGateService.upsert_table_data_full(
        user=user,
        table=table,
        values=s.validated_data['values'],
        client_ip=get_client_ip(request)
    )
    
    if not ok:
        logger.error(f"[upsert_table_data] {msg} tid={s.validated_data['tid']} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': msg}, status=403)
    
    logger.info(f"[upsert_table_data] 写入成功 tid={s.validated_data['tid']} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': msg})


def upsert_table_cells(request):
    user = _optional_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = SimpleUpsertTableDataSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    tid = serializer.validated_data['tid']
    try:
        table = GatesTable.objects.get(tid=tid)
    except GatesTable.DoesNotExist:
        logger.error(f"[upsert_table_cells] 表格不存在 tid={tid} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不存在'})

    try:
        ok, msg = OpenGateService.upsert_table_cells(
            user=user,
            table=table,
            cells=serializer.validated_data['values'],
            client_ip=get_client_ip(request),
            append=serializer.validated_data['append'],
            expected_max_row_index=serializer.validated_data.get('expected_max_row_index'),
            expected_table_version=serializer.validated_data.get('expected_table_version'),
        )
    except TableCellValidationError as exc:
        ok, msg = False, exc.errors
    except TableWriteConflict as exc:
        return JsonResponse(
            {'result': 0, 'message': str(exc), 'code': exc.code, 'data': exc.details},
            status=409,
        )

    if not ok:
        logger.error(f"[upsert_table_cells] 写入失败 {msg} tid={tid} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': msg}, status=403)

    logger.info(f"[upsert_table_cells] 写入成功 tid={tid} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': '写入成功', 'data': msg})


@api_view(['POST'])
def delete_table_row(request):
    user = _optional_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = DeleteTableRowSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)
    table = GatesTable.objects.filter(tid=serializer.validated_data['tid']).first()
    if not table:
        return JsonResponse({'result': 0, 'message': '表格不存在'}, status=404)
    if not OpenGateService.can_write_table(user, table):
        return JsonResponse({'result': 0, 'message': '没有写入权限'}, status=403)
    ok, result = OpenGateService.delete_table_row(
        user=user,
        table=table,
        row_index=serializer.validated_data['r'],
        client_ip=get_client_ip(request),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': result}, status=404 if result == '行不存在或已删除' else 500)
    return JsonResponse({'result': 1, 'message': '删除成功', 'data': {'deleted_cells': result}})


@login_checker
def get_permission(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id, is_activate=1)
    except:
        return JsonResponse({'result': 0, 'message': '用户不存在!'})

    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import AppUpsertTableDataSerializer
    su = AppUpsertTableDataSerializer(data=data_json)
    if not su.is_valid():
        return JsonResponse({'result': 0, 'message': su.errors})
    gatename = su.validated_data['gatename']
    table_name = data_json.get('table_name', '')
    try:
        gate = Gates.objects.get(gatename=gatename)
        table = GatesTable.objects.get(gate=gate, name=table_name)
    except:
        return JsonResponse({'result': 0, 'message': '应用或表格不存在'})

    table_permission = table.permission

    try:
        member = GatesMember.objects.get(table=table, member=user, is_valid=1)
    except Exception:
        return JsonResponse({'result':1, 'message': '未加入表格'})


    return JsonResponse({'result':1, 'message': str(member.permission) })



def view_gate_details(request):
    user = _optional_user_from_request(request)
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    if not isinstance(data_json, dict):
        return JsonResponse({'result': 0, 'message': '请求体必须是 JSON 对象'}, status=400)
    gate = Gates.objects.filter(gid=data_json.get('gid', '')).select_related('owner').first()
    if gate is None:
        return JsonResponse({'result': 0, 'message': '应用不存在'}, status=404)

    principal = Principal.objects.filter(type=Principal.TYPE_USER, external_id=user.uid).first() if user else None
    resource = Resource.objects.filter(type=Resource.TYPE_PROJECT, external_id=gate.gid).select_related('tenant').first()
    permissions = compute_effective_permission(principal, resource).permissions if principal and resource else 0
    has_internal_access = permissions >= Grant.ROLE_VIEWER
    company_gate = companyGate.objects.filter(opengate=gate).select_related('company').first() if gate.label == Gates.LABEL_COMPANY else None
    # Active employees may see basic metadata, never protected project contents.
    can_view_company_info = False
    if user and company_gate and is_company_verified_for_formal_access(company_gate.company):
        can_view_company_info = CompanyMember.objects.filter(
            company=company_gate.company, user=user, status=CompanyMember.STATUS_ACTIVE,
        ).exists() or Tenant.objects.filter(
            external_id=f'organization:{company_gate.company.cid}',
            status=Tenant.STATUS_ACTIVE, super_admin_user=user,
        ).exists()
    if not gate.allows_public_read() and not has_internal_access and not can_view_company_info:
        return JsonResponse({'result': 0, 'message': '无权访问或资源不存在'}, status=404)

    gate_info = gate.get_info()
    if not has_internal_access:
        if not gate.allows_public_read():
            gate_info = {key: gate_info[key] for key in (
                'gid', 'gatename', 'description', 'logo', 'label', 'created_at', 'updated_at',
            )}
        else:
            owner = gate_info.get('owner') or {}
            gate_info['owner'] = {
                'public_id': owner.get('public_id'), 'username': owner.get('username'),
                'avatar': owner.get('avatar'),
            }
    gate_info['can_manage_public_access'] = permissions >= Grant.ROLE_EDITOR
    gate_info['dashboard_access'] = {'can_view_info': True, 'permissions': permissions}
    if company_gate:
        gate_info['cid'] = company_gate.company.cid
        gate_info['company_name'] = company_gate.company.company_name
    response = JsonResponse({'result': 1, 'message': '获取成功', 'data': gate_info})
    response['Cache-Control'] = 'private, no-store'
    return response


@login_checker
def list_table_member(request):
    user = get_user_from_request(request)

    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import ListTableMemberSerializer
    s = ListTableMemberSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    try:
        ok, data = OpenGateService.list_table_members(
            user,
            s.validated_data['tid'],
            int(s.validated_data['limit']),
            int(s.validated_data['offset'])
        )
        if not ok:
            return JsonResponse({'result': 0, 'message': data})
        return JsonResponse({'result': 1, 'message': '获取成功', 'data': data})
    except Exception as e:
        logger.error(f"[list_table_member] 获取失败 tid={s.validated_data['tid']} user={user.username} ip={get_client_ip(request)} path={request.path} error={str(e)}")
        return JsonResponse({'result': 0, 'message': '获取失败', 'error': str(e)})

@login_checker
def create_gate_key(request):
    """
    创建API key
    TODO:需要手机号验证
    """
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response
    serializer = CreateGateKeySerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    gid = serializer.validated_data.get('gid', '')
    key_name = serializer.validated_data.get('key_name', '')

    try:
        gate = Gates.objects.get(gid=gid)
    except:
        logger.error(f"[create_gate_key] 应用不存在 gid={gid} user={user.username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result': 0, 'message': '应用不存在'})

    risk_decision = RiskControlService.evaluate(
        "api_key.create",
        request,
        actor_user_id=user.id,
        resource_id=gid,
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    ok, data = OpenGateService.create_gate_key(user, gate, key_name, client_ip=get_client_ip(request))
    if not ok:
        logger.error(f"[create_gate_key] 创建失败 gid={gid} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': data})
    logger.info(f"[create_gate_key] 创建成功 gid={gid} user={user.username} ip={get_client_ip(request)} path={request.path}")
    payload = {
        'secret_id': data['secret_id'],
        'secret_key': data['secret_key'],
        'api_key': make_api_bearer_token(data['secret_id'], data['secret_key']),
        'gid': gid,
        'key_name': key_name,
        'created_at': data['created_at'],
    }
    return JsonResponse({'result': 1, 'message': '创建成功', 'data': payload})
    
@login_checker
def delete_gate_key(request):
    """
    删除API key
    TODO:需要手机号验证
    """
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    data_json = json.loads(request.body.decode())
    serializer = DeleteGateKeySerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    gid = serializer.validated_data.get('gid', '')
    secret_id = serializer.validated_data.get('secret_id', '')

    try:
        gate = Gates.objects.get(gid=gid)
        api_key = APIKey.objects.get(gate=gate, secret_id=secret_id)
    except Exception:
        logger.error(f"[delete_gate_key] 应用不存在 gid={gid} user={user.username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result': 0, 'message': '应用或API key不存在'})
    ok = OpenGateService.delete_gate_key(user, api_key, gate, client_ip=get_client_ip(request))
    if not ok:
        logger.error(f"[delete_gate_key] 删除失败 gid={gid} secret_id={secret_id} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '删除失败'})
    logger.info(f"[delete_gate_key] 删除成功 gid={gid} secret_id={secret_id} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': '删除成功'})

@login_checker
def list_gate_key(request):
    user = get_user_from_request(request)   
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    data_json = json.loads(request.body.decode())
    serializer = ListGateKeySerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    gid = serializer.validated_data.get('gid', '')
    
    try:
        gate = Gates.objects.get(gid=gid)
    except:
        logger.error(f"[list_gate_key] 应用不存在 gid={gid} user={user.username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result': 0, 'message': '应用不存在'})
    
    api_keys = OpenGateService.list_gate_keys(user, gate)
    # api_keys 已经是字典列表（通过 get_info() 返回），直接添加 gid 字段
    api_keys_list = [
        {
            'gid': gid,
            **k  # 展开字典，包含 secret_id, key_name, created_at, is_active
        } for k in api_keys
    ]
    logger.info(f"[list_gate_key] 获取成功 gate={gate.gatename} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': '获取成功', 'data': api_keys_list})

@login_checker
def list_gate(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'})
    data_json = json.loads(request.body.decode())
    serializer = ListGateSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    uid = user.uid
    limit = serializer.validated_data['limit']
    offset = serializer.validated_data['offset']
    try:
        User.objects.get(uid=uid)
    except Exception:
        return JsonResponse({'result': 0, 'message': '用户不存在'})
    result = OpenGateService.list_gates_by_owner(uid, limit=int(limit), offset=int(offset))
    return JsonResponse({'result': 1, 'message': '获取成功', 'data': result})



@api_key_checker
def app_table_details(request):
    gid = request.gid
    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import AppTableDetailsSerializer
    serializer = AppTableDetailsSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    tid = serializer.validated_data['tid']
    try:
        table = GatesTable.objects.get(tid=tid)
    except:
        logger.error(f"[app_table_details] 表格不存在 tid={tid} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result':0, 'message':'表格不存在'})
    
    if table.gate.gid != gid:
        logger.error(f"[app_table_details] 表格不属于应用 gid={gid} tid={tid} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不属于应用'})

    table_info = {
        'table_id': table.id,
        'gid': table.gate.gid,
        'table_name': table.name,
        'permission': table.permission,
        'updated_at': table.updated_at,
        'description': table.description,
        'columns_config': table.columns_config
    }

    try:
        logger.info(f"[app_table_details] 获取成功 tid={tid} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result':1, 'message':table_info})
    except Exception as e:
        logger.error(f"[app_table_details] 获取失败 tid={tid} ip={get_client_ip(request)} path={request.path} error={str(e)}")
        return JsonResponse({'result':0, 'message':'获取失败', 'error':str(e)})

@api_key_checker
def app_table_data(request):
    gid = request.gid
    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import AppTableDataSerializer
    serializer = AppTableDataSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    tid = serializer.validated_data['tid']

    try:
        table = GatesTable.objects.get(tid=tid)
    except GatesTable.DoesNotExist:
        logger.error(f"[app_table_data] 表格不存在 tid={tid} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不存在'})
    if table.gate.gid != gid:
        logger.error(f"[app_table_data] 表格不属于应用 gid={gid} tid={tid} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不属于应用'})

    ok, data = OpenGateService.get_table_data(table=table, what_to_select=serializer.validated_data.get('what_to_select', ['*']), params=serializer.validated_data.get('params', {}), limit=int(serializer.validated_data.get('limit', 20)), offset=int(serializer.validated_data.get('offset', 0)), client_ip=get_client_ip(request))
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': data})


@api_key_checker
def app_upsert_table_data(request):
    gid = request.gid
    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import AppUpsertTableDataSerializer
    serializer = AppUpsertTableDataSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    tid = serializer.validated_data['tid']
    values = serializer.validated_data['values']

    try:
        table = GatesTable.objects.get(tid=tid)
    except GatesTable.DoesNotExist:
        logger.error(f"[app_upsert_table_data] 表格不存在 tid={tid} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不存在'})
    if table.gate.gid != gid:
        logger.error(f"[app_upsert_table_data] 表格不属于应用 gid={gid} tid={tid} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': '表格不属于应用'})

    ok, msg = OpenGateService.upsert_table_data_full(table=table, values=values, client_ip=get_client_ip(request))
    if not ok:
        logger.error(f"[app_upsert_table_data] 写入失败 tid={tid} ip={get_client_ip(request)} path={request.path} error={msg}")
        return JsonResponse({'result': 0, 'message': msg})
    logger.info(f"[app_upsert_table_data] 写入成功 tid={tid} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': '写入成功'})


@api_key_checker
def app_create_table(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': 'method not allowed'}, status=405)
    user, error_response = _api_key_principal(request)
    if error_response:
        return error_response
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response

    from apps.opengate.serializers import AppCreateTableSerializer
    serializer = AppCreateTableSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    ok, msg = OpenGateService.create_table_full(
        owner=user,
        gid=request.gid,
        table_name=serializer.validated_data['tableName'],
        description=serializer.validated_data.get('description', ''),
        permission=serializer.validated_data.get('permission', GatesTable.PERM_PRIVATE),
        column_titles=serializer.validated_data['columnTitles'],
        num_columns=serializer.validated_data['numColumns'],
        client_ip=get_client_ip(request),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg}, status=400)

    table = GatesTable.objects.filter(gate__gid=request.gid, name=serializer.validated_data['tableName']).order_by('-id').first()
    return JsonResponse({'result': 1, 'message': 'created', 'data': _external_table_payload(table)}, status=201)


@api_key_checker
def app_table_details(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': 'method not allowed'}, status=405)
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response

    from apps.opengate.serializers import AppTableDetailsSerializer
    serializer = AppTableDetailsSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    try:
        table = GatesTable.objects.get(tid=serializer.validated_data['tid'])
    except GatesTable.DoesNotExist:
        return JsonResponse({'result': 0, 'message': 'table not found'}, status=404)
    if table.gate.gid != request.gid:
        return JsonResponse({'result': 0, 'message': 'table does not belong to this project'}, status=403)
    return JsonResponse({'result': 1, 'message': 'success', 'data': _external_table_payload(table)})


@api_key_checker
def app_table_data(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': 'method not allowed'}, status=405)
    user, error_response = _api_key_principal(request)
    if error_response:
        return error_response
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response

    from apps.opengate.serializers import AppTableDataSerializer
    serializer = AppTableDataSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    try:
        table = GatesTable.objects.get(tid=serializer.validated_data['tid'])
    except GatesTable.DoesNotExist:
        return JsonResponse({'result': 0, 'message': 'table not found'}, status=404)
    if table.gate.gid != request.gid:
        return JsonResponse({'result': 0, 'message': 'table does not belong to this project'}, status=403)

    ok, data = OpenGateService.get_table_data(
        user=user,
        table=table,
        what_to_select=serializer.validated_data.get('what_to_select', ['*']),
        params=serializer.validated_data.get('params', {}),
        limit=int(serializer.validated_data.get('limit', 20)),
        offset=int(serializer.validated_data.get('offset', 0)),
        client_ip=get_client_ip(request),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data}, status=400)
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


@api_key_checker
def app_upsert_table_data(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': 'method not allowed'}, status=405)
    user, error_response = _api_key_principal(request)
    if error_response:
        return error_response
    data_json, error_response = _read_json_body(request)
    if error_response:
        return error_response

    from apps.opengate.serializers import AppUpsertTableDataSerializer
    serializer = AppUpsertTableDataSerializer(data=data_json)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    tid = serializer.validated_data['tid']
    try:
        table = GatesTable.objects.get(tid=tid)
    except GatesTable.DoesNotExist:
        return JsonResponse({'result': 0, 'message': 'table not found'}, status=404)
    if table.gate.gid != request.gid:
        return JsonResponse({'result': 0, 'message': 'table does not belong to this project'}, status=403)

    values = serializer.validated_data['values']
    risk_decision = RiskControlService.evaluate(
        "external_api.upsert",
        request,
        api_key_id=getattr(getattr(request, "api_key", None), "secret_id", ""),
        actor_user_id=getattr(user, "id", ""),
        company_id=request.gid,
        resource_id=tid,
    )
    if not risk_decision.allowed:
        return JsonResponse(risk_decision.to_response_payload(), status=risk_decision.status_code)

    try:
        ok, msg = OpenGateService.upsert_table_cells(
            user=user,
            table=table,
            cells=values,
            client_ip=get_client_ip(request),
            api_key=request.api_key,
            append=serializer.validated_data['append'],
            expected_max_row_index=serializer.validated_data.get('expected_max_row_index'),
            expected_table_version=serializer.validated_data.get('expected_table_version'),
        )
    except TableCellValidationError as exc:
        ok, msg = False, exc.errors
    except TableWriteConflict as exc:
        return JsonResponse(
            {'result': 0, 'message': str(exc), 'code': exc.code, 'data': exc.details},
            status=409,
        )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg}, status=400)
    return JsonResponse({'result': 1, 'message': 'success', 'data': {'tid': tid, 'written': len(values), **msg}})


@api_key_checker
def api_key_test(request):
    user_id = request.user_id
    return JsonResponse({'result': 1, 'message': '测试成功', 'user_id': user_id})


@login_checker
def transfer_owner(request):
    user_id = request.user_id
    current_user = User.objects.get(id=user_id)

    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import TransferOwnerSerializer
    s = TransferOwnerSerializer(data={'gatename': data_json.get('gate_name'), 'new_owner': data_json.get('email')})
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    gate_name = s.validated_data['gatename']
    target_user_email = s.validated_data['new_owner']
    
    try:
        gate = Gates.objects.get(gatename=gate_name)
    except Exception as e:
        result = {'result': 0, "message": "gate不存在", "error": str(e)}
        return JsonResponse(result)
    
    try:
        target_user = User.objects.get(email=target_user_email, is_activate=1)
    except Exception as e:
        logger.error(f"[transfer_owner] 目标邮箱无效 gate={gate_name} target_email={target_user_email} user={current_user.username} ip={request.META.get('REMOTE_ADDR')} path={request.path} error={str(e)}")
        result = {'result': 0, "message": "目标邮箱无效", "error": str(e)}
        return JsonResponse(result) 
    
    count = Gates.objects.filter(owner=target_user).count()
    if count > 500:
        logger.error(f"[transfer_owner] 目标用户创建的应用数量已达上限 gate={gate_name} target_email={target_user_email} user={current_user.username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        result = {'result': 0, "message": "目标用户创建的应用数量已达上限，请联系管理员"}
        return JsonResponse(result)
    
    if gate.owner.username != current_user.username:
        logger.error(f"[transfer_owner] 权限不足 gate={gate_name} current_user={current_user.username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        result = {'result': 0, "message": "权限不足"}
        return JsonResponse(result)
    
    gate.owner = target_user
    gate.updated_at = str(time.time())[:20]
    gate.save()

    # 更新gate中所有table的member信息，把转让后的用户信息添加到tablemember中。更新gateUser表
    tables = GatesTable.objects.filter(gate=gate)
    for table in tables:
        if len(GatesMember.objects.filter(table=table, member=current_user)) == 0:
            GatesMember.objects.create(table=table, member=target_user, permission='admin', is_valid=1, created_at=str(time.time())[:20], updated_at=str(time.time())[:20])
        else: 
            gm = GatesMember.objects.get(table=table, member=current_user)
            gm.permission = 'admin'
            gm.is_valid = 1
            gm.updated_at = str(time.time())[:20]
            gm.save()
    # refresh_gate_user(gate, target_user)  # 函数不存在，已注释
    # gate_user = GatesUser.objects.get(gate=gate, user=current_user)  # 模型不存在，已注释
    # gate_user.user = target_user  # 已注释
    # gate_user.save()  # 已注释
    
    
    logger.info(f"[transfer_owner] 修改成功 gate={gate_name} new_owner={target_user.username} operator={current_user.username} ip={get_client_ip(request)} path={request.path}")
    result = {"result": 1, "message": "修改成功"}
    return JsonResponse(result)


@login_checker
def toggle_gate_key(request):
    """
    暂停API key
    不需要手机号验证
    """
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    data_json = json.loads(request.body.decode())
    serializer = ToggleGateKeySerializer(data=data_json)

    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    gid = serializer.validated_data.get('gid', '')
    secret_id = serializer.validated_data.get('secret_id', '')

    ok, msg = OpenGateService.toggle_gate_key(user, gid, secret_id, client_ip=get_client_ip(request))
    if not ok:
        logger.error(f"[toggle_gate_key] {msg} gid={gid} secret_id={secret_id} user={user.username} ip={get_client_ip(request)} path={request.path}")
        return JsonResponse({'result': 0, 'message': msg})
    logger.info(f"[toggle_gate_key] {msg} gid={gid} secret_id={secret_id} user={user.username} ip={get_client_ip(request)} path={request.path}")
    return JsonResponse({'result': 1, 'message': msg})

@login_checker
def list_table_by_user(request):
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    data_json = json.loads(request.body.decode())
    from apps.opengate.serializers import ListTableByUserSerializer
    s = ListTableByUserSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    try:
        tables = OpenGateService.list_tables_by_user(user, s.validated_data['limit'], s.validated_data['offset'])
        return JsonResponse({'result': 1, 'message': '查找成功', 'data': tables})
    except Exception as e:
        logger.error(f"/opengate/tables/list 查找失败 user={user.username} ip={get_client_ip(request)} path={request.path} error={str(e)}")
        return JsonResponse({'result': 0, 'message': '查找失败'})


@login_checker
def add_table_member(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        data_json = json.loads(request.body.decode())
    except (JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'result': 0, 'message': '请求参数格式错误'}, status=400)
    from apps.opengate.serializers import UpdateMemberSerializer
    s = UpdateMemberSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors}, status=400)
    tid = s.validated_data['tid']
    uid = s.validated_data['uid']
    permission = s.validated_data['permission']
    client_ip = get_client_ip(request)
    ok, msg = OpenGateService.update_table_member(user, tid, uid, permission, client_ip=client_ip)
    if not ok:
        logger.error(f"[add_table_member] 添加失败 tid={tid} uid={uid} permission={permission} user={user.username} ip={client_ip} path={request.path}")
        fail_status = 403 if '无权限' in msg else 400
        return JsonResponse({'result': 0, 'message': msg}, status=fail_status)
    logger.info(f"[add_table_member] 添加成功 tid={tid} uid={uid} permission={permission} user={user.username} ip={client_ip} path={request.path}")
    return JsonResponse({'result': 1, 'message': msg})


@login_checker
def remove_table_member(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '请求方式错误'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        data_json = json.loads(request.body.decode())
    except (JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'result': 0, 'message': '请求参数格式错误'}, status=400)
    from apps.opengate.serializers import RemoveMemberSerializer
    s = RemoveMemberSerializer(data=data_json)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors}, status=400)
    tid = s.validated_data['tid']
    uid = s.validated_data['uid']
    client_ip = get_client_ip(request)
    ok, msg = OpenGateService.remove_table_member(user, tid, uid, client_ip=client_ip)
    if not ok:
        logger.error(f"[remove_table_member] 删除失败 tid={tid} uid={uid} user={user.username} ip={client_ip} path={request.path}")
        fail_status = 403 if '无权限' in msg else 400
        return JsonResponse({'result': 0, 'message': msg}, status=fail_status)
    logger.info(f"[remove_table_member] 删除成功 tid={tid} uid={uid} user={user.username} ip={client_ip} path={request.path}")
    return JsonResponse({'result': 1, 'message': msg})
