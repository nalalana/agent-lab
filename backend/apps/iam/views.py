from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .models import Principal, Resource, Grant
from .serializers import (
    PrincipalSerializer,
    ResourceSerializer,
    GrantSerializer,
    CheckAccessRequestSerializer,
    EffectivePermissionRequestSerializer,
    GrantRequestSerializer,
)
from .services import compute_effective_permission, grant_permissions, revoke_permissions
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request


@api_view(['POST'])
@login_checker
def grant(request):
    """
    给某个主体在指定资源上“授予权限”。
    支持读/写/管理 等位权限，默认可向下继承（inherit=True），可设置过期时间。
    若该主体在该资源已存在授权，则更新授权而不是重复创建。
    """
    serializer = GrantRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'result': -1, 'message': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    principal, _ = Principal.objects.get_or_create(
        type=data['principal_type'], external_id=data['principal_external_id']
    )
    resource_external_id = data.get('resource_external_id') or data.get('resource_uid')
    resource = get_object_or_404(Resource, external_id=resource_external_id)

    grant_obj = grant_permissions(
        principal=principal,
        resource=resource,
        permissions=data['permissions'],
        granter_user=request.user,
        inherit=data.get('inherit', True),
        expires_at=data.get('expires_at'),
    )
    return Response({'result': 0, 'data': GrantSerializer(grant_obj).data})


@api_view(['POST'])
@login_checker
def revoke(request):
    """撤销某个主体在指定资源上的“授权”。"""
    serializer = GrantRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'result': -1, 'message': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    principal = get_object_or_404(Principal, type=data['principal_type'], external_id=data['principal_external_id'])
    resource_external_id = data.get('resource_external_id') or data.get('resource_uid')
    resource = get_object_or_404(Resource, external_id=resource_external_id)

    revoke_permissions(principal=principal, resource=resource)
    return Response({'result': 0, 'message': 'revoked'})


@api_view(['POST'])
@login_checker
def list_grants(request):
    """列出指定资源上的所有“授权”。"""
    resource_uid = request.data.get('resource_uid') or request.data.get('resource_external_id')
    if not resource_uid:
        return Response({'result': -1, 'message': 'resource_uid required'}, status=status.HTTP_400_BAD_REQUEST)
    resource = get_object_or_404(Resource, exteranl_id=resource_uid)
    qs = Grant.objects.filter(resource=resource)
    return Response({'result': 0, 'data': GrantSerializer(qs, many=True).data})


@api_view(['POST'])
@login_checker
def effective(request):
    """计算某个主体在指定资源上的“有效权限”。"""
    serializer = EffectivePermissionRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'result': -1, 'message': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
    data = serializer.validated_data

    principal, _ = Principal.objects.get_or_create(
        type=data['principal_type'], external_id=data['principal_external_id']
    )
    resource_external_id = data.get('resource_external_id') or data.get('resource_uid')
    resource = get_object_or_404(Resource, external_id=resource_external_id)
    eff = compute_effective_permission(principal=principal, resource=resource)
    return Response({'result': 0, 'data': {'permissions': eff.permissions, 'can_read': eff.can_read(), 'can_write': eff.can_write()}})


@api_view(['POST'])
@login_checker
def list_grants_by_principal(request):
    """列出某个主体的所有“授权”。"""
    serializer = PrincipalSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'result': -1, 'message': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
    data = serializer.validated_data
    principal, _ = Principal.objects.get_or_create(
        type=data['principal_type'], external_id=data['principal_external_id']
    )
    qs = Grant.objects.filter(principal=principal)
    return Response({'result': 0, 'data': GrantSerializer(qs, many=True).data})


@api_view(['POST'])
@login_checker
def check_access(request):
    """Check whether the current login user can access a resource."""
    serializer = CheckAccessRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'result': 0, 'message': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    user = get_user_from_request(request)
    if not user:
        return Response({'result': 0, 'message': 'login required', 'code': 401}, status=status.HTTP_401_UNAUTHORIZED)

    data = serializer.validated_data
    permission_value = data.get('permission')
    if isinstance(permission_value, str):
        permission_value = {
            'read': Grant.PERM_READ,
            'write': Grant.PERM_WRITE,
            'admin': Grant.PERM_ADMIN,
            'super_admin': Grant.PERM_SUPER_ADMIN,
        }[permission_value]

    principal, _ = Principal.objects.get_or_create(type=Principal.TYPE_USER, external_id=user.uid)
    resource = Resource.objects.filter(
        type=data['resource_type'],
        external_id=data['resource_external_id'],
    ).first()

    eff_permissions = 0
    allowed = False
    if resource:
        eff = compute_effective_permission(principal=principal, resource=resource)
        eff_permissions = eff.permissions
        allowed = eff.permissions >= int(permission_value)

    response_status = status.HTTP_200_OK if allowed else status.HTTP_403_FORBIDDEN
    return Response(
        {
            'result': 1,
            'message': 'ok' if allowed else 'permission denied',
            'data': {
                'allowed': allowed,
                'resource_type': data['resource_type'],
                'resource_external_id': data['resource_external_id'],
                'required_permission': int(permission_value),
                'permissions': eff_permissions,
            },
        },
        status=response_status,
    )