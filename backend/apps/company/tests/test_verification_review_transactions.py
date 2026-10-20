from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import json
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings

from apps.company.models import (
    CompanyMember,
    CompanyVerification,
    CompanyVerificationReviewer,
    VerificationAuditEvent,
    VerificationNotification,
    VerificationOperation,
    VerificationReview,
)
from apps.company.providers.manual import RegistryVerificationResult
from apps.company.services import CompanyService
from apps.company.verification_domain import (
    VerificationDomainError,
    cancel_manual_verification,
    transition_manual_verification,
)
from apps.company.verification_reviews import (
    decide_manual_verification,
    process_review_evidence_refresh,
    process_verification_notification,
    send_in_app_verification_notification,
    submit_manual_verification,
)
from apps.iam.models import Grant, Principal, Resource
from apps.user.models import User
from apps.notification.models import Notification
from common.utils.Login_utils import sign_token

from .ev5_test_support import EV5FixtureMixin


EV5_SETTINGS = dict(
    COMPANY_VERIFICATION_ENABLED=True,
    COMPANY_VERIFICATION_ALLOW_MOCK=True,
    COMPANY_VERIFICATION_REVIEW_ALLOW_MOCK=True,
    COMPANY_VERIFICATION_AUTHORIZATION_TEMPLATE_VERSION="authorization-v1-draft",
)


@override_settings(**EV5_SETTINGS)
class VerificationReviewTransactionTests(EV5FixtureMixin, TestCase):
    def _submit(self):
        return submit_manual_verification(
            company_id=self.company.cid,
            application_id=str(self.application.public_id),
            user=self.applicant,
            expected_revision=self.application.revision,
            idempotency_key="submit-1",
            now=self.now,
        )

    def _approve(self, *, key="approve-1", reviewer=None, now=None, expected_revision=None):
        self.application.refresh_from_db()
        return decide_manual_verification(
            user=reviewer or self.reviewer_user,
            application_id=str(self.application.public_id),
            expected_revision=(
                self.application.revision if expected_revision is None else expected_revision
            ),
            idempotency_key=key,
            action="approve",
            reason_public="认证通过",
            reason_internal="isolated test approval",
            checklist=self.approval_checklist(),
            now=now or self.now,
        )

    def test_submit_freezes_versions_and_approve_is_the_atomic_manual_path(self):
        initial_counts = {
            "grant": Grant.objects.count(),
            "principal": Principal.objects.count(),
            "resource": Resource.objects.count(),
            "member": CompanyMember.objects.count(),
        }
        submitted = self._submit()
        self.assertEqual(submitted["stage"], CompanyVerification.STAGE_MANUAL_REVIEW)
        self.assertEqual(submitted["submitted_revision"], submitted["revision"])
        self.assertEqual(submitted["submitted_material_revision"], 1)

        submitted_revision = submitted["revision"]
        approved = self._approve(expected_revision=submitted_revision)
        self.assertEqual(approved["status"], CompanyVerification.STATUS_VERIFIED)
        self.assertEqual(approved["stage"], CompanyVerification.STAGE_COMPLETED)
        self.company.refresh_from_db()
        self.application.refresh_from_db()
        self.assertEqual(self.company.verification_status, self.company.VERIFICATION_STATUS_VERIFIED)
        self.assertEqual(self.company.verified_verification_id, self.application.id)
        self.assertEqual(self.company.verification_provider, self.company.VERIFICATION_PROVIDER_TENCENT_MANUAL_V1)
        self.assertEqual(VerificationReview.objects.filter(application=self.application).count(), 1)
        self.assertEqual(
            VerificationNotification.objects.filter(application=self.application).count(), 2
        )
        self.assertEqual(Grant.objects.count(), initial_counts["grant"] + 1)
        self.assertEqual(Principal.objects.count(), initial_counts["principal"] + 1)
        self.assertEqual(Resource.objects.count(), initial_counts["resource"] + 1)
        self.assertEqual(CompanyMember.objects.count(), initial_counts["member"])
        grant = Grant.objects.get(
            principal__type=Principal.TYPE_USER,
            principal__external_id=self.applicant.uid,
            resource__type=Resource.TYPE_COMPANY,
            resource__external_id=self.company.cid,
        )
        self.assertEqual(grant.permissions, Grant.PERM_SUPER_ADMIN)
        self.assertIsNone(grant.expires_at)
        self.assertTrue(grant.inherit)
        self.assertEqual(grant.granter, self.reviewer_user)
        ok, companies = CompanyService.list_companies_by_user(self.applicant, 20, 0)
        self.assertTrue(ok)
        self.assertIn(self.company.cid, {item["cid"] for item in companies["items"]})
        self.assertTrue(VerificationAuditEvent.objects.filter(
            application=self.application,
            action="verified_company_management_granted",
            detail__grant_id=grant.id,
        ).exists())

        replay = self._approve(expected_revision=submitted_revision)
        self.assertEqual(replay["status"], CompanyVerification.STATUS_VERIFIED)
        self.assertEqual(VerificationReview.objects.filter(application=self.application).count(), 1)
        self.assertEqual(Grant.objects.count(), initial_counts["grant"] + 1)

    def test_reject_and_supplement_never_verify_or_create_authorization(self):
        self._submit()
        self.application.refresh_from_db()
        result = decide_manual_verification(
            user=self.reviewer_user,
            application_id=str(self.application.public_id),
            expected_revision=self.application.revision,
            idempotency_key="supplement-1",
            action="request_supplement",
            reason_public="请重新上传清晰授权书",
            reason_internal="seal region is cropped",
            checklist=self.approval_checklist(authorization_document=False),
            supplement_fields=["authorization_document"],
            now=self.now,
        )
        self.assertEqual(result["stage"], CompanyVerification.STAGE_SUPPLEMENT_REQUIRED)
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_status, self.company.VERIFICATION_STATUS_UNVERIFIED)
        self.assertFalse(Grant.objects.exists())
        self.assertFalse(CompanyMember.objects.exists())

    def test_reject_is_terminal_and_repeat_decision_conflicts(self):
        self._submit()
        self.application.refresh_from_db()
        result = decide_manual_verification(
            user=self.reviewer_user,
            application_id=str(self.application.public_id),
            expected_revision=self.application.revision,
            idempotency_key="reject-1",
            action="reject",
            reason_public="授权内容与认证事项不符",
            reason_internal="actual document content is wrong",
            checklist=self.approval_checklist(authorization_document=False),
            now=self.now,
        )
        self.assertEqual(result["status"], CompanyVerification.STATUS_REJECTED)
        with self.assertRaises(VerificationDomainError):
            decide_manual_verification(
                user=self.other_reviewer_user,
                application_id=str(self.application.public_id),
                expected_revision=result["revision"],
                idempotency_key="approve-after-reject",
                action="approve",
                reason_public="",
                reason_internal="must not revive",
                checklist=self.approval_checklist(),
                now=self.now,
            )

    def test_revoked_expired_and_self_reviewer_are_rechecked_at_decision_time(self):
        self._submit()
        self.reviewer.enabled = False
        self.reviewer.revoked_at = self.now
        self.reviewer.save(update_fields=("enabled", "revoked_at"))
        with self.assertRaises(VerificationDomainError) as captured:
            self._approve()
        self.assertEqual(captured.exception.code, "reviewer_forbidden")

        applicant_reviewer = CompanyVerificationReviewer.objects.create(
            user=self.applicant,
            enabled=True,
            can_final_review=True,
            reason="test",
            granted_by=self.root,
            expires_at=self.now + timedelta(hours=1),
        )
        self.assertIsNotNone(applicant_reviewer)
        with self.assertRaises(VerificationDomainError) as captured:
            self._approve(key="self-approve", reviewer=self.applicant)
        self.assertEqual(captured.exception.code, "review_self_forbidden")

        self.other_reviewer.expires_at = self.now - timedelta(seconds=1)
        self.other_reviewer.save(update_fields=("expires_at",))
        with self.assertRaises(VerificationDomainError) as captured:
            self._approve(key="expired-approve", reviewer=self.other_reviewer_user)
        self.assertEqual(captured.exception.code, "reviewer_expired")

    def test_stale_registry_is_scheduled_refreshed_then_rechecked(self):
        self._submit()
        self.application.refresh_from_db()
        self.application.checks.filter(kind="registry").update(
            valid_until=self.now - timedelta(seconds=1)
        )
        with self.assertRaises(VerificationDomainError) as captured:
            self._approve(now=self.now)
        self.assertEqual(captured.exception.code, "evidence_refresh_pending")
        operation = VerificationOperation.objects.get(
            application=self.application,
            operation_kind=VerificationOperation.KIND_REVIEW_EVIDENCE_REFRESH,
        )
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_status, self.company.VERIFICATION_STATUS_UNVERIFIED)

        class Verifier:
            def verify(self, request):
                return RegistryVerificationResult(
                    outcome="passed",
                    retryable=False,
                    matched=True,
                    operating_status="1",
                    provider_code="0",
                    request_id="refresh-request",
                    evidence={"status_code": "1", "verify_result": "1"},
                )

        refreshed = process_review_evidence_refresh(
            operation_id=str(operation.id), verifier=Verifier(), now=self.now
        )
        self.assertTrue(refreshed["passed"])
        approved = self._approve(now=self.now + timedelta(seconds=1))
        self.assertEqual(approved["status"], CompanyVerification.STATUS_VERIFIED)

    def test_notification_failure_is_retryable_and_never_rolls_back_approval(self):
        self._submit()
        self._approve()
        notification = VerificationNotification.objects.get(
            application=self.application,
            event_type=VerificationNotification.EVENT_APPROVED,
        )

        def fail(_payload):
            raise RuntimeError("delivery unavailable")

        result = process_verification_notification(
            notification_id=str(notification.id), sender=fail, now=self.now
        )
        self.assertFalse(result["processed"])
        notification.refresh_from_db()
        self.assertEqual(notification.status, VerificationNotification.STATUS_FAILED)
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_status, self.company.VERIFICATION_STATUS_VERIFIED)

        delivered = []
        result = process_verification_notification(
            notification_id=str(notification.id),
            sender=delivered.append,
            now=notification.next_run_at,
        )
        self.assertTrue(result["processed"])
        self.assertEqual(delivered[0]["event_type"], VerificationNotification.EVENT_APPROVED)

    @patch("apps.notification.consumers.send_notification_via_websocket")
    def test_default_in_app_sender_is_idempotent(self, websocket_sender):
        self._submit()
        self._approve()
        outbox = VerificationNotification.objects.get(
            application=self.application,
            event_type=VerificationNotification.EVENT_APPROVED,
        )
        delivery = {
            "notification_id": str(outbox.id),
            "recipient_uid": self.applicant.uid,
            "event_type": outbox.event_type,
            "application_id": str(self.application.public_id),
            "application_revision": outbox.application_revision,
            "payload": dict(outbox.payload),
        }
        first_id = send_in_app_verification_notification(delivery)
        second_id = send_in_app_verification_notification(delivery)
        self.assertEqual(first_id, second_id)
        self.assertEqual(Notification.objects.filter(
            deduplication_key=f"enterprise-verification:{outbox.id}"
        ).count(), 1)
        self.assertEqual(websocket_sender.call_count, 2)

    def test_http_submit_queue_decision_and_applicant_receipt_contract(self):
        applicant_token = sign_token({"user_id": self.applicant.id})
        reviewer_token = sign_token({"user_id": self.reviewer_user.id})
        submit_response = self.client.post(
            f"/api/company/{self.company.cid}/verification/{self.application.public_id}/submit",
            data=json.dumps({"expected_revision": 10, "idempotency_key": "http-submit"}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=applicant_token,
        )
        self.assertEqual(submit_response.status_code, 200, submit_response.content)
        submitted = submit_response.json()["data"]

        queue_response = self.client.get(
            "/api/company/verification/reviews/queue?queue=final_review",
            HTTP_ACCESS_TOKEN=reviewer_token,
        )
        self.assertEqual(queue_response.status_code, 200, queue_response.content)
        self.assertEqual(queue_response.json()["data"]["total"], 1)

        decision_response = self.client.post(
            f"/api/company/verification/reviews/applications/{self.application.public_id}/decision",
            data=json.dumps({
                "expected_revision": submitted["revision"],
                "idempotency_key": "http-approve",
                "action": "approve",
                "reason_public": "认证通过",
                "reason_internal": "http contract test",
                "checklist": self.approval_checklist(),
            }),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=reviewer_token,
        )
        self.assertEqual(decision_response.status_code, 200, decision_response.content)
        self.assertEqual(
            decision_response.json()["data"]["status"],
            CompanyVerification.STATUS_VERIFIED,
        )

        receipt_response = self.client.get(
            f"/api/company/{self.company.cid}/verification/{self.application.public_id}",
            HTTP_ACCESS_TOKEN=applicant_token,
        )
        self.assertEqual(receipt_response.status_code, 200, receipt_response.content)
        receipt = receipt_response.json()["data"]
        self.assertEqual(receipt["review"]["action"], "approve")
        self.assertNotIn("reason_internal", receipt["review"])

    def test_http_reviewer_management_requires_root_and_is_audited(self):
        target = self.make_user("http-reviewer")
        payload = {
            "target_user_uid": target.uid,
            "capabilities": ["pre_review"],
            "reason": "HTTP qualification test",
        }
        forbidden = self.client.post(
            "/api/company/verification/reviewers",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=sign_token({"user_id": self.reviewer_user.id}),
        )
        self.assertEqual(forbidden.status_code, 403)

        granted = self.client.post(
            "/api/company/verification/reviewers",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=sign_token({"user_id": self.root.id}),
        )
        self.assertEqual(granted.status_code, 200, granted.content)
        reviewer_id = granted.json()["data"]["reviewer_id"]
        revoked = self.client.post(
            f"/api/company/verification/reviewers/{reviewer_id}/revoke",
            data=json.dumps({"reason": "HTTP qualification test completed"}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=sign_token({"user_id": self.root.id}),
        )
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertFalse(revoked.json()["data"]["enabled"])


@override_settings(**EV5_SETTINGS)
class VerificationReviewConcurrencyTests(EV5FixtureMixin, TransactionTestCase):
    reset_sequences = True

    def _prepare_manual_review(self):
        self.application.stage = CompanyVerification.STAGE_MANUAL_REVIEW
        self.application.submitted_at = self.now
        self.application.submitted_revision = self.application.revision
        self.application.submitted_material_revision = self.application.material_revision
        self.application.save(update_fields=(
            "stage", "submitted_at", "submitted_revision", "submitted_material_revision"
        ))

    def _decision_worker(self, user_id, key, barrier):
        close_old_connections()
        try:
            user = User.objects.get(pk=user_id)
            barrier.wait(timeout=10)
            result = decide_manual_verification(
                user=user,
                application_id=str(self.application.public_id),
                expected_revision=10,
                idempotency_key=key,
                action="approve",
                reason_public="认证通过",
                reason_internal="concurrency test",
                checklist=self.approval_checklist(),
                now=self.now,
            )
            return result["status"]
        except VerificationDomainError as exc:
            return exc.code
        finally:
            close_old_connections()

    def test_two_reviewers_cannot_both_approve_same_revision(self):
        self._prepare_manual_review()
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda args: self._decision_worker(*args, barrier),
                [
                    (self.reviewer_user.id, "approve-a"),
                    (self.other_reviewer_user.id, "approve-b"),
                ],
            ))
        self.assertEqual(results.count(CompanyVerification.STATUS_VERIFIED), 1)
        self.assertEqual(VerificationReview.objects.filter(application=self.application).count(), 1)
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_status, self.company.VERIFICATION_STATUS_VERIFIED)

    def test_cancel_and_approve_race_has_one_terminal_result(self):
        self._prepare_manual_review()
        barrier = Barrier(2)

        def approve():
            return self._decision_worker(self.reviewer_user.id, "race-approve", barrier)

        def cancel():
            close_old_connections()
            try:
                applicant = User.objects.get(pk=self.applicant.id)
                barrier.wait(timeout=10)
                result = cancel_manual_verification(
                    company_id=self.company.cid,
                    application_id=str(self.application.public_id),
                    user=applicant,
                    expected_revision=10,
                    idempotency_key="race-cancel",
                    now=self.now,
                )
                return result["status"]
            except VerificationDomainError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(approve), pool.submit(cancel)]
            results = [future.result(timeout=20) for future in results]
        self.application.refresh_from_db()
        self.assertIn(
            self.application.status,
            {CompanyVerification.STATUS_VERIFIED, CompanyVerification.STATUS_CANCELLED},
        )
        self.assertEqual(
            sum(value in {CompanyVerification.STATUS_VERIFIED, CompanyVerification.STATUS_CANCELLED} for value in results),
            1,
        )

    def test_expire_and_approve_race_has_one_terminal_result(self):
        self._prepare_manual_review()
        barrier = Barrier(2)

        def approve():
            return self._decision_worker(self.reviewer_user.id, "expire-race-approve", barrier)

        def expire():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                application = transition_manual_verification(
                    application_id=str(self.application.public_id),
                    expected_revision=10,
                    idempotency_key="expire-race",
                    new_status=CompanyVerification.STATUS_EXPIRED,
                    reason_code="application_expired",
                    now=self.now,
                )
                return application.status
            except VerificationDomainError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve), pool.submit(expire)]
            results = [future.result(timeout=20) for future in futures]
        self.application.refresh_from_db()
        self.assertIn(
            self.application.status,
            {CompanyVerification.STATUS_VERIFIED, CompanyVerification.STATUS_EXPIRED},
        )
        self.assertEqual(
            sum(value in {CompanyVerification.STATUS_VERIFIED, CompanyVerification.STATUS_EXPIRED} for value in results),
            1,
        )
