import base64
import json
import time
from unittest.mock import Mock, patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from apps.company.models import Company, CompanyMember, CompanyVerification, VerificationCheck, VerificationOperation
from apps.company.providers.manual import (
    DocumentExtractionRequest,
    RegistryVerificationRequest,
    RegistryVerificationResult,
)
from apps.company.providers.tencent_manual import TencentManualVerificationProvider
from apps.company.verification_domain import ManualVerificationStartInput, VerificationDomainError, create_manual_verification
from apps.company.verification_evidence import retry_due_manual_evidence, run_registry_verification
from apps.iam.models import Grant
from apps.user.models import User
from common.utils.Login_utils import sign_token


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
}


class RegistryFake:
    def __init__(self, result, *, assert_outside=False):
        self.result = result
        self.calls = 0
        self.assert_outside = assert_outside

    def verify(self, request):
        self.calls += 1
        if self.assert_outside:
            assert not connection.in_atomic_block
        self.request = request
        return self.result


class RegistryFixtureMixin:
    def make_fixture(self, suffix="registry"):
        stamp = str(time.time())[:20]
        self.user = User.objects.create(uid=f"{suffix}-owner", username=f"{suffix}-owner", created_at=stamp, updated_at=stamp)
        self.other = User.objects.create(uid=f"{suffix}-other", username=f"{suffix}-other", created_at=stamp, updated_at=stamp)
        self.company = Company.objects.create(
            account=self.user, cid=f"{suffix}-company", company_name="测试企业有限公司",
            registration_no="91350211M000100Y46", establishment_date="2020-01-01",
            address="厦门", created_at=stamp, updated_at=stamp,
        )
        payload = create_manual_verification(
            company_id=self.company.cid, user=self.user,
            value=ManualVerificationStartInput(
                applicant_name="张三", applicant_id_type="resident_identity_card",
                applicant_id_number="11010519491231002X", consent_version="privacy-v1",
                idempotency_key=f"{suffix}-start",
            ),
        )
        self.application = CompanyVerification.objects.get(public_id=payload["application_id"])

    def run_result(self, result, key="registry-check"):
        return run_registry_verification(
            company_id=self.company.cid, application_id=str(self.application.public_id),
            user=self.user, expected_revision=1, idempotency_key=key,
            verifier=RegistryFake(result),
        )


@override_settings(**SETTINGS)
class RegistryVerificationTests(RegistryFixtureMixin, TestCase):
    def setUp(self):
        self.make_fixture()

    def test_match_and_operating_advances_only_machine_stage(self):
        before = (Grant.objects.count(), CompanyMember.objects.count())
        payload = self.run_result(RegistryVerificationResult(
            outcome="passed", retryable=False, matched=True, operating_status="1",
            provider_code="registry_match", request_id="req-reg-1",
        ))
        self.company.refresh_from_db()
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertEqual(payload["status"], CompanyVerification.STATUS_PENDING)
        self.assertEqual(payload["company_check"], VerificationCheck.STATUS_PASSED)
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)
        self.assertEqual(before, (Grant.objects.count(), CompanyMember.objects.count()))

    @patch("apps.company.verification_evidence.get_registry_verifier")
    def test_http_refresh_uses_frozen_server_snapshot_and_enforces_owner(self, provider_factory):
        fake = RegistryFake(RegistryVerificationResult(
            outcome="passed", retryable=False, matched=True, operating_status="1",
            provider_code="registry_match", request_id="registry-http",
        ))
        provider_factory.return_value = fake
        owner_token = sign_token({"user_id": self.user.id})
        other_token = sign_token({"user_id": self.other.id})
        url = f"/api/company/{self.company.cid}/verification/refresh"
        body = {"expected_revision": 1, "idempotency_key": "registry-http"}

        forbidden = self.client.post(
            url, data=json.dumps(body), content_type="application/json",
            HTTP_ACCESS_TOKEN=other_token,
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(fake.calls, 0)

        tampered = self.client.post(
            url,
            data=json.dumps({**body, "mode": "four_elements"}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=owner_token,
        )
        self.assertEqual(tampered.status_code, 400)
        self.assertEqual(fake.calls, 0)

        response = self.client.post(
            url, data=json.dumps(body), content_type="application/json",
            HTTP_ACCESS_TOKEN=owner_token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["stage"], CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertEqual(fake.request.company_name, self.application.company_name_snapshot)
        self.assertEqual(fake.request.registration_no, self.application.registration_no_snapshot)
        self.assertEqual(fake.request.mode, CompanyVerification.MODE_TWO_ELEMENTS)

    def test_http_success_without_match_is_failed(self):
        payload = self.run_result(RegistryVerificationResult(
            outcome="passed", retryable=False, matched=False, operating_status="1",
            provider_code="registry_mismatch", request_id="req-reg-2",
        ))
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_COMPANY_CHECK)
        self.assertEqual(payload["company_check"], VerificationCheck.STATUS_FAILED)

    def test_matched_but_not_operating_is_failed(self):
        payload = self.run_result(RegistryVerificationResult(
            outcome="passed", retryable=False, matched=True, operating_status="3",
            provider_code="enterprise_not_operating", request_id="req-reg-3",
        ))
        self.assertEqual(payload["stage"], CompanyVerification.STAGE_COMPANY_CHECK)
        self.assertEqual(payload["company_check"], VerificationCheck.STATUS_FAILED)

    def test_system_unknown_is_retryable_and_never_advances(self):
        payload = self.run_result(RegistryVerificationResult(
            outcome="unknown", retryable=True, matched=None, provider_code="registry_system_error",
            request_id="req-reg-4",
        ))
        operation = VerificationOperation.objects.get(operation_kind=VerificationOperation.KIND_REGISTRY_CHECK)
        self.assertEqual(payload["company_check"], VerificationCheck.STATUS_UNKNOWN)
        self.assertEqual(operation.status, VerificationOperation.STATUS_FAILED)
        self.assertIsNotNone(operation.next_run_at)

    def test_idempotent_completed_operation_does_not_bill_twice(self):
        fake = RegistryFake(RegistryVerificationResult(
            outcome="failed", retryable=False, matched=False, operating_status="1",
            provider_code="registry_mismatch",
        ))
        kwargs = dict(company_id=self.company.cid, application_id=str(self.application.public_id),
                      user=self.user, expected_revision=1, idempotency_key="same-registry", verifier=fake)
        run_registry_verification(**kwargs)
        run_registry_verification(**kwargs)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(VerificationCheck.objects.filter(kind=VerificationCheck.KIND_REGISTRY).count(), 1)

    def test_cross_user_and_stale_revision_are_rejected_before_provider(self):
        fake = RegistryFake(RegistryVerificationResult(outcome="passed", retryable=False, matched=True, operating_status="1"))
        with self.assertRaises(VerificationDomainError) as forbidden:
            run_registry_verification(company_id=self.company.cid, application_id=str(self.application.public_id),
                                      user=self.other, expected_revision=1, idempotency_key="cross", verifier=fake)
        self.assertEqual(forbidden.exception.code, "application_forbidden")
        with self.assertRaises(VerificationDomainError) as stale:
            run_registry_verification(company_id=self.company.cid, application_id=str(self.application.public_id),
                                      user=self.user, expected_revision=99, idempotency_key="stale", verifier=fake)
        self.assertEqual(stale.exception.code, "stale_revision")
        self.assertEqual(fake.calls, 0)

    @override_settings(COMPANY_VERIFICATION_REGISTRY_MODE="three_elements")
    def test_unsupported_mode_is_rejected_instead_of_fallback(self):
        with self.assertRaises(VerificationDomainError) as captured:
            create_manual_verification(
                company_id=self.company.cid, user=self.user,
                value=ManualVerificationStartInput(
                    applicant_name="张三", applicant_id_type="resident_identity_card",
                    applicant_id_number="11010519491231002X", consent_version="v1",
                    idempotency_key="unsupported-mode",
                ),
            )
        self.assertEqual(captured.exception.code, "unsupported_registry_mode")

    def test_retry_task_reuses_operation_and_check_then_advances(self):
        self.run_result(RegistryVerificationResult(
            outcome="unknown", retryable=True, matched=None,
            provider_code="registry_system_error",
        ), key="retry-registry")
        result = retry_due_manual_evidence(
            now=timezone.now() + timedelta(seconds=60),
            registry_verifier=RegistryFake(RegistryVerificationResult(
                outcome="passed", retryable=False, matched=True, operating_status="1",
                provider_code="registry_match",
            )),
        )
        self.application.refresh_from_db()
        self.assertEqual(result, {"processed": 1, "failed": 0})
        self.assertEqual(self.application.stage, CompanyVerification.STAGE_APPLICANT_FACE)
        self.assertEqual(VerificationCheck.objects.filter(kind=VerificationCheck.KIND_REGISTRY).count(), 1)

    def test_no_raw_identity_or_company_input_is_stored_in_operation_result(self):
        self.run_result(RegistryVerificationResult(
            outcome="failed", retryable=False, matched=False, operating_status="1",
            provider_code="registry_mismatch", request_id="req-safe",
        ))
        serialized = json.dumps(list(VerificationOperation.objects.values("result")), ensure_ascii=False)
        self.assertNotIn("测试企业有限公司", serialized)
        self.assertNotIn("91350211M000100Y46", serialized)


@override_settings(**SETTINGS)
class RegistryTransactionBoundaryTests(RegistryFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def test_provider_runs_outside_database_transaction(self):
        self.make_fixture("registry-tx")
        fake = RegistryFake(RegistryVerificationResult(
            outcome="failed", retryable=False, matched=False, operating_status="1",
            provider_code="registry_mismatch",
        ), assert_outside=True)
        run_registry_verification(
            company_id=self.company.cid, application_id=str(self.application.public_id),
            user=self.user, expected_revision=1, idempotency_key="registry-tx-call", verifier=fake,
        )
        self.assertEqual(fake.calls, 1)


@override_settings(**SETTINGS)
class TencentRegistryAdapterContractTests(SimpleTestCase):
    def test_business_license_ocr_maps_identity_fields(self):
        class Response:
            Name = "测试企业有限公司"
            RegNum = "91350211M000100Y46"
            Person = "张三"
            RecognizeWarnCode = [0]
            IsDuplication = 0
            Electronic = True
            RequestId = "license-request-id"

        class OcrClient:
            def BizLicenseOCR(self, request):
                self.request = request
                return Response()

        client = OcrClient()
        provider = TencentManualVerificationProvider(
            registry_client=object(),
            face_client=object(),
            ocr_client_instance=client,
        )
        result = provider.extract_business_license(
            DocumentExtractionRequest(content=b"image", mime_type="image/png")
        )
        self.assertEqual(result.outcome, "passed")
        self.assertEqual(result.company_name, "测试企业有限公司")
        self.assertEqual(result.registration_no, "91350211M000100Y46")
        self.assertEqual(result.legal_representative, "张三")
        self.assertEqual(result.is_duplication, 0)
        self.assertIs(result.electronic, True)
        self.assertTrue(client.request.ImageBase64)
        self.assertTrue(client.request.EnableCopyWarn)

    def test_registry_sdk_request_and_business_response_mapping(self):
        class Response:
            StatusCode = 0
            VerifyResult = 1
            OperatingStatus = "1"
            RequestId = "registry-request-id"

        class RegistryClient:
            def VerifyBizLicenseEnterprise3(self, request):
                self.request = request
                return Response()

        client = RegistryClient()
        provider = TencentManualVerificationProvider(registry_client=client, face_client=object())
        result = provider.verify(RegistryVerificationRequest(
            company_name="测试企业有限公司", registration_no="91350211M000100Y46",
            mode="two_elements",
        ))
        self.assertEqual(result.outcome, "passed")
        self.assertEqual(client.request.CreditCode, "91350211M000100Y46")
        self.assertEqual(client.request.EntName, "测试企业有限公司")
        self.assertEqual(client.request.VerifyType, "ENT_2META")

    def test_registry_three_elements_includes_verified_legal_representative(self):
        class Response:
            StatusCode = 0
            VerifyResult = 1
            OperatingStatus = "1"
            RequestId = "registry-three-request-id"

        class RegistryClient:
            def VerifyBizLicenseEnterprise3(self, request):
                self.request = request
                return Response()

        client = RegistryClient()
        provider = TencentManualVerificationProvider(registry_client=client, face_client=object())
        result = provider.verify(RegistryVerificationRequest(
            company_name="测试企业有限公司",
            registration_no="91350211M000100Y46",
            mode="three_elements",
            legal_representative="张三",
        ))
        self.assertEqual(result.outcome, "passed")
        self.assertEqual(client.request.VerifyType, "ENT_3META")
        self.assertEqual(client.request.LrName, "张三")

    def test_registry_system_and_no_result_are_not_match(self):
        class RegistryClient:
            def __init__(self, status): self.status = status
            def VerifyBizLicenseEnterprise3(self, request):
                response = type("Response", (), {})()
                response.StatusCode = self.status
                response.VerifyResult = None
                response.OperatingStatus = None
                response.RequestId = "request-id"
                return response

        system = TencentManualVerificationProvider(registry_client=RegistryClient(1), face_client=object()).verify(
            RegistryVerificationRequest("企业", "91350211M000100Y46", "two_elements")
        )
        no_result = TencentManualVerificationProvider(registry_client=RegistryClient(2), face_client=object()).verify(
            RegistryVerificationRequest("企业", "91350211M000100Y46", "two_elements")
        )
        self.assertEqual((system.outcome, system.retryable), ("unknown", True))
        self.assertEqual((no_result.outcome, no_result.retryable), ("unknown", False))
