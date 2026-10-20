import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta

from django.db import transaction
from django.http import JsonResponse, HttpResponseRedirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.sso.models import SSOAuthorizationCode, SSOClient
from apps.sso.serializers import SSOExchangeCodeSerializer, SSOAuthorizeSerializer
from apps.user.models import User
from apps.company.models import Company, TransparentCompany
from apps.company.permissions import can_access_formal_company
from apps.iam.models import Grant
from common.utils.Login_utils import login_checker, sign_token
from common.utils.common_utils import get_user_from_request

logger = logging.getLogger(__name__)


@login_checker
def sso_authorize(request):
    """
    SSO authorization endpoint - generates authorization code for logged-in user

    Query parameters:
    - client_id: The client ID of the requesting application
    - redirect_uri: The URI to redirect back to with the authorization code
    - state: Optional state parameter for CSRF protection
    - response_type: Must be 'code' (authorization code flow)
    """
    serializer = SSOAuthorizeSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': str(serializer.errors)}, status=400)

    client_id = serializer.validated_data['client_id']
    redirect_uri = serializer.validated_data['redirect_uri']
    state = serializer.validated_data.get('state', '')
    company_id = serializer.validated_data.get('company_id', '')
    # Get current user
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    # Verify client exists and is active
    try:
        client = SSOClient.objects.get(client_id=client_id, is_active=True)
    except SSOClient.DoesNotExist:
        return JsonResponse({'result': 0, 'message': '无效的客户端ID'}, status=400)

    # Verify redirect_uri is allowed
    allowed_uris = client.get_redirect_uris()
    if redirect_uri not in allowed_uris:
        return JsonResponse({'result': 0, 'message': '无效的回调地址'}, status=400)

    # Get user's primary company (first company where user is owner/member)
    # You may need to adjust this logic based on your business requirements
    try:
        # Try to find a company where user is owner first
        company = Company.objects.filter(cid=company_id).first()
        if not company or not can_access_formal_company(company, user, Grant.PERM_READ):
            return JsonResponse(
                {'result': 0, 'message': '无权访问该公司'},
                status=403,
            )
        if not TransparentCompany.objects.filter(company=company).exists():
            return JsonResponse({'result': 0, 'message': '公司不是透明公司'}, status=400)
    except Exception as e:
        return JsonResponse({'result': 0, 'message': f'获取公司信息失败: {str(e)}'}, status=500)

    # Generate authorization code
    code = secrets.token_urlsafe(32)
    expires_at = timezone.now() + timedelta(minutes=10)  # Code expires in 10 minutes

    # Save authorization code
    with transaction.atomic():
        SSOAuthorizationCode.objects.create(
            code=code,
            user=user,
            company=company,
            client_id=client_id,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
            is_used=False
        )

    # Build redirect URL with code
    separator = '&' if '?' in redirect_uri else '?'
    redirect_url = f"{redirect_uri}{separator}code={code}"
    if state:
        redirect_url += f"&state={state}"

    return HttpResponseRedirect(redirect_url)


@csrf_exempt
def sso_exchange_code(request):
    """
    SSO code exchange endpoint - exchange authorization code for user/company info

    POST body:
    {
        "code": "authorization_code",
        "client_id": "client_id",  // optional
        "client_secret": "client_secret"  // optional
    }

    Response:
    {
        "result": 1,
        "message": "success",
        "data": {
            "user_id": "xxx",
            "company_id": "xxx"
        }
    }
    """
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError as e:
        logger.warning("[sso/exchange] invalid JSON: %s", e)
        return JsonResponse({'result': 0, 'message': '无效的JSON'}, status=400)

    serializer = SSOExchangeCodeSerializer(data=body)
    if not serializer.is_valid():
        logger.warning("[sso/exchange] validation failed: %s", serializer.errors)
        return JsonResponse({'result': 0, 'message': str(serializer.errors)}, status=400)

    code = serializer.validated_data['code']
    client_id = serializer.validated_data.get('client_id', '').strip()
    client_secret = serializer.validated_data.get('client_secret', '').strip()

    # Find authorization code
    try:
        auth_code = SSOAuthorizationCode.objects.get(code=code)
    except SSOAuthorizationCode.DoesNotExist:
        logger.warning("[sso/exchange] invalid or unknown code")
        return JsonResponse({'result': 0, 'message': '无效的授权码'}, status=400)

    # Verify code is not expired
    if timezone.now() > auth_code.expires_at:
        logger.warning("[sso/exchange] code expired code=%s", code[:8])
        return JsonResponse({'result': 0, 'message': '授权码已过期'}, status=400)

    # Verify code has not been used
    if auth_code.is_used:
        logger.warning("[sso/exchange] code already used code=%s", code[:8])
        return JsonResponse({'result': 0, 'message': '授权码已被使用'}, status=400)

    # When credentials are omitted, trust the client recorded on the authorization code.
    if not client_id:
        client_id = auth_code.client_id

    if client_id != auth_code.client_id:
        logger.warning("[sso/exchange] client_id mismatch")
        return JsonResponse({'result': 0, 'message': '客户端ID不匹配'}, status=400)

    try:
        client = SSOClient.objects.get(
            client_id=client_id,
            is_active=True
        )
    except SSOClient.DoesNotExist:
        logger.warning("[sso/exchange] invalid client credentials client_id=%s", client_id)
        return JsonResponse({'result': 0, 'message': '无效的客户端凭证'}, status=400)

    access_token = sign_token({'user_id': auth_code.user.id})

    # Mark code as used
    with transaction.atomic():
        auth_code.is_used = True
        auth_code.save()

    # Return user and company info
    return JsonResponse({
        'result': 1,
        'message': 'success',
        'access_token': access_token,
        'token_type': 'Bearer',
        'user_id': str(auth_code.user.uid),
        'company_id': str(auth_code.company.cid),
        'data': {
            'user_id': str(auth_code.user.uid),
            'company_id': str(auth_code.company.cid),
        }
    })


@login_checker
def sso_me(request):
    """
    SSO current user endpoint - get current logged-in user info
    
    Headers:
    - Access-Token: JWT token (optional, also accepts token from query params)
    
    Response:
    {
        "result": 1,
        "message": "success",
        "data": {
            "user_id": "xxx",
            "username": "xxx",
            "email": "xxx",
            "phone": "xxx",
            "avatar": "xxx",
            "bio": "xxx",
            "role": "xxx"
        }
    }
    """
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    
    return JsonResponse({
        'result': 1,
        'message': 'success',
        'data': user.get_info()
    })
