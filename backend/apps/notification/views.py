"""
/backend/apps/notification/views.py
消息通知视图函数
"""
import json
import logging
from django.http import JsonResponse
from rest_framework.decorators import api_view
from common.utils.Login_utils import login_checker, get_client_ip
from common.utils.common_utils import get_user_from_request
from apps.notification.serializers import (
    NotificationListSerializer,
    NotificationMarkReadSerializer,
    NotificationMarkAllReadSerializer,
    NotificationUnreadCountSerializer
)
from apps.notification.services import NotificationService

logger = logging.getLogger('touming')


@api_view(['GET'])
@login_checker
def notification_list(request):
    """
    获取通知列表
    
    GET /api/notification/list?limit=20&offset=0&status=all&type=login
    """
    serializer = NotificationListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    
    user = get_user_from_request(request)
    limit = serializer.validated_data['limit']
    offset = serializer.validated_data['offset']
    status = serializer.validated_data['status']
    type = serializer.validated_data.get('type', '')
    
    data = NotificationService.get_notifications(
        user=user,
        limit=limit,
        offset=offset,
        status=status,
        type=type if type else None
    )
    
    return JsonResponse({
        'result': 1,
        'message': '获取成功',
        'data': data
    })


@api_view(['POST'])
@login_checker
def notification_mark_read(request):
    """
    标记通知为已读
    
    POST /api/notification/mark-read
    Body: {"notification_id": 123}
    """
    try:
        body = json.loads(request.body.decode())
    except json.JSONDecodeError:
        return JsonResponse({'result': 0, 'message': '请求体格式错误'})
    
    serializer = NotificationMarkReadSerializer(data=body)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    
    user = get_user_from_request(request)
    notification_id = serializer.validated_data['notification_id']
    
    ok, message = NotificationService.mark_as_read(user, notification_id)
    if not ok:
        return JsonResponse({'result': 0, 'message': message})
    
    return JsonResponse({'result': 1, 'message': message})


@api_view(['POST'])
@login_checker
def notification_mark_all_read(request):
    """
    标记所有通知为已读
    
    POST /api/notification/mark-all-read
    """
    user = get_user_from_request(request)
    
    ok, message = NotificationService.mark_all_as_read(user)
    if not ok:
        return JsonResponse({'result': 0, 'message': message})
    
    return JsonResponse({'result': 1, 'message': message})


@api_view(['GET'])
@login_checker
def notification_unread_count(request):
    """
    获取未读通知数量
    
    GET /api/notification/unread-count
    """
    user = get_user_from_request(request)
    count = NotificationService.get_unread_count(user)
    
    return JsonResponse({
        'result': 1,
        'message': '获取成功',
        'data': {'count': count}
    })
