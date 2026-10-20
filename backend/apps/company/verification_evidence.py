"""EV-3 registry and FaceID orchestration; provider I/O is always outside locks."""

from __future__ import annotations

import hmac
import json
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationFaceSession,
    VerificationOperation,
)
from apps.company.providers import get_applicant_identity_verifier, get_registry_verifier
from apps.company.providers.base import (
    VerificationProviderError,
    VerificationProviderProtocolError,
)
from apps.company.providers.manual import (
    ApplicantIdentityQueryRequest,
    ApplicantIdentityStartRequest,
    RegistryVerificationRequest,
)
from apps.company.verification_crypto import VerificationCryptoError, get_identity_protector
from apps.company.verification_domain import (
    ACTIVE_STATUSES,
    VerificationDomainError,
    _configuration,
    _identity_precheck_is_current,
    _request_hash,
    serialize_manual_verification,
)


logger = logging.getLogger("touming")


def _setting_int(name: str, default: int, minimum: int = 1) -> int:
    return max(int(getattr(settings, name, default)), minimum)


def _clean_code(value, maximum=64) -> str:
    return str(value or "").strip()[:maximum]


def _normalize_person_name(value: str) -> str:
    return "".join(str(value or "").split())


def _verified_legal_representative(application, *, now) -> str:
    document = application.documents.filter(
        document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()
    if not document:
        return ""
    check = application.checks.filter(
        kind=VerificationCheck.KIND_BUSINESS_LICENSE,
        document=document,
        status=VerificationCheck.STATUS_PASSED,
        valid_until__gt=now,
    ).order_by("-checked_at", "-created_at").first()
    if not check or (check.result or {}).get("adopted") is not True:
        return ""
    return str((check.result or {}).get("legal_representative") or "").strip()


def _has_valid_authorization(application, *, now) -> bool:
    document = application.documents.filter(
        document_type=VerificationDocument.TYPE_AUTHORIZATION,
        material_revision=application.material_revision,
        scan_status=VerificationDocument.SCAN_PASSED,
    ).order_by("-uploaded_at", "-id").first()
    if not document:
        return False
    check = application.checks.filter(
        kind=VerificationCheck.KIND_CONSISTENCY,
        document=document,
        material_revision=application.material_revision,
        status=VerificationCheck.STATUS_PASSED,
        valid_until__gt=now,
    ).order_by("-checked_at", "-created_at").first()
    return bool(check and isinstance(check.result, dict) and check.result.get("adopted") is True)


def _require_key(value: str) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    return key


_FACE_CONFIGURATION_ERROR_CODES = {
    "InvalidParameterValue.RuleIdNotExist",
    "InvalidParameterValue.RuleIdDisabled",
    "UnauthorizedOperation.Nonactivated",
    "UnauthorizedOperation.ActivateError",
    "UnauthorizedOperation.Activating",
}


def _face_public_error(code: str) -> str:
    if code == "InvalidParameterValue.RuleIdNotExist":
        return "face_rule_not_found"
    if code == "InvalidParameterValue.RuleIdDisabled":
        return "face_rule_disabled"
    if code in {
        "UnauthorizedOperation.Nonactivated",
        "UnauthorizedOperation.ActivateError",
        "UnauthorizedOperation.Activating",
    }:
        return "face_service_not_activated"
    return "provider_unavailable"


def _lock_application(*, company_id: str, application_id: str, user):
    company = Company.objects.select_for_update().filter(cid=company_id).first()
    if not company:
        raise VerificationDomainError("company_not_found", status_code=404)
    application = CompanyVerification.objects.select_for_update().filter(
        company=company, public_id=application_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).first()
    if not application:
        raise VerificationDomainError("application_not_found", status_code=404)
    if not user or application.applicant_user_id != user.id:
        raise VerificationDomainError("application_forbidden", status_code=403)
    return company, application


def _require_current(application, *, expected_revision: int, stage: str):
    if application.status not in ACTIVE_STATUSES or application.stage != stage:
        raise VerificationDomainError("transition_not_allowed", status_code=409)
    if application.revision != int(expected_revision):
        raise VerificationDomainError("stale_revision", status_code=409)
    if not application.expires_at or application.expires_at <= timezone.now():
        raise VerificationDomainError("transition_not_allowed", status_code=409)


def enforce_evidence_rate_limit(company_id: str, user_id: int, action: str):
    seconds = max(int(getattr(
        settings, "COMPANY_VERIFICATION_EVIDENCE_REFRESH_RATE_LIMIT_SECONDS", 10
    )), 0)
    if seconds and not cache.add(
        f"company-verification-evidence:{action}:{company_id}:{user_id}", "1", timeout=seconds
    ):
        raise VerificationDomainError("operation_in_progress", status_code=429)


def _claim_operation(
    *, company, application, kind, key, request_hash, now, face_attempt_no=None, check=None
):
    operation = VerificationOperation.objects.select_for_update().filter(
        company=company, operation_kind=kind, idempotency_key=key
    ).first()
    if operation:
        if operation.request_hash != request_hash:
            raise VerificationDomainError("idempotency_conflict", status_code=409)
        if operation.status == VerificationOperation.STATUS_COMPLETED:
            return operation, False, None
        if (
            operation.status == VerificationOperation.STATUS_RUNNING
            and operation.lease_until and operation.lease_until > now
        ):
            raise VerificationDomainError("operation_in_progress", status_code=409)
        maximum = _setting_int("COMPANY_VERIFICATION_OPERATION_MAX_ATTEMPTS", 3)
        if operation.attempts >= maximum:
            raise VerificationDomainError("provider_unavailable", status_code=503)
        if operation.next_run_at and operation.next_run_at > now:
            raise VerificationDomainError("operation_in_progress", status_code=409)
        operation.status = VerificationOperation.STATUS_RUNNING
        operation.attempts += 1
        operation.lease_until = now + timedelta(seconds=_setting_int(
            "COMPANY_VERIFICATION_OPERATION_LEASE_SECONDS", 60
        ))
        operation.next_run_at = None
        operation.last_error_code = ""
        operation.save(update_fields=(
            "status", "attempts", "lease_until", "next_run_at", "last_error_code", "updated_at"
        ))
        check_id = operation.result.get("check_id") if isinstance(operation.result, dict) else None
        return operation, True, (VerificationCheck.objects.filter(pk=check_id).first() if check_id else check)
    operation = VerificationOperation.objects.create(
        company=company, application=application, operation_kind=kind,
        application_revision=application.revision, face_attempt_no=face_attempt_no,
        idempotency_key=key, request_hash=request_hash,
        status=VerificationOperation.STATUS_RUNNING, attempts=1,
        lease_until=now + timedelta(seconds=_setting_int(
            "COMPANY_VERIFICATION_OPERATION_LEASE_SECONDS", 60
        )),
        result={"check_id": str(check.id)} if check else {},
    )
    return operation, True, check


def _mark_operation_failed(operation_id, *, check_id=None, code="provider_unavailable", now=None):
    now = now or timezone.now()
    with transaction.atomic():
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        result = dict(operation.result or {})
        if check_id:
            result["check_id"] = str(check_id)
        operation.status = VerificationOperation.STATUS_FAILED
        exhausted = operation.attempts >= _setting_int(
            "COMPANY_VERIFICATION_OPERATION_MAX_ATTEMPTS", 3
        )
        operation.last_error_code = "retry_exhausted" if exhausted else _clean_code(code)
        operation.lease_until = None
        operation.next_run_at = None if exhausted else now + timedelta(seconds=_setting_int(
            "COMPANY_VERIFICATION_OPERATION_RETRY_SECONDS", 30
        ))
        operation.result = result
        operation.save(update_fields=(
            "status", "last_error_code", "lease_until", "next_run_at", "result", "updated_at"
        ))
        if check_id:
            VerificationCheck.objects.filter(pk=check_id).update(
                status=VerificationCheck.STATUS_UNKNOWN,
                provider_code=_clean_code(code, 128), checked_at=now, valid_until=None,
            )


def run_registry_verification(
    *, company_id: str, application_id: str, user, expected_revision: int,
    idempotency_key: str, verifier=None, now=None
) -> dict:
    _, configured_environment, _, configured_mode = _configuration()
    key = _require_key(idempotency_key)
    fixed_now = now is not None
    now = now or timezone.now()
    request_hash = ""
    operation_id = check_id = None
    with transaction.atomic():
        company, application = _lock_application(
            company_id=company_id, application_id=application_id, user=user
        )
        _require_current(
            application, expected_revision=expected_revision,
            stage=CompanyVerification.STAGE_COMPANY_CHECK,
        )
        if application.provider_environment != configured_environment:
            raise VerificationDomainError("provider_unavailable", status_code=503)
        registry_mode = application.mode
        legal_representative = ""
        if application.applicant_role:
            legal_representative = _verified_legal_representative(application, now=now)
            if not legal_representative:
                raise VerificationDomainError("business_license_required", status_code=409)
            registry_mode = CompanyVerification.MODE_THREE_ELEMENTS
        elif application.mode != configured_mode:
            raise VerificationDomainError("provider_unavailable", status_code=503)
        request_hash = _request_hash({
            "application_id": str(application.public_id), "revision": application.revision,
            "company_name": application.company_name_snapshot,
            "registration_no": application.registration_no_snapshot, "mode": registry_mode,
            "legal_representative": legal_representative,
        })
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company, operation_kind=VerificationOperation.KIND_REGISTRY_CHECK,
            idempotency_key=key,
        ).first()
        if existing and existing.request_hash == request_hash and existing.status == VerificationOperation.STATUS_COMPLETED:
            return serialize_manual_verification(application)
        check = None
        if not existing:
            attempt_no = (application.checks.filter(kind=VerificationCheck.KIND_REGISTRY)
                          .order_by("-attempt_no").values_list("attempt_no", flat=True).first() or 0) + 1
            check = VerificationCheck.objects.create(
                application=application, kind=VerificationCheck.KIND_REGISTRY,
                attempt_no=attempt_no, input_digest=request_hash, provider=application.provider,
                action="VerifyBizLicenseEnterprise3", mode=registry_mode,
                provider_environment=application.provider_environment,
                status=VerificationCheck.STATUS_RUNNING, rule_version=application.policy_version,
            )
        operation, should_call, claimed_check = _claim_operation(
            company=company, application=application,
            kind=VerificationOperation.KIND_REGISTRY_CHECK, key=key,
            request_hash=request_hash, now=now, check=check,
        )
        if not should_call:
            return serialize_manual_verification(application)
        check = claimed_check or check
        if not check:
            raise VerificationDomainError("provider_unavailable", status_code=503)
        VerificationCheck.objects.filter(pk=check.pk).update(status=VerificationCheck.STATUS_RUNNING)
        operation_id, check_id = operation.id, check.id
        provider_request = RegistryVerificationRequest(
            company_name=application.company_name_snapshot,
            registration_no=application.registration_no_snapshot,
            mode=registry_mode,
            legal_representative=legal_representative,
        )
    try:
        provider_started = time.monotonic()
        result = (verifier or get_registry_verifier()).verify(provider_request)
        provider_seconds = time.monotonic() - provider_started
    except VerificationProviderError as exc:
        _mark_operation_failed(operation_id, check_id=check_id, code=exc.code, now=now)
        raise VerificationDomainError("provider_unavailable", status_code=503) from exc
    except Exception as exc:
        _mark_operation_failed(operation_id, check_id=check_id, code=type(exc).__name__, now=now)
        raise VerificationDomainError("provider_unavailable", status_code=503) from exc

    completed_at = now if fixed_now else timezone.now()
    logger.info(
        "[company-verification-registry] mode=%s provider_ms=%s outcome=%s",
        provider_request.mode,
        round(provider_seconds * 1000),
        result.outcome,
    )
    passed = (
        result.outcome == "passed" and result.matched is True and result.operating_status == "1"
    )
    check_status = (VerificationCheck.STATUS_PASSED if passed else
                    VerificationCheck.STATUS_UNKNOWN if result.outcome == "unknown" else
                    VerificationCheck.STATUS_FAILED)
    with transaction.atomic():
        company, application = _lock_application(
            company_id=company_id, application_id=application_id, user=user
        )
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        check = VerificationCheck.objects.select_for_update().get(pk=check_id)
        current = (
            application.revision == operation.application_revision
            and application.status in ACTIVE_STATUSES
            and application.stage == CompanyVerification.STAGE_COMPANY_CHECK
            and check.input_digest == request_hash
        )
        check.status = check_status
        check.provider_code = _clean_code(result.provider_code, 128)
        check.request_id = _clean_code(result.request_id, 255)
        provider_evidence = dict(result.evidence or {})
        check.result = {
            "outcome": result.outcome, "matched": result.matched,
            "operating_status": result.operating_status,
            "status_code": provider_evidence.get("status_code"),
            "verify_result": provider_evidence.get("verify_result"),
        }
        check.checked_at = completed_at
        check.valid_until = completed_at + timedelta(seconds=_setting_int(
            "COMPANY_VERIFICATION_REGISTRY_MAX_AGE_SECONDS", 86400
        )) if passed else None
        check.save(update_fields=(
            "status", "provider_code", "request_id", "result", "checked_at", "valid_until"
        ))
        operation.result = {"check_id": str(check.id), "outcome": result.outcome}
        operation.lease_until = None
        operation.completed_at = completed_at if not result.retryable else None
        operation.next_run_at = (completed_at + timedelta(seconds=_setting_int(
            "COMPANY_VERIFICATION_OPERATION_RETRY_SECONDS", 30
        ))) if result.retryable else None
        operation.status = (VerificationOperation.STATUS_FAILED if result.retryable
                            else VerificationOperation.STATUS_COMPLETED)
        operation.last_error_code = _clean_code(result.provider_code) if result.retryable else ""
        operation.save(update_fields=(
            "result", "lease_until", "completed_at", "next_run_at", "status",
            "last_error_code", "updated_at"
        ))
        if passed and current:
            application.stage = (
                CompanyVerification.STAGE_AUTHORIZATION_UPLOAD
                if application.applicant_role
                == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
                else CompanyVerification.STAGE_APPLICANT_FACE
            )
            application.revision += 1
            application.last_refreshed_at = completed_at
            application.failure_code = ""
            application.public_reason = ""
            application.save(update_fields=(
                "stage", "revision", "last_refreshed_at", "failure_code", "public_reason"
            ))
            VerificationAuditEvent.objects.create(
                company=company, application=application, actor=user,
                action="registry_check_passed", revision=application.revision,
                detail={"check_id": str(check.id), "request_id": check.request_id},
            )
        elif current:
            application.last_refreshed_at = completed_at
            application.failure_code = _clean_code(result.provider_code)
            application.public_reason = "企业信息暂未通过核验" if check_status == VerificationCheck.STATUS_FAILED else "企业信息核验暂不可用"
            application.save(update_fields=("last_refreshed_at", "failure_code", "public_reason"))
    return serialize_manual_verification(application)


def _secret_payload(protector, face_session):
    raw = protector.decrypt(face_session.token_encrypted, context=f"face:{face_session.id}")
    value = json.loads(raw)
    if not isinstance(value, dict) or not value.get("token"):
        raise VerificationCryptoError("invalid_face_secret")
    return value


def _face_start_response(application, face_session, protector, *, include_url=True):
    payload = serialize_manual_verification(application)
    response = {"session_id": str(face_session.id), "attempt_no": face_session.attempt_no,
                "status": face_session.status}
    if include_url and face_session.token_encrypted:
        try:
            response["verification_url"] = _secret_payload(protector, face_session).get("url", "")
        except Exception:
            response["verification_url"] = ""
    payload["face_start"] = response
    return payload


def _expire_ambiguous_face_start(*, company_id, application_id, user, now):
    """Release a start attempt whose response was lost after its bounded lease."""
    with transaction.atomic():
        company, application = _lock_application(
            company_id=company_id, application_id=application_id, user=user
        )
        operation = VerificationOperation.objects.select_for_update().filter(
            application=application, operation_kind=VerificationOperation.KIND_FACE_START,
            status=VerificationOperation.STATUS_RUNNING, lease_until__lte=now,
        ).order_by("-created_at").first()
        if not operation or operation.face_attempt_no is None:
            return False
        face = application.face_sessions.select_for_update().filter(
            attempt_no=operation.face_attempt_no, status=VerificationFaceSession.STATUS_CREATED,
            token_encrypted="",
        ).first()
        if not face:
            return False
        face.status = VerificationFaceSession.STATUS_FAILED
        face.outcome_code = "ambiguous_face_start"
        face.save(update_fields=("status", "outcome_code"))
        operation.status = VerificationOperation.STATUS_FAILED
        operation.lease_until = None
        operation.next_run_at = None
        operation.last_error_code = "ambiguous_face_start"
        operation.save(update_fields=(
            "status", "lease_until", "next_run_at", "last_error_code", "updated_at"
        ))
        return True


def start_applicant_face(
    *, company_id: str, application_id: str, user, expected_revision: int,
    idempotency_key: str, verifier=None, protector=None, now=None
) -> dict:
    _, configured_environment, _, _ = _configuration()
    key = _require_key(idempotency_key)
    now = now or timezone.now()
    protector = protector or get_identity_protector()
    rule_id = str(getattr(settings, "TENCENT_FACEID_RULE_ID", "")).strip()
    redirect_url = str(getattr(settings, "TENCENT_FACEID_REDIRECT_URL", "")).strip()
    if not rule_id or not redirect_url:
        raise VerificationDomainError("provider_unavailable", status_code=503)
    _expire_ambiguous_face_start(
        company_id=company_id, application_id=application_id, user=user, now=now
    )
    with transaction.atomic():
        company, application = _lock_application(
            company_id=company_id, application_id=application_id, user=user
        )
        _require_current(
            application, expected_revision=expected_revision,
            stage=CompanyVerification.STAGE_APPLICANT_FACE,
        )
        if application.provider_environment != configured_environment:
            raise VerificationDomainError("provider_unavailable", status_code=503)
        legal_representative_path = (
            application.applicant_role
            == CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE
        )
        legal_representative = (
            _verified_legal_representative(application, now=now)
            if legal_representative_path
            else ""
        )
        if legal_representative_path and not legal_representative:
            raise VerificationDomainError("business_license_required", status_code=409)
        if (
            legal_representative_path
            and _normalize_person_name(application.applicant_name_snapshot)
            != _normalize_person_name(legal_representative)
        ):
            raise VerificationDomainError("applicant_identity_mismatch", status_code=409)
        if not (
            application.applicant_name_snapshot
            and application.applicant_id_type == "resident_identity_card"
            and application.applicant_id_encrypted
            and application.applicant_id_hmac
        ):
            raise VerificationDomainError("applicant_identity_required", status_code=409)
        if not _identity_precheck_is_current(application):
            raise VerificationDomainError("applicant_identity_required", status_code=409)
        request_hash = _request_hash({
            "application_id": str(application.public_id), "revision": application.revision,
            "identity_hmac": application.applicant_id_hmac,
            "legal_representative": legal_representative,
            "identity_source": "server_bound",
            "rule_id": rule_id,
        })
        existing_op = VerificationOperation.objects.select_for_update().filter(
            company=company, operation_kind=VerificationOperation.KIND_FACE_START,
            idempotency_key=key,
        ).first()
        if existing_op:
            if existing_op.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            face = application.face_sessions.filter(attempt_no=existing_op.face_attempt_no).first()
            if existing_op.status == VerificationOperation.STATUS_COMPLETED and face:
                return _face_start_response(application, face, protector)
            if existing_op.status == VerificationOperation.STATUS_RUNNING and existing_op.lease_until and existing_op.lease_until > now:
                raise VerificationDomainError("operation_in_progress", status_code=409)
            # Starting FaceID is billable and ambiguous after a lost response; never auto-repeat it.
            raise VerificationDomainError("provider_unavailable", status_code=503)
        active = application.face_sessions.select_for_update().filter(
            status__in=(VerificationFaceSession.STATUS_CREATED, VerificationFaceSession.STATUS_IN_PROGRESS)
        ).order_by("-attempt_no").first()
        if active and active.provider_expires_at and active.provider_expires_at <= now:
            active.status = VerificationFaceSession.STATUS_EXPIRED
            active.outcome_code = "provider_token_expired"
            active.save(update_fields=("status", "outcome_code"))
            active = None
        if active:
            if active.status == VerificationFaceSession.STATUS_CREATED:
                raise VerificationDomainError("operation_in_progress", status_code=409)
            return _face_start_response(application, active, protector)
        attempt_no = (application.face_sessions.order_by("-attempt_no")
                      .values_list("attempt_no", flat=True).first() or 0) + 1
        consumed_attempts = application.face_sessions.exclude(
            outcome_code__in=_FACE_CONFIGURATION_ERROR_CODES
        ).count()
        if consumed_attempts >= _setting_int("COMPANY_VERIFICATION_FACE_MAX_ATTEMPTS", 3):
            raise VerificationDomainError("provider_unavailable", status_code=503)
        face = VerificationFaceSession.objects.create(
            application=application, attempt_no=attempt_no, provider=application.provider,
            provider_environment=application.provider_environment, rule_id=rule_id,
            status=VerificationFaceSession.STATUS_CREATED,
        )
        operation = VerificationOperation.objects.create(
            company=company, application=application,
            operation_kind=VerificationOperation.KIND_FACE_START,
            application_revision=application.revision, face_attempt_no=attempt_no,
            idempotency_key=key, request_hash=request_hash,
            status=VerificationOperation.STATUS_RUNNING, attempts=1,
            lease_until=now + timedelta(seconds=_setting_int(
                "COMPANY_VERIFICATION_OPERATION_LEASE_SECONDS", 60
            )),
            result={"face_session_id": str(face.id)},
        )
        try:
            identity_number = protector.decrypt(
                application.applicant_id_encrypted,
                context=str(application.public_id),
            )
        except VerificationCryptoError as exc:
            face.status = VerificationFaceSession.STATUS_FAILED
            face.outcome_code = "identity_unavailable"
            face.save(update_fields=("status", "outcome_code"))
            operation.status = VerificationOperation.STATUS_FAILED
            operation.last_error_code = "identity_unavailable"
            operation.lease_until = None
            operation.save(update_fields=(
                "status", "last_error_code", "lease_until", "updated_at"
            ))
            raise VerificationDomainError(
                "provider_unavailable", status_code=503
            ) from exc
        provider_request = ApplicantIdentityStartRequest(
            name=application.applicant_name_snapshot,
            identity_number=identity_number,
            identity_type=application.applicant_id_type,
            rule_id=rule_id,
            redirect_url=redirect_url, correlation_id=str(face.id),
        )
        operation_id, face_id = operation.id, face.id
    try:
        result = (verifier or get_applicant_identity_verifier()).start(provider_request)
        if result.outcome != "started" or not result.token or not result.verification_url:
            raise VerificationProviderProtocolError(
                "FaceID start response is incomplete", code="missing_face_session"
            )
        encrypted = protector.protect_secret(
            json.dumps({"token": result.token, "url": result.verification_url}, separators=(",", ":")),
            context=f"face:{face_id}",
        )
        token_digest = protector.digest_secret(result.token, purpose="face-token")
    except Exception as exc:
        with transaction.atomic():
            face = VerificationFaceSession.objects.select_for_update().get(pk=face_id)
            operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
            face.status = VerificationFaceSession.STATUS_FAILED
            face.outcome_code = _clean_code(getattr(exc, "code", type(exc).__name__))
            face.save(update_fields=("status", "outcome_code"))
            operation.status = VerificationOperation.STATUS_FAILED
            operation.last_error_code = face.outcome_code
            operation.lease_until = None
            operation.next_run_at = None
            operation.save(update_fields=(
                "status", "last_error_code", "lease_until", "next_run_at", "updated_at"
            ))
        raise VerificationDomainError(
            _face_public_error(_clean_code(getattr(exc, "code", type(exc).__name__), 128)),
            status_code=503,
        ) from exc
    with transaction.atomic():
        company, application = _lock_application(
            company_id=company_id, application_id=application_id, user=user
        )
        face = VerificationFaceSession.objects.select_for_update().get(pk=face_id)
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        latest_attempt = application.face_sessions.order_by("-attempt_no").values_list("attempt_no", flat=True).first()
        if (application.revision != operation.application_revision or
                application.stage != CompanyVerification.STAGE_APPLICANT_FACE or
                application.status not in ACTIVE_STATUSES or latest_attempt != face.attempt_no):
            face.status = VerificationFaceSession.STATUS_CANCELLED
            face.outcome_code = "stale_face_start"
            face.save(update_fields=("status", "outcome_code"))
            operation.status = VerificationOperation.STATUS_COMPLETED
            operation.lease_until = None
            operation.completed_at = now
            operation.result = {"face_session_id": str(face.id), "outcome": "stale"}
            operation.save(update_fields=("status", "lease_until", "completed_at", "result", "updated_at"))
            raise VerificationDomainError("stale_revision", status_code=409)
        provider_ttl = max(int(result.expires_in_seconds or 7200), 60)
        face.token_encrypted = encrypted
        face.token_digest = token_digest
        face.request_id = _clean_code(result.request_id, 255)
        face.status = VerificationFaceSession.STATUS_IN_PROGRESS
        face.outcome_code = _clean_code(result.provider_code)
        face.provider_expires_at = now + timedelta(seconds=provider_ttl)
        query_deadline = now + timedelta(seconds=_setting_int(
            "COMPANY_VERIFICATION_FACE_QUERY_TTL_SECONDS", 259200
        ))
        face.query_expires_at = min(query_deadline, application.expires_at)
        face.save(update_fields=(
            "token_encrypted", "token_digest", "request_id", "status", "outcome_code",
            "provider_expires_at", "query_expires_at"
        ))
        operation.status = VerificationOperation.STATUS_COMPLETED
        operation.lease_until = None
        operation.completed_at = now
        operation.result = {"face_session_id": str(face.id), "outcome": "started"}
        operation.save(update_fields=("status", "lease_until", "completed_at", "result", "updated_at"))
        VerificationAuditEvent.objects.create(
            company=company, application=application, actor=user, action="face_session_started",
            revision=application.revision, detail={"face_session_id": str(face.id), "attempt_no": face.attempt_no},
        )
    payload = _face_start_response(application, face, protector, include_url=False)
    payload["face_start"]["verification_url"] = result.verification_url
    return payload


def query_applicant_face(
    *, company_id: str, application_id: str, user, expected_revision: int,
    idempotency_key: str, verifier=None, protector=None, now=None
) -> dict:
    _, configured_environment, _, _ = _configuration()
    key = _require_key(idempotency_key)
    now = now or timezone.now()
    protector = protector or get_identity_protector()
    with transaction.atomic():
        company, application = _lock_application(
            company_id=company_id, application_id=application_id, user=user
        )
        _require_current(
            application, expected_revision=expected_revision,
            stage=CompanyVerification.STAGE_APPLICANT_FACE,
        )
        if application.provider_environment != configured_environment:
            raise VerificationDomainError("provider_unavailable", status_code=503)
        face = application.face_sessions.select_for_update().order_by("-attempt_no").first()
        if not face or not face.token_encrypted:
            raise VerificationDomainError("face_session_unavailable", status_code=409)
        if not face.query_expires_at or face.query_expires_at <= now:
            face.status = VerificationFaceSession.STATUS_EXPIRED
            face.outcome_code = "face_query_expired"
            face.save(update_fields=("status", "outcome_code"))
            raise VerificationDomainError("face_session_expired", status_code=409)
        if face.status not in (VerificationFaceSession.STATUS_CREATED, VerificationFaceSession.STATUS_IN_PROGRESS):
            raise VerificationDomainError("face_session_unavailable", status_code=409)
        request_hash = _request_hash({
            "application_id": str(application.public_id), "revision": application.revision,
            "face_attempt_no": face.attempt_no, "token_digest": face.token_digest,
            "rule_id": face.rule_id,
        })
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company, operation_kind=VerificationOperation.KIND_FACE_QUERY,
            idempotency_key=key,
        ).first()
        if existing and existing.request_hash == request_hash and existing.status == VerificationOperation.STATUS_COMPLETED:
            return serialize_manual_verification(application)
        check = None
        if not existing:
            check_attempt = (application.checks.filter(kind=VerificationCheck.KIND_FACE)
                             .order_by("-attempt_no").values_list("attempt_no", flat=True).first() or 0) + 1
            check = VerificationCheck.objects.create(
                application=application, kind=VerificationCheck.KIND_FACE,
                attempt_no=check_attempt, input_digest=request_hash, face_session=face,
                provider=application.provider, action="GetDetectInfoEnhanced",
                provider_environment=application.provider_environment,
                status=VerificationCheck.STATUS_RUNNING, rule_version=application.policy_version,
            )
        operation, should_call, claimed_check = _claim_operation(
            company=company, application=application, kind=VerificationOperation.KIND_FACE_QUERY,
            key=key, request_hash=request_hash, now=now, face_attempt_no=face.attempt_no, check=check,
        )
        if not should_call:
            return serialize_manual_verification(application)
        check = claimed_check or check
        if not check:
            raise VerificationDomainError("provider_unavailable", status_code=503)
        VerificationCheck.objects.filter(pk=check.pk).update(status=VerificationCheck.STATUS_RUNNING)
        try:
            secret = _secret_payload(protector, face)
            legal_representative_path = (
                application.applicant_role
                == CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE
            )
            legal_representative = (
                _verified_legal_representative(application, now=now)
                if legal_representative_path
                else ""
            )
            if legal_representative_path:
                if not legal_representative:
                    raise VerificationCryptoError("legal_representative_unavailable")
                if _normalize_person_name(application.applicant_name_snapshot) != (
                    _normalize_person_name(legal_representative)
                ):
                    raise VerificationCryptoError("legal_representative_mismatch")
            expected_identity = protector.decrypt(
                application.applicant_id_encrypted,
                context=str(application.public_id),
            )
        except (VerificationCryptoError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise VerificationDomainError("provider_unavailable", status_code=503) from exc
        provider_request = ApplicantIdentityQueryRequest(token=secret["token"], rule_id=face.rule_id)
        operation_id, check_id, face_id = operation.id, check.id, face.id
    try:
        result = (verifier or get_applicant_identity_verifier()).query(provider_request)
    except VerificationProviderError as exc:
        _mark_operation_failed(operation_id, check_id=check_id, code=exc.code, now=now)
        raise VerificationDomainError(_face_public_error(exc.code), status_code=503) from exc
    except Exception as exc:
        _mark_operation_failed(operation_id, check_id=check_id, code=type(exc).__name__, now=now)
        raise VerificationDomainError("provider_unavailable", status_code=503) from exc

    identity_match = False
    if result.outcome == "passed":
        try:
            identity_match = (
                result.identity_type == "resident_identity_card"
                and result.name == application.applicant_name_snapshot
                and result.correlation_id == str(face_id)
                and hmac.compare_digest(
                    protector.digest(result.identity_number),
                    application.applicant_id_hmac,
                )
                and result.identity_number == expected_identity
            )
        except VerificationCryptoError:
            identity_match = False
    passed = result.outcome == "passed" and identity_match
    with transaction.atomic():
        company, application = _lock_application(
            company_id=company_id, application_id=application_id, user=user
        )
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        check = VerificationCheck.objects.select_for_update().get(pk=check_id)
        face = VerificationFaceSession.objects.select_for_update().get(pk=face_id)
        latest_attempt = application.face_sessions.order_by("-attempt_no").values_list("attempt_no", flat=True).first()
        current = (
            application.revision == operation.application_revision
            and application.stage == CompanyVerification.STAGE_APPLICANT_FACE
            and application.status in ACTIVE_STATUSES
            and latest_attempt == operation.face_attempt_no == face.attempt_no
            and check.face_session_id == face.id
            and check.input_digest == request_hash
        )
        check.status = (VerificationCheck.STATUS_PASSED if passed else
                        VerificationCheck.STATUS_UNKNOWN if result.outcome in {"unknown", "in_progress"} else
                        VerificationCheck.STATUS_FAILED)
        check.provider_code = _clean_code(
            result.provider_code if identity_match or result.outcome != "passed" else "face_identity_mismatch", 128
        )
        check.request_id = _clean_code(result.request_id, 255)
        provider_evidence = dict(result.evidence or {})
        check.result = {
            "outcome": result.outcome, "identity_match": identity_match,
            "err_code": provider_evidence.get("err_code"),
            "live_status": provider_evidence.get("live_status"),
            "compare_status": provider_evidence.get("compare_status"),
        }
        check.checked_at = now
        check.valid_until = now + timedelta(seconds=_setting_int(
            "COMPANY_VERIFICATION_FACE_RESULT_MAX_AGE_SECONDS", 86400
        )) if passed else None
        check.save(update_fields=(
            "status", "provider_code", "request_id", "result", "checked_at", "valid_until"
        ))
        operation.result = {"check_id": str(check.id), "face_session_id": str(face.id), "outcome": result.outcome}
        operation.lease_until = None
        if result.retryable:
            operation.status = VerificationOperation.STATUS_FAILED
            operation.next_run_at = now + timedelta(seconds=_setting_int(
                "COMPANY_VERIFICATION_OPERATION_RETRY_SECONDS", 30
            ))
            operation.last_error_code = check.provider_code
            operation.completed_at = None
        else:
            operation.status = VerificationOperation.STATUS_COMPLETED
            operation.next_run_at = None
            operation.last_error_code = ""
            operation.completed_at = now
        operation.save(update_fields=(
            "result", "lease_until", "status", "next_run_at", "last_error_code",
            "completed_at", "updated_at"
        ))
        if passed and current:
            face.status = VerificationFaceSession.STATUS_PASSED
            face.identity_match = True
            face.outcome_code = check.provider_code
            face.request_id = check.request_id
            face.last_queried_at = now
            face.result_valid_until = check.valid_until
            face.save(update_fields=(
                "status", "identity_match", "outcome_code", "request_id",
                "last_queried_at", "result_valid_until"
            ))
            application.stage = (
                CompanyVerification.STAGE_READY_TO_SUBMIT
                if (
                    application.applicant_role
                    == CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE
                    or (
                        application.applicant_role
                        == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
                        and _has_valid_authorization(application, now=now)
                    )
                )
                else CompanyVerification.STAGE_AUTHORIZATION_UPLOAD
            )
            application.revision += 1
            application.last_refreshed_at = now
            application.failure_code = ""
            application.public_reason = ""
            update_fields = [
                "stage", "revision", "last_refreshed_at", "failure_code", "public_reason"
            ]
            application.save(update_fields=update_fields)
            VerificationAuditEvent.objects.create(
                company=company, application=application, actor=user,
                action="face_check_passed", revision=application.revision,
                detail={"check_id": str(check.id), "face_session_id": str(face.id), "attempt_no": face.attempt_no},
            )
        elif current:
            face.last_queried_at = now
            face.outcome_code = check.provider_code
            if result.outcome == "passed" or result.outcome == "failed":
                face.status = VerificationFaceSession.STATUS_FAILED
                face.identity_match = False
            elif face.provider_expires_at and face.provider_expires_at <= now:
                face.status = VerificationFaceSession.STATUS_EXPIRED
            face.save(update_fields=("last_queried_at", "outcome_code", "status", "identity_match"))
    return serialize_manual_verification(application)


def retry_due_manual_evidence(*, limit=50, registry_verifier=None, identity_verifier=None, now=None):
    now = now or timezone.now()
    maximum = _setting_int("COMPANY_VERIFICATION_OPERATION_MAX_ATTEMPTS", 3)
    evidence_kinds = (
        VerificationOperation.KIND_REGISTRY_CHECK,
        VerificationOperation.KIND_FACE_QUERY,
    )
    VerificationOperation.objects.filter(
        operation_kind__in=evidence_kinds,
        status=VerificationOperation.STATUS_FAILED,
        attempts__gte=maximum,
    ).exclude(last_error_code="retry_exhausted").update(
        next_run_at=None,
        lease_until=None,
        last_error_code="retry_exhausted",
    )
    operations = list(VerificationOperation.objects.filter(
        operation_kind__in=evidence_kinds,
        attempts__lt=maximum,
    ).filter(
        Q(status=VerificationOperation.STATUS_FAILED, next_run_at__lte=now)
        | Q(status=VerificationOperation.STATUS_RUNNING, lease_until__lte=now)
    ).select_related("application", "application__applicant_user", "company").order_by("next_run_at")[:limit])
    processed = failed = 0
    for operation in operations:
        application = operation.application
        if not application or not application.applicant_user:
            failed += 1
            continue
        kwargs = dict(
            company_id=operation.company.cid, application_id=str(application.public_id),
            user=application.applicant_user, expected_revision=operation.application_revision,
            idempotency_key=operation.idempotency_key, now=now,
        )
        try:
            if operation.operation_kind == VerificationOperation.KIND_REGISTRY_CHECK:
                run_registry_verification(verifier=registry_verifier, **kwargs)
            else:
                query_applicant_face(verifier=identity_verifier, **kwargs)
            processed += 1
        except VerificationDomainError:
            failed += 1
    return {"processed": processed, "failed": failed}
