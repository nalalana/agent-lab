import base64
import json
import time
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.company.models import Company, CompanyMember, CompanyVerification, VerificationCheck, VerificationDocument, VerificationFaceSession, VerificationOperation
from apps.company.providers.manual import (
    ApplicantIdentityPrecheckRequest, ApplicantIdentityPrecheckResult,
    ApplicantIdentityQueryResult,
    ApplicantIdentityStartResult, RegistryVerificationResult,
)
from apps.company.providers.base import VerificationProviderUnavailableError
from apps.company.providers.tencent_manual import TencentManualVerificationProvider
from apps.company.serializers import ManualVerificationEvidenceSerializer
from apps.company.verification_domain import (
    ManualVerificationStartInput,
    VerificationDomainError,
    create_manual_verification,
)
from apps.company.verification_evidence import query_applicant_face, run_registry_verification, start_applicant_face
from apps.iam.models import Grant
from apps.user.models import User
from common.utils.Login_utils import sign_token


IDENTITY = "11010519491231002X"
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
    "TENCENT_FACEID_RULE_ID": "rule-test",
    "TENCENT_FACEID_REDIRECT_URL": "https://example.test/face-return",
    "COMPANY_VERIFICATION_EVIDENCE_REFRESH_RATE_LIMIT_SECONDS": 0,
}


class RegistryPass:
    def verify(self, request):
        return RegistryVerificationResult(
            outcome="passed", retryable=False, matched=True, operating_status="1",
            provider_code="registry_match", request_id="registry-request",
        )


class FaceFake:
    def __init__(self, *, query_result=None, token="server-only-biz-token"):
        self.start_calls = 0
        self.query_calls = 0
        self.query_result = query_result
        self.token = token
        self.last_token = None

    def precheck(self, request):
        self.precheck_request = request
        return ApplicantIdentityPrecheckResult(
            outcome="passed",
            retryable=False,
            matched=True,
            provider_code="identity_match",
            request_id="identity-precheck-request",
        )

    def start(self, request):
        self.start_calls += 1
        self.start_request = request
        return ApplicantIdentityStartResult(
            outcome="started", retryable=False, token=self.token,
            verification_url="https://face.example.test/session", provider_code="face_started",
            request_id="face-start-request", expires_in_seconds=7200,
        )

    def query(self, request):
        self.query_calls += 1
        self.last_token = request.token
        return self.query_result


class FaceConfigurationErrorFake:
    def start(self, request):
        raise VerificationProviderUnavailableError(
            "RuleId does not exist",
            code="InvalidParameterValue.RuleIdNotExist",
        )


@override_settings(**SETTINGS)
class FaceIdVerificationTests(TestCase):
    def setUp(self):
        stamp = str(time.time())[:20]
        self.user = User.objects.create(uid="face-owner", username="face-owner", created_at=stamp, updated_at=stamp)
        self.other = User.objects.create(uid="face-other", username="face-other", created_at=stamp, updated_at=stamp)
        self.company = Company.objects.create(
            account=self.user, cid="face-company", company_name="人脸测试企业",
            registration_no="91350211M000100Y46", establishment_date="2020-01-01",
            address="厦门", created_at=stamp, updated_at=stamp,
        )
        payload = create_manual_verification(
            company_id=self.company.cid, user=self.user,
            value=ManualVerificationStartInput(
                applicant_name="张三", applicant_id_type="resident_identity_card",
                applicant_id_number=IDENTITY, consent_version="privacy-v1", idempotency_key="face-app-start",
            ),
        )
        self.application = CompanyVerification.objects.get(public_id=payload["application_id"])
        run_registry_verification(
            company_id=self.company.cid, application_id=str(self.application.public_id), user=self.user,
            expected_revision=1, idempotency_key="registry-pass", verifier=RegistryPass(),
        )
        self.application.refresh_from_db()
        VerificationOperation.objects.create(
            company=self.company,
            application=self.application,
            operation_kind=VerificationOperation.KIND_APPLICANT_IDENTITY,
            application_revision=self.application.revision,
            idempotency_key="face-identity-precheck",
            request_hash="f" * 64,
            status=VerificationOperation.STATUS_COMPLETED,
            result={
                "identity_precheck": "passed",
                "identity_hmac": self.application.applicant_id_hmac,
            },
            completed_at=timezone.now(),
        )

    def add_valid_document_check(self, *, document_type, kind, result):
        self.application.material_revision = 1
        self.application.save(update_fields=("material_revision",))
        document = VerificationDocument.objects.create(
            application=self.application,
            material_revision=1,
            document_type=document_type,
            template_version="test-v1",
            storage_key=f"test/{self.application.public_id}/{document_type}",
            sha256="a" * 64,
            size_bytes=1,
            mime_type="image/png",
            scan_status=VerificationDocument.SCAN_PASSED,
        )
        VerificationCheck.objects.create(
            application=self.application,
            kind=kind,
            attempt_no=1,
            input_digest=f"{document_type}-digest".ljust(64, "0")[:64],
            material_revision=1,
            document=document,
            provider="test",
            action="test",
            provider_environment="mock",
            status=VerificationCheck.STATUS_PASSED,
            result=result,
            rule_version="manual_v1",
            checked_at=timezone.now(),
            valid_until=timezone.now() + timedelta(hours=1),
        )
        return document

    def start_face(self, fake=None, key="face-start"):
        fake = fake or FaceFake()
        payload = start_applicant_face(
            company_id=self.company.cid, application_id=str(self.application.public_id), user=self.user,
            expected_revision=2, idempotency_key=key, verifier=fake,
        )
        return fake, payload

    def query_face(self, result, key="face-query", fake=None):
        face = self.application.face_sessions.order_by("-attempt_no").first()
        result = result or ApplicantIdentityQueryResult(
            outcome="passed", retryable=False, name="张三", identity_number=IDENTITY,
            identity_type="resident_identity_card", correlation_id=str(face.id),
            provider_code="face_0", request_id="face-query-request",
        )
        fake = fake or FaceFake(query_result=result)
        payload = query_applicant_face(
            company_id=self.company.cid, application_id=str(self.application.public_id), user=self.user,
            expected_revision=2, idempotency_key=key, verifier=fake,
        )
        return fake, payload

    def test_start_encrypts_token_and_repeated_start_reuses_current_attempt(self):
        fake, payload = self.start_face()
        face = VerificationFaceSession.objects.get(application=self.application)
        self.assertEqual(face.status, VerificationFaceSession.STATUS_IN_PROGRESS)
        self.assertNotIn("server-only-biz-token", face.token_encrypted)
        self.assertNotIn("server-only-biz-token", json.dumps(payload))
        self.assertEqual(payload["face_start"]["verification_url"], "https://face.example.test/session")
        second_fake = FaceFake()
        _, repeated = self.start_face(second_fake, key="different-face-key")
        self.assertEqual(second_fake.start_calls, 0)
        self.assertEqual(repeated["face_start"]["session_id"], str(face.id))
        self.assertEqual(VerificationFaceSession.objects.count(), 1)

    def test_start_rejects_identity_without_current_two_element_precheck(self):
        self.application.operations.filter(
            operation_kind=VerificationOperation.KIND_APPLICANT_IDENTITY
        ).update(result={"identity_precheck": "not_started"})
        fake = FaceFake()

        with self.assertRaises(VerificationDomainError) as captured:
            self.start_face(fake=fake, key="face-without-identity-precheck")

        self.assertEqual(captured.exception.code, "applicant_identity_required")
        self.assertEqual(fake.start_calls, 0)
        self.assertFalse(self.application.face_sessions.exists())

    def test_rule_id_error_is_explicit_and_does_not_consume_face_budget(self):
        for index in range(3):
            with self.assertRaises(VerificationDomainError) as captured:
                self.start_face(
                    fake=FaceConfigurationErrorFake(),
                    key=f"invalid-rule-{index}",
                )
            self.assertEqual(captured.exception.code, "face_rule_not_found")

        fake, payload = self.start_face(fake=FaceFake(), key="valid-rule-after-fix")
        self.assertEqual(fake.start_calls, 1)
        self.assertEqual(payload["face_start"]["status"], "in_progress")

    def test_legal_representative_skips_authorization_after_face_passes(self):
        self.application.applicant_role = (
            CompanyVerification.APPLICANT_ROLE_LEGAL_REPRESENTATIVE
        )
        self.application.save(update_fields=("applicant_role",))
        self.add_valid_document_check(
            document_type=VerificationDocument.TYPE_BUSINESS_LICENSE,
            kind=VerificationCheck.KIND_BUSINESS_LICENSE,
            result={"adopted": True, "legal_representative": "张三"},
        )
        fake, _ = self.start_face()
        self.assertEqual(fake.start_request.name, "张三")
        self.assertEqual(fake.start_request.identity_number, IDENTITY)
        _, payload = self.query_face(None)
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_READY_TO_SUBMIT)

    def test_authorized_employee_face_passes_only_after_valid_authorization(self):
        self.application.applicant_role = CompanyVerification.APPLICANT_ROLE_AUTHORIZED_PERSON
        self.application.save(update_fields=("applicant_role",))
        self.add_valid_document_check(
            document_type=VerificationDocument.TYPE_AUTHORIZATION,
            kind=VerificationCheck.KIND_CONSISTENCY,
            result={"adopted": True},
        )
        fake, _ = self.start_face()
        self.assertEqual(fake.start_request.name, "张三")
        self.assertEqual(fake.start_request.identity_number, IDENTITY)
        _, payload = self.query_face(None)
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_READY_TO_SUBMIT)

    @patch("apps.company.verification_evidence.get_applicant_identity_verifier")
    def test_http_face_flow_uses_only_server_bound_token_and_identity(self, provider_factory):
        fake = FaceFake()
        provider_factory.return_value = fake
        token = sign_token({"user_id": self.user.id})
        start_url = (
            f"/api/company/{self.company.cid}/verification/"
            f"{self.application.public_id}/face/start"
        )
        response = self.client.post(
            start_url,
            data=json.dumps({"expected_revision": 2, "idempotency_key": "face-http-start"}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        start_payload = response.json()["data"]
        session_id = start_payload["face_start"]["session_id"]
        serialized_start = json.dumps(start_payload)
        self.assertNotIn("server-only-biz-token", serialized_start)
        self.assertNotIn(IDENTITY, serialized_start)

        fake.query_result = ApplicantIdentityQueryResult(
            outcome="passed", retryable=False, name="张三", identity_number=IDENTITY,
            identity_type="resident_identity_card", correlation_id=session_id,
            provider_code="face_0", request_id="face-http-query",
        )
        refresh_url = (
            f"/api/company/{self.company.cid}/verification/"
            f"{self.application.public_id}/face/refresh"
        )
        refreshed = self.client.post(
            refresh_url,
            data=json.dumps({"expected_revision": 2, "idempotency_key": "face-http-refresh"}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(
            refreshed.json()["data"]["stage"],
            CompanyVerification.STAGE_AUTHORIZATION_UPLOAD,
        )
        self.assertEqual(fake.last_token, "server-only-biz-token")
        self.assertNotIn(IDENTITY, json.dumps(refreshed.json()))

    @patch("apps.company.verification_evidence.get_applicant_identity_verifier")
    def test_http_face_start_rejects_client_token_and_cross_user(self, provider_factory):
        url = (
            f"/api/company/{self.company.cid}/verification/"
            f"{self.application.public_id}/face/start"
        )
        owner_token = sign_token({"user_id": self.user.id})
        other_token = sign_token({"user_id": self.other.id})
        tampered = self.client.post(
            url,
            data=json.dumps({
                "expected_revision": 2, "idempotency_key": "face-http-tampered",
                "token": "attacker-controlled-token", "success": True,
            }),
            content_type="application/json", HTTP_ACCESS_TOKEN=owner_token,
        )
        self.assertEqual(tampered.status_code, 400)
        forbidden = self.client.post(
            url,
            data=json.dumps({"expected_revision": 2, "idempotency_key": "face-http-cross"}),
            content_type="application/json", HTTP_ACCESS_TOKEN=other_token,
        )
        self.assertEqual(forbidden.status_code, 403)
        provider_factory.assert_not_called()

    def test_trusted_server_query_advances_to_authorization_only(self):
        before = (Grant.objects.count(), CompanyMember.objects.count())
        self.start_face()
        fake, payload = self.query_face(None)
        self.company.refresh_from_db()
        face = self.application.face_sessions.get()
        face.refresh_from_db()
        self.assertEqual(fake.last_token, "server-only-biz-token")
        self.assertEqual(face.status, VerificationFaceSession.STATUS_PASSED)
        self.assertTrue(face.identity_match)
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_AUTHORIZATION_UPLOAD)
        self.assertEqual(payload["status"], CompanyVerification.STATUS_PENDING)
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)
        self.assertEqual(before, (Grant.objects.count(), CompanyMember.objects.count()))

    def test_wrong_identity_or_correlation_never_passes(self):
        self.start_face()
        face = self.application.face_sessions.get()
        _, payload = self.query_face(ApplicantIdentityQueryResult(
            outcome="passed", retryable=False, name="李四", identity_number=IDENTITY,
            identity_type="resident_identity_card", correlation_id=str(face.id), provider_code="face_0",
        ))
        face.refresh_from_db()
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertEqual(payload["applicant_check"], VerificationCheck.STATUS_FAILED)
        self.assertEqual(face.status, VerificationFaceSession.STATUS_FAILED)
        self.assertFalse(face.identity_match)

    def test_intermediate_failure_keeps_attempt_then_same_server_token_can_pass(self):
        self.start_face()
        first, payload = self.query_face(ApplicantIdentityQueryResult(
            outcome="in_progress", retryable=True, provider_code="face_result_pending"
        ), key="query-pending")
        face = self.application.face_sessions.get()
        face.refresh_from_db()
        self.assertEqual(face.status, VerificationFaceSession.STATUS_IN_PROGRESS)
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        second, payload = self.query_face(None, key="query-success")
        self.assertEqual(first.last_token, second.last_token)
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_AUTHORIZATION_UPLOAD)

    def test_cross_user_query_is_rejected_before_token_access(self):
        self.start_face()
        fake = FaceFake(query_result=ApplicantIdentityQueryResult(outcome="passed", retryable=False))
        with self.assertRaises(VerificationDomainError) as captured:
            query_applicant_face(
                company_id=self.company.cid, application_id=str(self.application.public_id), user=self.other,
                expected_revision=2, idempotency_key="cross-query", verifier=fake,
            )
        self.assertEqual(captured.exception.code, "application_forbidden")
        self.assertEqual(fake.query_calls, 0)

    def test_lost_start_response_is_bounded_then_recoverable_with_new_key(self):
        now = timezone.now()
        face = VerificationFaceSession.objects.create(
            application=self.application, attempt_no=1, provider=self.application.provider,
            provider_environment=self.application.provider_environment, rule_id="rule-test",
            status=VerificationFaceSession.STATUS_CREATED,
        )
        operation = VerificationOperation.objects.create(
            company=self.company, application=self.application,
            operation_kind=VerificationOperation.KIND_FACE_START,
            application_revision=2, face_attempt_no=1, idempotency_key="lost-start",
            request_hash="a" * 64, status=VerificationOperation.STATUS_RUNNING, attempts=1,
            lease_until=now + timedelta(seconds=30), result={"face_session_id": str(face.id)},
        )
        fake = FaceFake(token="recovered-token")
        with self.assertRaises(VerificationDomainError) as active:
            start_applicant_face(
                company_id=self.company.cid, application_id=str(self.application.public_id),
                user=self.user, expected_revision=2, idempotency_key="new-start",
                verifier=fake, now=now,
            )
        self.assertEqual(active.exception.code, "operation_in_progress")
        self.assertEqual(fake.start_calls, 0)
        VerificationOperation.objects.filter(pk=operation.pk).update(lease_until=now - timedelta(seconds=1))
        payload = start_applicant_face(
            company_id=self.company.cid, application_id=str(self.application.public_id),
            user=self.user, expected_revision=2, idempotency_key="new-start",
            verifier=fake, now=now,
        )
        face.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual(face.status, VerificationFaceSession.STATUS_FAILED)
        self.assertEqual(operation.last_error_code, "ambiguous_face_start")
        self.assertEqual(payload["face_start"]["attempt_no"], 2)
        self.assertEqual(fake.start_calls, 1)

    def test_expired_provider_token_allows_controlled_new_attempt(self):
        self.start_face()
        face = self.application.face_sessions.get()
        VerificationFaceSession.objects.filter(pk=face.pk).update(provider_expires_at=timezone.now() - timedelta(seconds=1))
        second = FaceFake(token="second-server-token")
        _, payload = self.start_face(second, key="face-restart")
        face.refresh_from_db()
        self.assertEqual(face.status, VerificationFaceSession.STATUS_EXPIRED)
        self.assertEqual(payload["face_start"]["attempt_no"], 2)
        self.assertEqual(second.start_calls, 1)

    def test_late_old_attempt_is_historical_and_does_not_advance(self):
        self.start_face()
        old = self.application.face_sessions.get()

        class LateResult(FaceFake):
            def query(inner_self, request):
                VerificationFaceSession.objects.filter(pk=old.pk).update(status=VerificationFaceSession.STATUS_FAILED)
                inner_self.new = VerificationFaceSession.objects.create(
                    application=self.application, attempt_no=2, provider=self.application.provider,
                    provider_environment=self.application.provider_environment, status=VerificationFaceSession.STATUS_CREATED,
                )
                return ApplicantIdentityQueryResult(
                    outcome="passed", retryable=False, name="张三", identity_number=IDENTITY,
                    identity_type="resident_identity_card", correlation_id=str(old.id), provider_code="face_0",
                )

        payload = query_applicant_face(
            company_id=self.company.cid, application_id=str(self.application.public_id), user=self.user,
            expected_revision=2, idempotency_key="late-query", verifier=LateResult(),
        )
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.application.refresh_from_db()
        self.assertEqual(self.application.revision, 2)

    def test_face_serializer_rejects_frontend_token_success_and_mode(self):
        serializer = ManualVerificationEvidenceSerializer(data={
            "expected_revision": 2, "idempotency_key": "client-tamper",
            "token": "attacker-token", "success": True, "mode": "four_elements",
        })
        self.assertFalse(serializer.is_valid())


@override_settings(**SETTINGS)
class TencentFaceAdapterContractTests(SimpleTestCase):
    def test_identity_precheck_uses_authoritative_two_element_api(self):
        class Response:
            Result = "0"
            RequestId = "identity-request"

        class FaceClient:
            def IdCardVerification(self, request):
                self.request = request
                return Response()

        face_client = FaceClient()
        provider = TencentManualVerificationProvider(
            registry_client=object(), face_client=face_client
        )
        result = provider.precheck(ApplicantIdentityPrecheckRequest(
            name="张三",
            identity_number=IDENTITY,
            identity_type="resident_identity_card",
        ))

        self.assertEqual(face_client.request.Name, "张三")
        self.assertEqual(face_client.request.IdCard, IDENTITY)
        self.assertEqual(result.outcome, "passed")
        self.assertTrue(result.matched)
        self.assertEqual(result.provider_code, "identity_match")

    def test_identity_precheck_maps_mismatch_without_starting_face(self):
        class Response:
            Result = "-1"
            RequestId = "identity-mismatch-request"

        class FaceClient:
            def __init__(self):
                self.detect_auth_calls = 0

            def IdCardVerification(self, request):
                return Response()

            def DetectAuth(self, request):
                self.detect_auth_calls += 1

        face_client = FaceClient()
        provider = TencentManualVerificationProvider(
            registry_client=object(), face_client=face_client
        )
        result = provider.precheck(ApplicantIdentityPrecheckRequest(
            name="张三",
            identity_number=IDENTITY,
            identity_type="resident_identity_card",
        ))

        self.assertEqual(result.outcome, "failed")
        self.assertFalse(result.matched)
        self.assertEqual(result.provider_code, "identity_mismatch")
        self.assertEqual(face_client.detect_auth_calls, 0)

    def test_query_requests_text_only_and_never_image_or_video(self):
        class Text:
            ErrCode = 0
            Name = "张三"
            IdCard = IDENTITY
            UseIDType = 0
            Extra = "correlation"
            LiveStatus = 0
            Comparestatus = 0

        class Response:
            def __init__(self):
                self.Text = Text()
                self.RequestId = "request-id"

        class FaceClient:
            def GetDetectInfoEnhanced(self, request):
                self.request = request
                return Response()

        face_client = FaceClient()
        provider = TencentManualVerificationProvider(registry_client=object(), face_client=face_client)
        from apps.company.providers.manual import ApplicantIdentityQueryRequest
        result = provider.query(ApplicantIdentityQueryRequest(token="token", rule_id="rule-test"))
        request = face_client.request
        self.assertEqual(result.outcome, "passed")
        self.assertEqual(request.InfoType, "1")
        self.assertEqual(request.BestFramesCount, 0)
        self.assertFalse(request.IsCutIdCardImage)
        self.assertFalse(request.IsNeedIdCardAvatar)
        self.assertFalse(request.IsReturnAllVideo)

    def test_query_fails_closed_when_detail_status_conflicts_with_success_code(self):
        class Text:
            ErrCode = 0
            Name = "张三"
            IdCard = IDENTITY
            UseIDType = 0
            Extra = "correlation"
            LiveStatus = 0
            Comparestatus = 1

        class Response:
            def __init__(self):
                self.Text = Text()
                self.RequestId = "request-id"

        class FaceClient:
            def GetDetectInfoEnhanced(self, request):
                return Response()

        provider = TencentManualVerificationProvider(
            registry_client=object(), face_client=FaceClient()
        )
        from apps.company.providers.manual import ApplicantIdentityQueryRequest
        result = provider.query(
            ApplicantIdentityQueryRequest(token="token", rule_id="rule-test")
        )
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.provider_code, "face_incomplete_result")
