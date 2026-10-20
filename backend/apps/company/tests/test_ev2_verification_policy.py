import base64
import hashlib
import time
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationCheck,
    VerificationDocument,
    VerificationFaceSession,
)
from apps.company.verification_domain import (
    ManualVerificationStartInput,
    create_manual_verification,
)
from apps.company.verification_policy import evaluate_manual_approval
from apps.user.models import User


TEST_ENCRYPTION_KEY = base64.b64encode(b"e" * 32).decode()
TEST_HMAC_KEY = base64.b64encode(b"h" * 32).decode()


@override_settings(
    COMPANY_VERIFICATION_ENABLED=True,
    COMPANY_VERIFICATION_PROVIDER="tencent_manual_v1",
    COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="mock",
    COMPANY_VERIFICATION_POLICY_VERSION="manual_v1",
    COMPANY_VERIFICATION_REGISTRY_MODE="two_elements",
    COMPANY_VERIFICATION_APPLICATION_TTL_SECONDS=3600,
    COMPANY_VERIFICATION_ALLOW_MOCK=True,
    COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
    COMPANY_VERIFICATION_TEST_HMAC_KEY=TEST_HMAC_KEY,
    COMPANY_VERIFICATION_TEST_KEY_VERSION="ev2-test-v1",
)
class ManualVerificationPolicyTests(TestCase):
    def setUp(self):
        stamp = str(time.time())[:20]
        self.user = User.objects.create(
            uid="ev2-policy-user",
            username="ev2-policy-user",
            created_at=stamp,
            updated_at=stamp,
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="ev2-policy-company",
            company_name="EV2 Policy Company",
            registration_no="91350211M000100Y46",
            establishment_date="2020-01-01",
            address="Xiamen",
            created_at=stamp,
            updated_at=stamp,
        )
        payload = create_manual_verification(
            company_id=self.company.cid,
            user=self.user,
            value=ManualVerificationStartInput(
                applicant_name="Applicant",
                applicant_id_type="resident_identity_card",
                applicant_id_number="11010519491231002X",
                consent_version="privacy-v1",
                idempotency_key="policy-start",
            ),
        )
        self.application = CompanyVerification.objects.get(
            public_id=payload["application_id"]
        )
        self.now = timezone.now()
        self.application.stage = CompanyVerification.STAGE_MANUAL_REVIEW
        self.application.material_revision = 1
        self.application.submitted_at = self.now
        self.application.submitted_revision = self.application.revision
        self.application.submitted_material_revision = self.application.material_revision
        self.application.save(update_fields=[
            "stage",
            "material_revision",
            "submitted_at",
            "submitted_revision",
            "submitted_material_revision",
        ])

    def _complete_evidence(self):
        document = VerificationDocument.objects.create(
            id="00000000-0000-0000-0000-000000000001",
            application=self.application,
            material_revision=1,
            document_type=VerificationDocument.TYPE_AUTHORIZATION,
            template_version="authorization-v1-draft",
            storage_key=(
                f"enterprise-verification/mock/{self.application.public_id}/1/"
                "00000000-0000-0000-0000-000000000001/original.pdf"
            ),
            storage_version="v1",
            preview_storage_key=(
                f"enterprise-verification/mock/{self.application.public_id}/1/"
                "00000000-0000-0000-0000-000000000001/preview.png"
            ),
            preview_storage_version="pv1",
            sha256="a" * 64,
            size_bytes=1024,
            mime_type="application/pdf",
            page_count=1,
            uploaded_by=self.user,
            scan_status=VerificationDocument.SCAN_PASSED,
            scanned_at=self.now,
        )
        face = VerificationFaceSession.objects.create(
            application=self.application,
            attempt_no=1,
            provider="mock-face",
            provider_environment="mock",
            status=VerificationFaceSession.STATUS_PASSED,
            identity_match=True,
            result_valid_until=self.now + timedelta(minutes=30),
        )
        for kind in (
            VerificationCheck.KIND_REGISTRY,
            VerificationCheck.KIND_FACE,
            VerificationCheck.KIND_DOCUMENT_SECURITY,
            VerificationCheck.KIND_DOCUMENT_TEXT,
            VerificationCheck.KIND_SEAL,
            VerificationCheck.KIND_CONSISTENCY,
        ):
            is_document = kind in {
                VerificationCheck.KIND_DOCUMENT_SECURITY,
                VerificationCheck.KIND_DOCUMENT_TEXT,
                VerificationCheck.KIND_SEAL,
                VerificationCheck.KIND_CONSISTENCY,
            }
            provider, action, result = {
                VerificationCheck.KIND_REGISTRY: (
                    "tencent_manual_v1", "VerifyBizLicenseEnterprise3",
                    {"outcome": "passed", "matched": True, "operating_status": "1"},
                ),
                VerificationCheck.KIND_FACE: (
                    "tencent_manual_v1", "GetDetectInfoEnhanced",
                    {"outcome": "passed", "identity_match": True},
                ),
                VerificationCheck.KIND_DOCUMENT_SECURITY: (
                    "local_file_security", "inspect_and_scan",
                    {"adopted": True, "sha256": document.sha256},
                ),
                VerificationCheck.KIND_DOCUMENT_TEXT: (
                    "tencent_manual_v1", "GeneralAccurateOCR",
                    {"outcome": "passed", "adopted": True},
                ),
                VerificationCheck.KIND_SEAL: (
                    "tencent_manual_v1", "SealOCR",
                    {"outcome": "passed", "adopted": True},
                ),
                VerificationCheck.KIND_CONSISTENCY: (
                    "touming_rules", "authorization_consistency_v1",
                    {"outcome": "passed", "adopted": True},
                ),
            }[kind]
            VerificationCheck.objects.create(
                application=self.application,
                kind=kind,
                attempt_no=1,
                input_digest=hashlib.sha256(kind.encode()).hexdigest(),
                material_revision=1 if is_document else None,
                face_session=face if kind == VerificationCheck.KIND_FACE else None,
                document=document if is_document else None,
                provider=provider,
                action=action,
                mode="two_elements",
                provider_environment="mock",
                status=VerificationCheck.STATUS_PASSED,
                result=result,
                rule_version="manual_v1",
                checked_at=self.now,
                valid_until=self.now + timedelta(minutes=30),
            )
        return document, face

    def test_missing_evidence_is_denied_with_reasons(self):
        decision = evaluate_manual_approval(
            self.application,
            now=self.now,
            allow_mock=True,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("authorization_document_missing", decision.reasons)
        self.assertIn("registry_check_missing", decision.reasons)

    def test_complete_current_evidence_is_allowed_only_when_mock_explicit(self):
        self._complete_evidence()
        denied = evaluate_manual_approval(self.application, now=self.now)
        allowed = evaluate_manual_approval(
            self.application,
            now=self.now,
            allow_mock=True,
        )
        self.assertFalse(denied.allowed)
        self.assertIn("mock_application", denied.reasons)
        self.assertTrue(allowed.allowed, allowed.reasons)

    def test_stale_or_changed_material_is_denied(self):
        self._complete_evidence()
        self.application.material_revision = 2
        self.application.save(update_fields=["material_revision"])

        decision = evaluate_manual_approval(
            self.application,
            now=self.now,
            allow_mock=True,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("authorization_document_missing", decision.reasons)
        self.assertIn("document_text_check_missing", decision.reasons)

    def test_company_snapshot_change_is_denied(self):
        self._complete_evidence()
        self.company.address = "Changed address"
        self.company.save(update_fields=["address"])

        decision = evaluate_manual_approval(
            self.application,
            now=self.now,
            allow_mock=True,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("company_snapshot_changed", decision.reasons)

    def test_cross_application_document_link_is_denied(self):
        document, _ = self._complete_evidence()
        other_company = Company.objects.create(
            account=self.user,
            cid="ev2-policy-other",
            company_name="Other",
            registration_no="91350211M000101Y42",
            created_at=str(time.time())[:20],
            updated_at=str(time.time())[:20],
        )
        other = CompanyVerification.objects.create(
            company=other_company,
            applicant_user=self.user,
            provider=CompanyVerification.PROVIDER_TENCENT_ESS,
            status=CompanyVerification.STATUS_CANCELLED,
        )
        foreign_document = VerificationDocument.objects.create(
            application=other,
            material_revision=1,
            document_type=VerificationDocument.TYPE_AUTHORIZATION,
            template_version="authorization-v1",
            storage_key="private/ev2/foreign.pdf",
            sha256="b" * 64,
            size_bytes=1024,
            mime_type="application/pdf",
            scan_status=VerificationDocument.SCAN_PASSED,
        )
        check = self.application.checks.get(kind=VerificationCheck.KIND_DOCUMENT_TEXT)
        check.document = foreign_document
        check.save(update_fields=["document"])

        decision = evaluate_manual_approval(
            self.application,
            now=self.now,
            allow_mock=True,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("document_text_document_mismatch", decision.reasons)
        self.assertNotEqual(document.id, foreign_document.id)
