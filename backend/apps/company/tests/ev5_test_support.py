import uuid
from datetime import timedelta

from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyVerification,
    CompanyVerificationReviewer,
    VerificationCheck,
    VerificationDocument,
    VerificationFaceSession,
)
from apps.user.models import User


VALID_REGISTRATION_NO = "91370600165207843W"
VALID_DIGEST = "a" * 64


class EV5FixtureMixin:
    def make_user(self, name, *, role="user"):
        return User.objects.create(
            uid=f"{name}-{uuid.uuid4().hex[:8]}",
            username=name,
            role=role,
            created_at="1",
            updated_at="1",
        )

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.root = self.make_user("root", role="root")
        self.applicant = self.make_user("applicant")
        self.reviewer_user = self.make_user("reviewer")
        self.other_reviewer_user = self.make_user("reviewer-2")
        self.company = Company.objects.create(
            cid=f"ev5-{uuid.uuid4().hex[:12]}",
            account=self.applicant,
            company_name="EV5测试企业",
            registration_no=VALID_REGISTRATION_NO,
            establishment_date="2020-01-01",
            address="测试地址",
            created_at="1",
            updated_at="1",
        )
        self.application = self.build_application()
        self.reviewer = self.grant_fixture_reviewer(self.reviewer_user)
        self.other_reviewer = self.grant_fixture_reviewer(self.other_reviewer_user)

    def grant_fixture_reviewer(self, user, **overrides):
        values = {
            "enabled": True,
            "can_pre_review": True,
            "can_final_review": True,
            "reason": "isolated test fixture",
            "granted_by": self.root,
            "expires_at": self.now + timedelta(hours=2),
        }
        values.update(overrides)
        return CompanyVerificationReviewer.objects.create(user=user, **values)

    def build_application(self, *, stage=CompanyVerification.STAGE_READY_TO_SUBMIT):
        revision = 10
        application = CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.applicant,
            provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
            provider_environment=CompanyVerification.ENVIRONMENT_MOCK,
            mode=CompanyVerification.MODE_TWO_ELEMENTS,
            stage=stage,
            policy_version="manual_v1",
            revision=revision,
            material_revision=1,
            expires_at=self.now + timedelta(hours=1),
            submitted_at=(self.now if stage == CompanyVerification.STAGE_MANUAL_REVIEW else None),
            submitted_revision=(revision if stage == CompanyVerification.STAGE_MANUAL_REVIEW else None),
            submitted_material_revision=(1 if stage == CompanyVerification.STAGE_MANUAL_REVIEW else None),
            company_name_snapshot=self.company.company_name,
            registration_no_snapshot=self.company.registration_no,
            establishment_date_snapshot=self.company.establishment_date,
            address_snapshot=self.company.address,
            applicant_name_snapshot="测试申请人",
            applicant_id_type="resident_identity_card",
            applicant_id_encrypted="ciphertext",
            applicant_id_masked="370***********001X",
            applicant_id_hmac="b" * 64,
            applicant_id_key_version="test-v1",
            consent_version="consent-v1",
            consented_at=self.now,
            status=CompanyVerification.STATUS_PENDING,
        )
        document_id = uuid.uuid4()
        prefix = f"enterprise-verification/mock/{application.public_id}/1/{document_id}/"
        document = VerificationDocument.objects.create(
            id=document_id,
            application=application,
            material_revision=1,
            document_type=VerificationDocument.TYPE_AUTHORIZATION,
            template_version="authorization-v1-draft",
            storage_key=f"{prefix}original.pdf",
            storage_version="original-version",
            preview_storage_key=f"{prefix}preview.png",
            preview_storage_version="preview-version",
            sha256=VALID_DIGEST,
            size_bytes=512,
            mime_type="application/pdf",
            page_count=1,
            uploaded_by=self.applicant,
            scan_status=VerificationDocument.SCAN_PASSED,
            scanned_at=self.now,
        )
        face = VerificationFaceSession.objects.create(
            application=application,
            attempt_no=1,
            provider=application.provider,
            provider_environment=application.provider_environment,
            status=VerificationFaceSession.STATUS_PASSED,
            identity_match=True,
            result_valid_until=self.now + timedelta(hours=1),
        )
        valid_until = self.now + timedelta(hours=1)
        specifications = {
            VerificationCheck.KIND_REGISTRY: {
                "provider": application.provider,
                "action": "VerifyBizLicenseEnterprise3",
                "result": {"outcome": "passed", "matched": True, "operating_status": "1"},
            },
            VerificationCheck.KIND_FACE: {
                "provider": application.provider,
                "action": "GetDetectInfoEnhanced",
                "result": {"outcome": "passed", "identity_match": True},
                "face_session": face,
            },
            VerificationCheck.KIND_DOCUMENT_SECURITY: {
                "provider": "local_file_security",
                "action": "inspect_and_scan",
                "result": {"adopted": True, "sha256": document.sha256},
                "document": document,
                "material_revision": 1,
            },
            VerificationCheck.KIND_DOCUMENT_TEXT: {
                "provider": application.provider,
                "action": "GeneralAccurateOCR",
                "result": {"adopted": True, "outcome": "passed"},
                "document": document,
                "material_revision": 1,
            },
            VerificationCheck.KIND_SEAL: {
                "provider": application.provider,
                "action": "SealOCR",
                "result": {"adopted": True, "outcome": "passed"},
                "document": document,
                "material_revision": 1,
            },
            VerificationCheck.KIND_CONSISTENCY: {
                "provider": "touming_rules",
                "action": "authorization_consistency_v1",
                "result": {
                    "adopted": True,
                    "outcome": "passed",
                    "fields": {},
                    "mismatches": [],
                    "pre_review_status": "not_required",
                    "manual_transcription_allowed": False,
                },
                "document": document,
                "material_revision": 1,
            },
        }
        for attempt_no, (kind, values) in enumerate(specifications.items(), start=1):
            VerificationCheck.objects.create(
                application=application,
                kind=kind,
                attempt_no=attempt_no,
                input_digest=(str(attempt_no) * 64)[:64],
                provider=values["provider"],
                action=values["action"],
                mode=(application.mode if kind in {VerificationCheck.KIND_REGISTRY, VerificationCheck.KIND_FACE} else ""),
                provider_environment=application.provider_environment,
                status=VerificationCheck.STATUS_PASSED,
                result=values["result"],
                rule_version=application.policy_version,
                checked_at=self.now,
                valid_until=valid_until,
                material_revision=values.get("material_revision"),
                document=values.get("document"),
                face_session=values.get("face_session"),
            )
        return application

    @staticmethod
    def approval_checklist(**overrides):
        values = {
            "company_identity": True,
            "applicant_identity": True,
            "authorization_document": True,
            "evidence_versions": True,
            "conflict_of_interest": True,
        }
        values.update(overrides)
        return values
