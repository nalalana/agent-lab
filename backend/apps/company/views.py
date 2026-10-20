import base64
import json
import os
import re
import time
import uuid
import datetime
from django.conf import settings
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from common.utils.Tengcentcos_utils import get_moderated_image_presigned_url, put_object

from apps.moderation.models import ImageModerationAsset
from apps.moderation.services import ImageModerationService, moderation_payload
from apps.user.permissions import is_platform_administrator
from apps.user.models import User
from common.utils.Login_utils import login_checker
from apps.company.serializers import (
    CompanyCreateSerializer,
    CompanyUpdateSerializer,
    ManualVerificationCancelSerializer,
    ManualVerificationEvidenceSerializer,
    ManualVerificationIdentitySerializer,
    ManualVerificationRoleSerializer,
    VerificationAuditListSerializer,
    ManualVerificationDocumentUploadSerializer,
    ManualVerificationStartSerializer,
    ManualVerificationSubmitSerializer,
    VerificationCompanyListSerializer,
    VerificationReviewDecisionSerializer,
    VerificationReviewerGrantSerializer,
    VerificationReviewerListSerializer,
    VerificationReviewerRevokeSerializer,
    VerificationReviewQueueSerializer,
    VerificationRetentionPlanSerializer,
    VerificationSuspensionSerializer,
    VerificationTranscriptionSerializer,
)
from apps.company.services import CompanyService
from apps.company.verification import (
    VerificationFlowError,
    continue_company_verification,
    enforce_refresh_rate_limit,
    get_company_verification,
    refresh_company_verification_for_user,
    start_company_verification,
)
from apps.company.models import CompanyVerification
from apps.company.verification_domain import (
    ManualVerificationIdentityInput,
    ManualVerificationRoleInput,
    ManualVerificationStartInput,
    VerificationDomainError,
    bind_manual_verification_identity,
    cancel_manual_verification,
    continue_manual_verification_to_next_step,
    create_manual_verification,
    get_manual_verification,
    return_manual_verification_to_previous_step,
    select_manual_verification_applicant_role,
)
from apps.company.verification_evidence import (
    enforce_evidence_rate_limit,
    query_applicant_face,
    run_registry_verification,
    start_applicant_face,
)
from apps.company.verification_business_license import run_business_license_ocr
from apps.company.verification_documents import (
    generate_authorization_template,
    get_authorization_preview,
    upload_authorization_document,
    upload_business_license_document,
)
from apps.company.verification_reviews import (
    decide_manual_verification,
    get_reviewer_context,
    get_review_application,
    get_review_document_preview,
    grant_or_update_reviewer,
    list_registered_companies,
    list_verification_audit_events,
    list_review_queue,
    list_reviewers,
    restore_company_service,
    restore_company_verification,
    revoke_reviewer,
    submit_manual_verification,
    suspend_company_verification,
    suspend_company_service,
    transcribe_authorization_document,
)
from apps.company.verification_runtime import (
    enforce_verification_rate_limit,
    plan_company_draft_retention,
    plan_verification_document_retention,
    verification_job_health,
)
from common.utils.common_utils import get_user_from_request

import logging
logger = logging.getLogger('touming')


def _verification_error_response(exc):
    return JsonResponse(
        {'result': 0, 'code': exc.code, 'message': str(exc)},
        status=exc.status_code,
    )


def _request_json(request):
    try:
        return json.loads(request.body.decode() or '{}')
    except (TypeError, UnicodeDecodeError, ValueError):
        return {}


def _has_manual_verification(company_id: str) -> bool:
    return CompanyVerification.objects.filter(
        company__cid=company_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).exists()


def _require_method(request, method: str):
    if request.method == method:
        return None
    return JsonResponse(
        {'result': 0, 'message': f'仅支持{method}请求'},
        status=405,
    )


def _rate_limit(user, scope: str, resource: str = ''):
    enforce_verification_rate_limit(
        scope=scope,
        principal=str(user.id),
        resource=str(resource or ''),
    )


def _no_store_json(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    return response


def _sse_encode(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _optional_query_bool(request, serializer, field_name):
    if field_name not in request.GET or request.GET.get(field_name) == '':
        return None
    return serializer.validated_data.get(field_name)


def _optional_user_from_request(request):
    user = get_user_from_request(request)
    if user:
        return user
    token = request.META.get('HTTP_ACCESS_TOKEN')
    if not token:
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
    if not token:
        token = request.GET.get('access_token') or request.GET.get('token')
    if not token:
        return None
    try:
        from common.utils.Login_utils import check_token

        payload = check_token(token)
        user_id = int((payload or {}).get('user_id') or 0)
        if user_id <= 0:
            return None
        return User.objects.filter(id=user_id).first()
    except Exception:
        return None


# Create your views here.
@login_checker
def company_list(request):
    from apps.company.serializers import CompanyQuerySerializer
    from apps.company.services import CompanyService
    s = CompanyQuerySerializer(data=request.GET)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    data = CompanyService.list_companies(
        keyword=s.validated_data.get('keyword', ''),
        limit=s.validated_data['limit'],
        offset=s.validated_data['offset']
    )
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': data['items'], 'total': data['total']})

@login_checker
def company_list_by_user(request):
    from apps.company.serializers import CompanyListByUserSerializer
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = CompanyListByUserSerializer(data=json.loads(request.body.decode()))
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    limit = serializer.validated_data['limit']
    offset = serializer.validated_data['offset']
    data = CompanyService.list_companies_by_user(user=user, limit=limit, offset=offset)
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': data})
    
@login_checker
def company_create(request):
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    s = CompanyCreateSerializer(data=json.loads(request.body.decode()))
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    
    import hashlib
    from django.db import transaction
    from django.utils import timezone
    from apps.user.models import CompanyInvitation
    with transaction.atomic():
        invite = CompanyInvitation.objects.select_for_update().filter(
            code_hash=hashlib.sha256(s.validated_data['invitation_code'].encode()).hexdigest(),
            remaining_uses__gt=0, expires_at__gt=timezone.now(),
        ).first()
        if not invite:
            return JsonResponse({'result': 0, 'message': '邀请码无效、已使用或已过期'}, status=400)
        ok, company_or_err = CompanyService.create_company(
            company_name=s.validated_data['company_name'],
            description=s.validated_data.get('description', ''),
            logo=s.validated_data.get('logo', ''),
            background_image=s.validated_data.get('background_image', ''),
            owner=user,
            client_ip=request.META.get('REMOTE_ADDR'),
            registration_no=s.validated_data.get('registration_no', ''),
            establishment_date=s.validated_data.get('establishment_date', ''),
            company_size=s.validated_data.get('company_size', ''),
            contact_phone=s.validated_data.get('contact_phone', ''),
            contact_email=s.validated_data.get('contact_email', ''),
            website=s.validated_data.get('website', ''),
            address=s.validated_data.get('address', ''),
        )
        if ok:
            invite.remaining_uses -= 1
            invite.save(update_fields=['remaining_uses'])

    if not ok:
        logger.error(f"[company-create] {company_or_err} operator={user.username} ip={request.META.get('REMOTE_ADDR')} path={request.path}")
        return JsonResponse({'result': 0, 'message': company_or_err})
    
    company = company_or_err
    return JsonResponse({'result': 1, 'message': '创建成功', 'data': company.cid})


def company_detail(request):
    from apps.company.serializers import CompanyDetailSerializer
    serializer = CompanyDetailSerializer(data=json.loads(request.body.decode()))
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    ok, detail = CompanyService.get_company_detail(
        cid=serializer.validated_data['cid'],
        user=_optional_user_from_request(request),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': detail})
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': detail})

@login_checker
def company_update(request):
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = CompanyUpdateSerializer(data=json.loads(request.body.decode()))
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    ok, msg = CompanyService.update_company(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
        company_name=serializer.validated_data.get('company_name'),
        description=serializer.validated_data.get('description'),
        logo=serializer.validated_data.get('logo'),
        background_image=serializer.validated_data.get('background_image'),
        status=serializer.validated_data.get('status'),
        registration_no=serializer.validated_data.get('registration_no'),
        establishment_date=serializer.validated_data.get('establishment_date'),
        company_size=serializer.validated_data.get('company_size'),
        contact_phone=serializer.validated_data.get('contact_phone'),
        contact_email=serializer.validated_data.get('contact_email'),
        website=serializer.validated_data.get('website'),
        address=serializer.validated_data.get('address'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg or '更新失败'})
    return JsonResponse({'result': 1, 'message': msg or '更新成功'})


@login_checker
def company_draft_delete(request):
    from apps.company.serializers import CompanyDraftDeleteSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        payload = json.loads(request.body.decode() or '{}')
    except (TypeError, ValueError):
        payload = {}
    serializer = CompanyDraftDeleteSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    ok, message = CompanyService.delete_company_draft(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': message or '删除失败'})
    return JsonResponse({'result': 1, 'message': '删除成功'})


@login_checker
def company_unbind(request):
    from apps.company.serializers import CompanyDraftDeleteSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = CompanyDraftDeleteSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)
    ok, message = CompanyService.unbind_company(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': message or '解绑失败'})
    return JsonResponse({'result': 1, 'message': '解绑成功'})


@login_checker
def company_verification_start(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'start', company_id)
        if getattr(settings, 'COMPANY_VERIFICATION_ENABLED', False):
            serializer = ManualVerificationStartSerializer(data=_request_json(request))
            if not serializer.is_valid():
                return JsonResponse(
                    {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
                    status=400,
                )
            result = create_manual_verification(
                company_id=company_id,
                user=user,
                value=ManualVerificationStartInput(**serializer.validated_data),
            )
            data = result
        else:
            if not getattr(settings, 'COMPANY_VERIFICATION_ESS_START_ENABLED', False):
                raise VerificationDomainError('feature_disabled', status_code=503)
            data = start_company_verification(
                company_id=company_id,
                user=user,
            ).as_dict()
    except (VerificationFlowError, VerificationDomainError) as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '企业认证已发起', 'data': data})


@login_checker
def company_verification_continue(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'query', company_id)
        if _has_manual_verification(company_id):
            data = get_manual_verification(company_id=company_id, user=user)
        else:
            if not getattr(settings, 'COMPANY_VERIFICATION_ESS_START_ENABLED', False):
                raise VerificationDomainError('provider_unavailable', status_code=503)
            data = continue_company_verification(
                company_id=company_id,
                user=user,
            ).as_dict()
    except (VerificationFlowError, VerificationDomainError) as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '认证申请已读取', 'data': data})


@login_checker
def company_verification_refresh(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        if _has_manual_verification(company_id):
            serializer = ManualVerificationEvidenceSerializer(data=_request_json(request))
            if not serializer.is_valid():
                return JsonResponse(
                    {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
                    status=400,
                )
            current = get_manual_verification(company_id=company_id, user=user)
            enforce_evidence_rate_limit(company_id, user.id, 'refresh')
            kwargs = dict(
                company_id=company_id, application_id=current['application_id'],
                user=user, **serializer.validated_data,
            )
            if current['stage'] == CompanyVerification.STAGE_COMPANY_CHECK:
                if current.get('applicant_role'):
                    license_kwargs = {
                        **kwargs,
                        'idempotency_key': (
                            f"license:{serializer.validated_data['idempotency_key']}"
                        )[:128],
                    }
                    result = run_business_license_ocr(**license_kwargs)
                    if result.get('business_license_check') == 'passed':
                        registry_kwargs = {
                            **kwargs,
                            'idempotency_key': (
                                f"registry:{serializer.validated_data['idempotency_key']}"
                            )[:128],
                        }
                        result = run_registry_verification(**registry_kwargs)
                else:
                    result = run_registry_verification(**kwargs)
            elif current['stage'] == CompanyVerification.STAGE_APPLICANT_FACE:
                result = query_applicant_face(**kwargs)
            else:
                result = current
        else:
            if not getattr(settings, 'COMPANY_VERIFICATION_ESS_RECONCILE_ENABLED', False):
                raise VerificationDomainError('provider_unavailable', status_code=503)
            enforce_refresh_rate_limit(company_id, user.id)
            result = refresh_company_verification_for_user(company_id=company_id, user=user)
    except (VerificationFlowError, VerificationDomainError) as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '认证状态已刷新', 'data': result})


@login_checker
def company_verification_detail(request, company_id, application_id=None):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'query', company_id)
        if application_id or _has_manual_verification(company_id):
            result = get_manual_verification(
                company_id=company_id,
                user=user,
                application_id=str(application_id) if application_id else None,
            )
        else:
            result = get_company_verification(company_id=company_id, user=user)
    except (VerificationFlowError, VerificationDomainError) as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': result})


@login_checker
def company_verification_applicant_identity(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationIdentitySerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'identity', company_id)
        result = bind_manual_verification_identity(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            value=ManualVerificationIdentityInput(**serializer.validated_data),
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '核验人身份信息已安全保存', 'data': result})


@login_checker
def company_verification_applicant_role(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationRoleSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'identity', company_id)
        result = select_manual_verification_applicant_role(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            value=ManualVerificationRoleInput(**serializer.validated_data),
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '认证方式已更新', 'data': result})


@login_checker
def company_verification_face_start(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationEvidenceSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors}, status=400
        )
    try:
        enforce_evidence_rate_limit(company_id, user.id, 'face-start')
        result = start_applicant_face(
            company_id=company_id, application_id=str(application_id), user=user,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '实名核身会话已创建', 'data': result})


@login_checker
def company_verification_face_refresh(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationEvidenceSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors}, status=400
        )
    try:
        enforce_evidence_rate_limit(company_id, user.id, 'face-refresh')
        result = query_applicant_face(
            company_id=company_id, application_id=str(application_id), user=user,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '实名核身结果已刷新', 'data': result})


@login_checker
def company_verification_cancel(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationCancelSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        result = cancel_manual_verification(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '认证申请已取消', 'data': result})


@login_checker
def company_verification_previous_step(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationCancelSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        result = return_manual_verification_to_previous_step(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '已返回认证流程上一步', 'data': result})


@login_checker
def company_verification_next_step(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationCancelSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        result = continue_manual_verification_to_next_step(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '已进入认证流程下一步', 'data': result})


@login_checker
def company_verification_authorization_template(request, company_id, application_id):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'query', company_id)
        result = generate_authorization_template(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    response = HttpResponse(result.content, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{result.filename}"'
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Template-Version'] = result.template_version
    return response


@login_checker
def company_verification_documents(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    upload_file = request.FILES.get('file')
    if not upload_file:
        return JsonResponse(
            {'result': 0, 'code': 'document_invalid', 'message': '缺少授权书文件'},
            status=400,
        )
    serializer = ManualVerificationDocumentUploadSerializer(data=request.POST)
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'upload', company_id)
        result = upload_authorization_document(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            upload_file=upload_file,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    if result.get('adopted'):
        try:
            from apps.company.tasks import process_verification_document_task

            process_verification_document_task.delay(result['document_id'])
        except Exception:
            logger.exception(
                '[company-verification-document] queue_failed document_id=%s',
                result['document_id'],
            )
    return JsonResponse(
        {'result': 1, 'message': '授权书已安全接收，等待识别', 'data': result},
        status=202,
    )


@login_checker
def company_verification_business_license(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    upload_file = request.FILES.get('file')
    if not upload_file:
        return JsonResponse(
            {'result': 0, 'code': 'document_invalid', 'message': '缺少营业执照图片'},
            status=400,
        )
    serializer = ManualVerificationDocumentUploadSerializer(data=request.POST)
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'upload-license', company_id)
        result = upload_business_license_document(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            upload_file=upload_file,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    processing_error = None
    if result.get('adopted'):
        try:
            upload_file.seek(0)
            content = upload_file.read(
                max(int(getattr(
                    settings, 'COMPANY_VERIFICATION_MAX_FILE_BYTES', 5 * 1024 * 1024
                )), 1) + 1
            )
            current = result['application']
            current = run_business_license_ocr(
                company_id=company_id,
                application_id=str(application_id),
                user=user,
                expected_revision=current['revision'],
                idempotency_key=(
                    f"license:{serializer.validated_data['idempotency_key']}"
                )[:128],
                content=content,
            )
            if current.get('business_license_check') == 'passed':
                current = run_registry_verification(
                    company_id=company_id,
                    application_id=str(application_id),
                    user=user,
                    expected_revision=current['revision'],
                    idempotency_key=(
                        f"registry:{serializer.validated_data['idempotency_key']}"
                    )[:128],
                )
            result['application'] = current
        except VerificationDomainError as exc:
            processing_error = exc.code
            logger.warning(
                '[company-verification-license] automatic_check_incomplete '
                'application_id=%s error_code=%s',
                application_id,
                exc.code,
            )
            result['application'] = get_manual_verification(
                company_id=company_id,
                application_id=str(application_id),
                user=user,
            )
    if processing_error:
        result['processing_error'] = processing_error
    return JsonResponse(
        {
            'result': 1,
            'message': (
                '营业执照已安全接收，自动核验暂未完成'
                if processing_error else '营业执照已接收并完成自动核验'
            ),
            'data': result,
        },
        status=202,
    )


@login_checker
def company_verification_document_preview(request, company_id, application_id, document_id):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'query', company_id)
        content = get_authorization_preview(
            company_id=company_id,
            application_id=str(application_id),
            document_id=str(document_id),
            user=user,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    response = HttpResponse(content, content_type='image/png')
    response['Content-Disposition'] = 'inline; filename="authorization-preview.png"'
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@login_checker
def company_verification_submit(request, company_id, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = ManualVerificationSubmitSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors}, status=400
        )
    try:
        _rate_limit(user, 'submit', company_id)
        result = submit_manual_verification(
            company_id=company_id,
            application_id=str(application_id),
            user=user,
            **serializer.validated_data,
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '认证申请已提交审核', 'data': result})


@login_checker
def verification_reviewer_me(request):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'query', 'reviewer-me')
        result = get_reviewer_context(user=user)
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json({'result': 1, 'message': '审核资格查询成功', 'data': result})


@login_checker
def verification_reviewers(request):
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        if request.method == 'GET':
            serializer = VerificationReviewerListSerializer(data=request.GET)
            if not serializer.is_valid():
                return JsonResponse(
                    {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
                    status=400,
                )
            result = list_reviewers(actor=user, **serializer.validated_data)
        elif request.method == 'POST':
            serializer = VerificationReviewerGrantSerializer(data=_request_json(request))
            if not serializer.is_valid():
                return JsonResponse(
                    {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
                    status=400,
                )
            result = grant_or_update_reviewer(actor=user, **serializer.validated_data)
        else:
            return JsonResponse({'result': 0, 'message': '仅支持GET或POST请求'}, status=405)
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '审核资格操作成功', 'data': result})


@login_checker
def verification_reviewer_revoke(request, reviewer_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationReviewerRevokeSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors}, status=400
        )
    try:
        result = revoke_reviewer(
            actor=user, reviewer_id=reviewer_id, **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '审核资格已撤销', 'data': result})


@login_checker
def verification_review_queue(request):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationReviewQueueSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors}, status=400
        )
    try:
        _rate_limit(user, 'review', 'queue')
        result = list_review_queue(user=user, **serializer.validated_data)
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '审核队列查询成功', 'data': result})


@login_checker
def verification_review_companies(request):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationCompanyListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'review', 'companies')
        result = list_registered_companies(user=user, **serializer.validated_data)
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json(
        {'result': 1, 'message': '已注册企业查询成功', 'data': result}
    )


@login_checker
def verification_audit_events(request):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationAuditListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'review', 'audit-events')
        result = list_verification_audit_events(
            user=user, **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json(
        {'result': 1, 'message': '认证审计日志查询成功', 'data': result}
    )



@login_checker
def verification_review_application(request, application_id):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'review', str(application_id))
        result = get_review_application(user=user, application_id=str(application_id))
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '审核申请查询成功', 'data': result})


@login_checker
def verification_review_transcription(request, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationTranscriptionSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors}, status=400
        )
    try:
        _rate_limit(user, 'review', str(application_id))
        result = transcribe_authorization_document(
            user=user, application_id=str(application_id), **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '材料转录预审已完成', 'data': result})


@login_checker
def verification_review_decision(request, application_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationReviewDecisionSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors}, status=400
        )
    try:
        _rate_limit(user, 'review', str(application_id))
        result = decide_manual_verification(
            user=user, application_id=str(application_id), **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return JsonResponse({'result': 1, 'message': '认证审核决定已记录', 'data': result})


@login_checker
def verification_review_document_preview(request, application_id, document_id):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        _rate_limit(user, 'review', str(application_id))
        content = get_review_document_preview(
            user=user,
            application_id=str(application_id),
            document_id=str(document_id),
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    response = HttpResponse(content, content_type='image/png')
    response['Content-Disposition'] = 'inline; filename="authorization-review-preview.png"'
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['Pragma'] = 'no-cache'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@login_checker
def company_verification_suspend(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationSuspensionSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'suspend', company_id)
        result = suspend_company_verification(
            user=user, company_id=company_id, **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json(
        {'result': 1, 'message': '企业认证已暂停，既有 IAM 授权未变更', 'data': result}
    )


@login_checker
def company_verification_revoke(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationSuspensionSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'suspend', company_id)
        result = suspend_company_verification(
            user=user, company_id=company_id, **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json({
        'result': 1,
        'message': '企业认证已撤销；历史审核内容和既有 IAM 授权均已保留',
        'data': result,
    })


@login_checker
def company_service_suspend(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationSuspensionSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'suspend', company_id)
        result = suspend_company_service(
            user=user, company_id=company_id, **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json({
        'result': 1,
        'message': '企业服务已停用；认证历史和既有企业授权均已保留',
        'data': result,
    })


@login_checker
def company_verification_restore(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationSuspensionSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'suspend', company_id)
        result = restore_company_verification(
            user=user, company_id=company_id, **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json({
        'result': 1,
        'message': '企业认证已恢复；历史审核内容和既有企业授权均已保留',
        'data': result,
    })


@login_checker
def company_service_restore(request, company_id):
    method_error = _require_method(request, 'POST')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = VerificationSuspensionSerializer(data=_request_json(request))
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    try:
        _rate_limit(user, 'suspend', company_id)
        result = restore_company_service(
            user=user, company_id=company_id, **serializer.validated_data
        )
    except VerificationDomainError as exc:
        return _verification_error_response(exc)
    return _no_store_json({
        'result': 1,
        'message': '企业服务已恢复正常',
        'data': result,
    })



@login_checker
def verification_runtime_health(request):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    if not is_platform_administrator(user):
        return JsonResponse(
            {'result': 0, 'code': 'reviewer_management_forbidden', 'message': '权限不足'},
            status=403,
        )
    return _no_store_json(
        {'result': 1, 'message': '认证任务健康快照', 'data': verification_job_health()}
    )


@login_checker
def verification_retention_plan(request):
    method_error = _require_method(request, 'GET')
    if method_error:
        return method_error
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    if not is_platform_administrator(user):
        return JsonResponse(
            {'result': 0, 'code': 'reviewer_management_forbidden', 'message': '权限不足'},
            status=403,
        )
    serializer = VerificationRetentionPlanSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse(
            {'result': 0, 'code': 'invalid_request', 'message': serializer.errors},
            status=400,
        )
    result = plan_verification_document_retention(**serializer.validated_data)
    result["drafts"] = plan_company_draft_retention(
        limit=serializer.validated_data["limit"]
    )
    return _no_store_json(
        {'result': 1, 'message': '仅生成 dry-run 清理计划，未删除材料', 'data': result}
    )


@login_checker
def company_upload_image(request):
    """上传公司相关图片（logo/background/avatar）到 COS，返回 file_key 和 presigned url。"""
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
    if field not in {'logo', 'background_image', 'avatar'}:
        return JsonResponse({'result': 0, 'message': 'field 仅支持 logo/background_image/avatar'}, status=400)

    ext = os.path.splitext(upload_file.name or '')[1].lower()
    if not ext:
        ext = '.jpg'
    file_name = f'company_{field}_{uuid.uuid4().hex[:12]}{ext}'

    try:
        file_key = put_object(upload_file.read(), file_name)
        asset = ImageModerationService.register_uploaded_image(
            file_key,
            source=ImageModerationAsset.SOURCE_COMPANY_IMAGE,
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
                'avatar_url': file_url,
                'moderation': moderation_payload(asset),
            }
        })
    except Exception as e:
        logger.error(f"[company-upload-image] 上传失败 user={user.uid} ip={request.META.get('REMOTE_ADDR')} err={e}")
        return JsonResponse({'result': 0, 'message': '上传失败'}, status=500)

@login_checker
def company_gate_list(request):
    from apps.company.serializers import CompanyGateListSerializer
    serializer = CompanyGateListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    ok, data = CompanyService.list_company_programs(
        user=user,
        cid=serializer.validated_data['cid'],
        limit=serializer.validated_data['limit'],
        offset=serializer.validated_data['offset'],
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': data})


@login_checker
def company_gate_bind(request):
    from apps.company.serializers import CompanyBindProgramSerializer
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    serializer = CompanyBindProgramSerializer(data=json.loads(request.body.decode()))
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    ok, msg = CompanyService.bind_program(
        cid=serializer.validated_data['cid'],
        gid=serializer.validated_data['gid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR')
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg or '绑定失败'})
    return JsonResponse({'result': 1, 'message': '绑定成功'})


def search(request):
    from apps.company.serializers import CompanySearchSerializer
    s = CompanySearchSerializer(data=request.GET)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    data = CompanyService.search_companies(
        keyword=s.validated_data['keyword'],
        offset=s.validated_data['offset'],
        limit=s.validated_data['limit']
    )
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': data})


@login_checker
def company_gate_delete(request):
    from apps.company.serializers import CompanyGateDeleteSerializer
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    s = CompanyGateDeleteSerializer(data=json.loads(request.body.decode()))
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    ok, msg = CompanyService.delete_company_program(
        user=user,
        cid=s.validated_data['cid'],
        gid=s.validated_data['gid'],
        client_ip=request.META.get('REMOTE_ADDR')
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg or '删除失败'})
    return JsonResponse({'result': 1, 'message': '删除成功'})


@login_checker
def transparent_company_create(request):
    from apps.company.serializers import TransparentCompanyCreateSerializer
    s = TransparentCompanyCreateSerializer(data=json.loads(request.body.decode()))
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    ok, msg = CompanyService.create_transparent_company(
        cid=s.validated_data['cid'],
        avatar=s.validated_data['avatar'] or "",
        prompts=s.validated_data['prompts'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR')
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg or '创建失败'})
    return JsonResponse({'result': 1, 'message': '创建成功'})

@login_checker
def transparent_company_update(request):
    from apps.company.serializers import TransparentCompanyUpdateSerializer

    s = TransparentCompanyUpdateSerializer(data=json.loads(request.body.decode()))
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    ok, msg = CompanyService.update_transparent_company(
        cid=s.validated_data['cid'],
        avatar=s.validated_data['avatar'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': msg or '更新失败'})
    return JsonResponse({'result': 1, 'message': '更新成功'})

@login_checker
def transparent_company_delete(request):
    pass

@login_checker
def transparent_company_detail(request):
    """
    透明公司详情

    当前前端通过 GET /api/company/transparent/detail?company_id=<cid> 访问，
    这里兼容从查询参数读取 cid，而不是解析空的请求体。
    """
    from apps.company.serializers import TransparentCompanyDetailSerializer

    # 优先从查询参数读取，兼容 company_id / cid 两种命名
    cid = request.GET.get("cid")
    data = {"cid": cid} if cid else {}

    s = TransparentCompanyDetailSerializer(data=data)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, detail = CompanyService.get_transparent_company_detail(cid=s.validated_data['cid'])
    if not ok:
        return JsonResponse({'result': 0, 'message': detail})
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': detail})


def transparent_company_client_entry(request):
    """
    获取透明公司面向 C 端暴露的 Agent 入口信息。

    当前通过 GET /api/company/transparent/client-entry?cid=<cid> 访问，
    不要求登录，只返回已经发布给 C 端的 Agent 列表和默认入口。
    """
    from apps.company.serializers import TransparentCompanyClientEntrySerializer

    cid = request.GET.get("cid") or request.GET.get("company_id")
    data = {"cid": cid} if cid else {}

    s = TransparentCompanyClientEntrySerializer(data=data)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, detail = CompanyService.get_transparent_company_client_entry(
        cid=s.validated_data['cid']
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': detail})
    return JsonResponse({'result': 1, 'message': '查找成功', 'data': detail})

@login_checker
def transparent_company_client_entry_chat_stream(request):
    from apps.company.serializers import TransparentCompanyClientEntryChatStreamSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    serializer = TransparentCompanyClientEntryChatStreamSerializer(
        data=json.loads(request.body.decode() or "{}")
    )
    if not serializer.is_valid():
        return JsonResponse({"result": 0, "message": serializer.errors}, status=400)

    ok, stream_or_error = CompanyService.stream_transparent_company_client_entry_message(
        cid=serializer.validated_data["cid"],
        content=serializer.validated_data["content"],
        user=user,
        entry_key=serializer.validated_data.get("entry_key") or "",
        route_key=serializer.validated_data.get("route_key") or "",
        conversation_id=serializer.validated_data.get("conversation_id") or "",
        session_key=serializer.validated_data.get("session_key") or "",
        messages=serializer.validated_data.get("messages") or [],
    )
    if not ok:
        return JsonResponse({"result": 0, "message": stream_or_error}, status=403)

    def event_stream():
        yield ": connected\n\n"
        for item in stream_or_error:
            if not isinstance(item, dict):
                continue
            event_name = str(item.get("event") or "message")
            payload = item.get("data")
            if not isinstance(payload, dict):
                payload = {key: value for key, value in item.items() if key != "event"}
            yield _sse_encode(event_name, payload)

    response = StreamingHttpResponse(
        event_stream(),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


@login_checker
def transparent_company_history(request):
    pass

@login_checker
def transparent_company_history_list(request):
    pass


@login_checker
def transparent_company_client_entry_conversations_list(request):
    from apps.company.serializers import TransparentCompanyClientEntryConversationListSerializer

    serializer = TransparentCompanyClientEntryConversationListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.list_transparent_company_client_entry_conversations(
        cid=serializer.validated_data['cid'],
        user=user,
        agent_id=serializer.validated_data.get('agent_id', ''),
        limit=serializer.validated_data.get('limit', 20),
        offset=serializer.validated_data.get('offset', 0),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data}, status=403)
    return JsonResponse({'result': 1, 'message': '鏌ヨ鎴愬姛', 'data': data})


@login_checker
def transparent_company_client_entry_conversation_messages(request):
    from apps.company.serializers import TransparentCompanyClientEntryConversationMessagesSerializer

    serializer = TransparentCompanyClientEntryConversationMessagesSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.get_transparent_company_client_entry_messages(
        cid=serializer.validated_data['cid'],
        conversation_id=serializer.validated_data['conversation_id'],
        user=user,
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data}, status=403)
    return JsonResponse({'result': 1, 'message': '鏌ヨ鎴愬姛', 'data': data})


def transparent_company_public_agents_list(request):
    """
    Public read-only Agent Prompt list for the public company detail page.

    It exposes Agent and Prompt transparency data only. Chat still goes through
    the client-entry endpoint selected by the company configuration.
    """
    from apps.company.serializers import TransparentCompanyAgentListSerializer

    params = {
        'cid': request.GET.get('cid') or request.GET.get('company_id'),
    }
    raw_status = request.GET.get('status')
    if raw_status not in (None, ''):
        params['status'] = raw_status
    raw_enabled = request.GET.get('enabled')
    if raw_enabled not in (None, ''):
        val = str(raw_enabled).lower()
        if val in {'1', 'true', 'yes'}:
            params['enabled'] = True
        elif val in {'0', 'false', 'no'}:
            params['enabled'] = False

    s = TransparentCompanyAgentListSerializer(data=params)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors}, status=400)

    ok, agents = CompanyService.list_transparent_company_agents(
        cid=s.validated_data['cid'],
        status=s.validated_data.get('status'),
        enabled=s.validated_data.get('enabled'),
        public_only=True,
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': agents}, status=404)

    try:
        from apps.company.models import Company
        from apps.runtime.models import OpenClawConfigVersion

        company = Company.objects.get(cid=s.validated_data['cid'])
        evidence_by_agent = {}
        try:
            from common.immudbIO.audit_service import AgentPromptEvidenceService

            evidence_service = AgentPromptEvidenceService()
            for agent in agents:
                agent_id = str(agent.get('agentId') or agent.get('agent_id') or agent.get('id') or '').strip()
                if not agent_id:
                    continue
                evidence_ok, evidence_rows = evidence_service.list_prompt_versions(
                    cid=s.validated_data['cid'],
                    agent_id=agent_id,
                    limit=30,
                )
                if evidence_ok and evidence_rows:
                    evidence_by_agent[agent_id] = evidence_rows
        except Exception:
            logger.exception('[public-agents] read prompt evidence failed')

        versions = list(
            OpenClawConfigVersion.objects
            .filter(company=company)
            .order_by('-id')[:30]
        )

        def _payload_agents(version):
            try:
                payload = json.loads(version.config_payload or '{}')
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                return []
            result = []
            for key in ('agents', 'agent_configs', 'agentConfigs'):
                items = payload.get(key)
                if isinstance(items, list):
                    result.extend([item for item in items if isinstance(item, dict)])
            return result

        def _agent_id(item):
            return str(item.get('agentId') or item.get('agent_id') or item.get('id') or '').strip()

        def _agent_prompt(item):
            return str(
                item.get('systemPrompt')
                or item.get('system_prompt')
                or item.get('prompt')
                or ''
            ).strip()

        for agent in agents:
            agent_id = _agent_id(agent)
            current_prompt = _agent_prompt(agent) or '暂无 Prompt 配置。'
            prompt_versions_ok, prompt_versions_payload = CompanyService.get_transparent_company_agent_prompt_versions(
                agent_id=agent_id,
                cid=s.validated_data['cid'],
                user=None,
                limit=30,
            )
            if prompt_versions_ok and isinstance(prompt_versions_payload, dict):
                agent['promptHistory'] = [
                    {
                        'version': item.get('version') or '',
                        'updatedAt': item.get('created_at') or '',
                        'updatedBy': item.get('created_by') or 'platform',
                        'changeNote': item.get('change_reason') or '',
                        'changeType': item.get('change_type') or '',
                        'content': item.get('content') or '',
                        'diffSummary': item.get('diff_summary') or {},
                        'current': bool(item.get('is_current')),
                    }
                    for item in (prompt_versions_payload.get('versions') or [])
                    if isinstance(item, dict)
                ]
                if agent['promptHistory']:
                    continue
            evidence_rows = evidence_by_agent.get(agent_id) or []
            if evidence_rows:
                history = []
                for index, row in enumerate(evidence_rows):
                    history.append({
                        'version': row.get('runtime_version') or f'v{index + 1}',
                        'updatedAt': row.get('created_at') or '',
                        'updatedBy': row.get('operator_id') or '平台配置',
                        'changeNote': row.get('notes') or 'immudb Prompt 存证',
                        'content': row.get('prompt_content') or '',
                        'promptSha256': row.get('prompt_sha256') or '',
                        'evidenceId': row.get('evidence_id') or '',
                        'readbackDigest': row.get('readback_digest') or '',
                        'diffVerified': bool(row.get('diff_verified')),
                        'current': index == 0,
                    })
                agent['promptHistory'] = history
                continue

            seen = set()
            history = []
            for version in versions:
                matched = None
                for item in _payload_agents(version):
                    if _agent_id(item) == agent_id:
                        matched = item
                        break
                if not matched:
                    continue
                content = _agent_prompt(matched)
                if not content or content in seen:
                    continue
                seen.add(content)
                history.append({
                    'version': version.version or f'v{len(history) + 1}',
                    'updatedAt': version.published_at or version.updated_at or version.created_at or '',
                    'updatedBy': version.created_by or '平台配置',
                    'changeNote': version.notes or '运行配置发布版本',
                    'content': content,
                    'current': content == current_prompt,
                })

            if not history or history[0].get('content') != current_prompt:
                history.insert(0, {
                    'version': '当前草稿' if history else '当前版本',
                    'updatedAt': agent.get('updated_at') or agent.get('updatedAt') or '',
                    'updatedBy': '平台配置',
                    'changeNote': '当前 Agent Prompt',
                    'content': current_prompt,
                    'current': True,
                })

            for index, item in enumerate(history):
                item['current'] = index == 0
            agent['promptHistory'] = history
    except Exception:
        logger.exception('[public-agents] build prompt history failed')

    return JsonResponse({'result': 1, 'message': '查询成功', 'data': agents})


@login_checker
def transparent_company_is_transparent(request):
    """
    判断公司是否为透明公司

    前端通过 GET /api/company/transparent/is-transparent?cid=<cid> 调用。
    """
    from apps.company.serializers import TransparentCompanyDetailSerializer

    cid = request.GET.get("cid") or request.GET.get("company_id")
    data = {"cid": cid} if cid else {}

    s = TransparentCompanyDetailSerializer(data=data)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.is_transparent_company(cid=s.validated_data['cid'])
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({
        'result': 1,
        'message': '查询成功',
        'data': data,
    })


@login_checker
def transparent_company_skills_list(request):
    """
    获取透明公司 skills 列表

    支持可选过滤：
    - status: active / inactive / deprecated
    - enabled: true / false
    """
    from apps.company.serializers import (
        TransparentCompanySkillListSerializer,
    )

    # 兼容 GET 查询参数，仅在有值时才放入可选字段，避免传 null
    params = {
        'cid': request.GET.get('cid') or request.GET.get('company_id'),
    }

    raw_status = request.GET.get('status')
    if raw_status not in (None, ''):
        params['status'] = raw_status

    raw_source_type = request.GET.get('source_type')
    if raw_source_type not in (None, ''):
        params['source_type'] = raw_source_type

    raw_runtime_status = request.GET.get('runtime_status')
    if raw_runtime_status not in (None, ''):
        params['runtime_status'] = raw_runtime_status

    raw_enabled = request.GET.get('enabled')
    if raw_enabled not in (None, ''):
        val = str(raw_enabled).lower()
        if val in {'1', 'true', 'yes'}:
            params['enabled'] = True
        elif val in {'0', 'false', 'no'}:
            params['enabled'] = False

    s = TransparentCompanySkillListSerializer(data=params)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    cid = s.validated_data['cid']
    status = s.validated_data.get('status')
    source_type = s.validated_data.get('source_type')
    runtime_status = s.validated_data.get('runtime_status')
    enabled = s.validated_data.get('enabled')

    ok, skills = CompanyService.list_transparent_company_skills(
        cid=cid,
        status=status,
        source_type=source_type,
        runtime_status=runtime_status,
        enabled=enabled,
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': skills})

    return JsonResponse({
        'result': 1,
        'message': '查找成功',
        'data': skills,
    })


@login_checker
def transparent_company_skills_toggle_status(request):
    """
    切换透明公司某个 skill 的 active / inactive 状态

    请求体(JSON):
    - cid: 公司 cid
    - skill_id: 技能 ID (skillId)
    """
    from apps.company.serializers import TransparentCompanySkillToggleStatusSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    s = TransparentCompanySkillToggleStatusSerializer(data=payload)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.toggle_transparent_company_skill_status(
        cid=s.validated_data['cid'],
        skill_id=s.validated_data['skill_id'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({
        'result': 1,
        'message': '更新成功',
        'data': data,
    })


@login_checker
def transparent_company_skills_delete(request):
    """删除用户创建的技能及其智能体绑定。"""
    from apps.company.serializers import TransparentCompanySkillDeleteSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = TransparentCompanySkillDeleteSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.delete_transparent_company_skill(
        cid=serializer.validated_data['cid'],
        skill_id=serializer.validated_data['skill_id'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({
        'result': 1,
        'message': '删除成功',
        'data': data,
    })


@login_checker
def transparent_company_skills_upsert(request):
    from apps.company.serializers import TransparentCompanySkillUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    s = TransparentCompanySkillUpsertSerializer(data=payload)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.upsert_transparent_company_skill(
        cid=s.validated_data['cid'],
        user=user,
        skill_id=s.validated_data.get('skill_id'),
        name=s.validated_data['name'],
        version=s.validated_data.get('version', '1.0.0'),
        category=s.validated_data['category'],
        priority=s.validated_data.get('priority', 10),
        enabled=s.validated_data.get('enabled'),
        short_description=s.validated_data.get('short_description', ''),
        author=s.validated_data.get('author', ''),
        status=s.validated_data.get('status'),
        source_type=s.validated_data.get('source_type'),
        runtime_status=s.validated_data.get('runtime_status'),
        is_system_skill=s.validated_data.get('is_system_skill', False),
        content=s.validated_data.get('content', ''),
        usage=s.validated_data.get('usage', ''),
        instructions=s.validated_data.get('instructions', ''),
        shell_script=s.validated_data.get('shell_script', ''),
        shell_script_name=s.validated_data.get('shell_script_name', ''),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({'result': 1, 'message': '保存成功', 'data': data})

@login_checker
def transparent_company_agents_list(request):
    from apps.company.serializers import TransparentCompanyAgentListSerializer

    params = {
        'cid': request.GET.get('cid') or request.GET.get('company_id'),
    }
    raw_status = request.GET.get('status')
    if raw_status not in (None, ''):
        params['status'] = raw_status

    raw_source_type = request.GET.get('source_type')
    if raw_source_type not in (None, ''):
        params['source_type'] = raw_source_type

    raw_runtime_status = request.GET.get('runtime_status')
    if raw_runtime_status not in (None, ''):
        params['runtime_status'] = raw_runtime_status
    raw_visibility = request.GET.get('visibility')
    if raw_visibility not in (None, ''):
        params['visibility'] = raw_visibility
    raw_client_visibility = request.GET.get('client_visibility')
    if raw_client_visibility not in (None, ''):
        params['client_visibility'] = raw_client_visibility
    raw_exposed_to_client = request.GET.get('exposed_to_client')
    if raw_exposed_to_client not in (None, ''):
        val = str(raw_exposed_to_client).lower()
        if val in {'1', 'true', 'yes'}:
            params['exposed_to_client'] = True
        elif val in {'0', 'false', 'no'}:
            params['exposed_to_client'] = False
    raw_is_default_entry = request.GET.get('is_default_entry')
    if raw_is_default_entry not in (None, ''):
        val = str(raw_is_default_entry).lower()
        if val in {'1', 'true', 'yes'}:
            params['is_default_entry'] = True
        elif val in {'0', 'false', 'no'}:
            params['is_default_entry'] = False
    raw_enabled = request.GET.get('enabled')
    if raw_enabled not in (None, ''):
        val = str(raw_enabled).lower()
        if val in {'1', 'true', 'yes'}:
            params['enabled'] = True
        elif val in {'0', 'false', 'no'}:
            params['enabled'] = False

    s = TransparentCompanyAgentListSerializer(data=params)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    ok, agents = CompanyService.list_transparent_company_agents(
        cid=s.validated_data['cid'],
        status=s.validated_data.get('status'),
        visibility=s.validated_data.get('visibility'),
        client_visibility=s.validated_data.get('client_visibility'),
        exposed_to_client=s.validated_data.get('exposed_to_client'),
        is_default_entry=s.validated_data.get('is_default_entry'),
        enabled=s.validated_data.get('enabled'),
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': agents})

    return JsonResponse({'result': 1, 'message': '查询成功', 'data': agents})


@login_checker
def transparent_company_agents_upsert(request):
    from apps.company.serializers import TransparentCompanyAgentUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    s = TransparentCompanyAgentUpsertSerializer(data=payload)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.upsert_transparent_company_agent(
        cid=s.validated_data['cid'],
        user=user,
        agent_id=s.validated_data.get('agent_id'),
        name=s.validated_data['name'],
        role=s.validated_data.get('role'),
        description=s.validated_data.get('description', ''),
        system_prompt=s.validated_data.get('system_prompt', ''),
        accent_color=s.validated_data.get('accent_color'),
        visibility=s.validated_data.get('visibility'),
        client_visibility=s.validated_data.get('client_visibility'),
        exposed_to_client=s.validated_data.get('exposed_to_client', False),
        is_default_entry=s.validated_data.get('is_default_entry', False),
        route_key=s.validated_data.get('route_key', ''),
        exposure_order=s.validated_data.get('exposure_order'),
        enabled=s.validated_data.get('enabled'),
        is_core_agent=s.validated_data.get('is_core_agent', False),
        status=s.validated_data.get('status'),
        sort_order=s.validated_data.get('sort_order', 0),
        version_label=s.validated_data.get('version_label', ''),
        change_reason=s.validated_data.get('change_reason', ''),
        change_type=s.validated_data.get('change_type'),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


def transparent_company_agent_prompt_versions(request, agent_id):
    from apps.company.serializers import TransparentCompanyAgentPromptVersionListSerializer

    params = {
        'agent_id': agent_id,
        'cid': request.GET.get('cid') or request.GET.get('company_id') or '',
    }
    if request.GET.get('limit') not in (None, ''):
        params['limit'] = request.GET.get('limit')

    serializer = TransparentCompanyAgentPromptVersionListSerializer(data=params)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    user = _optional_user_from_request(request)
    ok, data = CompanyService.get_transparent_company_agent_prompt_versions(
        agent_id=serializer.validated_data['agent_id'],
        cid=serializer.validated_data.get('cid', ''),
        user=user,
        limit=serializer.validated_data.get('limit', 30),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data}, status=404)
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


@login_checker
def transparent_company_agents_reorder(request):
    from apps.company.serializers import TransparentCompanyAgentReorderSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': 'POST required'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': 'login required'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    s = TransparentCompanyAgentReorderSerializer(data=payload)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.reorder_transparent_company_agents(
        cid=s.validated_data['cid'],
        user=user,
        orders=s.validated_data.get('orders') or [],
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({'result': 1, 'message': 'saved', 'data': data})


@login_checker
def transparent_company_agents_toggle_status(request):
    from apps.company.serializers import TransparentCompanyAgentToggleStatusSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    s = TransparentCompanyAgentToggleStatusSerializer(data=payload)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.toggle_transparent_company_agent_status(
        cid=s.validated_data['cid'],
        agent_id=s.validated_data['agent_id'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def transparent_company_agent_skill_bindings_list(request):
    from apps.company.serializers import TransparentCompanyAgentSkillBindingListSerializer

    params = {
        'cid': request.GET.get('cid') or request.GET.get('company_id'),
    }
    raw_agent_id = request.GET.get('agent_id')
    if raw_agent_id not in (None, ''):
        params['agent_id'] = raw_agent_id
    raw_skill_id = request.GET.get('skill_id')
    if raw_skill_id not in (None, ''):
        params['skill_id'] = raw_skill_id
    raw_enabled = request.GET.get('enabled')
    if raw_enabled not in (None, ''):
        val = str(raw_enabled).lower()
        if val in {'1', 'true', 'yes'}:
            params['enabled'] = True
        elif val in {'0', 'false', 'no'}:
            params['enabled'] = False

    s = TransparentCompanyAgentSkillBindingListSerializer(data=params)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    ok, bindings = CompanyService.list_transparent_company_agent_skill_bindings(
        cid=s.validated_data['cid'],
        agent_id=s.validated_data.get('agent_id'),
        skill_id=s.validated_data.get('skill_id'),
        enabled=s.validated_data.get('enabled'),
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': bindings})

    return JsonResponse({'result': 1, 'message': '查询成功', 'data': bindings})


@login_checker
def transparent_company_agent_skill_bindings_upsert(request):
    from apps.company.serializers import TransparentCompanyAgentSkillBindingUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    s = TransparentCompanyAgentSkillBindingUpsertSerializer(data=payload)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.upsert_transparent_company_agent_skill_binding(
        cid=s.validated_data['cid'],
        agent_id=s.validated_data['agent_id'],
        skill_id=s.validated_data['skill_id'],
        user=user,
        enabled=s.validated_data.get('enabled'),
        sort_order=s.validated_data.get('sort_order', 0),
        note=s.validated_data.get('note', ''),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def transparent_company_agent_skill_bindings_toggle_status(request):
    from apps.company.serializers import TransparentCompanyAgentSkillBindingToggleStatusSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    s = TransparentCompanyAgentSkillBindingToggleStatusSerializer(data=payload)
    if not s.is_valid():
        return JsonResponse({'result': 0, 'message': s.errors})

    ok, data = CompanyService.toggle_transparent_company_agent_skill_binding(
        cid=s.validated_data['cid'],
        agent_id=s.validated_data['agent_id'],
        skill_id=s.validated_data['skill_id'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})

    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


def model_providers_list(request):
    from apps.company.serializers import ModelProviderListSerializer

    serializer = ModelProviderListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.list_model_providers(
        cid=serializer.validated_data.get('cid'),
        status=serializer.validated_data.get('status'),
        auth_mode=serializer.validated_data.get('auth_mode'),
        platform_managed=serializer.validated_data.get('platform_managed'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


def model_definitions_list(request):
    from apps.company.serializers import ModelDefinitionListSerializer

    serializer = ModelDefinitionListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.list_model_definitions(
        cid=serializer.validated_data.get('cid'),
        provider_id=serializer.validated_data.get('provider_id'),
        status=serializer.validated_data.get('status'),
        category=serializer.validated_data.get('category'),
        supports_tools=serializer.validated_data.get('supports_tools'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


def model_packages_list(request):
    from apps.company.serializers import ModelPackageListSerializer

    serializer = ModelPackageListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.list_model_packages(
        cid=serializer.validated_data.get('cid'),
        status=serializer.validated_data.get('status'),
        is_default=serializer.validated_data.get('is_default'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_model_policy_detail(request):
    from apps.company.serializers import CompanyModelPolicyDetailSerializer

    serializer = CompanyModelPolicyDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.get_company_model_policy(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_model_policy_upsert(request):
    from apps.company.serializers import CompanyModelPolicyUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = CompanyModelPolicyUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.upsert_company_model_policy(
        cid=serializer.validated_data['cid'],
        user=user,
        package_id=serializer.validated_data.get('package_id', ''),
        provider_id=serializer.validated_data.get('provider_id', ''),
        default_model_id=serializer.validated_data.get('default_model_id', ''),
        selected_credential_name=serializer.validated_data.get('selected_credential_name', ''),
        source_mode=serializer.validated_data.get('source_mode'),
        status=serializer.validated_data.get('status'),
        rate_limit_rpm=serializer.validated_data.get('rate_limit_rpm', 60),
        rate_limit_tpm=serializer.validated_data.get('rate_limit_tpm', 120000),
        fallback_to_platform=serializer.validated_data.get('fallback_to_platform', True),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def company_api_credentials_list(request):
    return JsonResponse({'result': 0, 'message': 'platform-managed model credentials only'}, status=403)


@login_checker
def company_api_credentials_upsert(request):
    return JsonResponse({'result': 0, 'message': 'platform-managed model credentials only'}, status=403)


@login_checker
def company_session_memory_policy_detail(request):
    from apps.company.serializers import CompanySessionMemoryPolicyDetailSerializer

    serializer = CompanySessionMemoryPolicyDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.get_company_session_memory_policy(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_session_memory_policy_upsert(request):
    from apps.company.serializers import CompanySessionMemoryPolicyUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = CompanySessionMemoryPolicyUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.upsert_company_session_memory_policy(
        cid=serializer.validated_data['cid'],
        user=user,
        mode=serializer.validated_data.get('mode'),
        status=serializer.validated_data.get('status'),
        enabled=serializer.validated_data.get('enabled', True),
        recent_message_window=serializer.validated_data.get('recent_message_window', 12),
        max_tokens=serializer.validated_data.get('max_tokens', 4000),
        summary_trigger_tokens=serializer.validated_data.get('summary_trigger_tokens', 2800),
        summary_max_tokens=serializer.validated_data.get('summary_max_tokens', 800),
        summary_style=serializer.validated_data.get('summary_style'),
        session_ttl_days=serializer.validated_data.get('session_ttl_days', 30),
        redact_pii=serializer.validated_data.get('redact_pii', False),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def transparent_company_agent_memory_bindings_list(request):
    from apps.company.serializers import TransparentCompanyAgentMemoryBindingListSerializer

    serializer = TransparentCompanyAgentMemoryBindingListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.list_transparent_company_agent_memory_bindings(
        cid=serializer.validated_data['cid'],
        user=user,
        agent_id=serializer.validated_data.get('agent_id'),
        enabled=_optional_query_bool(request, serializer, 'enabled'),
        status=serializer.validated_data.get('status'),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def transparent_company_agent_memory_bindings_upsert(request):
    from apps.company.serializers import TransparentCompanyAgentMemoryBindingUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = TransparentCompanyAgentMemoryBindingUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.upsert_transparent_company_agent_memory_binding(
        cid=serializer.validated_data['cid'],
        agent_id=serializer.validated_data['agent_id'],
        user=user,
        memory_scope=serializer.validated_data.get('memory_scope'),
        enabled=serializer.validated_data.get('enabled', False),
        inherit_company_policy=serializer.validated_data.get('inherit_company_policy', True),
        max_tokens_override=serializer.validated_data.get('max_tokens_override'),
        summary_style_override=serializer.validated_data.get('summary_style_override', ''),
        note=serializer.validated_data.get('note', ''),
        status=serializer.validated_data.get('status'),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def transparent_company_agent_memory_bindings_toggle_status(request):
    from apps.company.serializers import TransparentCompanyAgentMemoryBindingToggleStatusSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = TransparentCompanyAgentMemoryBindingToggleStatusSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.toggle_transparent_company_agent_memory_binding(
        cid=serializer.validated_data['cid'],
        agent_id=serializer.validated_data['agent_id'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def company_upgrade_to_transparent(request):
    """将普通公司升级为透明公司"""
    from apps.company.serializers import CompanyUpgradeToTransparentSerializer
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    
    try:
        request_payload = json.loads(request.body.decode('utf-8-sig') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'result': 0, 'message': 'request body must be a valid JSON object'}, status=400)

    serializer = CompanyUpgradeToTransparentSerializer(data=request_payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    runtime_request = {}

    ok, result = CompanyService.upgrade_to_transparent_company(
        cid=serializer.validated_data['cid'],
        philosophy=serializer.validated_data.get('philosophy', ''),
        avatar=serializer.validated_data.get('avatar', ''),
        runtime_request=runtime_request,
        user=user,
        client_ip=request.META.get('REMOTE_ADDR')
    )
    
    if not ok:
        return JsonResponse({'result': 0, 'message': result or '升级失败'})

    return JsonResponse({
        'result': 1,
        'message': '升级成功，运行时部署任务已提交',
        'data': result
    })

@login_checker
def transparent_company_permission_templates_list(request):
    from apps.company.serializers import TransparentCompanyPermissionTemplateListSerializer

    serializer = TransparentCompanyPermissionTemplateListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.list_transparent_company_permission_templates(
        cid=serializer.validated_data['cid'],
        user=user,
        status=serializer.validated_data.get('status'),
        enabled=_optional_query_bool(request, serializer, 'enabled'),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def transparent_company_permission_templates_upsert(request):
    from apps.company.serializers import TransparentCompanyPermissionTemplateUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = TransparentCompanyPermissionTemplateUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    ok, data = CompanyService.upsert_transparent_company_permission_template(
        cid=serializer.validated_data['cid'],
        user=user,
        template_id=serializer.validated_data.get('template_id', ''),
        name=serializer.validated_data['name'],
        description=serializer.validated_data.get('description', ''),
        agent_type=serializer.validated_data.get('agent_type', ''),
        skills_include=serializer.validated_data.get('skills_include'),
        skills_exclude=serializer.validated_data.get('skills_exclude'),
        knowledge_read=serializer.validated_data.get('knowledge_read'),
        knowledge_write=serializer.validated_data.get('knowledge_write'),
        knowledge_delete=serializer.validated_data.get('knowledge_delete'),
        knowledge_deny=serializer.validated_data.get('knowledge_deny'),
        knowledge_read_deny=serializer.validated_data.get('knowledge_read_deny'),
        knowledge_write_deny=serializer.validated_data.get('knowledge_write_deny'),
        knowledge_delete_deny=serializer.validated_data.get('knowledge_delete_deny'),
        knowledge_fields_provided={
            field_name
            for field_name in (
                'knowledge_read',
                'knowledge_write',
                'knowledge_delete',
                'knowledge_deny',
                'knowledge_read_deny',
                'knowledge_write_deny',
                'knowledge_delete_deny',
            )
            if field_name in serializer.validated_data
        },
        # Forward only validated memory overrides to the service layer.
        memory_read=serializer.validated_data.get('memory_read'),
        memory_write=serializer.validated_data.get('memory_write'),
        memory_update=serializer.validated_data.get('memory_update'),
        memory_delete=serializer.validated_data.get('memory_delete'),
        memory_fields_provided={
            field_name
            for field_name in ('memory_read', 'memory_write', 'memory_update', 'memory_delete')
            if field_name in payload
        },
        enabled=serializer.validated_data.get('enabled'),
        status=serializer.validated_data.get('status'),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def transparent_company_agent_permission_bindings_list(request):
    from apps.company.serializers import TransparentCompanyAgentPermissionBindingListSerializer

    serializer = TransparentCompanyAgentPermissionBindingListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.list_transparent_company_agent_permission_bindings(
        cid=serializer.validated_data['cid'],
        user=user,
        agent_id=serializer.validated_data.get('agent_id'),
        enabled=_optional_query_bool(request, serializer, 'enabled'),
        status=serializer.validated_data.get('status'),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def transparent_company_agent_permission_bindings_upsert(request):
    from apps.company.serializers import TransparentCompanyAgentPermissionBindingUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = TransparentCompanyAgentPermissionBindingUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    ok, data = CompanyService.upsert_transparent_company_agent_permission_binding(
        cid=serializer.validated_data['cid'],
        user=user,
        agent_id=serializer.validated_data['agent_id'],
        create_only=serializer.validated_data.get('create_only', False),
        template_id=serializer.validated_data.get('template_id', ''),
        skills_include=serializer.validated_data.get('skills_include'),
        skills_exclude=serializer.validated_data.get('skills_exclude'),
        knowledge_read=serializer.validated_data.get('knowledge_read'),
        knowledge_write=serializer.validated_data.get('knowledge_write'),
        knowledge_delete=serializer.validated_data.get('knowledge_delete'),
        knowledge_deny=serializer.validated_data.get('knowledge_deny'),
        knowledge_read_deny=serializer.validated_data.get('knowledge_read_deny'),
        knowledge_write_deny=serializer.validated_data.get('knowledge_write_deny'),
        knowledge_delete_deny=serializer.validated_data.get('knowledge_delete_deny'),
        knowledge_fields_provided={
            field_name
            for field_name in (
                'knowledge_read',
                'knowledge_write',
                'knowledge_delete',
                'knowledge_deny',
                'knowledge_read_deny',
                'knowledge_write_deny',
                'knowledge_delete_deny',
            )
            if field_name in serializer.validated_data
        },
        # Agent bindings use the same independent memory permission contract.
        memory_read=serializer.validated_data.get('memory_read'),
        memory_write=serializer.validated_data.get('memory_write'),
        memory_update=serializer.validated_data.get('memory_update'),
        memory_delete=serializer.validated_data.get('memory_delete'),
        memory_fields_provided={
            field_name
            for field_name in ('memory_read', 'memory_write', 'memory_update', 'memory_delete')
            if field_name in payload
        },
        enabled=serializer.validated_data.get('enabled'),
        status=serializer.validated_data.get('status'),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def transparent_company_agent_permission_bindings_toggle_status(request):
    from apps.company.serializers import TransparentCompanyAgentPermissionBindingToggleStatusSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = TransparentCompanyAgentPermissionBindingToggleStatusSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.toggle_transparent_company_agent_permission_binding(
        cid=serializer.validated_data['cid'],
        user=user,
        agent_id=serializer.validated_data['agent_id'],
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def company_permission_config_detail(request):
    from apps.company.serializers import CompanyPermissionConfigDetailSerializer

    serializer = CompanyPermissionConfigDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.get_company_permission_config(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_permission_workspace_tree(request):
    from apps.company.serializers import CompanyPermissionWorkspaceTreeSerializer

    serializer = CompanyPermissionWorkspaceTreeSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.list_company_permission_workspace_tree(
        cid=serializer.validated_data['cid'],
        user=user,
        path=serializer.validated_data.get('path', ''),
        recursive=serializer.validated_data.get('recursive', False),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        if isinstance(data, dict):
            return JsonResponse(
                {'result': 0, 'message': data.get('message') or '鏌ヨ澶辫触'},
                status=int(data.get('status') or 400),
            )
        return JsonResponse({'result': 0, 'message': data}, status=403)
    return JsonResponse({'result': 1, 'message': '鏌ヨ鎴愬姛', 'data': data})


@login_checker
def company_permission_config_upsert(request):
    from apps.company.serializers import CompanyPermissionConfigUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}

    serializer = CompanyPermissionConfigUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    ok, data = CompanyService.upsert_company_permission_config(
        cid=serializer.validated_data['cid'],
        user=user,
        config_json=serializer.validated_data.get('config_json'),
        enabled=serializer.validated_data.get('enabled', True),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def company_conversations_list(request):
    from apps.company.serializers import CompanyConversationListSerializer

    serializer = CompanyConversationListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.list_company_conversations(
        cid=serializer.validated_data['cid'],
        user=user,
        status=serializer.validated_data.get('status', ''),
        agent_id=serializer.validated_data.get('agent_id', ''),
        source=serializer.validated_data.get('source', 'all'),
        limit=serializer.validated_data.get('limit', 50),
        offset=serializer.validated_data.get('offset', 0),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_conversation_messages(request):
    from apps.company.serializers import CompanyConversationMessagesSerializer

    serializer = CompanyConversationMessagesSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.get_company_conversation_messages(
        cid=serializer.validated_data['cid'],
        conversation_id=serializer.validated_data['conversation_id'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_control_overview(request):
    from apps.company.serializers import CompanyControlOverviewSerializer

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    serializer = CompanyControlOverviewSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors}, status=400)

    ok, data = CompanyService.get_company_control_overview(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data}, status=403)
    return JsonResponse({'result': 1, 'message': '鏌ユ壘鎴愬姛', 'data': data})


@login_checker
def company_members_list(request):
    from apps.company.serializers import CompanyMembersListSerializer

    serializer = CompanyMembersListSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})

    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)

    ok, data = CompanyService.list_company_members(
        cid=serializer.validated_data['cid'],
        user=user,
        status=serializer.validated_data.get('status', ''),
        role=serializer.validated_data.get('role', ''),
        keyword=serializer.validated_data.get('keyword', ''),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_member_upsert(request):
    from apps.company.serializers import CompanyMemberUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}
    serializer = CompanyMemberUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    ok, data = CompanyService.upsert_company_member(
        cid=serializer.validated_data['cid'],
        user=user,
        member_id=serializer.validated_data.get('member_id'),
        member_uid=serializer.validated_data.get('member_uid', ''),
        name=serializer.validated_data['name'],
        email=serializer.validated_data.get('email', ''),
        role=serializer.validated_data['role'],
        status=serializer.validated_data.get('status'),
        last_active_at=serializer.validated_data.get('last_active_at', ''),
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def company_member_add_by_public_id(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'result': 0, 'message': '请求体必须是 JSON 对象'}, status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get('cid'), str) or not isinstance(payload.get('public_id'), str):
        return JsonResponse({'result': 0, 'message': '请填写企业和用户 ID'}, status=400)
    public_id = payload['public_id'].strip().upper()
    if len(public_id) != 13:
        return JsonResponse({'result': 0, 'message': '用户 ID 格式不正确'}, status=400)
    ok, data = CompanyService.add_company_member_by_public_id(
        cid=payload['cid'], user=user, public_id=public_id,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data}, status=400)
    return JsonResponse({'result': 1, 'message': '员工已加入企业', 'data': data})


@login_checker
def company_member_add_by_phone(request):
    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持 POST'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'result': 0, 'message': '请求体必须是 JSON 对象'}, status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get('cid'), str) or not isinstance(payload.get('phone'), str):
        return JsonResponse({'result': 0, 'message': '请填写企业和手机号'}, status=400)
    phone = payload['phone'].strip()
    if not phone.isdigit() or not 6 <= len(phone) <= 20:
        return JsonResponse({'result': 0, 'message': '手机号格式不正确'}, status=400)
    ok, data = CompanyService.add_company_member_by_phone(
        cid=payload['cid'], user=user, phone=phone,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data}, status=400)
    return JsonResponse({'result': 1, 'message': '员工已加入企业', 'data': data})


@login_checker
def company_member_toggle_status(request):
    from apps.company.serializers import CompanyMemberToggleStatusSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}
    serializer = CompanyMemberToggleStatusSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    ok, data = CompanyService.toggle_company_member_status(
        cid=serializer.validated_data['cid'],
        user=user,
        member_id=serializer.validated_data['member_id'],
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})


@login_checker
def company_system_setting_detail(request):
    from apps.company.serializers import CompanySystemSettingDetailSerializer

    serializer = CompanySystemSettingDetailSerializer(data=request.GET)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    ok, data = CompanyService.get_company_system_setting(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '查询成功', 'data': data})


@login_checker
def company_system_setting_upsert(request):
    from apps.company.serializers import CompanySystemSettingUpsertSerializer

    if request.method != 'POST':
        return JsonResponse({'result': 0, 'message': '仅支持POST请求'}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({'result': 0, 'message': '请先登录'}, status=401)
    try:
        payload = json.loads(request.body.decode() or '{}')
    except Exception:
        payload = {}
    serializer = CompanySystemSettingUpsertSerializer(data=payload)
    if not serializer.is_valid():
        return JsonResponse({'result': 0, 'message': serializer.errors})
    ok, data = CompanyService.upsert_company_system_setting(
        cid=serializer.validated_data['cid'],
        user=user,
        client_ip=request.META.get('REMOTE_ADDR'),
        **{k: v for k, v in serializer.validated_data.items() if k != 'cid'},
    )
    if not ok:
        return JsonResponse({'result': 0, 'message': data})
    return JsonResponse({'result': 1, 'message': '更新成功', 'data': data})
