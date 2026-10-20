import json
import logging
from django.http import JsonResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.follow.services import FollowingService
from common.utils.Login_utils import login_checker
from common.utils.Tengcentcos_utils import get_moderated_image_presigned_url
from common.utils.common_utils import get_user_from_request

logger = logging.getLogger('touming')

@api_view(['POST'])
@login_checker
def gate_operation(request):
    """关注Gate"""
    from apps.follow.serializers import GateOperationSerializer
    try:
        data = json.loads(request.body.decode())
        s = GateOperationSerializer(data=data)
        if not s.is_valid():
            return JsonResponse({
                'result': 0,
                'message': s.errors
            }, status=400)
        user = get_user_from_request(request)
        if not user:
            return JsonResponse({
                'result': 0,
                'message': '请先登录'
            }, status=401)
        if s.validated_data['operation'] == GateOperationSerializer.OPRATION_FOLLOW:
            success, message = FollowingService.follow_gate(user, s.validated_data['gid'])
        else:
            success, message = FollowingService.unfollow_gate(user, s.validated_data['gid'])
        return JsonResponse({
            'result': 1 if success else 0,
            'message': message
        })
        
    except Exception as e:
        logger.error(f"关注应用失败: {e}")
        return JsonResponse({
            'result': 0,
            'message': str(e)
        }, status=500)



@login_checker
def check_gate_following_status(request):
    """检查是否已关注Gate"""
    from apps.follow.serializers import CheckGateFollowingStatusSerializer    

    s = CheckGateFollowingStatusSerializer(data=json.loads(request.body.decode()))
    if not s.is_valid():
        return JsonResponse({
            'result': 0,
            'message': s.errors
        }, status=400)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    gid = s.validated_data['gid']
    success, is_following = FollowingService.is_following_gate(user, gid)
    if not success:
        return JsonResponse({'result': 0, 'message': is_following}, status=400)
    return JsonResponse({'result': 1, 'message': '检查成功', 'data': {'is_following': is_following}})


@api_view(['POST'])
@login_checker
def company_operation(request):
    """关注Company"""
    from apps.follow.serializers import CompanyOperationSerializer
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({
            'result': 0,
            'message': '请先登录'
        }, status=401)
    s = CompanyOperationSerializer(data=json.loads(request.body.decode()))
    if not s.is_valid():
        return JsonResponse({
            'result': 0,
            'message': s.errors
        }, status=400)
    cid = s.validated_data['cid']
    if s.validated_data['operation'] == CompanyOperationSerializer.OPRATION_FOLLOW:
        success, message = FollowingService.follow_company(user, cid)
    else:
        success, message = FollowingService.unfollow_company(user, cid)
        
    return JsonResponse({'result': 1 if success else 0, 'message': message})


@api_view(['POST'])
def list_company_followers(request):
    """获取Company的粉丝列表"""
    try:
        data = json.loads(request.body.decode())
        company_id = data.get('company_id')
        limit = data.get('limit', 20)
        offset = data.get('offset', 0)
        
        if not company_id:
            return JsonResponse({
                'result': 0,
                'message': '公司ID不能为空'
            }, status=400)
        
        followers_list = FollowingService.get_company_followers(company_id, limit, offset)
        
        # 处理头像URL
        for follower in followers_list:
            if follower['avatar']:
                follower['avatar'] = get_moderated_image_presigned_url(follower['avatar'])
        
        return JsonResponse({
            'result': 1,
            'message': followers_list
        })
        
    except Exception as e:
        logger.error(f"获取公司粉丝列表失败: {e}")
        return JsonResponse({
            'result': 0,
            'message': str(e)
        }, status=500)


@login_checker
def check_company_following_status(request):
    """检查是否已关注Company"""
    from apps.follow.serializers import CheckCompanyFollowingStatusSerializer

    s = CheckCompanyFollowingStatusSerializer(data=json.loads(request.body.decode()))
    if not s.is_valid():
        return JsonResponse({
            'result': 0,
            'message': s.errors
        }, status=400)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    cid = s.validated_data['cid']
    success, is_following = FollowingService.is_following_company(user, cid)
    if not success:
        return JsonResponse({'result': 0, 'message': is_following}, status=400)
    return JsonResponse({'result': 1, 'message': '检查成功', 'data': {'is_following': is_following}})



@login_checker
def get_following_gates(request):
    """获取用户关注的Gate列表"""
    from apps.follow.serializers import GetFollowingGatesSerializer    
    serializer = GetFollowingGatesSerializer(data=json.loads(request.body.decode()))
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    limit = serializer.validated_data['limit']
    offset = serializer.validated_data['offset']
    success, data = FollowingService.get_following_gates(user, limit, offset)
    if not success:
        return JsonResponse({'result': 0, 'message': data}, status=400)
    return JsonResponse({'result': 1 if success else 0, 'message': '获取成功', 'data': data})
    

@login_checker
def get_following_companies(request):
    """获取用户关注的Company列表"""
    from apps.follow.serializers import GetFollowingCompaniesSerializer
    try:
        serializer = GetFollowingCompaniesSerializer(data=json.loads(request.body.decode()))
        if not serializer.is_valid():
            return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)
        user = get_user_from_request(request)
        if not user:
            return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
        limit = serializer.validated_data['limit']
        offset = serializer.validated_data['offset']
        success, data = FollowingService.get_following_companies(user, limit, offset)
        if not success:
            return JsonResponse({'result': 0, 'message': data}, status=400)
        return JsonResponse({'result': 1 if success else 0, 'message': '获取成功', 'data': data})
        
    except Exception as e:
        logger.error(f"获取关注的Company列表失败: {e}")
        return JsonResponse({
            'result': 0,
            'message': str(e)
        }, status=500)
