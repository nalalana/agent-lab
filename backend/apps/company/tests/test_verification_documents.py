import base64
import io
import json
import time

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import F
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image
from pypdf import PdfReader, PdfWriter

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationOperation,
)
from apps.company.providers.manual import (
    DocumentTextExtractionResult,
    SealExtractionResult,
)
from apps.company.verification_documents import (
    DocumentSecurityError,
    authorization_expected_values,
    generate_authorization_template,
    get_authorization_preview,
    inspect_authorization_document,
    process_authorization_document,
    retry_document_cleanup,
    upload_authorization_document,
)
from apps.company.verification_domain import (
    ManualVerificationStartInput,
    VerificationDomainError,
    create_manual_verification,
)
from apps.company.verification_crypto import get_identity_protector
from apps.company.verification_storage import (
    StoredVerificationObject,
    VerificationStorageError,
)
from apps.iam.models import Grant
from apps.user.models import User


KEY = base64.b64encode(b"e" * 32).decode()
HMAC_KEY = base64.b64encode(b"h" * 32).decode()
SETTINGS = {
    "COMPANY_VERIFICATION_ENABLED": True,
    "COMPANY_VERIFICATION_PROVIDER": "tencent_manual_v1",
    "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT": "mock",
    "COMPANY_VERIFICATION_POLICY_VERSION": "manual_v1",
    "COMPANY_VERIFICATION_REGISTRY_MODE": "two_elements",
    "COMPANY_VERIFICATION_ALLOW_MOCK": True,
    "COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY": KEY,
    "COMPANY_VERIFICATION_TEST_HMAC_KEY": HMAC_KEY,
    "COMPANY_VERIFICATION_TEST_KEY_VERSION": "ev4-test-v1",
    "COMPANY_VERIFICATION_AUTHORIZATION_TEMPLATE_VERSION": "authorization-v1-draft",
}


def png_bytes(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (80, 60), color=color).save(output, format="PNG")
    return output.getvalue()


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.deleted = []

    def put(self, key, content, *, content_type):
        self.put_calls.append((key, content_type))
        self.objects[key] = bytes(content)
        return StoredVerificationObject(key=key, version=f"version-{len(self.put_calls)}")

    def get(self, key, *, max_bytes):
        content = self.objects[key]
        if len(content) > max_bytes:
            raise AssertionError("test object exceeds requested bound")
        return content

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


class PartialFailureStorage(FakeStorage):
    def __init__(self):
        super().__init__()
        self.fail_delete = True

    def put(self, key, content, *, content_type):
        if len(self.put_calls) == 1:
            self.put_calls.append((key, content_type))
            raise VerificationStorageError("storage_write_failed")
        return super().put(key, content, content_type=content_type)

    def delete(self, key):
        if self.fail_delete:
            raise VerificationStorageError("storage_delete_failed")
        super().delete(key)


class StaleAfterWriteStorage(FakeStorage):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def put(self, key, content, *, content_type):
        result = super().put(key, content, content_type=content_type)
        if len(self.put_calls) == 2:
            self.callback()
        return result


class TextReader:
    def __init__(self, result, callback=None):
        self.result = result
        self.callback = callback
        self.requests = []

    def extract_document_text(self, request):
        self.requests.append(request)
        if self.callback:
            self.callback()
        return self.result


class SealReader:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def extract_seal(self, request):
        self.requests.append(request)
        return self.result


class AuthorizationDocumentSecurityTests(SimpleTestCase):
    def _upload(self, content, name="authorization.bin", content_type="application/octet-stream"):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def test_magic_bytes_override_client_mime_and_reencode_preview(self):
        result = inspect_authorization_document(
            self._upload(png_bytes(), name="fake.pdf", content_type="application/pdf"),
            scanner=lambda content: "test-scanner",
        )
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.page_count, 1)
        self.assertTrue(result.preview.startswith(b"\x89PNG"))

    def test_unknown_bytes_and_oversize_fail_closed(self):
        with self.assertRaises(DocumentSecurityError) as invalid:
            inspect_authorization_document(
                self._upload(b"not-a-document"), scanner=lambda content: "test-scanner"
            )
        self.assertEqual(invalid.exception.code, "document_format_unsupported")

        with override_settings(COMPANY_VERIFICATION_MAX_FILE_BYTES=4):
            with self.assertRaises(DocumentSecurityError) as oversized:
                inspect_authorization_document(
                    self._upload(b"12345"), scanner=lambda content: "test-scanner"
                )
        self.assertEqual(oversized.exception.code, "document_too_large")

    def test_multi_page_and_encrypted_pdf_are_rejected_before_render(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaises(DocumentSecurityError) as multi:
            inspect_authorization_document(
                self._upload(stream.getvalue()),
                scanner=lambda content: "test-scanner",
                pdf_renderer=lambda content: png_bytes(),
            )
        self.assertEqual(multi.exception.code, "pdf_page_count_invalid")

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("secret")
        stream = io.BytesIO()
        writer.write(stream)
        with self.assertRaises(DocumentSecurityError) as encrypted:
            inspect_authorization_document(
                self._upload(stream.getvalue()),
                scanner=lambda content: "test-scanner",
                pdf_renderer=lambda content: png_bytes(),
            )
        self.assertEqual(encrypted.exception.code, "pdf_encrypted")


@override_settings(**SETTINGS)
class AuthorizationDocumentFlowTests(TestCase):
    def setUp(self):
        stamp = str(time.time())[:20]
        self.user = User.objects.create(
            uid="ev4-owner", username="ev4-owner", created_at=stamp, updated_at=stamp
        )
        self.other = User.objects.create(
            uid="ev4-other", username="ev4-other", created_at=stamp, updated_at=stamp
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="ev4-company",
            company_name="测试企业有限公司",
            registration_no="91350211M000100Y46",
            establishment_date="2020-01-01",
            address="厦门",
            created_at=stamp,
            updated_at=stamp,
        )
        payload = create_manual_verification(
            company_id=self.company.cid,
            user=self.user,
            value=ManualVerificationStartInput(
                applicant_name="张三",
                applicant_id_type="resident_identity_card",
                applicant_id_number="11010519491231002X",
                consent_version="privacy-v1",
                idempotency_key="ev4-start",
            ),
        )
        self.application = CompanyVerification.objects.get(public_id=payload["application_id"])
        self.application.stage = CompanyVerification.STAGE_AUTHORIZATION_UPLOAD
        self.application.revision = 3
        self.application.save(update_fields=("stage", "revision"))
        self.storage = FakeStorage()

    def _upload(self, key="ev4-upload"):
        result = upload_authorization_document(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key=key,
            upload_file=SimpleUploadedFile("authorization.png", png_bytes()),
            storage=self.storage,
            scanner=lambda content: "test-scanner",
        )
        self.application.refresh_from_db()
        return result, VerificationDocument.objects.get(pk=result["document_id"])

    def _matching_results(self):
        issued = self.application.started_at.date().isoformat()
        valid_until = self.application.expires_at.date().isoformat()
        text = " ".join((
            self.application.company_name_snapshot,
            self.application.registration_no_snapshot,
            self.application.applicant_name_snapshot,
            str(self.application.public_id),
            "authorization-v1-draft",
            "企业认证 提交 相关 材料",
            issued,
            valid_until,
        ))
        return (
            DocumentTextExtractionResult(
                outcome="passed",
                retryable=False,
                lines=({"text": text, "confidence": 99},),
                provider_code="document_text_extracted",
                request_id="ocr-text-1",
            ),
            SealExtractionResult(
                outcome="passed",
                retryable=False,
                seals=({"text": self.application.company_name_snapshot},),
                provider_code="seal_extracted",
                request_id="ocr-seal-1",
            ),
        )

    def _prepare_employee_application(self):
        self.application.applicant_role = CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
        self.application.save(update_fields=("applicant_role",))

    def _employee_text_result(self, *, include_identity=True):
        issued = self.application.started_at.date().isoformat()
        valid_until = self.application.expires_at.date().isoformat()
        values = [
            self.application.company_name_snapshot,
            self.application.registration_no_snapshot,
            str(self.application.public_id),
            "authorization-v1-draft",
            "企业认证 提交 相关 材料",
            issued,
            valid_until,
        ]
        if include_identity:
            values.extend((
                "经办人姓名（请正楷填写）：张三",
                "居民身份证号（请清晰填写）：11010519491231002X",
            ))
        return DocumentTextExtractionResult(
            outcome="passed",
            retryable=False,
            lines=({"text": " ".join(values), "confidence": 99},),
            provider_code="document_text_extracted",
            request_id="ocr-employee-text",
        )

    def test_template_is_single_page_masked_scoped_draft_and_audited(self):
        result = generate_authorization_template(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
        )
        self.assertTrue(result.content.startswith(b"%PDF-"))
        self.assertEqual(len(PdfReader(io.BytesIO(result.content)).pages), 1)
        self.assertNotIn(b"11010519491231002X", result.content)
        self.assertEqual(result.template_version, "authorization-v1-draft")
        self.assertEqual(
            authorization_expected_values(self.application)["authorization_scope"],
            "代表企业办理本平台企业认证及提交相关认证材料",
        )
        self.assertTrue(VerificationAuditEvent.objects.filter(
            application=self.application,
            action="authorization_template_downloaded",
        ).exists())
        with self.assertRaises(VerificationDomainError) as forbidden:
            generate_authorization_template(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                user=self.other,
            )
        self.assertEqual(forbidden.exception.code, "application_forbidden")

    def test_employee_template_contains_bound_identity_and_upload_requires_it(self):
        self._prepare_employee_application()
        result = generate_authorization_template(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
        )
        text = PdfReader(io.BytesIO(result.content)).pages[0].extract_text()
        self.assertIn("张三", text)
        self.assertIn("11010519491231002X", text)

        CompanyVerification.objects.filter(pk=self.application.pk).update(
            applicant_name_snapshot="",
            applicant_id_type="",
            applicant_id_encrypted="",
            applicant_id_masked="",
            applicant_id_hmac="",
            applicant_id_key_version="",
        )
        self.application.refresh_from_db()
        with self.assertRaises(VerificationDomainError) as missing:
            self._upload(key="employee-upload-without-identity")
        self.assertEqual(missing.exception.code, "applicant_identity_required")

    def test_upload_uses_server_keys_versions_security_evidence_and_no_grant(self):
        before_grants = Grant.objects.count()
        payload, document = self._upload()
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_DOCUMENT_CHECK)
        self.assertEqual(self.application.material_revision, 1)
        self.assertEqual(document.scan_status, VerificationDocument.SCAN_PASSED)
        self.assertTrue(document.storage_key.startswith(
            f"enterprise-verification/mock/{self.application.public_id}/1/{document.id}/"
        ))
        self.assertTrue(document.storage_version)
        self.assertTrue(document.preview_storage_version)
        self.assertNotIn("storage_key", json.dumps(payload))
        self.assertEqual(Grant.objects.count(), before_grants)
        self.assertEqual(
            set(document.operations.values_list("operation_kind", flat=True)),
            {
                VerificationOperation.KIND_DOCUMENT_UPLOAD,
                VerificationOperation.KIND_DOCUMENT_SCAN,
            },
        )
        security = document.checks.get(kind=VerificationCheck.KIND_DOCUMENT_SECURITY)
        self.assertEqual(security.status, VerificationCheck.STATUS_PASSED)
        self.assertTrue(security.result["adopted"])

    def test_unauthorized_or_stale_upload_is_rejected_before_file_scanning(self):
        calls = []

        def scanner(content):
            calls.append(len(content))
            return "must-not-run"

        common = {
            "company_id": self.company.cid,
            "application_id": str(self.application.public_id),
            "idempotency_key": "rejected-before-scan",
            "upload_file": SimpleUploadedFile("authorization.png", png_bytes()),
            "storage": self.storage,
            "scanner": scanner,
        }
        with self.assertRaises(VerificationDomainError) as forbidden:
            upload_authorization_document(
                user=self.other,
                expected_revision=self.application.revision,
                **common,
            )
        self.assertEqual(forbidden.exception.code, "application_forbidden")
        with self.assertRaises(VerificationDomainError) as stale:
            upload_authorization_document(
                user=self.user,
                expected_revision=self.application.revision + 1,
                **common,
            )
        self.assertEqual(stale.exception.code, "stale_revision")
        self.assertEqual(calls, [])

    def test_all_ocr_checks_advance_only_to_ready_to_submit(self):
        _, document = self._upload()
        text_result, seal_result = self._matching_results()
        before_grants = Grant.objects.count()
        result = process_authorization_document(
            document_id=str(document.id),
            storage=self.storage,
            text_reader=TextReader(text_result),
            seal_reader=SealReader(seal_result),
        )
        self.application.refresh_from_db()
        self.company.refresh_from_db()
        self.assertTrue(result["adopted"])
        self.assertEqual(result["pre_review_status"], "not_required")
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_READY_TO_SUBMIT)
        self.assertEqual(self.application.status, CompanyVerification.STATUS_PENDING)
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)
        self.assertEqual(Grant.objects.count(), before_grants)
        for kind in (
            VerificationCheck.KIND_DOCUMENT_TEXT,
            VerificationCheck.KIND_SEAL,
            VerificationCheck.KIND_CONSISTENCY,
        ):
            check = document.checks.get(kind=kind)
            self.assertEqual(check.status, VerificationCheck.STATUS_PASSED)
            self.assertTrue(check.result["adopted"])

    def test_pdf_ocr_uses_rendered_png_preview_instead_of_original_pdf(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO()
        writer.write(stream)
        payload = upload_authorization_document(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="pdf-authorization",
            upload_file=SimpleUploadedFile(
                "authorization.pdf",
                stream.getvalue(),
                content_type="application/pdf",
            ),
            storage=self.storage,
            scanner=lambda content: "test-scanner",
            pdf_renderer=lambda content: png_bytes(),
        )
        document = VerificationDocument.objects.get(pk=payload["document_id"])
        text_result, seal_result = self._matching_results()
        text_reader = TextReader(text_result)
        seal_reader = SealReader(seal_result)

        process_authorization_document(
            document_id=str(document.id),
            storage=self.storage,
            text_reader=text_reader,
            seal_reader=seal_reader,
        )

        self.assertEqual(text_reader.requests[0].mime_type, "image/png")
        self.assertEqual(seal_reader.requests[0].mime_type, "image/png")
        self.assertTrue(text_reader.requests[0].content.startswith(b"\x89PNG"))
        self.assertEqual(
            text_reader.requests[0].content,
            self.storage.objects[document.preview_storage_key],
        )
        self.assertNotEqual(
            text_reader.requests[0].content,
            self.storage.objects[document.storage_key],
        )

    def test_employee_identity_is_confirmed_from_authorization_before_face(self):
        self._prepare_employee_application()
        _, document = self._upload(key="employee-authorization")
        _, seal_result = self._matching_results()
        result = process_authorization_document(
            document_id=str(document.id),
            storage=self.storage,
            text_reader=TextReader(self._employee_text_result()),
            seal_reader=SealReader(seal_result),
        )
        self.application.refresh_from_db()
        self.assertEqual(result["pre_review_status"], "not_required")
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertEqual(self.application.applicant_name_snapshot, "张三")
        self.assertEqual(self.application.applicant_id_masked, "110***********002X")
        self.assertEqual(
            get_identity_protector().decrypt(
                self.application.applicant_id_encrypted,
                context=str(self.application.public_id),
            ),
            "11010519491231002X",
        )
        text_check = document.checks.get(kind=VerificationCheck.KIND_DOCUMENT_TEXT)
        serialized = json.dumps(text_check.result, ensure_ascii=False)
        self.assertNotIn("11010519491231002X", serialized)
        self.assertIn("[身份证号已加密]", serialized)
        consistency = document.checks.get(kind=VerificationCheck.KIND_CONSISTENCY)
        self.assertTrue(consistency.result["fields"]["applicant_identity"])
        self.assertFalse(consistency.result["manual_transcription_allowed"])

    def test_employee_identity_ocr_failure_requires_new_upload_not_manual_entry(self):
        self._prepare_employee_application()
        _, document = self._upload(key="employee-authorization-missing-identity")
        _, seal_result = self._matching_results()
        result = process_authorization_document(
            document_id=str(document.id),
            storage=self.storage,
            text_reader=TextReader(self._employee_text_result(include_identity=False)),
            seal_reader=SealReader(seal_result),
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_SUPPLEMENT_REQUIRED)
        self.assertEqual(result["pre_review_status"], "technical_retry")
        self.assertFalse(result["application"]["pre_review"]["manual_transcription_allowed"])
        self.assertIn("重新上传", self.application.public_reason)

    def test_employee_provider_error_stays_in_document_check_for_automatic_retry(self):
        self._prepare_employee_application()
        _, document = self._upload(key="employee-authorization-provider-retry")
        _, seal_result = self._matching_results()
        result = process_authorization_document(
            document_id=str(document.id),
            storage=self.storage,
            text_reader=TextReader(DocumentTextExtractionResult(
                outcome="unknown",
                retryable=True,
                provider_code="document_text_temporarily_unavailable",
            )),
            seal_reader=SealReader(seal_result),
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_DOCUMENT_CHECK)
        self.assertEqual(result["pre_review_status"], "technical_retry")
        operation = document.operations.get(
            operation_kind=VerificationOperation.KIND_DOCUMENT_TEXT_OCR
        )
        self.assertEqual(operation.status, VerificationOperation.STATUS_FAILED)
        self.assertIsNotNone(operation.next_run_at)

    def test_partial_storage_failure_is_compensated_and_retryable(self):
        storage = PartialFailureStorage()
        with self.assertRaises(VerificationDomainError) as failed:
            upload_authorization_document(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                user=self.user,
                expected_revision=self.application.revision,
                idempotency_key="partial-storage-failure",
                upload_file=SimpleUploadedFile("authorization.png", png_bytes()),
                storage=storage,
                scanner=lambda content: "test-scanner",
            )
        self.assertEqual(failed.exception.code, "document_storage_unavailable")
        document = VerificationDocument.objects.get()
        cleanup = document.operations.get(
            operation_kind=VerificationOperation.KIND_DOCUMENT_CLEANUP
        )
        self.assertEqual(cleanup.status, VerificationOperation.STATUS_FAILED)
        self.assertTrue(cleanup.result["pending"])
        storage.fail_delete = False
        result = retry_document_cleanup(operation_id=str(cleanup.id), storage=storage)
        cleanup.refresh_from_db()
        self.assertTrue(result["cleaned"])
        self.assertEqual(cleanup.status, VerificationOperation.STATUS_COMPLETED)
        self.assertEqual(storage.objects, {})

    def test_supplement_creates_new_material_version_without_overwriting_original(self):
        _, first = self._upload(key="first-material")
        first_keys = {first.storage_key, first.preview_storage_key}
        self.application.stage = CompanyVerification.STAGE_SUPPLEMENT_REQUIRED
        self.application.revision += 1
        self.application.save(update_fields=("stage", "revision"))

        _, second = self._upload(key="second-material")

        self.assertEqual(second.material_revision, 2)
        self.assertNotEqual(first.id, second.id)
        self.assertTrue(first_keys.isdisjoint({second.storage_key, second.preview_storage_key}))
        self.assertTrue(first_keys.issubset(self.storage.objects))
        self.assertEqual(self.application.material_revision, 2)
        self.assertEqual(
            self.application.documents.filter(scan_status=VerificationDocument.SCAN_PASSED).count(),
            2,
        )

    def test_late_upload_is_quarantined_and_scheduled_for_exact_cleanup(self):
        def change_application():
            CompanyVerification.objects.filter(pk=self.application.pk).update(
                stage=CompanyVerification.STAGE_SUPPLEMENT_REQUIRED,
                revision=F("revision") + 1,
            )

        storage = StaleAfterWriteStorage(change_application)
        result = upload_authorization_document(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="late-upload",
            upload_file=SimpleUploadedFile("authorization.png", png_bytes()),
            storage=storage,
            scanner=lambda content: "test-scanner",
        )
        document = VerificationDocument.objects.get(pk=result["document_id"])
        cleanup = document.operations.get(
            operation_kind=VerificationOperation.KIND_DOCUMENT_CLEANUP
        )
        self.assertFalse(result["adopted"])
        self.assertEqual(document.scan_status, VerificationDocument.SCAN_QUARANTINED)
        self.assertEqual(cleanup.status, VerificationOperation.STATUS_FAILED)
        self.assertTrue(cleanup.result["pending"])
        cleanup_result = retry_document_cleanup(
            operation_id=str(cleanup.id), storage=storage
        )
        self.assertTrue(cleanup_result["cleaned"])
        self.assertEqual(storage.objects, {})

    def test_content_mismatch_stays_in_document_check_for_pre_review(self):
        _, document = self._upload()
        text = DocumentTextExtractionResult(
            outcome="passed", retryable=False, lines=({"text": "不相关内容"},)
        )
        seal = SealExtractionResult(
            outcome="passed", retryable=False, seals=({"text": "另一家公司"},)
        )
        result = process_authorization_document(
            document_id=str(document.id), storage=self.storage,
            text_reader=TextReader(text), seal_reader=SealReader(seal),
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_DOCUMENT_CHECK)
        self.assertEqual(result["pre_review_status"], "required")
        self.assertEqual(result["application"]["next_action"], "wait_document_pre_review")
        self.assertTrue(result["application"]["pre_review"]["manual_transcription_allowed"])
        self.assertFalse(
            result["application"]["pre_review"]["manual_transcription_is_final_approval"]
        )

    def test_retryable_text_ocr_retries_only_failed_provider_operation(self):
        _, document = self._upload()
        _, seal_result = self._matching_results()
        first_text = TextReader(DocumentTextExtractionResult(
            outcome="unknown",
            retryable=True,
            provider_code="document_text_empty",
        ))
        first_seal = SealReader(seal_result)
        first = process_authorization_document(
            document_id=str(document.id), storage=self.storage,
            text_reader=first_text, seal_reader=first_seal,
        )
        self.assertEqual(first["pre_review_status"], "technical_retry")
        text_operation = document.operations.get(
            operation_kind=VerificationOperation.KIND_DOCUMENT_TEXT_OCR
        )
        seal_operation = document.operations.get(
            operation_kind=VerificationOperation.KIND_SEAL_OCR
        )
        self.assertEqual(text_operation.status, VerificationOperation.STATUS_FAILED)
        self.assertIsNotNone(text_operation.next_run_at)
        self.assertEqual(seal_operation.status, VerificationOperation.STATUS_COMPLETED)

        matching_text, _ = self._matching_results()
        second_text = TextReader(matching_text)
        unused_seal = SealReader(seal_result)
        second = process_authorization_document(
            document_id=str(document.id), storage=self.storage,
            text_reader=second_text, seal_reader=unused_seal,
        )
        self.application.refresh_from_db()
        self.assertEqual(second["pre_review_status"], "not_required")
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_READY_TO_SUBMIT)
        self.assertEqual(len(second_text.requests), 1)
        self.assertEqual(len(unused_seal.requests), 0)

    def test_late_ocr_result_is_recorded_but_never_adopted(self):
        _, document = self._upload()
        text_result, seal_result = self._matching_results()

        def change_application():
            CompanyVerification.objects.filter(pk=self.application.pk).update(
                stage=CompanyVerification.STAGE_SUPPLEMENT_REQUIRED,
                revision=F("revision") + 1,
            )

        result = process_authorization_document(
            document_id=str(document.id),
            storage=self.storage,
            text_reader=TextReader(text_result, callback=change_application),
            seal_reader=SealReader(seal_result),
        )
        self.application.refresh_from_db()
        self.assertFalse(result["adopted"])
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_SUPPLEMENT_REQUIRED)
        self.assertFalse(document.checks.get(
            kind=VerificationCheck.KIND_DOCUMENT_TEXT
        ).result["adopted"])
        self.assertFalse(document.checks.get(
            kind=VerificationCheck.KIND_CONSISTENCY
        ).result["adopted"])

    def test_preview_is_applicant_only_and_uses_private_storage(self):
        _, document = self._upload()
        preview = get_authorization_preview(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            document_id=str(document.id),
            user=self.user,
            storage=self.storage,
        )
        self.assertTrue(preview.startswith(b"\x89PNG"))
        with self.assertRaises(VerificationDomainError) as forbidden:
            get_authorization_preview(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                document_id=str(document.id),
                user=self.other,
                storage=self.storage,
            )
        self.assertEqual(forbidden.exception.code, "application_forbidden")
