from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    TencentEssOperatorIdentity,
)
from apps.company.providers import get_enterprise_verification_provider
from apps.company.providers.base import (
    ConsoleUrlInput,
    EnterpriseVerificationProvider,
    OrganizationResult,
    VerificationProviderError,
)
from apps.company.validators import normalize_social_credit_code, validate_social_credit_code
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import grant_permissions
from apps.user.models import User


logger = logging.getLogger('touming')

ACTIVE_VERIFICATION_STATUSES = (
    CompanyVerification.STATUS_INITIATING,
    CompanyVerification.STATUS_PENDING,
)


class VerificationFlowError(Exception):
    MESSAGES = {
        'company_not_found': '公司不存在',
        'not_draft_owner': '只有企业草稿创建者可以执行认证',
        'already_verified': '企业已完成认证',
        'verification_in_progress': '企业认证正在进行中',
        'company_identity_incomplete': '请先完善企业名称和统一社会信用代码',
        'provider_unavailable': '企业认证服务暂时不可用，请稍后重试',
        'provider_identity_mismatch': '企业认证主体信息不一致，请联系平台处理',
        'verification_expired': '企业认证已失效，请重新发起',
        'verification_not_found': '企业认证记录不存在',
        'refresh_rate_limited': '状态刷新过于频繁，请稍后重试',
    }

    def __init__(self, code: str, *, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(self.MESSAGES.get(code, '企业认证操作失败'))


@dataclass(frozen=True)
class VerificationStartResult:
    verification_id: int
    status: str
    verification_url: str

    def as_dict(self):
        return {
            'verification_id': str(self.verification_id),
            'status': self.status,
            'verification_url': self.verification_url,
        }


def serialize_verification(verification: CompanyVerification) -> dict:
    return {
        'status': verification.status,
        'provider': verification.provider,
        'started_at': verification.started_at.isoformat() if verification.started_at else None,
        'finished_at': verification.finished_at.isoformat() if verification.finished_at else None,
        'failure_reason': verification.failure_reason or None,
    }


def _provider_or_error(
    provider: Optional[EnterpriseVerificationProvider],
) -> EnterpriseVerificationProvider:
    if provider is not None:
        return provider
    try:
        return get_enterprise_verification_provider(
            CompanyVerification.PROVIDER_TENCENT_ESS
        )
    except VerificationProviderError as exc:
        logger.error(
            '[company-verification-provider-config] provider_error_code=%s',
            exc.code,
        )
        raise VerificationFlowError('provider_unavailable', status_code=503) from exc


def _require_draft_owner(company: Company, user: User):
    if company.verification_status == Company.VERIFICATION_STATUS_VERIFIED:
        raise VerificationFlowError('already_verified', status_code=409)
    if company.account_id != user.id:
        raise VerificationFlowError('not_draft_owner', status_code=403)


def _validate_company_identity(company: Company):
    if (
        not str(company.company_name or '').strip()
        or not str(company.registration_no or '').strip()
    ):
        raise VerificationFlowError('company_identity_incomplete')
    try:
        canonical = validate_social_credit_code(company.registration_no, allow_blank=False)
    except ValidationError as exc:
        raise VerificationFlowError('company_identity_incomplete') from exc
    if canonical != company.registration_no:
        raise VerificationFlowError('company_identity_incomplete')


def _ensure_company_open_id(company: Company) -> str:
    if company.proxy_organization_open_id:
        return company.proxy_organization_open_id

    for _ in range(5):
        candidate = f'org_{uuid.uuid4().hex}'
        company.proxy_organization_open_id = candidate
        try:
            with transaction.atomic():
                company.save(update_fields=['proxy_organization_open_id'])
            return candidate
        except IntegrityError:
            company.proxy_organization_open_id = None
    raise VerificationFlowError('provider_unavailable', status_code=503)


def _stable_operator_open_id(user: User, application_id: str) -> str:
    User.objects.select_for_update().get(pk=user.pk)
    identity = TencentEssOperatorIdentity.objects.filter(
        user=user,
        application_id=application_id,
    ).first()
    if identity:
        return identity.proxy_operator_open_id

    for _ in range(5):
        candidate = f'operator_{uuid.uuid4().hex}'
        try:
            with transaction.atomic():
                identity = TencentEssOperatorIdentity.objects.create(
                    user=user,
                    application_id=application_id,
                    proxy_operator_open_id=candidate,
                )
            return identity.proxy_operator_open_id
        except IntegrityError:
            identity = TencentEssOperatorIdentity.objects.filter(
                user=user,
                application_id=application_id,
            ).first()
            if identity:
                return identity.proxy_operator_open_id
    raise VerificationFlowError('provider_unavailable', status_code=503)


def _console_input(company: Company, verification: CompanyVerification) -> ConsoleUrlInput:
    return ConsoleUrlInput(
        proxy_organization_open_id=verification.proxy_organization_open_id,
        proxy_operator_open_id=verification.proxy_operator_open_id,
        organization_name=company.company_name,
        unified_social_credit_code=company.registration_no,
    )


def _log_provider_failure(
    action: str,
    company: Company,
    verification: CompanyVerification,
    provider: EnterpriseVerificationProvider,
    exc,
):
    logger.warning(
        '[company-verification-provider] action=%s company_id=%s verification_id=%s '
        'application_id=%s request_id=%s provider_error_code=%s',
        action,
        company.cid,
        verification.id,
        provider.application_id,
        getattr(exc, 'request_id', ''),
        getattr(exc, 'code', type(exc).__name__),
    )


def start_company_verification(
    *,
    company_id: str,
    user: User,
    provider: Optional[EnterpriseVerificationProvider] = None,
) -> VerificationStartResult:
    provider = _provider_or_error(provider)
    if not provider.application_id or len(provider.application_id) > 64:
        raise VerificationFlowError('provider_unavailable', status_code=503)

    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationFlowError('company_not_found', status_code=404)
        _require_draft_owner(company, user)
        _validate_company_identity(company)
        if CompanyVerification.objects.filter(
            company=company,
            status__in=ACTIVE_VERIFICATION_STATUSES,
        ).exists():
            raise VerificationFlowError('verification_in_progress', status_code=409)

        organization_open_id = _ensure_company_open_id(company)
        if len(organization_open_id) > 64:
            raise VerificationFlowError('provider_unavailable', status_code=503)
        operator_open_id = _stable_operator_open_id(user, provider.application_id)
        verification = CompanyVerification.objects.create(
            company=company,
            applicant_user=user,
            provider=CompanyVerification.PROVIDER_TENCENT_ESS,
            application_id=provider.application_id,
            proxy_organization_open_id=organization_open_id,
            proxy_operator_open_id=operator_open_id,
            status=CompanyVerification.STATUS_INITIATING,
        )

    try:
        console_result = provider.create_console_url(_console_input(company, verification))
    except VerificationProviderError as exc:
        _log_provider_failure('start', company, verification, provider, exc)
        now = timezone.now()
        CompanyVerification.objects.filter(
            pk=verification.pk,
            status=CompanyVerification.STATUS_INITIATING,
        ).update(
            status=CompanyVerification.STATUS_REJECTED,
            failure_reason='start_failed',
            request_id=exc.request_id or None,
            finished_at=now,
        )
        raise VerificationFlowError('provider_unavailable', status_code=503) from exc

    with transaction.atomic():
        locked = CompanyVerification.objects.select_for_update().get(pk=verification.pk)
        if locked.status != CompanyVerification.STATUS_INITIATING:
            raise VerificationFlowError('verification_expired', status_code=409)
        locked.status = CompanyVerification.STATUS_PENDING
        locked.request_id = console_result.request_id or None
        locked.failure_reason = ''
        locked.save(update_fields=['status', 'request_id', 'failure_reason'])

    logger.info(
        '[company-verification-transition] company_id=%s verification_id=%s '
        'application_id=%s request_id=%s old_status=initiating new_status=pending',
        company.cid,
        verification.id,
        provider.application_id,
        console_result.request_id,
    )
    return VerificationStartResult(
        verification_id=verification.id,
        status=CompanyVerification.STATUS_PENDING,
        verification_url=console_result.verification_url,
    )


def continue_company_verification(
    *,
    company_id: str,
    user: User,
    provider: Optional[EnterpriseVerificationProvider] = None,
) -> VerificationStartResult:
    provider = _provider_or_error(provider)
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationFlowError('company_not_found', status_code=404)
        _require_draft_owner(company, user)
        _validate_company_identity(company)
        verification = CompanyVerification.objects.select_for_update().filter(
            company=company,
            provider=CompanyVerification.PROVIDER_TENCENT_ESS,
            status=CompanyVerification.STATUS_PENDING,
        ).order_by('-id').first()
        if not verification:
            raise VerificationFlowError('verification_expired', status_code=409)
        if verification.application_id != provider.application_id:
            raise VerificationFlowError('provider_unavailable', status_code=503)
        if verification.proxy_organization_open_id != company.proxy_organization_open_id:
            raise VerificationFlowError('provider_identity_mismatch', status_code=409)

    try:
        console_result = provider.create_console_url(_console_input(company, verification))
    except VerificationProviderError as exc:
        _log_provider_failure('continue', company, verification, provider, exc)
        raise VerificationFlowError('provider_unavailable', status_code=503) from exc

    updated = CompanyVerification.objects.filter(
        pk=verification.pk,
        status=CompanyVerification.STATUS_PENDING,
    ).update(request_id=console_result.request_id or None)
    if not updated:
        raise VerificationFlowError('verification_expired', status_code=409)
    return VerificationStartResult(
        verification_id=verification.id,
        status=CompanyVerification.STATUS_PENDING,
        verification_url=console_result.verification_url,
    )


def _find_verification(company: Company) -> Optional[CompanyVerification]:
    return CompanyVerification.objects.filter(company=company).order_by('-id').first()


def get_company_verification(*, company_id: str, user: User) -> dict:
    company = Company.objects.filter(cid=company_id).first()
    if not company:
        raise VerificationFlowError('company_not_found', status_code=404)
    verification = _find_verification(company)
    if company.account_id != user.id and (
        not verification or verification.applicant_user_id != user.id
    ):
        raise VerificationFlowError('not_draft_owner', status_code=403)
    if not verification:
        raise VerificationFlowError('verification_not_found', status_code=404)
    return serialize_verification(verification)


def enforce_refresh_rate_limit(company_id: str, user_id: int):
    seconds = max(int(getattr(settings, 'TENCENT_ESS_REFRESH_RATE_LIMIT_SECONDS', 10)), 0)
    if not seconds:
        return
    key = f'company-verification-refresh:{company_id}:{user_id}'
    if not cache.add(key, '1', timeout=seconds):
        raise VerificationFlowError('refresh_rate_limited', status_code=429)


def _identity_matches(company: Company, result: OrganizationResult) -> bool:
    return (
        result.organization_open_id == company.proxy_organization_open_id
        and str(result.organization_name or '').strip()
        == str(company.company_name or '').strip()
        and normalize_social_credit_code(result.unified_social_credit_code)
        == company.registration_no
    )


def refresh_company_verification(
    *,
    company: Company,
    verification: CompanyVerification,
    provider: Optional[EnterpriseVerificationProvider] = None,
) -> dict:
    provider = _provider_or_error(provider)
    if verification.provider != CompanyVerification.PROVIDER_TENCENT_ESS:
        raise VerificationFlowError('provider_unavailable', status_code=503)
    if verification.status == CompanyVerification.STATUS_VERIFIED:
        return serialize_verification(verification)
    if verification.status != CompanyVerification.STATUS_PENDING:
        raise VerificationFlowError('verification_expired', status_code=409)
    if verification.application_id != provider.application_id:
        raise VerificationFlowError('provider_unavailable', status_code=503)
    if verification.proxy_organization_open_id != company.proxy_organization_open_id:
        raise VerificationFlowError('provider_identity_mismatch', status_code=409)

    try:
        organization = provider.describe_organization(
            proxy_organization_open_id=company.proxy_organization_open_id or '',
            proxy_operator_open_id=verification.proxy_operator_open_id,
        )
    except VerificationProviderError as exc:
        _log_provider_failure('refresh', company, verification, provider, exc)
        raise VerificationFlowError('provider_unavailable', status_code=503) from exc

    now = timezone.now()
    if organization.provider_status != 'VERIFIED':
        CompanyVerification.objects.filter(
            pk=verification.pk,
            status=CompanyVerification.STATUS_PENDING,
        ).update(
            provider_status=organization.provider_status,
            request_id=organization.request_id or None,
            last_refreshed_at=now,
        )
        verification.refresh_from_db()
        return serialize_verification(verification)

    identity_mismatch = False
    with transaction.atomic():
        locked_company = Company.objects.select_for_update().get(pk=company.pk)
        locked_verification = CompanyVerification.objects.select_for_update().get(
            pk=verification.pk
        )
        if locked_verification.status == CompanyVerification.STATUS_VERIFIED:
            return serialize_verification(locked_verification)
        if locked_verification.status != CompanyVerification.STATUS_PENDING:
            raise VerificationFlowError('verification_expired', status_code=409)
        if not _identity_matches(locked_company, organization):
            locked_verification.provider_status = organization.provider_status
            locked_verification.request_id = organization.request_id or None
            locked_verification.last_refreshed_at = now
            locked_verification.failure_reason = 'provider_identity_mismatch'
            locked_verification.save(
                update_fields=[
                    'provider_status',
                    'request_id',
                    'last_refreshed_at',
                    'failure_reason',
                ]
            )
            identity_mismatch = True
        else:
            if locked_verification.applicant_user_id is None:
                raise VerificationFlowError('provider_unavailable', status_code=503)
            locked_verification.status = CompanyVerification.STATUS_VERIFIED
            locked_verification.provider_status = organization.provider_status
            locked_verification.request_id = organization.request_id or None
            locked_verification.last_refreshed_at = now
            locked_verification.finished_at = now
            locked_verification.failure_reason = ''
            locked_verification.save(
                update_fields=[
                    'status',
                    'provider_status',
                    'request_id',
                    'last_refreshed_at',
                    'finished_at',
                    'failure_reason',
                ]
            )
            locked_company.verification_status = Company.VERIFICATION_STATUS_VERIFIED
            locked_company.verification_provider = Company.VERIFICATION_PROVIDER_TENCENT_ESS
            locked_company.verified_at = now
            locked_company.verified_verification = locked_verification
            locked_company.save(
                update_fields=[
                    'verification_status',
                    'verification_provider',
                    'verified_at',
                    'verified_verification',
                ]
            )
            applicant = locked_verification.applicant_user
            principal, _ = Principal.objects.get_or_create(
                type=Principal.TYPE_USER, external_id=applicant.uid,
            )
            resource, _ = Resource.objects.get_or_create(
                type=Resource.TYPE_COMPANY, external_id=locked_company.cid,
            )
            grant_permissions(
                principal=principal, resource=resource,
                permissions=Grant.PERM_SUPER_ADMIN,
                granter_user=applicant,
                inherit=True, expires_at=None,
            )

    if identity_mismatch:
        logger.error(
            '[company-verification-identity-mismatch] company_id=%s verification_id=%s '
            'application_id=%s request_id=%s',
            company.cid,
            verification.id,
            provider.application_id,
            organization.request_id,
        )
        raise VerificationFlowError('provider_identity_mismatch', status_code=409)

    logger.info(
        '[company-verification-transition] company_id=%s verification_id=%s '
        'application_id=%s request_id=%s old_status=pending new_status=verified',
        company.cid,
        verification.id,
        provider.application_id,
        organization.request_id,
    )
    locked_verification.refresh_from_db()
    return serialize_verification(locked_verification)


def refresh_company_verification_for_user(
    *,
    company_id: str,
    user: User,
    provider: Optional[EnterpriseVerificationProvider] = None,
) -> dict:
    company = Company.objects.filter(cid=company_id).first()
    if not company:
        raise VerificationFlowError('company_not_found', status_code=404)
    verification = _find_verification(company)
    if company.account_id != user.id and (
        not verification or verification.applicant_user_id != user.id
    ):
        raise VerificationFlowError('not_draft_owner', status_code=403)
    if not verification:
        raise VerificationFlowError('verification_not_found', status_code=404)
    return refresh_company_verification(
        company=company,
        verification=verification,
        provider=provider,
    )


def expire_stale_initiating_verifications(*, limit: int = 100) -> int:
    timeout_seconds = max(
        int(getattr(settings, 'TENCENT_ESS_INITIATING_TIMEOUT_SECONDS', 900)),
        1,
    )
    cutoff = timezone.now() - timedelta(seconds=timeout_seconds)
    ids = list(
        CompanyVerification.objects.filter(
            provider=CompanyVerification.PROVIDER_TENCENT_ESS,
            status=CompanyVerification.STATUS_INITIATING,
            started_at__lt=cutoff,
        )
        .order_by('id')
        .values_list('id', flat=True)[:limit]
    )
    if not ids:
        return 0
    return CompanyVerification.objects.filter(
        id__in=ids,
        provider=CompanyVerification.PROVIDER_TENCENT_ESS,
        status=CompanyVerification.STATUS_INITIATING,
    ).update(
        status=CompanyVerification.STATUS_EXPIRED,
        failure_reason='initiation_timeout',
        finished_at=timezone.now(),
    )


def pending_verifications_for_reconciliation(*, limit: int = 50):
    interval_seconds = max(
        int(getattr(settings, 'TENCENT_ESS_PENDING_RECONCILE_INTERVAL_SECONDS', 900)),
        1,
    )
    cutoff = timezone.now() - timedelta(seconds=interval_seconds)
    return list(
        CompanyVerification.objects.filter(
            provider=CompanyVerification.PROVIDER_TENCENT_ESS,
            status=CompanyVerification.STATUS_PENDING,
        ).filter(
            models.Q(last_refreshed_at__isnull=True)
            | models.Q(last_refreshed_at__lt=cutoff)
        ).select_related('company').order_by('id')[:limit]
    )
