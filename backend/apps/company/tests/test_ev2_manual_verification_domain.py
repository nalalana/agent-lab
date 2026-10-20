import base64
import json
import time
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.company.checks import enterprise_verification_configuration_check
from apps.company.models import (
    Company,
    CompanyVerification,
    VerificationAuditEvent,
    VerificationCheck,
    VerificationDocument,
    VerificationOperation,
)
from apps.company.permissions import (
    can_access_company_draft,
    is_company_verified_for_formal_access,
)
from apps.company.providers import get_manual_verification_provider
from apps.company.services import CompanyService
from apps.company.providers.base import VerificationProviderUnavailableError
from apps.company.tasks import reconcile_pending_company_verifications
from apps.company.verification import (
    expire_stale_initiating_verifications,
    pending_verifications_for_reconciliation,
)
from apps.company.verification_crypto import get_identity_protector
from apps.company.verification_storage import VerificationStorageError
from apps.company.verification_domain import (
    ManualVerificationStartInput,
    VerificationDomainError,
    cancel_manual_verification,
    create_manual_verification,
    expire_due_manual_verifications,
    return_manual_verification_to_previous_step,
    serialize_manual_verification,
    transition_manual_verification,
)
from apps.follow.services import FollowingService
from apps.iam.models import Grant, Principal, Resource
from apps.user.models import User
from common.utils.Login_utils import sign_token


VALID_REGISTRATION_NO = "91350211M000100Y46"
IDENTITY_NUMBER = "11010519491231002X"
TEST_ENCRYPTION_KEY = base64.b64encode(b"e" * 32).decode()
TEST_HMAC_KEY = base64.b64encode(b"h" * 32).decode()

ISOLATED_SETTINGS = {
    "COMPANY_VERIFICATION_ENABLED": True,
    "COMPANY_VERIFICATION_PROVIDER": "tencent_manual_v1",
    "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT": "mock",
    "COMPANY_VERIFICATION_POLICY_VERSION": "manual_v1",
    "COMPANY_VERIFICATION_REGISTRY_MODE": "two_elements",
    "COMPANY_VERIFICATION_APPLICATION_TTL_SECONDS": 3600,
    "COMPANY_VERIFICATION_ALLOW_MOCK": True,
    "COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
    "COMPANY_VERIFICATION_TEST_HMAC_KEY": TEST_HMAC_KEY,
    "COMPANY_VERIFICATION_TEST_KEY_VERSION": "ev2-test-v1",
}


@override_settings(**ISOLATED_SETTINGS)
class ManualVerificationDomainTests(TestCase):
    def setUp(self):
        stamp = str(time.time())[:20]
        self.owner = User.objects.create(
            uid="ev2-owner",
            username="ev2-owner",
            created_at=stamp,
            updated_at=stamp,
        )
        self.other = User.objects.create(
            uid="ev2-other",
            username="ev2-other",
            created_at=stamp,
            updated_at=stamp,
        )
        self.company = Company.objects.create(
            account=self.owner,
            cid="ev2-company",
            company_name="EV2 Company",
            registration_no=VALID_REGISTRATION_NO,
            establishment_date="2020-01-01",
            address="Xiamen",
            created_at=stamp,
            updated_at=stamp,
        )

    def _input(self, key="start-1", name="Applicant", applicant_role=""):
        return ManualVerificationStartInput(
            applicant_name="" if applicant_role else name,
            applicant_id_type="" if applicant_role else "resident_identity_card",
            applicant_id_number="" if applicant_role else IDENTITY_NUMBER,
            consent_version="privacy-2026-09",
            idempotency_key=key,
            applicant_role=applicant_role,
        )

    def _start(self, **kwargs):
        payload = create_manual_verification(
            company_id=self.company.cid,
            user=self.owner,
            value=self._input(**kwargs),
        )
        return payload, CompanyVerification.objects.get(public_id=payload["application_id"])

    def test_start_snapshots_encrypts_and_creates_no_authorization(self):
        before_auth = (
            Principal.objects.count(),
            Resource.objects.count(),
            Grant.objects.count(),
        )

        payload, application = self._start()

        self.assertEqual(application.provider, "tencent_manual_v1")
        self.assertEqual(application.provider_environment, "mock")
        self.assertEqual(application.revision, 1)
        self.assertEqual(application.stage, CompanyVerification.STAGE_COMPANY_CHECK)
        self.assertEqual(application.company_name_snapshot, self.company.company_name)
        self.assertEqual(application.registration_no_snapshot, VALID_REGISTRATION_NO)
        self.assertNotIn(IDENTITY_NUMBER, application.applicant_id_encrypted)
        self.assertNotEqual(application.applicant_id_hmac, IDENTITY_NUMBER)
        self.assertEqual(application.applicant_id_masked, "110***********002X")
        self.assertEqual(
            get_identity_protector().decrypt(
                application.applicant_id_encrypted,
                context=str(application.public_id),
            ),
            IDENTITY_NUMBER,
        )
        self.assertNotIn(IDENTITY_NUMBER, json.dumps(payload))
        self.assertNotEqual(payload["applicant"]["name"], "Applicant")
        self.assertEqual(
            before_auth,
            (
                Principal.objects.count(),
                Resource.objects.count(),
                Grant.objects.count(),
            ),
        )
        self.assertEqual(application.operations.count(), 1)
        self.assertEqual(application.audit_events.count(), 1)

    def test_registration_routes_start_with_business_license_upload(self):
        for index, role in enumerate((
            CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE,
            CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON,
        )):
            if index:
                CompanyVerification.objects.update(
                    status=CompanyVerification.STATUS_CANCELLED
                )
            payload, application = self._start(
                key=f"registration-{index}",
                applicant_role=role,
            )
            self.assertEqual(
                application.stage,
                CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD,
            )
            self.assertEqual(payload["applicant_role"], role)
            self.assertEqual(payload["next_action"], "upload_business_license")
            self.assertEqual(application.applicant_name_snapshot, "")
            self.assertEqual(application.applicant_id_encrypted, "")

    def test_start_is_idempotent_and_conflicts_fail_closed(self):
        first, application = self._start()
        second = create_manual_verification(
            company_id=self.company.cid,
            user=self.owner,
            value=self._input(),
        )
        self.assertEqual(first, second)
        self.assertEqual(CompanyVerification.objects.count(), 1)
        self.assertEqual(VerificationOperation.objects.count(), 1)

        with self.assertRaises(VerificationDomainError) as captured:
            create_manual_verification(
                company_id=self.company.cid,
                user=self.owner,
                value=self._input(name="Different Applicant"),
            )
        self.assertEqual(captured.exception.code, "idempotency_conflict")

        with self.assertRaises(VerificationDomainError) as captured:
            create_manual_verification(
                company_id=self.company.cid,
                user=self.owner,
                value=self._input(key="start-2"),
            )
        self.assertEqual(captured.exception.code, "application_active")
        application.refresh_from_db()
        self.assertEqual(application.status, CompanyVerification.STATUS_PENDING)

    def test_start_requires_creator_and_feature_flag(self):
        with self.assertRaises(VerificationDomainError) as captured:
            create_manual_verification(
                company_id=self.company.cid,
                user=self.other,
                value=self._input(),
            )
        self.assertEqual(captured.exception.code, "not_draft_owner")

        with override_settings(COMPANY_VERIFICATION_ENABLED=False):
            with self.assertRaises(VerificationDomainError) as captured:
                create_manual_verification(
                    company_id=self.company.cid,
                    user=self.owner,
                    value=self._input(),
                )
        self.assertEqual(captured.exception.code, "feature_disabled")

    def test_cancel_uses_revision_and_idempotency(self):
        _, application = self._start()
        with self.assertRaises(VerificationDomainError) as captured:
            cancel_manual_verification(
                company_id=self.company.cid,
                application_id=str(application.public_id),
                user=self.owner,
                expected_revision=99,
                idempotency_key="cancel-1",
            )
        self.assertEqual(captured.exception.code, "stale_revision")

        payload = cancel_manual_verification(
            company_id=self.company.cid,
            application_id=str(application.public_id),
            user=self.owner,
            expected_revision=1,
            idempotency_key="cancel-1",
        )
        repeated = cancel_manual_verification(
            company_id=self.company.cid,
            application_id=str(application.public_id),
            user=self.owner,
            expected_revision=1,
            idempotency_key="cancel-1",
        )
        self.assertEqual(payload, repeated)
        self.assertEqual(payload["status"], CompanyVerification.STATUS_CANCELLED)
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(application.operations.count(), 2)

    def test_cancelled_application_can_restart_with_a_fresh_application(self):
        _, first_application = self._start()
        cancelled = cancel_manual_verification(
            company_id=self.company.cid,
            application_id=str(first_application.public_id),
            user=self.owner,
            expected_revision=1,
            idempotency_key="cancel-before-restart",
        )

        restarted, second_application = self._start(key="start-after-cancel")

        self.assertEqual(cancelled["allowed_actions"], [])
        self.assertNotEqual(second_application.pk, first_application.pk)
        self.assertNotEqual(restarted["application_id"], str(first_application.public_id))
        self.assertEqual(second_application.status, CompanyVerification.STATUS_PENDING)
        first_application.refresh_from_db()
        self.assertEqual(first_application.status, CompanyVerification.STATUS_CANCELLED)

    @patch("apps.company.services.get_verification_storage")
    def test_incomplete_history_is_purged_when_draft_is_deleted(self, storage_factory):
        _, application = self._start()
        document = VerificationDocument.objects.create(
            application=application,
            material_revision=1,
            document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
            template_version="business-license-v1",
            storage_key="enterprise-verification/mock/company/license.png",
            preview_storage_key="enterprise-verification/mock/company/license-preview.png",
            sha256="a" * 64,
            size_bytes=128,
            mime_type="image/png",
            scan_status=VerificationDocument.SCAN_PASSED,
        )
        check = VerificationCheck.objects.create(
            application=application,
            kind=VerificationCheck.KIND_BUSINESS_LICENSE,
            attempt_no=1,
            input_digest="b" * 64,
            material_revision=1,
            document=document,
            provider="mock",
            action="recognize",
            provider_environment="mock",
            status=VerificationCheck.STATUS_PASSED,
            rule_version="test-v1",
        )
        company_id = self.company.pk

        ok, message = CompanyService.delete_company_draft(
            self.company.cid,
            self.owner,
        )

        self.assertTrue(ok)
        self.assertIsNone(message)
        self.assertFalse(Company.objects.filter(pk=company_id).exists())
        self.assertFalse(CompanyVerification.objects.filter(pk=application.pk).exists())
        self.assertFalse(VerificationDocument.objects.filter(pk=document.pk).exists())
        self.assertFalse(VerificationCheck.objects.filter(pk=check.pk).exists())
        self.assertFalse(
            VerificationOperation.objects.filter(company_id=company_id).exists()
        )
        self.assertFalse(
            VerificationAuditEvent.objects.filter(company_id=company_id).exists()
        )
        storage_factory.return_value.delete.assert_any_call(document.storage_key)
        storage_factory.return_value.delete.assert_any_call(
            document.preview_storage_key
        )

    @patch("apps.company.services.get_verification_storage")
    def test_storage_cleanup_failure_rolls_back_draft_delete(self, storage_factory):
        _, application = self._start()
        document = VerificationDocument.objects.create(
            application=application,
            material_revision=1,
            document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
            template_version="business-license-v1",
            storage_key="enterprise-verification/mock/company/license.png",
            sha256="c" * 64,
            size_bytes=128,
            mime_type="image/png",
            scan_status=VerificationDocument.SCAN_PASSED,
        )
        storage_factory.return_value.delete.side_effect = VerificationStorageError(
            "storage_delete_failed"
        )

        ok, message = CompanyService.delete_company_draft(
            self.company.cid,
            self.owner,
        )

        self.assertFalse(ok)
        self.assertEqual(message, "认证材料清理失败，请稍后重试")
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
        self.assertTrue(CompanyVerification.objects.filter(pk=application.pk).exists())
        self.assertTrue(VerificationDocument.objects.filter(pk=document.pk).exists())

    def test_applicant_can_return_to_previous_public_step(self):
        _, application = self._start()
        application.stage = CompanyVerification.STAGE_APPLICANT_FACE
        application.save(update_fields=["stage"])

        payload = return_manual_verification_to_previous_step(
            company_id=self.company.cid,
            application_id=str(application.public_id),
            user=self.owner,
            expected_revision=1,
            idempotency_key="back-to-company-check",
        )
        repeated = return_manual_verification_to_previous_step(
            company_id=self.company.cid,
            application_id=str(application.public_id),
            user=self.owner,
            expected_revision=1,
            idempotency_key="back-to-company-check",
        )

        self.assertEqual(payload, repeated)
        self.assertEqual(
            payload["stage"], CompanyVerification.STAGE_BUSINESS_LICENSE_UPLOAD
        )
        self.assertEqual(payload["revision"], 2)
        self.assertTrue(
            VerificationAuditEvent.objects.filter(
                application=application,
                action="application_returned_to_previous_step",
            ).exists()
        )

    def test_terminal_state_cannot_revive_and_verified_is_ev5_only(self):
        _, application = self._start()
        with self.assertRaises(VerificationDomainError) as captured:
            transition_manual_verification(
                application_id=str(application.public_id),
                expected_revision=1,
                idempotency_key="transition-verified-denied",
                new_status=CompanyVerification.STATUS_VERIFIED,
            )
        self.assertEqual(captured.exception.code, "manual_approval_required")

        cancelled = transition_manual_verification(
            application_id=str(application.public_id),
            expected_revision=1,
            idempotency_key="transition-cancel",
            new_status=CompanyVerification.STATUS_CANCELLED,
        )
        with self.assertRaises(VerificationDomainError) as captured:
            transition_manual_verification(
                application_id=str(cancelled.public_id),
                expected_revision=cancelled.revision,
                idempotency_key="transition-revive",
                new_status=CompanyVerification.STATUS_PENDING,
            )
        self.assertEqual(captured.exception.code, "transition_not_allowed")

    def test_stage_transitions_are_explicit(self):
        _, application = self._start()
        with self.assertRaises(VerificationDomainError):
            transition_manual_verification(
                application_id=str(application.public_id),
                expected_revision=1,
                idempotency_key="transition-skip-stage",
                new_stage=CompanyVerification.STAGE_MANUAL_REVIEW,
            )
        advanced = transition_manual_verification(
            application_id=str(application.public_id),
            expected_revision=1,
            idempotency_key="transition-face",
            new_stage=CompanyVerification.STAGE_APPLICANT_FACE,
        )
        self.assertEqual(advanced.stage, CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertEqual(advanced.revision, 2)
        repeated = transition_manual_verification(
            application_id=str(application.public_id),
            expected_revision=1,
            idempotency_key="transition-face",
            new_stage=CompanyVerification.STAGE_APPLICANT_FACE,
        )
        self.assertEqual(repeated.revision, 2)
        self.assertEqual(
            application.operations.filter(
                operation_kind=VerificationOperation.KIND_TRANSITION
            ).count(),
            1,
        )
        with self.assertRaises(VerificationDomainError) as captured:
            transition_manual_verification(
                application_id=str(application.public_id),
                expected_revision=1,
                idempotency_key="transition-face",
                new_stage=CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
            )
        self.assertEqual(captured.exception.code, "idempotency_conflict")

    def test_expiry_is_audited_and_operationalized(self):
        now = timezone.now()
        _, application = self._start()
        CompanyVerification.objects.filter(pk=application.pk).update(
            expires_at=now - timedelta(seconds=1)
        )

        self.assertEqual(expire_due_manual_verifications(now=now), 1)

        application.refresh_from_db()
        self.assertEqual(application.status, CompanyVerification.STATUS_EXPIRED)
        self.assertTrue(
            application.operations.filter(
                operation_kind=VerificationOperation.KIND_EXPIRE
            ).exists()
        )
        self.assertTrue(
            VerificationAuditEvent.objects.filter(
                application=application,
                action="application_transitioned",
            ).exists()
        )

    def test_expired_application_cannot_be_cancelled(self):
        now = timezone.now()
        _, application = self._start()
        CompanyVerification.objects.filter(pk=application.pk).update(
            expires_at=now - timedelta(seconds=1)
        )
        self.assertEqual(expire_due_manual_verifications(now=now), 1)
        with self.assertRaises(VerificationDomainError) as captured:
            cancel_manual_verification(
                company_id=self.company.cid,
                application_id=str(application.public_id),
                user=self.owner,
                expected_revision=2,
                idempotency_key="cancel-after-expire",
            )
        self.assertEqual(captured.exception.code, "transition_not_allowed")

    def test_manual_snapshot_and_suspension_constraints_are_database_enforced(self):
        stamp = str(time.time())[:20]
        other_company = Company.objects.create(
            account=self.owner,
            cid="ev2-constraint-company",
            company_name="Constraint Company",
            registration_no="91350211M000101Y42",
            created_at=stamp,
            updated_at=stamp,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyVerification.objects.create(
                company=other_company,
                applicant_user=self.owner,
                provider=CompanyVerification.PROVIDER_TENCENT_MANUAL_V1,
                status=CompanyVerification.STATUS_PENDING,
            )

        self.company.verification_suspended_at = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.company.save(update_fields=["verification_suspended_at"])

    def test_active_application_constraint_and_history_protection(self):
        _, application = self._start()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyVerification.objects.create(
                company=self.company,
                provider=CompanyVerification.PROVIDER_TENCENT_ESS,
                status=CompanyVerification.STATUS_PENDING,
            )
        with self.assertRaises(ProtectedError):
            self.company.delete()
        self.assertTrue(CompanyVerification.objects.filter(pk=application.pk).exists())

    def test_public_serializer_contains_no_protected_fields(self):
        _, application = self._start()
        payload = serialize_manual_verification(application)
        serialized = json.dumps(payload)
        self.assertNotIn("encrypted", serialized)
        self.assertNotIn("hmac", serialized.lower())
        self.assertNotIn(IDENTITY_NUMBER, serialized)
        self.assertEqual(payload["version"], payload["revision"])
        self.assertEqual(payload["next_action"], "wait_company_check")
        self.assertIn("cancel", payload["allowed_actions"])
        with self.assertRaises(ProtectedError):
            application.audit_events.get().delete()

    def test_legacy_ess_maintenance_does_not_select_manual_applications(self):
        _, application = self._start()
        self.assertEqual(pending_verifications_for_reconciliation(), [])
        CompanyVerification.objects.filter(pk=application.pk).update(
            status=CompanyVerification.STATUS_INITIATING,
            started_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(expire_stale_initiating_verifications(), 0)

    @override_settings(COMPANY_VERIFICATION_ESS_RECONCILE_ENABLED=False)
    @patch("apps.company.tasks.get_enterprise_verification_provider")
    def test_legacy_reconciliation_is_disabled_by_default(self, provider_factory):
        result = reconcile_pending_company_verifications.run(limit=10)
        self.assertEqual(result, {"processed": 0, "failed": 0, "disabled": True})
        provider_factory.assert_not_called()

    def test_manual_provider_factory_is_explicitly_unavailable(self):
        provider = get_manual_verification_provider()
        with self.assertRaises(VerificationProviderUnavailableError) as captured:
            provider.verify_company(snapshot={}, mode="two_elements")
        self.assertEqual(captured.exception.code, "provider_not_configured")

    def test_http_start_detail_and_cancel_use_opaque_id(self):
        token = sign_token({"user_id": self.owner.id})
        body = {
            "applicant_name": "Applicant",
            "applicant_id_type": "resident_identity_card",
            "applicant_id_number": IDENTITY_NUMBER,
            "consent_version": "privacy-2026-09",
            "idempotency_key": "http-start",
        }
        start = self.client.post(
            f"/api/company/{self.company.cid}/verification/start",
            data=json.dumps(body),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(start.status_code, 200)
        application_id = start.json()["data"]["application_id"]
        self.assertNotEqual(application_id, "1")

        detail = self.client.get(
            f"/api/company/{self.company.cid}/verification/{application_id}",
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["application_id"], application_id)

        cancel = self.client.post(
            f"/api/company/{self.company.cid}/verification/{application_id}/cancel",
            data=json.dumps({"expected_revision": 1, "idempotency_key": "http-cancel"}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.json()["data"]["status"], "cancelled")

    def test_suspension_blocks_formal_access(self):
        self.company.verification_status = Company.VERIFICATION_STATUS_VERIFIED
        self.company.verification_provider = Company.VERIFICATION_PROVIDER_LEGACY_IMPORT
        self.company.verified_at = timezone.now()
        self.company.save(
            update_fields=["verification_status", "verification_provider", "verified_at"]
        )
        self.assertTrue(is_company_verified_for_formal_access(self.company))
        self.company.verification_suspended_at = timezone.now()
        self.company.verification_suspension_reason = "risk_hold"
        self.company.save(
            update_fields=[
                "verification_suspended_at",
                "verification_suspension_reason",
            ]
        )
        self.assertFalse(is_company_verified_for_formal_access(self.company))
        self.assertFalse(can_access_company_draft(self.company, self.owner))
        self.assertEqual(CompanyService.list_companies()["total"], 0)
        self.assertEqual(CompanyService.search_companies("", 0, 20)["total"], 0)
        detail_ok, _ = CompanyService.get_company_detail(
            self.company.cid,
            self.owner,
        )
        follow_ok, _ = FollowingService.follow_company(
            self.owner,
            self.company.cid,
        )
        self.assertFalse(detail_ok)
        self.assertFalse(follow_ok)


class VerificationConfigurationCheckTests(TestCase):
    def test_disabled_configuration_has_no_ev2_errors(self):
        with override_settings(COMPANY_VERIFICATION_ENABLED=False):
            self.assertEqual(enterprise_verification_configuration_check(None), [])

    @override_settings(
        COMPANY_VERIFICATION_ENABLED=True,
        COMPANY_VERIFICATION_PROVIDER="tencent_manual_v1",
        COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT="production",
        COMPANY_VERIFICATION_REGISTRY_MODE="two_elements",
        COMPANY_VERIFICATION_ALLOW_MOCK=False,
        COMPANY_VERIFICATION_SECRETS_LOAD_ERROR="",
        COMPANY_VERIFICATION_PII_ENCRYPTION_KEY="",
        COMPANY_VERIFICATION_PII_HMAC_KEY="",
        COMPANY_VERIFICATION_PII_KEY_VERSION="",
        TENCENT_VERIFICATION_SECRET_ID="",
        TENCENT_VERIFICATION_SECRET_KEY="",
        TENCENT_FACEID_RULE_ID="",
        TENCENT_FACEID_REDIRECT_URL="",
        VERIFICATION_PRIVATE_COS_BUCKET="",
        VERIFICATION_PRIVATE_COS_REGION="",
        VERIFICATION_RETENTION_POLICY="",
    )
    def test_production_configuration_is_fail_closed(self):
        ids = {
            item.id for item in enterprise_verification_configuration_check(None)
        }
        self.assertEqual(
            ids,
            {
                "company.EV2005",
                "company.EV2006",
                "company.EV2010",
                "company.EV3002",
                "company.EV3003",
                "company.EV3005",
                "company.EV4002",
            },
        )
