import base64
import json
import time
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.company.models import Company, CompanyVerification, VerificationCheck, VerificationOperation
from apps.company.providers.base import VerificationProviderUnavailableError
from apps.company.providers.manual import (
    ApplicantIdentityPrecheckResult,
    BusinessLicenseExtractionResult,
    RegistryVerificationResult,
)
from apps.company.verification_business_license import run_business_license_ocr
from apps.company.verification_documents import upload_business_license_document
from apps.company.verification_domain import (
    ManualVerificationIdentityInput,
    ManualVerificationRoleInput,
    ManualVerificationStartInput,
    VerificationDomainError,
    bind_manual_verification_identity,
    continue_manual_verification_to_next_step,
    create_manual_verification,
    return_manual_verification_to_previous_step,
    select_manual_verification_applicant_role,
)
from apps.company.verification_crypto import get_identity_protector
from apps.company.verification_evidence import run_registry_verification
from apps.company.tests.test_verification_documents import FakeStorage, png_bytes
from apps.user.models import User
from common.utils.Login_utils import sign_token


SETTINGS = {
    "COMPANY_VERIFICATION_ENABLED": True,
    "COMPANY_VERIFICATION_PROVIDER": "tencent_manual_v1",
    "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT": "mock",
    "COMPANY_VERIFICATION_POLICY_VERSION": "manual_v1",
    "COMPANY_VERIFICATION_REGISTRY_MODE": "two_elements",
    "COMPANY_VERIFICATION_ALLOW_MOCK": True,
    "COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY": base64.b64encode(b"e" * 32).decode(),
    "COMPANY_VERIFICATION_TEST_HMAC_KEY": base64.b64encode(b"h" * 32).decode(),
}


IDENTITY = "11010519491231002X"


class BusinessLicenseReaderFake:
    def __init__(
        self, legal_representative="张三", *, warning_codes=(),
        is_duplication=None, electronic=None,
    ):
        self.legal_representative = legal_representative
        self.warning_codes = warning_codes
        self.is_duplication = is_duplication
        self.electronic = electronic

    def extract_business_license(self, request):
        self.request = request
        return BusinessLicenseExtractionResult(
            outcome="passed",
            retryable=False,
            company_name="注册流程测试企业",
            registration_no="91350211M000100Y46",
            legal_representative=self.legal_representative,
            warning_codes=self.warning_codes,
            is_duplication=self.is_duplication,
            electronic=self.electronic,
            provider_code="business_license_extracted",
            request_id="license-request",
        )


class BusinessLicenseNotRecognizedFake:
    def extract_business_license(self, request):
        raise VerificationProviderUnavailableError(
            "not a business license", code="FailedOperation.NoBizLicense", request_id="ocr-no-license"
        )


class RegistryPassFake:
    def verify(self, request):
        self.request = request
        return RegistryVerificationResult(
            outcome="passed",
            retryable=False,
            matched=True,
            operating_status="1",
            provider_code="registry_match",
            request_id="registry-request",
        )


class IdentityPrecheckFake:
    def __init__(self, matched=True):
        self.matched = matched
        self.calls = 0

    def precheck(self, request):
        self.calls += 1
        self.request = request
        return ApplicantIdentityPrecheckResult(
            outcome="passed" if self.matched else "failed",
            retryable=False,
            matched=self.matched,
            provider_code="identity_match" if self.matched else "identity_mismatch",
            request_id="identity-precheck-request",
        )


@override_settings(**SETTINGS)
class VerificationRegistrationFlowTests(TestCase):
    def setUp(self):
        stamp = str(time.time())[:20]
        self.user = User.objects.create(
            uid="registration-owner",
            username="registration-owner",
            created_at=stamp,
            updated_at=stamp,
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="registration-company",
            company_name="注册流程测试企业",
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
                applicant_name="",
                applicant_id_type="",
                applicant_id_number="",
                consent_version="privacy-v1",
                idempotency_key="registration-start",
                applicant_role=CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE,
            ),
        )
        self.application = CompanyVerification.objects.get(
            public_id=payload["application_id"]
        )
        self.storage = FakeStorage()

    def _upload_license(self):
        result = upload_business_license_document(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="license-upload",
            upload_file=SimpleUploadedFile("license.png", png_bytes()),
            storage=self.storage,
            scanner=lambda content: "test-scanner",
        )
        self.application.refresh_from_db()
        return result

    def test_license_ocr_must_pass_before_registry_and_face_stage(self):
        self._upload_license()
        with self.assertRaises(VerificationDomainError) as missing:
            run_registry_verification(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                user=self.user,
                expected_revision=self.application.revision,
                idempotency_key="registry-too-early",
                verifier=RegistryPassFake(),
            )
        self.assertEqual(missing.exception.code, "business_license_required")

        license_result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="license-ocr",
            reader=BusinessLicenseReaderFake(),
            storage=self.storage,
        )
        self.assertEqual(license_result["business_license_check"], "passed")

        result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="registry-after-license",
            verifier=RegistryPassFake(),
        )
        self.assertEqual(result["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertTrue(result["identity_required"])
        self.assertEqual(result["next_action"], "provide_applicant_identity")

    def test_license_ocr_can_reuse_uploaded_content_without_cos_read(self):
        self._upload_license()
        reader = BusinessLicenseReaderFake()
        content = png_bytes()

        result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="license-ocr-inline-content",
            reader=reader,
            content=content,
        )

        self.assertEqual(result["business_license_check"], "passed")
        self.assertEqual(reader.request.content, content)

    def test_photo_or_copy_warning_is_advisory_and_flow_continues(self):
        self._upload_license()
        license_result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="license-ocr-photo-copy",
            reader=BusinessLicenseReaderFake(
                warning_codes=(-9102,),
                is_duplication=1,
            ),
            storage=self.storage,
        )

        self.application.refresh_from_db()
        self.assertEqual(license_result["business_license_check"], "passed")
        self.assertEqual(license_result["business_license_authenticity"], "warning")
        self.assertEqual(
            license_result["business_license_result"],
            {
                "recognized": True,
                "company_name": "注册流程测试企业",
                "registration_no": "91350211M000100Y46",
                "legal_representative": "张*",
                "material_type": "photo_or_copy",
                "copy_or_photo_warning": True,
            },
        )
        self.assertEqual(self.application.failure_code, "")
        self.assertEqual(self.application.public_reason, "")

        result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=license_result["revision"],
            idempotency_key="registry-after-photo-copy-license",
            verifier=RegistryPassFake(),
        )
        self.assertEqual(result["stage"], CompanyVerification.STAGE_APPLICANT_FACE)

    def test_legal_representative_is_taken_from_license_not_client_input(self):
        self._upload_license()
        result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="license-ocr-mismatch",
            reader=BusinessLicenseReaderFake(legal_representative="李四"),
            storage=self.storage,
        )
        self.assertEqual(result["stage"], CompanyVerification.STAGE_COMPANY_CHECK)
        self.assertEqual(result["business_license_check"], VerificationCheck.STATUS_PASSED)
        self.assertEqual(result["legal_representative"]["name"], "李*")

        verifier = RegistryPassFake()
        result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=result["revision"],
            idempotency_key="registry-with-license-legal-person",
            verifier=verifier,
        )
        self.assertEqual(result["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertEqual(verifier.request.mode, CompanyVerification.MODE_THREE_ELEMENTS)
        self.assertEqual(verifier.request.legal_representative, "李四")

        with self.assertRaises(VerificationDomainError) as mismatch:
            bind_manual_verification_identity(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                user=self.user,
                value=ManualVerificationIdentityInput(
                    expected_revision=result["revision"],
                    idempotency_key="legal-identity-mismatch",
                    applicant_name="张三",
                    applicant_id_type="resident_identity_card",
                    applicant_id_number=IDENTITY,
                ),
            )
        self.assertEqual(mismatch.exception.code, "applicant_identity_mismatch")

        expected_revision = result["revision"]
        precheck = IdentityPrecheckFake()
        with patch(
            "apps.company.verification_domain.get_applicant_identity_verifier",
            return_value=precheck,
        ):
            response = self.client.post(
                (
                    f"/api/company/{self.company.cid}/verification/"
                    f"{self.application.public_id}/applicant-identity"
                ),
                data=json.dumps({
                    "expected_revision": expected_revision,
                    "idempotency_key": "legal-identity",
                    "applicant_name": "",
                    "applicant_id_type": "resident_identity_card",
                    "applicant_id_number": IDENTITY,
                }),
                content_type="application/json",
                HTTP_ACCESS_TOKEN=sign_token({"user_id": self.user.id}),
            )
        self.assertEqual(response.status_code, 200)
        bound = response.json()["data"]
        self.application.refresh_from_db()
        self.assertFalse(bound["identity_required"])
        self.assertTrue(bound["identity_bound"])
        self.assertEqual(bound["next_action"], "start_applicant_face")
        self.assertEqual(bound["identity_precheck"], "passed")
        self.assertEqual(precheck.calls, 1)
        self.assertEqual(precheck.request.name, "李四")
        self.assertEqual(self.application.applicant_name_snapshot, "李四")
        self.assertEqual(self.application.applicant_id_masked, "110***********002X")
        self.assertNotIn(IDENTITY, self.application.applicant_id_encrypted)
        self.assertEqual(
            get_identity_protector().decrypt(
                self.application.applicant_id_encrypted,
                context=str(self.application.public_id),
            ),
            IDENTITY,
        )
        repeated = bind_manual_verification_identity(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            value=ManualVerificationIdentityInput(
                expected_revision=expected_revision,
                idempotency_key="legal-identity",
                applicant_name="",
                applicant_id_type="resident_identity_card",
                applicant_id_number=IDENTITY,
            ),
        )
        self.assertEqual(repeated["revision"], bound["revision"])

    def test_authorized_employee_waits_for_authorization_before_face(self):
        self.application.status = CompanyVerification.STATUS_CANCELLED
        self.application.save(update_fields=("status",))
        payload = create_manual_verification(
            company_id=self.company.cid,
            user=self.user,
            value=ManualVerificationStartInput(
                applicant_name="",
                applicant_id_type="",
                applicant_id_number="",
                consent_version="privacy-v1",
                idempotency_key="authorized-registration-start",
                applicant_role=CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON,
            ),
        )
        self.application = CompanyVerification.objects.get(
            public_id=payload["application_id"]
        )
        self._upload_license()
        license_result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="authorized-license-ocr",
            reader=BusinessLicenseReaderFake(),
            storage=self.storage,
        )
        verifier = RegistryPassFake()
        result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=license_result["revision"],
            idempotency_key="authorized-registry",
            verifier=verifier,
        )
        self.assertEqual(result["stage"], CompanyVerification.STAGE_AUTHORIZATION_UPLOAD)
        self.assertEqual(result["next_action"], "provide_applicant_identity")
        self.assertTrue(result["identity_required"])
        self.assertEqual(verifier.request.legal_representative, "张三")

        bound = bind_manual_verification_identity(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            verifier=IdentityPrecheckFake(),
            value=ManualVerificationIdentityInput(
                expected_revision=result["revision"],
                idempotency_key="authorized-identity",
                applicant_name="王五",
                applicant_id_type="resident_identity_card",
                applicant_id_number=IDENTITY,
            ),
        )
        self.assertFalse(bound["identity_required"])
        self.assertEqual(bound["next_action"], "upload_authorization")
        self.assertEqual(bound["applicant"]["name"], "王*")

    def test_identity_precheck_rejects_mismatch_before_face_session(self):
        self._upload_license()
        license_result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="identity-mismatch-license",
            reader=BusinessLicenseReaderFake(),
            storage=self.storage,
        )
        registry_result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=license_result["revision"],
            idempotency_key="identity-mismatch-registry",
            verifier=RegistryPassFake(),
        )

        with self.assertRaises(VerificationDomainError) as mismatch:
            bind_manual_verification_identity(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                user=self.user,
                verifier=IdentityPrecheckFake(matched=False),
                value=ManualVerificationIdentityInput(
                    expected_revision=registry_result["revision"],
                    idempotency_key="identity-mismatch-precheck",
                    applicant_name="",
                    applicant_id_type="resident_identity_card",
                    applicant_id_number=IDENTITY,
                ),
            )

        self.assertEqual(mismatch.exception.code, "identity_information_mismatch")
        self.assertEqual(mismatch.exception.status_code, 422)
        self.application.refresh_from_db()
        self.assertEqual(self.application.applicant_id_hmac, "")
        self.assertFalse(self.application.face_sessions.exists())
        operation = self.application.operations.get(
            idempotency_key="identity-mismatch-precheck"
        )
        self.assertEqual(operation.result["identity_precheck"], "failed")

    def test_back_preserves_company_evidence_and_allows_role_change(self):
        self._upload_license()
        license_result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="role-change-license",
            reader=BusinessLicenseReaderFake(),
            storage=self.storage,
        )
        registry_result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=license_result["revision"],
            idempotency_key="role-change-registry",
            verifier=RegistryPassFake(),
        )
        original_document_id = registry_result["business_license_document"]["document_id"]

        previous = return_manual_verification_to_previous_step(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=registry_result["revision"],
            idempotency_key="role-change-back",
        )
        self.assertEqual(
            previous["stage"], CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD
        )
        self.assertEqual(previous["business_license_check"], "passed")
        self.assertEqual(
            previous["business_license_document"]["document_id"], original_document_id
        )

        changed = select_manual_verification_applicant_role(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            value=ManualVerificationRoleInput(
                expected_revision=previous["revision"],
                idempotency_key="role-change-authorized",
                applicant_role=CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON,
            ),
        )
        self.assertEqual(
            changed["stage"], CompanyVerification.STAGE_AUTHORIZATION_UPLOAD
        )
        self.assertEqual(
            changed["applicant_role"],
            CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON,
        )
        self.assertTrue(changed["identity_required"])
        self.assertEqual(changed["business_license_check"], "passed")
        self.assertEqual(
            changed["business_license_document"]["document_id"], original_document_id
        )

    def test_same_role_resume_keeps_prechecked_identity(self):
        self._upload_license()
        license_result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="same-role-license",
            reader=BusinessLicenseReaderFake(),
            storage=self.storage,
        )
        registry_result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=license_result["revision"],
            idempotency_key="same-role-registry",
            verifier=RegistryPassFake(),
        )
        bound = bind_manual_verification_identity(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            verifier=IdentityPrecheckFake(),
            value=ManualVerificationIdentityInput(
                expected_revision=registry_result["revision"],
                idempotency_key="same-role-identity",
                applicant_name="",
                applicant_id_type="resident_identity_card",
                applicant_id_number=IDENTITY,
            ),
        )
        self.application.refresh_from_db()
        original_identity_hmac = self.application.applicant_id_hmac
        previous = return_manual_verification_to_previous_step(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=bound["revision"],
            idempotency_key="same-role-back",
        )
        resumed = select_manual_verification_applicant_role(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            value=ManualVerificationRoleInput(
                expected_revision=previous["revision"],
                idempotency_key="same-role-resume",
                applicant_role=CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE,
            ),
        )

        self.application.refresh_from_db()
        self.assertEqual(self.application.applicant_id_hmac, original_identity_hmac)
        self.assertEqual(resumed["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertFalse(resumed["identity_required"])
        self.assertEqual(resumed["identity_precheck"], "passed")

    def test_existing_evidence_can_continue_after_returning_to_previous_step(self):
        self._upload_license()
        license_result = run_business_license_ocr(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="next-step-license",
            reader=BusinessLicenseReaderFake(),
            storage=self.storage,
        )
        registry_result = run_registry_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=license_result["revision"],
            idempotency_key="next-step-registry",
            verifier=RegistryPassFake(),
        )
        bound = bind_manual_verification_identity(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            verifier=IdentityPrecheckFake(),
            value=ManualVerificationIdentityInput(
                expected_revision=registry_result["revision"],
                idempotency_key="next-step-identity",
                applicant_name="",
                applicant_id_type="resident_identity_card",
                applicant_id_number=IDENTITY,
            ),
        )
        previous = return_manual_verification_to_previous_step(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=bound["revision"],
            idempotency_key="next-step-back",
        )
        self.assertEqual(
            previous["stage"],
            CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD,
        )
        self.assertEqual(previous["next_action"], "continue_to_next_step")
        self.assertIn("continue_to_next_step", previous["allowed_actions"])

        continued = continue_manual_verification_to_next_step(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=previous["revision"],
            idempotency_key="next-step-continue",
        )

        self.assertEqual(continued["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertFalse(continued["identity_required"])
        self.assertEqual(continued["identity_precheck"], "passed")
        self.assertTrue(self.application.audit_events.filter(
            action="application_continued_to_next_step"
        ).exists())

    def test_unrecognized_license_is_actionable_and_can_be_replaced(self):
        self._upload_license()
        with self.assertRaises(VerificationDomainError) as rejected:
            run_business_license_ocr(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                user=self.user,
                expected_revision=self.application.revision,
                idempotency_key="license-ocr-not-recognized",
                reader=BusinessLicenseNotRecognizedFake(),
                storage=self.storage,
            )
        self.assertEqual(rejected.exception.code, "business_license_not_recognized")
        self.assertEqual(rejected.exception.status_code, 422)

        self.application.refresh_from_db()
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_COMPANY_CHECK)
        self.assertEqual(self.application.failure_code, "business_license_not_recognized")
        self.assertIn("未识别到有效营业执照", self.application.public_reason)
        check = self.application.checks.get(kind=VerificationCheck.KIND_BUSINESS_LICENSE)
        self.assertEqual(check.status, VerificationCheck.STATUS_FAILED)
        self.assertEqual(check.provider_code, "FailedOperation.NoBizLicense")
        operation = self.application.operations.get(
            operation_kind=VerificationOperation.KIND_BUSINESS_LICENSE_OCR
        )
        self.assertEqual(operation.status, VerificationOperation.STATUS_FAILED)
        self.assertEqual(operation.last_error_code, "FailedOperation.NoBizLicense")

        with self.assertRaises(VerificationDomainError) as idempotent_rejection:
            run_business_license_ocr(
                company_id=self.company.cid,
                application_id=str(self.application.public_id),
                user=self.user,
                expected_revision=self.application.revision,
                idempotency_key="license-ocr-not-recognized",
                reader=BusinessLicenseReaderFake(),
                storage=self.storage,
            )
        self.assertEqual(
            idempotent_rejection.exception.code,
            "business_license_not_recognized",
        )

        replacement = upload_business_license_document(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.user,
            expected_revision=self.application.revision,
            idempotency_key="license-replacement",
            upload_file=SimpleUploadedFile("replacement.png", png_bytes(color="blue")),
            storage=self.storage,
            scanner=lambda content: "test-scanner",
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_COMPANY_CHECK)
        self.assertEqual(self.application.material_revision, 2)
        self.assertEqual(self.application.revision, 3)
        self.assertEqual(self.application.failure_code, "")
        self.assertEqual(self.application.public_reason, "")
        self.assertEqual(replacement["application"]["business_license_check"], "not_started")
        self.assertIn(
            "upload_business_license",
            replacement["application"]["allowed_actions"],
        )
