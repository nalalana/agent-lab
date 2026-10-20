from datetime import timedelta

from django.test import TestCase, override_settings

from apps.company.models import (
    CompanyMember,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationReviewerEvent,
    VerificationTranscription,
)
from apps.company.verification_domain import VerificationDomainError
from apps.company.verification_reviews import (
    get_reviewer_context,
    get_review_document_preview,
    grant_or_update_reviewer,
    list_review_queue,
    revoke_reviewer,
    transcribe_authorization_document,
)
from apps.iam.models import Grant, Principal, Resource

from .ev5_test_support import EV5FixtureMixin


@override_settings(
    COMPANY_VERIFICATION_ENABLED=True,
    COMPANY_VERIFICATION_ALLOW_MOCK=True,
    COMPANY_VERIFICATION_REVIEW_ALLOW_MOCK=True,
    COMPANY_VERIFICATION_AUTHORIZATION_TEMPLATE_VERSION="authorization-v1-draft",
)
class VerificationReviewPermissionTests(EV5FixtureMixin, TestCase):
    def test_reviewer_context_only_exposes_active_effective_capabilities(self):
        active = get_reviewer_context(user=self.reviewer_user, now=self.now)
        self.assertTrue(active["enabled"])
        self.assertEqual(active["capabilities"], ["pre_review", "final_review"])
        self.assertEqual(active["expires_at"], self.reviewer.expires_at.isoformat())

        unqualified = get_reviewer_context(user=self.make_user("unqualified"), now=self.now)
        self.assertEqual(
            unqualified,
            {"enabled": False, "capabilities": [], "expires_at": None},
        )

        self.reviewer.expires_at = self.now
        self.reviewer.save(update_fields=("expires_at",))
        expired = get_reviewer_context(user=self.reviewer_user, now=self.now)
        self.assertEqual(expired["enabled"], False)
        self.assertEqual(expired["capabilities"], [])

    def test_platform_administrators_explicitly_grant_and_revoke_qualification(self):
        target = self.make_user("new-reviewer")
        granted = grant_or_update_reviewer(
            actor=self.root,
            target_user_uid=target.uid,
            capabilities=["pre_review"],
            reason="材料预审值班",
            expires_at=self.now + timedelta(hours=4),
            now=self.now,
        )
        self.assertEqual(granted["capabilities"], ["pre_review"])
        self.assertTrue(VerificationReviewerEvent.objects.filter(
            reviewer_id=granted["reviewer_id"], action="granted"
        ).exists())

        ordinary_user = self.make_user("ordinary-user")
        with self.assertRaises(VerificationDomainError) as captured:
            grant_or_update_reviewer(
                actor=ordinary_user,
                target_user_uid=target.uid,
                capabilities=["final_review"],
                reason="should fail",
                now=self.now,
            )
        self.assertEqual(captured.exception.code, "reviewer_management_forbidden")

        revoked = revoke_reviewer(
            actor=self.root,
            reviewer_id=granted["reviewer_id"],
            reason="轮值结束",
            now=self.now,
        )
        self.assertFalse(revoked["enabled"])
        self.assertTrue(VerificationReviewerEvent.objects.filter(
            reviewer_id=granted["reviewer_id"], action="revoked"
        ).exists())

    def test_queue_requires_capability_and_hides_conflicted_applications(self):
        self.application.stage = CompanyVerification.STAGE_MANUAL_REVIEW
        self.application.submitted_at = self.now
        self.application.submitted_revision = self.application.revision
        self.application.submitted_material_revision = self.application.material_revision
        self.application.save(update_fields=(
            "stage", "submitted_at", "submitted_revision", "submitted_material_revision"
        ))
        result = list_review_queue(user=self.reviewer_user, queue="final_review", now=self.now)
        self.assertEqual(result["total"], 1)

        CompanyMember.objects.create(
            company=self.company,
            user=self.reviewer_user,
            name="reviewer",
            role="member",
            status=CompanyMember.STATUS_DISABLED,
            created_at="1",
            updated_at="1",
        )
        result = list_review_queue(user=self.reviewer_user, queue="final_review", now=self.now)
        self.assertEqual(result["total"], 0)

        unqualified = self.make_user("unqualified")
        with self.assertRaises(VerificationDomainError) as captured:
            list_review_queue(user=unqualified, queue="final_review", now=self.now)
        self.assertEqual(captured.exception.code, "reviewer_forbidden")

    def test_any_existing_company_iam_grant_is_a_review_conflict(self):
        principal = Principal.objects.create(type=Principal.TYPE_USER, external_id=self.reviewer_user.uid)
        resource = Resource.objects.create(type=Resource.TYPE_COMPANY, external_id=self.company.cid)
        Grant.objects.create(
            principal=principal,
            resource=resource,
            permissions=Grant.PERM_READ,
            granter=self.root,
        )
        self.application.stage = CompanyVerification.STAGE_MANUAL_REVIEW
        self.application.submitted_at = self.now
        self.application.submitted_revision = self.application.revision
        self.application.submitted_material_revision = self.application.material_revision
        self.application.save(update_fields=(
            "stage", "submitted_at", "submitted_revision", "submitted_material_revision"
        ))
        result = list_review_queue(user=self.reviewer_user, queue="final_review", now=self.now)
        self.assertEqual(result["items"], [])

    def _mark_pre_review_required(self, *, manual_allowed=True, status="required"):
        self.application.stage = CompanyVerification.STAGE_DOCUMENT_CHECK
        self.application.save(update_fields=("stage",))
        document = self.application.documents.get(material_revision=1)
        return VerificationCheck.objects.create(
            application=self.application,
            kind=VerificationCheck.KIND_CONSISTENCY,
            attempt_no=99,
            input_digest="f" * 64,
            material_revision=1,
            document=document,
            provider="touming_rules",
            action="authorization_consistency_v1",
            provider_environment=self.application.provider_environment,
            status=VerificationCheck.STATUS_UNKNOWN,
            provider_code="document_pre_review_required",
            result={
                "outcome": "unknown",
                "fields": {"company_name": False},
                "mismatches": ["company_name"],
                "pre_review_status": status,
                "pre_review_reasons": ["company_name"],
                "manual_transcription_allowed": manual_allowed,
                "adopted": True,
                "source_check_ids": [],
            },
            rule_version=self.application.policy_version,
            checked_at=self.now + timedelta(seconds=1),
        )

    def test_transcription_is_audited_and_only_reaches_ready_to_submit(self):
        self._mark_pre_review_required()
        result = transcribe_authorization_document(
            user=self.reviewer_user,
            application_id=str(self.application.public_id),
            expected_revision=self.application.revision,
            idempotency_key="transcription-1",
            corrections=[{
                "field": "company_name",
                "original_value": "EVS测试企业",
                "corrected_value": self.company.company_name,
                "source_location": "正文第1行",
                "reason": "OCR 将数字5识别为字母S",
            }],
            reason="对照当前原件逐字核对",
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(result["stage"], CompanyVerification.STAGE_READY_TO_SUBMIT)
        self.assertEqual(result["status"], CompanyVerification.STATUS_PENDING)
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_status, self.company.VERIFICATION_STATUS_UNVERIFIED)
        transcription = VerificationTranscription.objects.get(application=self.application)
        self.assertEqual(transcription.corrections[0]["field"], "company_name")
        self.assertEqual(transcription.result_check.status, VerificationCheck.STATUS_PASSED)
        self.assertTrue(VerificationAuditEvent.objects.filter(
            application=self.application, action="authorization_transcription_completed"
        ).exists())

    def test_transcription_cannot_override_wrong_content_or_technical_unknown(self):
        self._mark_pre_review_required()
        with self.assertRaises(VerificationDomainError) as captured:
            transcribe_authorization_document(
                user=self.reviewer_user,
                application_id=str(self.application.public_id),
                expected_revision=self.application.revision,
                idempotency_key="transcription-wrong",
                corrections=[{
                    "field": "company_name",
                    "original_value": "错误企业",
                    "corrected_value": "另一家企业",
                    "source_location": "正文第1行",
                    "reason": "不能把真实错误改成通过",
                }],
                reason="invalid",
                now=self.now + timedelta(seconds=2),
            )
        self.assertEqual(captured.exception.code, "transcription_invalid")

        VerificationCheck.objects.filter(
            application=self.application,
            kind=VerificationCheck.KIND_CONSISTENCY,
            attempt_no=99,
        ).update(result={
            "outcome": "unknown",
            "mismatches": ["company_name"],
            "pre_review_status": "technical_retry",
            "manual_transcription_allowed": False,
            "adopted": True,
        })
        with self.assertRaises(VerificationDomainError) as captured:
            transcribe_authorization_document(
                user=self.reviewer_user,
                application_id=str(self.application.public_id),
                expected_revision=self.application.revision,
                idempotency_key="transcription-technical",
                corrections=[{
                    "field": "company_name",
                    "original_value": "unknown",
                    "corrected_value": self.company.company_name,
                    "source_location": "正文第1行",
                    "reason": "technical unknown",
                }],
                reason="invalid",
                now=self.now + timedelta(seconds=2),
            )
        self.assertEqual(captured.exception.code, "transcription_not_allowed")

    def test_reviewer_preview_is_private_and_audited(self):
        self.application.stage = CompanyVerification.STAGE_MANUAL_REVIEW
        self.application.save(update_fields=("stage",))
        document = self.application.documents.get(material_revision=1)

        class Storage:
            def get(self, key, max_bytes):
                self.key = key
                self.max_bytes = max_bytes
                return b"private-preview"

        storage = Storage()
        content = get_review_document_preview(
            user=self.reviewer_user,
            application_id=str(self.application.public_id),
            document_id=str(document.id),
            storage=storage,
            now=self.now,
        )
        self.assertEqual(content, b"private-preview")
        self.assertEqual(storage.key, document.preview_storage_key)
        self.assertTrue(VerificationAuditEvent.objects.filter(
            application=self.application,
            actor=self.reviewer_user,
            action="review_authorization_document_previewed",
        ).exists())
