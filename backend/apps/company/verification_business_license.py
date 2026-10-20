"""Business-license evidence used before registry and FaceID checks."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationOperation,
)
from apps.company.providers import get_business_license_reader
from apps.company.providers.base import VerificationProviderError
from apps.company.providers.manual import DocumentExtractionRequest
from apps.company.verification_domain import (
    ACTIVE_STATUSES,
    VerificationDomainError,
    _request_hash,
    serialize_manual_verification,
)
from apps.company.verification_storage import VerificationStorageError, get_verification_storage


BUSINESS_LICENSE_NOT_RECOGNIZED_CODES = {"FailedOperation.NoBizLicense"}


logger = logging.getLogger("touming")


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(value or "")).upper()


def run_business_license_ocr(
    *, company_id: str, application_id: str, user, expected_revision: int,
    idempotency_key: str, reader=None, storage=None, content: bytes | None = None,
    now=None,
) -> dict:
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    fixed_now = now is not None
    now = now or timezone.now()
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(cid=company_id).first()
        if not company:
            raise VerificationDomainError("company_not_found", status_code=404)
        application = CompanyVerification.objects.select_for_update().filter(
            company=company,
            public_id=application_id,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
        ).first()
        if not application:
            raise VerificationDomainError("application_not_found", status_code=404)
        if application.applicant_user_id != user.id:
            raise VerificationDomainError("application_forbidden", status_code=403)
        if (
            application.status not in ACTIVE_STATUSES
            or application.stage != CompanyVerification.STAGE_COMPANY_CHECK
            or application.revision != int(expected_revision)
        ):
            raise VerificationDomainError("stale_revision", status_code=409)
        document = application.documents.filter(
            document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
            scan_status=VerificationDocument.SCAN_PASSED,
        ).order_by("-uploaded_at", "-id").first()
        if not document:
            raise VerificationDomainError("business_license_required", status_code=409)
        security = application.checks.filter(
            kind=VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY,
            document=document,
            status=VerificationCheck.STATUS_PASSED,
            valid_until__gt=now,
        ).order_by("-checked_at", "-created_at").first()
        if not security:
            raise VerificationDomainError("business_license_required", status_code=409)
        request_hash = _request_hash({
            "application_id": str(application.public_id),
            "revision": application.revision,
            "document_id": str(document.id),
            "sha256": document.sha256,
            "applicant_role": application.applicant_role,
        })
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_BUSINESS_LICENSE_OCR,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                return serialize_manual_verification(application)
            if (
                existing.status == VerificationOperation.STATUS_FAILED
                and existing.last_error_code in BUSINESS_LICENSE_NOT_RECOGNIZED_CODES
            ):
                raise VerificationDomainError(
                    "business_license_not_recognized", status_code=422
                )
            raise VerificationDomainError("operation_in_progress", status_code=409)
        attempt_no = (
            application.checks.filter(kind=VerificationCheck.KIND_BUSINESS_LICENSE)
            .order_by("-attempt_no")
            .values_list("attempt_no", flat=True)
            .first()
            or 0
        ) + 1
        check = VerificationCheck.objects.create(
            application=application,
            kind=VerificationCheck.KIND_BUSINESS_LICENSE,
            attempt_no=attempt_no,
            input_digest=request_hash,
            material_revision=document.material_revision,
            document=document,
            provider=application.provider,
            action="BizLicenseOCR",
            provider_environment=application.provider_environment,
            status=VerificationCheck.STATUS_RUNNING,
            rule_version=application.policy_version,
        )
        operation = VerificationOperation.objects.create(
            company=company,
            application=application,
            document=document,
            operation_kind=VerificationOperation.KIND_BUSINESS_LICENSE_OCR,
            application_revision=application.revision,
            material_revision=document.material_revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_RUNNING,
            attempts=1,
            lease_until=now + timedelta(seconds=60),
            result={"check_id": str(check.id)},
        )
        document_id = document.id
        operation_id = operation.id
        check_id = check.id

    try:
        document = VerificationDocument.objects.get(pk=document_id)
        storage_seconds = 0.0
        if content is None:
            storage = storage or get_verification_storage()
            storage_started = time.monotonic()
            content = storage.get(
                document.storage_key,
                max_bytes=max(
                    int(getattr(settings, "COMPANY_VERIFICATION_MAX_FILE_BYTES", 5 * 1024 * 1024)),
                    1,
                ),
            )
            storage_seconds = time.monotonic() - storage_started
        elif hashlib.sha256(content).hexdigest() != document.sha256:
            raise VerificationStorageError("storage_integrity_mismatch")
        provider_started = time.monotonic()
        result = (reader or get_business_license_reader()).extract_business_license(
            DocumentExtractionRequest(content=content, mime_type=document.mime_type)
        )
        provider_seconds = time.monotonic() - provider_started
    except (VerificationProviderError, VerificationStorageError) as exc:
        code = str(getattr(exc, "code", type(exc).__name__) or "provider_unavailable")[:64]
        request_id = str(getattr(exc, "request_id", "") or "")[:255]
        not_recognized = code in BUSINESS_LICENSE_NOT_RECOGNIZED_CODES
        with transaction.atomic():
            VerificationCheck.objects.filter(pk=check_id).update(
                status=(
                    VerificationCheck.STATUS_FAILED
                    if not_recognized
                    else VerificationCheck.STATUS_UNKNOWN
                ),
                provider_code=code,
                request_id=request_id,
                checked_at=now,
                valid_until=None,
            )
            VerificationOperation.objects.filter(pk=operation_id).update(
                status=VerificationOperation.STATUS_FAILED,
                lease_until=None,
                last_error_code=code,
                completed_at=now if not_recognized else None,
                result={
                    "check_id": str(check_id),
                    "outcome": (
                        VerificationCheck.STATUS_FAILED
                        if not_recognized
                        else VerificationCheck.STATUS_UNKNOWN
                    ),
                },
            )
            if not_recognized:
                CompanyVerification.objects.filter(pk=application.pk).update(
                    last_refreshed_at=now,
                    failure_code="business_license_not_recognized",
                    public_reason=(
                        "未识别到有效营业执照，请上传四边完整、文字清晰的营业执照原图"
                    ),
                )
        if not_recognized:
            raise VerificationDomainError(
                "business_license_not_recognized", status_code=422
            ) from exc
        raise VerificationDomainError("provider_unavailable", status_code=503) from exc
    except Exception as exc:
        code = type(exc).__name__[:64]
        with transaction.atomic():
            VerificationCheck.objects.filter(pk=check_id).update(
                status=VerificationCheck.STATUS_UNKNOWN,
                provider_code=code,
                checked_at=now,
            )
            VerificationOperation.objects.filter(pk=operation_id).update(
                status=VerificationOperation.STATUS_FAILED,
                lease_until=None,
                last_error_code=code,
            )
        raise VerificationDomainError("provider_unavailable", status_code=503) from exc

    completed_at = now if fixed_now else timezone.now()
    logger.info(
        "[company-verification-license-ocr] content_source=%s size_bytes=%s "
        "storage_get_ms=%s provider_ms=%s",
        "upload" if storage_seconds == 0 else "cos",
        len(content),
        round(storage_seconds * 1000),
        round(provider_seconds * 1000),
    )
    company_name_match = _normalize(result.company_name) == _normalize(application.company_name_snapshot)
    registration_no_match = _normalize(result.registration_no) == _normalize(
        application.registration_no_snapshot
    )
    legal_representative_match = bool(_normalize(result.legal_representative))
    authenticity_warning_codes = sorted(
        set(result.warning_codes).intersection({-9102, -9104})
    )
    copy_or_photo_warning = bool(
        result.electronic is not True
        and (authenticity_warning_codes or result.is_duplication == 1)
    )
    passed = bool(
        result.outcome == "passed"
        and company_name_match
        and registration_no_match
        and legal_representative_match
    )
    with transaction.atomic():
        application = CompanyVerification.objects.select_for_update().get(pk=application.pk)
        check = VerificationCheck.objects.select_for_update().get(pk=check_id)
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        current = (
            application.status in ACTIVE_STATUSES
            and application.stage == CompanyVerification.STAGE_COMPANY_CHECK
            and application.revision == operation.application_revision
            and application.documents.filter(pk=document_id).exists()
        )
        adopted = passed and current
        check.status = (
            VerificationCheck.STATUS_PASSED
            if adopted
            else VerificationCheck.STATUS_UNKNOWN
            if result.retryable
            else VerificationCheck.STATUS_FAILED
        )
        failure_code = "business_license_mismatch"
        check.provider_code = str(result.provider_code if passed else failure_code)[:128]
        check.request_id = str(result.request_id or "")[:255]
        check.result = {
            "outcome": result.outcome,
            "adopted": adopted,
            "company_name": result.company_name,
            "registration_no": result.registration_no,
            "company_name_match": company_name_match,
            "registration_no_match": registration_no_match,
            "legal_representative_match": legal_representative_match,
            "legal_representative": result.legal_representative,
            "authenticity": "warning" if copy_or_photo_warning else "passed",
            "authenticity_warning_codes": authenticity_warning_codes,
            "is_duplication": result.is_duplication,
            "electronic": result.electronic,
            "warning_codes": list(result.warning_codes),
        }
        check.checked_at = completed_at
        check.valid_until = (
            completed_at + timedelta(seconds=max(int(getattr(
                settings, "COMPANY_VERIFICATION_DOCUMENT_CHECK_MAX_AGE_SECONDS", 86400
            )), 1))
            if adopted else None
        )
        check.save(update_fields=(
            "status", "provider_code", "request_id", "result", "checked_at", "valid_until"
        ))
        operation.status = VerificationOperation.STATUS_COMPLETED
        operation.lease_until = None
        operation.completed_at = completed_at
        operation.result = {"check_id": str(check.id), "outcome": check.status}
        operation.save(update_fields=(
            "status", "lease_until", "completed_at", "result", "updated_at"
        ))
        application.last_refreshed_at = completed_at
        application.failure_code = "" if adopted else failure_code
        application.public_reason = "" if adopted else "营业执照识别结果与企业或法人信息不一致"
        if adopted and application.applicant_role:
            application.mode = CompanyVerification.MODE_THREE_ELEMENTS
        application.save(update_fields=(
            "last_refreshed_at", "failure_code", "public_reason", "mode"
        ))
        if adopted:
            VerificationAuditEvent.objects.create(
                company=application.company,
                application=application,
                actor=user,
                action="business_license_check_passed",
                revision=application.revision,
                detail={
                    "check_id": str(check.id),
                    "document_id": str(document_id),
                    "copy_or_photo_warning": copy_or_photo_warning,
                },
            )
    return serialize_manual_verification(application)
