"""EV-4 authorization templates, private documents and OCR evidence."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationOperation,
)
from apps.company.providers import (
    get_authorization_document_reader,
    get_seal_reader,
)
from apps.company.providers.base import VerificationProviderError
from apps.company.providers.manual import (
    DocumentExtractionRequest,
    DocumentTextExtractionResult,
    SealExtractionResult,
)
from apps.company.verification_domain import (
    ACTIVE_STATUSES,
    VerificationDomainError,
    _request_hash,
    _valid_resident_identity_card,
    serialize_manual_verification,
)
from apps.company.verification_crypto import VerificationCryptoError, get_identity_protector
from apps.company.verification_storage import (
    PrivateCosVerificationStorage,
    VerificationStorageError,
    get_verification_storage,
)


AUTHORIZATION_SCOPE = "代表企业办理本平台企业认证及提交相关认证材料"
ALLOWED_UPLOAD_STAGES = {
    CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
    CompanyVerification.STAGE_SUPPLEMENT_REQUIRED,
}


logger = logging.getLogger("touming")


@dataclass(frozen=True)
class AuthorizationTemplateResult:
    content: bytes
    filename: str
    template_version: str
    input_digest: str


@dataclass(frozen=True)
class InspectedDocument:
    content: bytes
    mime_type: str
    extension: str
    page_count: int
    sha256: str
    preview: bytes
    scan_engine: str


class DocumentSecurityError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _application_for_user(*, company_id: str, application_id: str, user, lock=False):
    companies = Company.objects.select_for_update() if lock else Company.objects
    company = companies.filter(cid=company_id).first()
    if not company:
        raise VerificationDomainError("company_not_found", status_code=404)
    applications = CompanyVerification.objects.select_for_update() if lock else CompanyVerification.objects
    application = applications.filter(
        company=company,
        public_id=application_id,
        provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
    ).first()
    if not application:
        raise VerificationDomainError("application_not_found", status_code=404)
    if not user or application.applicant_user_id != user.id:
        raise VerificationDomainError("application_forbidden", status_code=403)
    return company, application


def _template_context(
    application: CompanyVerification, *, applicant_identity_number: str = ""
) -> dict:
    issued = application.started_at.date()
    valid_until = application.expires_at.date() if application.expires_at else issued
    return {
        "template_version": str(
            getattr(
                settings,
                "COMPANY_VERIFICATION_AUTHORIZATION_TEMPLATE_VERSION",
                "authorization-v1-draft",
            )
        ).strip(),
        "application_id": str(application.public_id),
        "company_name": application.company_name_snapshot,
        "registration_no": application.registration_no_snapshot,
        "applicant_name": application.applicant_name_snapshot,
        "applicant_id_masked": (
            applicant_identity_number or application.applicant_id_masked
        ),
        "scope": AUTHORIZATION_SCOPE,
        "issued_on": issued.isoformat(),
        "valid_until": valid_until.isoformat(),
    }


def authorization_expected_values(application: CompanyVerification) -> dict:
    """Return the only facts a reviewer may confirm after an OCR mismatch."""

    context = _template_context(application)
    return {
        "company_name": context["company_name"],
        "registration_no": context["registration_no"],
        "applicant_name": context["applicant_name"],
        "application_id": context["application_id"],
        "template_version": context["template_version"],
        "authorization_scope": context["scope"],
        "issued_on": context["issued_on"],
        "valid_until": context["valid_until"],
        "seal_company_name": context["company_name"],
    }


def _context_digest(context: dict) -> str:
    encoded = json.dumps(context, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _authorization_pdf(context: dict) -> bytes:
    try:
        from reportlab.lib.colors import Color, black
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise VerificationDomainError("template_unavailable", status_code=503) from exc

    font_name = "STSong-Light"
    try:
        pdfmetrics.getFont(font_name)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4, pageCompression=1, invariant=1)
    width, height = A4
    page.setTitle("企业认证账号申请及经办人授权书（开发测试样稿）")
    page.setAuthor("Touming")

    def draw_watermark():
        page.saveState()
        page.setFillColor(Color(0.85, 0.1, 0.1, alpha=0.14))
        page.setFont(font_name, 30)
        page.translate(width / 2, height / 2)
        page.rotate(35)
        page.drawCentredString(0, 0, "开发测试样稿·未经法务签核")
        page.restoreState()
        page.setFillColor(black)

    def draw_paragraph(text, x, y, max_width, *, font_size=10.5, line_height=18):
        page.setFont(font_name, font_size)
        current = ""
        for character in text:
            candidate = current + character
            if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
                page.drawString(x, y, current)
                y -= line_height
                current = character
            else:
                current = candidate
        if current:
            page.drawString(x, y, current)
            y -= line_height
        return y

    draw_watermark()
    page.setFont(font_name, 19)
    page.drawCentredString(width / 2, height - 58, "企业认证账号申请及经办人授权书")
    page.setFont(font_name, 10)
    page.drawRightString(width - 54, height - 78, f"模板版本：{context['template_version']}")

    left = 58
    content_width = width - 116
    y = height - 105
    page.setFont(font_name, 11)
    page.drawString(left, y, "透明平台运营方：")
    y -= 25
    y = draw_paragraph(
        "为办理本机构在透明平台的企业认证，本机构确认以下信息真实、准确、完整，"
        "并授权下列经办人仅在本授权范围内提交认证材料及配合完成身份核验。",
        left,
        y,
        content_width,
    )
    y -= 5

    page.setFont(font_name, 12)
    page.drawString(left, y, "一、机构信息")
    y -= 24
    for label, value in (
        ("机构名称", context["company_name"]),
        ("统一社会信用代码", context["registration_no"]),
        ("认证申请编号", context["application_id"]),
    ):
        y = draw_paragraph(f"{label}：{value}", left + 12, y, content_width - 12)
        y -= 4

    page.setFont(font_name, 12)
    page.drawString(left, y, "二、经办人信息")
    y -= 24
    for label, value in (
        ("经办人姓名", context["applicant_name"] or "________________"),
        ("居民身份证号", context["applicant_id_masked"] or "________________________"),
    ):
        y = draw_paragraph(f"{label}：{value}", left + 12, y, content_width - 12)
        y -= 4

    page.setFont(font_name, 12)
    page.drawString(left, y, "三、授权范围与有效期")
    y -= 24
    y = draw_paragraph(
        f"授权事项：{context['scope']}。",
        left + 12,
        y,
        content_width - 12,
    )
    y = draw_paragraph(
        f"授权有效期：{context['issued_on']} 至 {context['valid_until']}。",
        left + 12,
        y - 3,
        content_width - 12,
    )
    y -= 5

    page.setFont(font_name, 12)
    page.drawString(left, y, "四、机构声明")
    y -= 24
    draw_paragraph(
        "本机构确认经办人在上述授权范围内提交的信息和材料代表本机构真实意思表示，"
        "并由本机构承担相应责任。本授权不包含企业所有权、平台 Owner 或管理员权限、"
        "资源访问权限、合同签署权、转授权或其他超出企业认证所必需的权限。",
        left + 12,
        y,
        content_width - 12,
    )

    box_y = 95
    page.setFont(font_name, 9.5)
    page.drawString(left, box_y + 92, "填写及上传注意事项：")
    page.setFont(font_name, 9)
    page.drawString(left + 12, box_y + 72, "仅可填写指定信息，不得删改授权声明。")
    page.drawString(left + 12, box_y + 57, "须加盖与机构名称一致且清晰可辨的公章。")
    page.drawString(left + 12, box_y + 42, "请在有效期内上传本单页文件。")
    page.drawString(left + 12, box_y + 27, "格式仅限 PDF、PNG 或 JPEG。")
    page.rect(width - 210, box_y + 8, 142, 88)
    page.setFont(font_name, 10)
    page.drawCentredString(width - 139, box_y - 7, "授权机构名称（公章）")
    page.drawString(left, 68, f"出具日期：{context['issued_on']}")
    page.setFont(font_name, 8.5)
    page.drawString(left, 39, "本文件为开发测试样稿；正式启用前须完成产品及法务签核。")
    page.drawRightString(width - 54, 39, "第 1 页 / 共 1 页")
    page.showPage()
    page.save()
    return buffer.getvalue()


def generate_authorization_template(*, company_id: str, application_id: str, user) -> AuthorizationTemplateResult:
    company, application = _application_for_user(
        company_id=company_id, application_id=application_id, user=user
    )
    if application.status not in ACTIVE_STATUSES or application.stage not in ALLOWED_UPLOAD_STAGES:
        raise VerificationDomainError("transition_not_allowed", status_code=409)
    identity_number = ""
    if application.applicant_role == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON:
        if not (
            application.applicant_name_snapshot
            and application.applicant_id_type == "resident_identity_card"
            and application.applicant_id_encrypted
            and application.applicant_id_hmac
        ):
            raise VerificationDomainError("applicant_identity_required", status_code=409)
        try:
            identity_number = get_identity_protector().decrypt(
                application.applicant_id_encrypted,
                context=str(application.public_id),
            )
        except VerificationCryptoError as exc:
            raise VerificationDomainError("template_unavailable", status_code=503) from exc
        if not _valid_resident_identity_card(identity_number):
            raise VerificationDomainError("template_unavailable", status_code=503)
    context = _template_context(
        application, applicant_identity_number=identity_number
    )
    digest = _context_digest({
        **context,
        "applicant_id_masked": application.applicant_id_masked,
    })
    content = _authorization_pdf(context)
    VerificationAuditEvent.objects.create(
        company=company,
        application=application,
        actor=user,
        action="authorization_template_downloaded",
        revision=application.revision,
        detail={"template_version": context["template_version"], "input_digest": digest},
    )
    return AuthorizationTemplateResult(
        content=content,
        filename=f"authorization-{application.public_id}.pdf",
        template_version=context["template_version"],
        input_digest=digest,
    )


def _read_upload(upload_file) -> bytes:
    maximum = max(int(getattr(settings, "COMPANY_VERIFICATION_MAX_FILE_BYTES", 5 * 1024 * 1024)), 1)
    chunks, total = [], 0
    for chunk in upload_file.chunks():
        total += len(chunk)
        if total > maximum:
            raise DocumentSecurityError("document_too_large")
        chunks.append(bytes(chunk))
    content = b"".join(chunks)
    if not content:
        raise DocumentSecurityError("document_empty")
    return content


def _clamav_scan(content: bytes) -> str:
    executable = shutil.which("clamscan")
    if not executable:
        raise DocumentSecurityError("scanner_unavailable")
    with tempfile.TemporaryDirectory(prefix="verification-scan-") as directory:
        os.chmod(directory, 0o700)
        path = os.path.join(directory, "upload.bin")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        try:
            result = subprocess.run(
                [executable, "--no-summary", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(int(getattr(
                    settings, "COMPANY_VERIFICATION_DOCUMENT_SCAN_TIMEOUT_SECONDS", 30
                )), 1),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DocumentSecurityError("scanner_unavailable") from exc
    if result.returncode == 1:
        raise DocumentSecurityError("malware_detected")
    if result.returncode != 0:
        raise DocumentSecurityError("scanner_unavailable")
    return "clamav"


def _image_preview(content: bytes, expected_format: str) -> bytes:
    maximum_pixels = max(int(getattr(
        settings, "COMPANY_VERIFICATION_DOCUMENT_MAX_PIXELS", 40_000_000
    )), 1)
    try:
        with Image.open(io.BytesIO(content)) as candidate:
            if candidate.format != expected_format or getattr(candidate, "n_frames", 1) != 1:
                raise DocumentSecurityError("document_format_mismatch")
            width, height = candidate.size
            if width <= 0 or height <= 0 or width * height > maximum_pixels:
                raise DocumentSecurityError("document_decode_limit")
            candidate.verify()
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            normalized = source.convert("RGB")
            output = io.BytesIO()
            normalized.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except DocumentSecurityError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise DocumentSecurityError("document_decode_failed") from exc


def _pdf_root_has_unsafe_features(reader: PdfReader) -> bool:
    root = reader.trailer.get("/Root")
    root = root.get_object() if hasattr(root, "get_object") else root
    if not isinstance(root, dict):
        return True
    if any(key in root for key in ("/OpenAction", "/AA", "/AcroForm")):
        return True
    names = root.get("/Names")
    names = names.get_object() if hasattr(names, "get_object") else names
    if isinstance(names, dict) and any(key in names for key in ("/EmbeddedFiles", "/JavaScript")):
        return True
    page = reader.pages[0]
    return any(key in page for key in ("/AA", "/Annots"))


def _render_pdf_preview(content: bytes) -> bytes:
    executable = shutil.which("pdftoppm")
    if not executable:
        raise DocumentSecurityError("pdf_renderer_unavailable")
    with tempfile.TemporaryDirectory(prefix="verification-pdf-") as directory:
        os.chmod(directory, 0o700)
        source = os.path.join(directory, "source.pdf")
        output_prefix = os.path.join(directory, "preview")
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        try:
            result = subprocess.run(
                [executable, "-f", "1", "-l", "1", "-singlefile", "-png", "-r", "144", source, output_prefix],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(int(getattr(
                    settings, "COMPANY_VERIFICATION_DOCUMENT_SCAN_TIMEOUT_SECONDS", 30
                )), 1),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DocumentSecurityError("pdf_renderer_unavailable") from exc
        preview_path = output_prefix + ".png"
        if result.returncode != 0 or not os.path.isfile(preview_path):
            raise DocumentSecurityError("pdf_render_failed")
        with open(preview_path, "rb") as stream:
            preview = stream.read(12 * 1024 * 1024 + 1)
    if len(preview) > 12 * 1024 * 1024:
        raise DocumentSecurityError("document_decode_limit")
    _image_preview(preview, "PNG")
    return preview


def inspect_authorization_document(
    upload_file,
    *,
    scanner: Callable[[bytes], str] | None = None,
    pdf_renderer: Callable[[bytes], bytes] | None = None,
) -> InspectedDocument:
    content = _read_upload(upload_file)
    scan_engine = (scanner or _clamav_scan)(content)
    if not scan_engine:
        raise DocumentSecurityError("scanner_unavailable")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type, extension = "image/png", "png"
        preview = _image_preview(content, "PNG")
        pages = 1
    elif content.startswith(b"\xff\xd8\xff"):
        mime_type, extension = "image/jpeg", "jpg"
        preview = _image_preview(content, "JPEG")
        pages = 1
    elif content.startswith(b"%PDF-"):
        mime_type, extension = "application/pdf", "pdf"
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise DocumentSecurityError("pdf_encrypted")
            pages = len(reader.pages)
            if pages != 1:
                raise DocumentSecurityError("pdf_page_count_invalid")
            if _pdf_root_has_unsafe_features(reader):
                raise DocumentSecurityError("pdf_active_content")
        except DocumentSecurityError:
            raise
        except Exception as exc:
            raise DocumentSecurityError("pdf_invalid") from exc
        preview = (pdf_renderer or _render_pdf_preview)(content)
        _image_preview(preview, "PNG")
    else:
        raise DocumentSecurityError("document_format_unsupported")
    return InspectedDocument(
        content=content,
        mime_type=mime_type,
        extension=extension,
        page_count=pages,
        sha256=hashlib.sha256(content).hexdigest(),
        preview=preview,
        scan_engine=str(scan_engine)[:64],
    )


def _storage_keys(application, material_revision: int, document_id, extension: str):
    base = (
        f"enterprise-verification/{application.provider_environment}/"
        f"{application.public_id}/{material_revision}/{document_id}"
    )
    return f"{base}/original.{extension}", f"{base}/preview.png"


def _mark_upload_failed(*, operation_id, scan_operation_id, document_id, code, now):
    with transaction.atomic():
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        scan_operation = VerificationOperation.objects.select_for_update().get(pk=scan_operation_id)
        document = VerificationDocument.objects.select_for_update().get(pk=document_id)
        operation.status = VerificationOperation.STATUS_FAILED
        operation.last_error_code = str(code)[:64]
        operation.lease_until = None
        operation.next_run_at = now + timedelta(seconds=30)
        operation.save(update_fields=(
            "status", "last_error_code", "lease_until", "next_run_at", "updated_at"
        ))
        scan_operation.status = VerificationOperation.STATUS_COMPLETED
        scan_operation.lease_until = None
        scan_operation.completed_at = now
        scan_operation.result = {"outcome": "passed", "adopted": False}
        scan_operation.save(update_fields=(
            "status", "lease_until", "completed_at", "result", "updated_at"
        ))
        document.scan_status = VerificationDocument.SCAN_FAILED
        document.scanned_at = now
        document.save(update_fields=("scan_status", "scanned_at"))


def _record_cleanup_operation(*, document_id, removed_count, cleanup_failed, now):
    with transaction.atomic():
        document = VerificationDocument.objects.select_for_update().select_related(
            "application", "application__company"
        ).get(pk=document_id)
        application = document.application
        request_hash = _request_hash({
            "document_id": str(document.id),
            "sha256": document.sha256,
            "action": "delete_partial_upload",
        })
        operation, _ = VerificationOperation.objects.update_or_create(
            company=application.company,
            operation_kind=VerificationOperation.KIND_DOCUMENT_CLEANUP,
            idempotency_key=f"cleanup:{document.id}",
            defaults={
                "application": application,
                "document": document,
                "application_revision": application.revision,
                "material_revision": document.material_revision,
                "request_hash": request_hash,
                "status": (
                    VerificationOperation.STATUS_FAILED
                    if cleanup_failed else VerificationOperation.STATUS_COMPLETED
                ),
                "attempts": 1,
                "next_run_at": now + timedelta(seconds=30) if cleanup_failed else None,
                "lease_until": None,
                "last_error_code": "storage_cleanup_failed" if cleanup_failed else "",
                "result": {"removed_count": removed_count, "pending": cleanup_failed},
                "completed_at": None if cleanup_failed else now,
            },
        )


def retry_document_cleanup(*, operation_id, storage=None, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        operation = VerificationOperation.objects.select_for_update().filter(
            pk=operation_id,
            operation_kind=VerificationOperation.KIND_DOCUMENT_CLEANUP,
        ).first()
        if not operation or not operation.document_id:
            return {"operation_id": str(operation_id), "cleaned": False, "reason": "not_found"}
        if operation.status == VerificationOperation.STATUS_COMPLETED:
            return {"operation_id": str(operation.id), "cleaned": True}
        if (
            operation.status == VerificationOperation.STATUS_RUNNING
            and operation.lease_until
            and operation.lease_until > now
        ):
            return {"operation_id": str(operation.id), "cleaned": False, "reason": "leased"}
        maximum = max(int(getattr(
            settings, "COMPANY_VERIFICATION_JOB_MAX_ATTEMPTS", 8
        )), 1)
        if operation.attempts >= maximum:
            operation.status = VerificationOperation.STATUS_FAILED
            operation.lease_until = None
            operation.next_run_at = None
            operation.last_error_code = "retry_exhausted"
            operation.save(update_fields=(
                "status", "lease_until", "next_run_at", "last_error_code", "updated_at"
            ))
            return {
                "operation_id": str(operation.id),
                "cleaned": False,
                "reason": "retry_exhausted",
            }
        operation.status = VerificationOperation.STATUS_RUNNING
        operation.attempts += 1
        operation.lease_until = now + timedelta(seconds=60)
        operation.next_run_at = None
        operation.save(update_fields=(
            "status", "attempts", "lease_until", "next_run_at", "updated_at"
        ))
        document = VerificationDocument.objects.select_for_update().get(
            pk=operation.document_id
        )
    failed = False
    removed = 0
    try:
        storage = storage or get_verification_storage()
        for storage_key in (document.storage_key, document.preview_storage_key):
            if not storage_key:
                continue
            storage.delete(storage_key)
            removed += 1
    except VerificationStorageError:
        failed = True
    with transaction.atomic():
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        operation.status = (
            VerificationOperation.STATUS_FAILED
            if failed else VerificationOperation.STATUS_COMPLETED
        )
        operation.lease_until = None
        exhausted = failed and operation.attempts >= maximum
        operation.next_run_at = (
            now + timedelta(seconds=30) if failed and not exhausted else None
        )
        operation.last_error_code = (
            "retry_exhausted" if exhausted else "storage_cleanup_failed" if failed else ""
        )
        operation.completed_at = None if failed else now
        operation.result = {"removed_count": removed, "pending": failed}
        operation.save(update_fields=(
            "status", "lease_until", "next_run_at", "last_error_code",
            "completed_at", "result", "updated_at"
        ))
    return {"operation_id": str(operation_id), "cleaned": not failed}


def _upload_verification_document(
    *,
    company_id: str,
    application_id: str,
    user,
    expected_revision: int,
    idempotency_key: str,
    upload_file,
    document_type: str,
    allowed_stages: set[str],
    next_stage: str,
    template_version: str,
    audit_action: str,
    storage=None,
    scanner=None,
    pdf_renderer=None,
    now=None,
) -> dict:
    fixed_now = now is not None
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise VerificationDomainError("invalid_request")
    inspection_started = time.monotonic()
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise VerificationDomainError("invalid_request") from exc
    _, preliminary_application = _application_for_user(
        company_id=company_id,
        application_id=application_id,
        user=user,
    )
    if (
        preliminary_application.status not in ACTIVE_STATUSES
        or preliminary_application.stage not in allowed_stages
    ):
        raise VerificationDomainError("transition_not_allowed", status_code=409)
    if preliminary_application.revision != expected_revision:
        raise VerificationDomainError("stale_revision", status_code=409)
    try:
        inspected = inspect_authorization_document(
            upload_file, scanner=scanner, pdf_renderer=pdf_renderer
        )
        if (
            document_type == VerificationDocument.TYPE_BUSINESS_LICENSE
            and inspected.mime_type not in {"image/jpeg", "image/png"}
        ):
            raise DocumentSecurityError("business_license_image_required")
    except DocumentSecurityError as exc:
        raise VerificationDomainError("document_invalid", status_code=400) from exc
    inspection_seconds = time.monotonic() - inspection_started
    now = now or timezone.now()
    operation_id = scan_operation_id = document_id = None
    with transaction.atomic():
        company, application = _application_for_user(
            company_id=company_id, application_id=application_id, user=user, lock=True
        )
        if application.status not in ACTIVE_STATUSES or application.stage not in allowed_stages:
            raise VerificationDomainError("transition_not_allowed", status_code=409)
        if application.revision != expected_revision:
            raise VerificationDomainError("stale_revision", status_code=409)
        request_hash = _request_hash({
            "application_id": str(application.public_id),
            "revision": application.revision,
            "sha256": inspected.sha256,
            "template_version": template_version,
            "document_type": document_type,
        })
        existing = VerificationOperation.objects.select_for_update().filter(
            company=company,
            operation_kind=VerificationOperation.KIND_DOCUMENT_UPLOAD,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.request_hash != request_hash:
                raise VerificationDomainError("idempotency_conflict", status_code=409)
            if existing.status == VerificationOperation.STATUS_COMPLETED:
                result = dict(existing.result or {})
                result["application"] = serialize_manual_verification(existing.application)
                return result
            raise VerificationDomainError("operation_in_progress", status_code=409)
        material_revision = application.material_revision + 1
        document_id = uuid.uuid4()
        original_key, preview_key = _storage_keys(
            application, material_revision, document_id, inspected.extension
        )
        document = VerificationDocument.objects.create(
            id=document_id,
            application=application,
            material_revision=material_revision,
            document_type=document_type,
            template_version=template_version,
            storage_key=original_key,
            preview_storage_key=preview_key,
            sha256=inspected.sha256,
            size_bytes=len(inspected.content),
            mime_type=inspected.mime_type,
            page_count=inspected.page_count,
            uploaded_by=user,
            scan_status=VerificationDocument.SCAN_PENDING,
        )
        operation = VerificationOperation.objects.create(
            company=company,
            application=application,
            document=document,
            operation_kind=VerificationOperation.KIND_DOCUMENT_UPLOAD,
            application_revision=application.revision,
            material_revision=material_revision,
            idempotency_key=key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_RUNNING,
            attempts=1,
            lease_until=now + timedelta(seconds=60),
        )
        operation_id = operation.id
        scan_operation = VerificationOperation.objects.create(
            company=company,
            application=application,
            document=document,
            operation_kind=VerificationOperation.KIND_DOCUMENT_SCAN,
            application_revision=application.revision,
            material_revision=material_revision,
            idempotency_key=f"scan:{document.id}:{document.sha256}",
            request_hash=_request_hash({
                "document_id": str(document.id),
                "sha256": document.sha256,
                "scan_engine": inspected.scan_engine,
            }),
            status=VerificationOperation.STATUS_RUNNING,
            attempts=1,
            lease_until=now + timedelta(seconds=60),
        )
        scan_operation_id = scan_operation.id
    stored_keys = []
    upload_durations = {}
    upload_started = time.monotonic()
    try:
        storage = storage or get_verification_storage()
        upload_specs = {
            "original": (original_key, inspected.content, "application/octet-stream"),
            "preview": (preview_key, inspected.preview, "image/png"),
        }

        def put_object(kind, spec):
            object_started = time.monotonic()
            stored = storage.put(spec[0], spec[1], content_type=spec[2])
            return kind, stored, time.monotonic() - object_started

        uploaded = {}
        if isinstance(storage, PrivateCosVerificationStorage):
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="verification-cos") as pool:
                futures = [pool.submit(put_object, kind, spec) for kind, spec in upload_specs.items()]
                first_error = None
                for future in as_completed(futures):
                    try:
                        kind, stored, duration = future.result()
                        uploaded[kind] = stored
                        upload_durations[kind] = duration
                        stored_keys.append(stored.key)
                    except VerificationStorageError as exc:
                        first_error = first_error or exc
                if first_error:
                    raise first_error
        else:
            for kind, spec in upload_specs.items():
                kind, stored, duration = put_object(kind, spec)
                uploaded[kind] = stored
                upload_durations[kind] = duration
                stored_keys.append(stored.key)
        original = uploaded["original"]
        preview = uploaded["preview"]
    except VerificationStorageError as exc:
        cleanup_failed = False
        removed_count = 0
        for stored_key in reversed(stored_keys):
            try:
                storage.delete(stored_key)
                removed_count += 1
            except VerificationStorageError:
                cleanup_failed = True
        _mark_upload_failed(
            operation_id=operation_id,
            scan_operation_id=scan_operation_id,
            document_id=document_id,
            code=exc.code,
            now=(now if fixed_now else timezone.now()),
        )
        if stored_keys:
            _record_cleanup_operation(
                document_id=document_id,
                removed_count=removed_count,
                cleanup_failed=cleanup_failed,
                now=now,
            )
        raise VerificationDomainError("document_storage_unavailable", status_code=503) from exc

    completed_at = now if fixed_now else timezone.now()
    upload_wall_seconds = time.monotonic() - upload_started
    logger.info(
        "[company-verification-upload] document_type=%s size_bytes=%s inspect_ms=%s "
        "original_put_ms=%s preview_put_ms=%s total_put_ms=%s",
        document_type,
        len(inspected.content),
        round(inspection_seconds * 1000),
        round(upload_durations.get("original", 0) * 1000),
        round(upload_durations.get("preview", 0) * 1000),
        round(upload_wall_seconds * 1000),
    )

    with transaction.atomic():
        company, application = _application_for_user(
            company_id=company_id, application_id=application_id, user=user, lock=True
        )
        operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
        scan_operation = VerificationOperation.objects.select_for_update().get(pk=scan_operation_id)
        document = VerificationDocument.objects.select_for_update().get(pk=document_id)
        current = (
            application.status in ACTIVE_STATUSES
            and application.stage in allowed_stages
            and application.revision == operation.application_revision
            and application.material_revision + 1 == operation.material_revision
        )
        document.storage_version = original.version
        document.preview_storage_version = preview.version
        document.scan_status = (
            VerificationDocument.SCAN_PASSED
            if current
            else VerificationDocument.SCAN_QUARANTINED
        )
        document.scanned_at = completed_at
        document.save(update_fields=(
            "storage_version", "preview_storage_version", "scan_status", "scanned_at"
        ))
        check = VerificationCheck.objects.create(
            application=application,
            kind=(
                VerificationCheck.KIND_BUSINESS_LICENSE_SECURITY
                if document_type == VerificationDocument.TYPE_BUSINESS_LICENSE
                else VerificationCheck.KIND_DOCUMENT_SECURITY
            ),
            attempt_no=1,
            input_digest=scan_operation.request_hash,
            material_revision=document.material_revision,
            document=document,
            provider="local_file_security",
            action="inspect_and_scan",
            provider_environment=application.provider_environment,
            status=(VerificationCheck.STATUS_PASSED if current else VerificationCheck.STATUS_FAILED),
            provider_code=("document_safe" if current else "stale_upload"),
            result={
                "adopted": current,
                "mime_type": document.mime_type,
                "page_count": document.page_count,
                "scan_engine": inspected.scan_engine,
                "sha256": document.sha256,
            },
            rule_version=application.policy_version,
            checked_at=completed_at,
            valid_until=(completed_at + timedelta(seconds=max(int(getattr(
                settings, "COMPANY_VERIFICATION_DOCUMENT_CHECK_MAX_AGE_SECONDS", 86400
            )), 1))) if current else None,
        )
        scan_operation.status = VerificationOperation.STATUS_COMPLETED
        scan_operation.lease_until = None
        scan_operation.completed_at = completed_at
        scan_operation.result = {
            "check_id": str(check.id),
            "outcome": "passed",
            "adopted": current,
        }
        scan_operation.save(update_fields=(
            "status", "lease_until", "completed_at", "result", "updated_at"
        ))
        result = {
            "document_id": str(document.id),
            "document_type": document.document_type,
            "material_revision": document.material_revision,
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
            "scan_status": document.scan_status,
            "adopted": current,
        }
        if current:
            application.material_revision = document.material_revision
            application.stage = next_stage
            update_fields = ["material_revision", "stage", "revision"]
            if document_type == VerificationDocument.TYPE_BUSINESS_LICENSE:
                application.failure_code = ""
                application.public_reason = ""
                update_fields.extend(("failure_code", "public_reason"))
            application.revision += 1
            application.save(update_fields=update_fields)
        operation.status = VerificationOperation.STATUS_COMPLETED
        operation.lease_until = None
        operation.result = {**result, "security_check_id": str(check.id)}
        operation.completed_at = completed_at
        operation.save(update_fields=(
            "status", "lease_until", "result", "completed_at", "updated_at"
        ))
        VerificationAuditEvent.objects.create(
            company=company,
            application=application,
            actor=user,
            action=audit_action,
            revision=application.revision,
            detail={**result, "sha256": document.sha256},
        )
        if not current:
            _record_cleanup_operation(
                document_id=document.id,
                removed_count=0,
                cleanup_failed=True,
                now=now,
            )
        result["application"] = serialize_manual_verification(application)
        return result


def upload_authorization_document(
    *, company_id: str, application_id: str, user, expected_revision: int,
    idempotency_key: str, upload_file, storage=None, scanner=None,
    pdf_renderer=None, now=None,
) -> dict:
    application = _application_for_user(
        company_id=company_id,
        application_id=application_id,
        user=user,
    )[1]
    if (
        application.applicant_role
        == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
        and not (
            application.applicant_name_snapshot
            and application.applicant_id_type == "resident_identity_card"
            and application.applicant_id_encrypted
            and application.applicant_id_hmac
        )
    ):
        raise VerificationDomainError("applicant_identity_required", status_code=409)
    return _upload_verification_document(
        company_id=company_id,
        application_id=application_id,
        user=user,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        upload_file=upload_file,
        document_type=VerificationDocument.TYPE_AUTHORIZATION,
        allowed_stages=ALLOWED_UPLOAD_STAGES,
        next_stage=CompanyVerification.STAGE_DOCUMENT_CHECK,
        template_version=_template_context(
            _application_for_user(
                company_id=company_id,
                application_id=application_id,
                user=user,
            )[1]
        )["template_version"],
        audit_action="authorization_document_uploaded",
        storage=storage,
        scanner=scanner,
        pdf_renderer=pdf_renderer,
        now=now,
    )


def upload_business_license_document(
    *, company_id: str, application_id: str, user, expected_revision: int,
    idempotency_key: str, upload_file, storage=None, scanner=None,
    pdf_renderer=None, now=None,
) -> dict:
    return _upload_verification_document(
        company_id=company_id,
        application_id=application_id,
        user=user,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        upload_file=upload_file,
        document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
        allowed_stages={
            CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD,
            CompanyVerification.STAGE_COMPANY_CHECK,
        },
        next_stage=CompanyVerification.STAGE_COMPANY_CHECK,
        template_version="business-license-v1",
        audit_action="business_license_uploaded",
        storage=storage,
        scanner=scanner,
        pdf_renderer=pdf_renderer,
        now=now,
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(value or "")).upper()


_RESIDENT_ID_WITH_SPACES = re.compile(
    r"(?<![0-9A-Za-z])(?:\d[\s]*){17}[\dXx](?![0-9A-Za-z])"
)
_PERSON_NAME = re.compile(r"[\u4e00-\u9fff][\u4e00-\u9fff\u00b7]{1,31}")


def _line_texts(lines) -> list[str]:
    return [str(line.get("text", "")).strip() for line in lines]


def _extract_authorized_person_identity(lines) -> dict:
    texts = _line_texts(lines)
    identity_number = ""
    for value in texts:
        compact = re.sub(r"\s+", "", value).upper()
        for candidate in re.findall(r"(?<![0-9A-Za-z])\d{17}[\dX](?![0-9A-Za-z])", compact):
            if _valid_resident_identity_card(candidate):
                identity_number = candidate
                break
        if identity_number:
            break

    name = ""
    for index, value in enumerate(texts):
        if "经办人姓名" not in value and not re.search(r"(^|[：:])姓名", value):
            continue
        labeled = re.search(
            r"(?:经办人姓名|姓名)[^：:]{0,20}[：:]\s*"
            r"([\u4e00-\u9fff][\u4e00-\u9fff\u00b7]{1,31})",
            value,
        )
        if labeled:
            name = labeled.group(1)
            break
        candidate = re.sub(r"^.*?(?:经办人姓名|姓名)[^：:]*[：:]?", "", value).strip()
        candidate = re.sub(r"[\s_＿-]", "", candidate)
        match = _PERSON_NAME.fullmatch(candidate)
        if match:
            name = match.group(0)
            break
        if index + 1 < len(texts):
            candidate = re.sub(r"[\s_＿-]", "", texts[index + 1])
            match = _PERSON_NAME.fullmatch(candidate)
            if match:
                name = match.group(0)
                break
    return {"name": name, "identity_number": identity_number}


def _redacted_ocr_lines(lines) -> list[dict]:
    redacted = []
    for line in lines:
        item = dict(line)
        item["text"] = _RESIDENT_ID_WITH_SPACES.sub("[身份证号已加密]", str(item.get("text", "")))
        redacted.append(item)
    return redacted


def _identity_payload(identity: dict) -> dict:
    protected = identity.get("evidence_protected")
    if not protected:
        return {}
    return {
        "name": str(identity.get("name") or ""),
        "id_type": "resident_identity_card",
        "id_encrypted": protected.ciphertext,
        "id_masked": protected.masked,
        "id_hmac": protected.digest,
        "id_key_version": protected.key_version,
    }


def _identity_from_check(check, *, protector, application, document) -> dict:
    payload = check.result if isinstance(check.result, dict) else {}
    identity = payload.get("applicant_identity")
    if not isinstance(identity, dict) or not identity.get("id_encrypted"):
        return {}
    identity_number = protector.decrypt(
        identity["id_encrypted"],
        context=f"authorization:{application.public_id}:{document.id}",
    )
    if not _valid_resident_identity_card(identity_number):
        raise VerificationCryptoError("invalid_authorization_identity")
    protected = protector.protect(identity_number, context=str(application.public_id))
    return {
        "name": str(identity.get("name") or "").strip(),
        "identity_number": identity_number,
        "application_protected": protected,
    }


def _consistency_result(application, text_result, seal_result, identity: dict | None = None) -> dict:
    text = _normalize_text("\n".join(str(line.get("text", "")) for line in text_result.lines))
    digits = re.sub(r"\D", "", text)
    context = _template_context(application)
    fields = {
        "company_name": _normalize_text(context["company_name"]) in text,
        "registration_no": _normalize_text(context["registration_no"]) in text,
        "application_id": _normalize_text(context["application_id"]) in text,
        "template_version": _normalize_text(context["template_version"]) in text,
        "authorization_scope": all(token in text for token in ("企业认证", "提交", "材料")),
        "issued_on": context["issued_on"].replace("-", "") in digits,
        "valid_until": context["valid_until"].replace("-", "") in digits,
    }
    company = _normalize_text(context["company_name"])
    seal_matches = [company in _normalize_text(item.get("text", "")) for item in seal_result.seals]
    fields["seal_company_name"] = any(seal_matches)
    if application.applicant_role == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON:
        identity = identity or {}
        recognized_name = str(identity.get("name") or "").strip()
        protected = identity.get("application_protected")
        expected_name = str(application.applicant_name_snapshot or "").strip()
        fields["applicant_name"] = bool(recognized_name) and (
            not expected_name
            or _normalize_text(recognized_name) == _normalize_text(expected_name)
        )
        fields["applicant_identity"] = bool(protected) and (
            not application.applicant_id_hmac
            or hmac.compare_digest(protected.digest, application.applicant_id_hmac)
        )
    else:
        fields["applicant_name"] = _normalize_text(context["applicant_name"]) in text
    technical_unknown = text_result.outcome != "passed" or seal_result.outcome != "passed"
    mismatches = [name for name, matched in fields.items() if not matched]
    passed = not technical_unknown and not mismatches
    employee_registration = (
        application.applicant_role == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
    )
    return {
        "outcome": "passed" if passed else "unknown",
        "fields": fields,
        "mismatches": mismatches,
        "pre_review_status": (
            "not_required"
            if passed
            else "technical_retry"
            if technical_unknown or employee_registration
            else "required"
        ),
        "pre_review_reasons": (
            ["ocr_technical_unknown"] if technical_unknown else mismatches
        ),
        "technical_unknown": technical_unknown,
        "manual_transcription_allowed": not technical_unknown and not employee_registration,
        "manual_transcription_is_final_approval": False,
    }


def _create_document_operation(application, document, kind, action, now):
    idempotency_key = f"{kind}:{document.id}:{document.sha256}"
    request_hash = _request_hash({
        "application_id": str(application.public_id),
        "material_revision": document.material_revision,
        "document_id": str(document.id),
        "sha256": document.sha256,
        "action": action,
    })
    operation = VerificationOperation.objects.select_for_update().filter(
        company=application.company,
        operation_kind=kind,
        idempotency_key=idempotency_key,
    ).first()
    if operation and operation.status == VerificationOperation.STATUS_COMPLETED:
        return operation, False
    if not operation:
        operation = VerificationOperation.objects.create(
            company=application.company,
            application=application,
            document=document,
            operation_kind=kind,
            application_revision=application.revision,
            material_revision=document.material_revision,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status=VerificationOperation.STATUS_RUNNING,
            attempts=1,
            lease_until=now + timedelta(seconds=60),
        )
    else:
        if (
            operation.status == VerificationOperation.STATUS_RUNNING
            and operation.lease_until
            and operation.lease_until > now
        ):
            return operation, False
        maximum = max(int(getattr(
            settings, "COMPANY_VERIFICATION_JOB_MAX_ATTEMPTS", 8
        )), 1)
        if operation.attempts >= maximum:
            operation.status = VerificationOperation.STATUS_FAILED
            operation.lease_until = None
            operation.next_run_at = None
            operation.last_error_code = "retry_exhausted"
            operation.save(update_fields=(
                "status", "lease_until", "next_run_at", "last_error_code", "updated_at"
            ))
            return operation, False
        operation.status = VerificationOperation.STATUS_RUNNING
        operation.attempts += 1
        operation.lease_until = now + timedelta(seconds=60)
        operation.next_run_at = None
        operation.last_error_code = ""
        operation.save(update_fields=(
            "status", "attempts", "lease_until", "next_run_at", "last_error_code", "updated_at"
        ))
    return operation, True


def _provider_check(
    application, document, *, kind, action, result, operation, adopted, now,
    identity=None,
):
    attempt = (application.checks.filter(kind=kind, document=document)
               .order_by("-attempt_no").values_list("attempt_no", flat=True).first() or 0) + 1
    status = (
        VerificationCheck.STATUS_PASSED
        if result.outcome == "passed" and adopted
        else VerificationCheck.STATUS_UNKNOWN
    )
    payload = {
        "outcome": result.outcome,
        "retryable": result.retryable,
        "adopted": adopted,
    }
    if hasattr(result, "lines"):
        payload["lines"] = _redacted_ocr_lines(result.lines)
        if identity:
            payload["applicant_identity"] = _identity_payload(identity)
    if hasattr(result, "seals"):
        payload["seals"] = list(result.seals)
    return VerificationCheck.objects.create(
        application=application,
        kind=kind,
        attempt_no=attempt,
        input_digest=operation.request_hash,
        material_revision=document.material_revision,
        document=document,
        provider=application.provider,
        action=action,
        provider_environment=application.provider_environment,
        status=status,
        provider_code=str(result.provider_code or "")[:128],
        request_id=str(result.request_id or "")[:255],
        result=payload,
        rule_version=application.policy_version,
        checked_at=now,
        valid_until=(now + timedelta(seconds=max(int(getattr(
            settings, "COMPANY_VERIFICATION_DOCUMENT_CHECK_MAX_AGE_SECONDS", 86400
        )), 1))) if status == VerificationCheck.STATUS_PASSED else None,
    )


def _text_result_from_check(check):
    payload = check.result if isinstance(check.result, dict) else {}
    return DocumentTextExtractionResult(
        outcome=str(payload.get("outcome", "unknown")),
        retryable=bool(payload.get("retryable", False)),
        lines=tuple(payload.get("lines") or ()),
        provider_code=check.provider_code,
        request_id=check.request_id,
    )


def _seal_result_from_check(check):
    payload = check.result if isinstance(check.result, dict) else {}
    return SealExtractionResult(
        outcome=str(payload.get("outcome", "unknown")),
        retryable=bool(payload.get("retryable", False)),
        seals=tuple(payload.get("seals") or ()),
        provider_code=check.provider_code,
        request_id=check.request_id,
    )


def process_authorization_document(
    *, document_id: str, storage=None, text_reader=None, seal_reader=None,
    protector=None, now=None
) -> dict:
    now = now or timezone.now()
    with transaction.atomic():
        document = VerificationDocument.objects.select_for_update().select_related(
            "application", "application__company"
        ).filter(pk=document_id).first()
        if not document:
            raise VerificationDomainError("document_not_found", status_code=404)
        application = CompanyVerification.objects.select_for_update().get(pk=document.application_id)
        security_check = application.checks.filter(
            kind=VerificationCheck.KIND_DOCUMENT_SECURITY,
            document=document,
            material_revision=document.material_revision,
            status=VerificationCheck.STATUS_PASSED,
        ).order_by("-checked_at", "-created_at").first()
        current = (
            application.status in ACTIVE_STATUSES
            and application.stage == CompanyVerification.STAGE_DOCUMENT_CHECK
            and application.material_revision == document.material_revision
            and document.scan_status == VerificationDocument.SCAN_PASSED
            and security_check is not None
            and security_check.valid_until is not None
            and security_check.valid_until > now
            and isinstance(security_check.result, dict)
            and security_check.result.get("adopted") is True
            and security_check.result.get("sha256") == document.sha256
        )
        if not current:
            return {"document_id": str(document.id), "adopted": False, "reason": "stale_document"}
        text_operation, text_call = _create_document_operation(
            application, document, VerificationOperation.KIND_DOCUMENT_TEXT_OCR,
            "GeneralAccurateOCR", now,
        )
        seal_operation, seal_call = _create_document_operation(
            application, document, VerificationOperation.KIND_SEAL_OCR, "SealOCR", now
        )
        if not text_call and not seal_call:
            consistency = application.checks.filter(
                kind=VerificationCheck.KIND_CONSISTENCY, document=document
            ).order_by("-created_at").first()
            return {
                "document_id": str(document.id),
                "adopted": bool(consistency and consistency.result.get("adopted")),
                "pre_review_status": (
                    consistency.result.get("pre_review_status") if consistency else "unknown"
                ),
            }
    try:
        storage = storage or get_verification_storage()
        ocr_storage_key = document.storage_key
        ocr_mime_type = document.mime_type
        ocr_max_bytes = max(
            int(getattr(settings, "COMPANY_VERIFICATION_MAX_FILE_BYTES", 5 * 1024 * 1024)),
            1,
        )
        if document.mime_type == "application/pdf":
            if not document.preview_storage_key:
                raise VerificationProviderError(
                    "PDF document has no safe image preview",
                    code="document_preview_missing",
                )
            ocr_storage_key = document.preview_storage_key
            ocr_mime_type = "image/png"
            ocr_max_bytes = 12 * 1024 * 1024
        content = storage.get(
            ocr_storage_key,
            max_bytes=ocr_max_bytes,
        )
        request = DocumentExtractionRequest(content=content, mime_type=ocr_mime_type)
        if text_call:
            text_result = (
                text_reader or get_authorization_document_reader()
            ).extract_document_text(request)
        else:
            text_check = application.checks.filter(
                pk=(text_operation.result or {}).get("check_id"),
                kind=VerificationCheck.KIND_DOCUMENT_TEXT,
                document=document,
            ).first()
            if text_check is None:
                raise VerificationProviderError(
                    "Completed text operation has no evidence",
                    code="document_evidence_missing",
                )
            text_result = _text_result_from_check(text_check)
        if seal_call:
            seal_result = (seal_reader or get_seal_reader()).extract_seal(request)
        else:
            seal_check = application.checks.filter(
                pk=(seal_operation.result or {}).get("check_id"),
                kind=VerificationCheck.KIND_SEAL,
                document=document,
            ).first()
            if seal_check is None:
                raise VerificationProviderError(
                    "Completed seal operation has no evidence",
                    code="document_evidence_missing",
                )
            seal_result = _seal_result_from_check(seal_check)
        recognized_identity = {}
        if application.applicant_role == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON:
            protector = protector or get_identity_protector()
            if text_call:
                extracted = _extract_authorized_person_identity(text_result.lines)
                if extracted["name"] and extracted["identity_number"]:
                    recognized_identity = {
                        **extracted,
                        "evidence_protected": protector.protect(
                            extracted["identity_number"],
                            context=f"authorization:{application.public_id}:{document.id}",
                        ),
                        "application_protected": protector.protect(
                            extracted["identity_number"],
                            context=str(application.public_id),
                        ),
                    }
            else:
                recognized_identity = _identity_from_check(
                    text_check,
                    protector=protector,
                    application=application,
                    document=document,
                )
    except (VerificationProviderError, VerificationStorageError, VerificationCryptoError) as exc:
        code = str(getattr(exc, "code", "provider_unavailable") or "provider_unavailable")[:64]
        with transaction.atomic():
            retry_operation_ids = []
            if text_call:
                retry_operation_ids.append(text_operation.id)
            if seal_call:
                retry_operation_ids.append(seal_operation.id)
            for operation_id in retry_operation_ids:
                operation = VerificationOperation.objects.select_for_update().get(pk=operation_id)
                exhausted = operation.attempts >= max(int(getattr(
                    settings, "COMPANY_VERIFICATION_JOB_MAX_ATTEMPTS", 8
                )), 1)
                operation.status = VerificationOperation.STATUS_FAILED
                operation.last_error_code = "retry_exhausted" if exhausted else code
                operation.lease_until = None
                operation.next_run_at = None if exhausted else now + timedelta(seconds=30)
                operation.save(update_fields=(
                    "status", "last_error_code", "lease_until", "next_run_at", "updated_at"
                ))
        raise VerificationDomainError("provider_unavailable", status_code=503) from exc

    with transaction.atomic():
        document = VerificationDocument.objects.select_for_update().get(pk=document_id)
        application = CompanyVerification.objects.select_for_update().select_related("company").get(
            pk=document.application_id
        )
        text_operation = VerificationOperation.objects.select_for_update().get(pk=text_operation.id)
        seal_operation = VerificationOperation.objects.select_for_update().get(pk=seal_operation.id)
        current = (
            application.status in ACTIVE_STATUSES
            and application.stage == CompanyVerification.STAGE_DOCUMENT_CHECK
            and application.material_revision == document.material_revision
            and text_operation.material_revision == document.material_revision
            and seal_operation.material_revision == document.material_revision
            and text_operation.document_id == document.id
            and seal_operation.document_id == document.id
        )
        if text_call:
            text_check = _provider_check(
                application, document, kind=VerificationCheck.KIND_DOCUMENT_TEXT,
                action="GeneralAccurateOCR", result=text_result, operation=text_operation,
                adopted=current, now=now, identity=recognized_identity,
            )
        else:
            text_check = VerificationCheck.objects.select_for_update().get(
                pk=(text_operation.result or {}).get("check_id")
            )
        if seal_call:
            seal_check = _provider_check(
                application, document, kind=VerificationCheck.KIND_SEAL,
                action="SealOCR", result=seal_result, operation=seal_operation,
                adopted=current, now=now,
            )
        else:
            seal_check = VerificationCheck.objects.select_for_update().get(
                pk=(seal_operation.result or {}).get("check_id")
            )
        consistency = _consistency_result(
            application, text_result, seal_result, identity=recognized_identity
        )
        consistency["adopted"] = current
        consistency["source_check_ids"] = [str(text_check.id), str(seal_check.id)]
        consistency_check = VerificationCheck.objects.create(
            application=application,
            kind=VerificationCheck.KIND_CONSISTENCY,
            attempt_no=(application.checks.filter(
                kind=VerificationCheck.KIND_CONSISTENCY, document=document
            ).count() + 1),
            input_digest=_request_hash({
                "document": str(document.id),
                "sha256": document.sha256,
                "text_check": str(text_check.id),
                "seal_check": str(seal_check.id),
            }),
            material_revision=document.material_revision,
            document=document,
            provider="touming_rules",
            action="authorization_consistency_v1",
            provider_environment=application.provider_environment,
            status=(
                VerificationCheck.STATUS_PASSED
                if current and consistency["outcome"] == "passed"
                else VerificationCheck.STATUS_UNKNOWN
            ),
            provider_code=(
                "authorization_consistent"
                if consistency["outcome"] == "passed"
                else "document_pre_review_required"
            ),
            result=consistency,
            rule_version=application.policy_version,
            checked_at=now,
            valid_until=(now + timedelta(seconds=max(int(getattr(
                settings, "COMPANY_VERIFICATION_DOCUMENT_CHECK_MAX_AGE_SECONDS", 86400
            )), 1))) if current and consistency["outcome"] == "passed" else None,
        )
        for operation, check, provider_result, was_called in (
            (text_operation, text_check, text_result, text_call),
            (seal_operation, seal_check, seal_result, seal_call),
        ):
            if not was_called:
                continue
            retryable = bool(provider_result.retryable)
            operation.status = (
                VerificationOperation.STATUS_FAILED
                if retryable else VerificationOperation.STATUS_COMPLETED
            )
            operation.lease_until = None
            exhausted = retryable and operation.attempts >= max(int(getattr(
                settings, "COMPANY_VERIFICATION_JOB_MAX_ATTEMPTS", 8
            )), 1)
            operation.next_run_at = (
                now + timedelta(seconds=30) if retryable and not exhausted else None
            )
            operation.last_error_code = (
                "retry_exhausted"
                if exhausted
                else str(provider_result.provider_code or "document_ocr_retry")[:64]
                if retryable
                else ""
            )
            operation.completed_at = None if retryable else now
            operation.result = {"check_id": str(check.id), "adopted": current}
            operation.save(update_fields=(
                "status", "lease_until", "next_run_at", "last_error_code",
                "completed_at", "result", "updated_at"
            ))
        if current and consistency_check.status == VerificationCheck.STATUS_PASSED:
            update_fields = ["stage", "revision"]
            if application.applicant_role == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON:
                if not application.applicant_id_hmac:
                    protected = recognized_identity["application_protected"]
                    application.applicant_name_snapshot = recognized_identity["name"]
                    application.applicant_id_type = "resident_identity_card"
                    application.applicant_id_encrypted = protected.ciphertext
                    application.applicant_id_masked = protected.masked
                    application.applicant_id_hmac = protected.digest
                    application.applicant_id_key_version = protected.key_version
                    update_fields.extend((
                        "applicant_name_snapshot", "applicant_id_type",
                        "applicant_id_encrypted", "applicant_id_masked",
                        "applicant_id_hmac", "applicant_id_key_version",
                    ))
                application.stage = (
                    CompanyVerification.STAGE_READY_TO_SUBMIT
                    if application.checks.filter(
                        kind=VerificationCheck.KIND_FACE,
                        status=VerificationCheck.STATUS_PASSED,
                        valid_until__gt=now,
                    ).exists()
                    else CompanyVerification.STAGE_APPLICANT_FACE
                )
            else:
                application.stage = CompanyVerification.STAGE_READY_TO_SUBMIT
            application.revision += 1
            application.failure_code = ""
            application.public_reason = ""
            update_fields.extend(("failure_code", "public_reason"))
            application.save(update_fields=update_fields)
        elif (
            current
            and application.applicant_role
            == CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
            and not consistency["technical_unknown"]
        ):
            application.stage = CompanyVerification.STAGE_SUPPLEMENT_REQUIRED
            application.revision += 1
            application.failure_code = "authorization_identity_not_recognized"
            application.public_reason = (
                "授权书未能完整识别经办人姓名、有效居民身份证号或企业公章，"
                "请按模板清晰填写并重新上传"
            )
            application.save(update_fields=(
                "stage", "revision", "failure_code", "public_reason"
            ))
        VerificationAuditEvent.objects.create(
            company=application.company,
            application=application,
            action="authorization_document_checked",
            revision=application.revision,
            detail={
                "document_id": str(document.id),
                "material_revision": document.material_revision,
                "adopted": current,
                "consistency_check_id": str(consistency_check.id),
                "pre_review_status": consistency["pre_review_status"],
            },
        )
        return {
            "document_id": str(document.id),
            "adopted": current,
            "pre_review_status": consistency["pre_review_status"],
            "application": serialize_manual_verification(application),
        }


def get_authorization_preview(
    *, company_id: str, application_id: str, document_id: str, user, storage=None
):
    company, application = _application_for_user(
        company_id=company_id, application_id=application_id, user=user
    )
    document = application.documents.filter(
        pk=document_id,
        document_type=VerificationDocument.TYPE_AUTHORIZATION,
    ).first()
    if not document:
        raise VerificationDomainError("document_not_found", status_code=404)
    if document.scan_status != VerificationDocument.SCAN_PASSED:
        raise VerificationDomainError("document_unavailable", status_code=409)
    try:
        storage = storage or get_verification_storage()
        content = storage.get(document.preview_storage_key, max_bytes=12 * 1024 * 1024)
    except VerificationStorageError as exc:
        raise VerificationDomainError("document_storage_unavailable", status_code=503) from exc
    VerificationAuditEvent.objects.create(
        company=company,
        application=application,
        actor=user,
        action="authorization_document_previewed",
        revision=application.revision,
        detail={"document_id": str(document.id), "material_revision": document.material_revision},
    )
    return content
