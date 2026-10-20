import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyMember,
    CompanyVerification,
    TencentEssEvent,
    TencentEssOperatorIdentity,
)
from apps.company.providers.base import (
    ConsoleUrlResult,
    EnterpriseVerificationProvider,
    OrganizationResult,
    VerificationProviderUnavailableError,
)
from apps.company.providers.tencent_ess import TencentEssVerificationProvider
from apps.company.tencent_ess_callbacks import (
    CallbackValidationError,
    process_tencent_ess_callback,
)
from apps.company.tasks import reconcile_pending_company_verifications
from apps.company.validators import SOCIAL_CREDIT_CODE_CHARS, SOCIAL_CREDIT_CODE_WEIGHTS
from apps.company.verification import (
    VerificationFlowError,
    continue_company_verification,
    expire_stale_initiating_verifications,
    get_company_verification,
    refresh_company_verification_for_user,
    start_company_verification,
)
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_resource
from apps.user.models import User
from common.utils.Login_utils import sign_token


VALID_REGISTRATION_NO = '91350211M000100Y46'
APP_ID = 'phase4-test-app'


def registration_no_for(sequence: int) -> str:
    prefix = f'91350211M{sequence:08d}'
    total = sum(
        SOCIAL_CREDIT_CODE_CHARS.index(char) * weight
        for char, weight in zip(prefix, SOCIAL_CREDIT_CODE_WEIGHTS)
    )
    return prefix + SOCIAL_CREDIT_CODE_CHARS[(31 - total % 31) % 31]


class FakeVerificationProvider(EnterpriseVerificationProvider):
    def __init__(
        self,
        *,
        application_id=APP_ID,
        organization=None,
        create_error=None,
        on_create=None,
    ):
        self._application_id = application_id
        self.organization = organization or OrganizationResult(
            provider_status='UNVERIFIED',
            organization_open_id='',
            organization_name='',
            unified_social_credit_code='',
            request_id='describe-request',
        )
        self.create_error = create_error
        self.on_create = on_create
        self.console_calls = []
        self.describe_calls = []
        self.url_sequence = 0

    @property
    def application_id(self):
        return self._application_id

    def create_console_url(self, request):
        self.console_calls.append(request)
        if self.on_create:
            self.on_create(request)
        if self.create_error:
            raise self.create_error
        self.url_sequence += 1
        return ConsoleUrlResult(
            verification_url=f'https://ess.example/verify/{self.url_sequence}',
            request_id=f'create-request-{self.url_sequence}',
        )

    def describe_organization(self, *, proxy_organization_open_id, proxy_operator_open_id):
        self.describe_calls.append((proxy_organization_open_id, proxy_operator_open_id))
        return self.organization


class Phase4VerificationFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = str(time.time())[:20]
        self.creator = self._user('phase4-creator')
        self.other = self._user('phase4-other')
        self.company = self._company('phase4-company', account=self.creator)

    def _user(self, uid):
        return User.objects.create(
            uid=uid,
            username=uid,
            email=f'{uid}@example.com',
            phone='13800138000',
            created_at=self.now,
            updated_at=self.now,
        )

    def _company(self, cid, *, account, **overrides):
        values = {
            'account': account,
            'cid': cid,
            'company_name': f'{cid} 有限公司',
            'registration_no': registration_no_for(Company.objects.count() + 1),
            'created_at': self.now,
            'updated_at': self.now,
        }
        values.update(overrides)
        company = Company.objects.create(**values)
        get_or_create_resource(Resource.TYPE_COMPANY, cid)
        return company

    def _start(self, provider=None, company=None):
        provider = provider or FakeVerificationProvider()
        company = company or self.company
        result = start_company_verification(
            company_id=company.cid,
            user=company.account,
            provider=provider,
        )
        return provider, result, CompanyVerification.objects.get(id=result.verification_id)

    def test_created_by_starts_pending_with_stable_ids_and_exact_identity(self):
        provider, result, verification = self._start()

        self.company.refresh_from_db()
        self.assertEqual(result.status, CompanyVerification.STATUS_PENDING)
        self.assertTrue(result.verification_url.startswith('https://ess.example/'))
        self.assertTrue(self.company.proxy_organization_open_id.startswith('org_'))
        self.assertEqual(verification.application_id, APP_ID)
        identity = TencentEssOperatorIdentity.objects.get(
            user=self.creator,
            application_id=APP_ID,
        )
        self.assertEqual(verification.proxy_operator_open_id, identity.proxy_operator_open_id)
        request = provider.console_calls[0]
        self.assertEqual(
            request.proxy_organization_open_id,
            self.company.proxy_organization_open_id,
        )
        self.assertEqual(request.organization_name, self.company.company_name)
        self.assertEqual(request.unified_social_credit_code, self.company.registration_no)

    def test_start_rejects_non_creator_verified_incomplete_and_active(self):
        provider = FakeVerificationProvider()
        with self.assertRaises(VerificationFlowError) as captured:
            start_company_verification(
                company_id=self.company.cid,
                user=self.other,
                provider=provider,
            )
        self.assertEqual(captured.exception.code, 'not_draft_owner')

        self.company.verification_status = Company.VERIFICATION_STATUS_VERIFIED
        self.company.save(update_fields=['verification_status'])
        with self.assertRaises(VerificationFlowError) as captured:
            start_company_verification(
                company_id=self.company.cid,
                user=self.creator,
                provider=provider,
            )
        self.assertEqual(captured.exception.code, 'already_verified')

        incomplete = self._company('phase4-incomplete', account=self.creator, registration_no='')
        with self.assertRaises(VerificationFlowError) as captured:
            start_company_verification(
                company_id=incomplete.cid,
                user=self.creator,
                provider=provider,
            )
        self.assertEqual(captured.exception.code, 'company_identity_incomplete')

        active = self._company('phase4-active', account=self.creator)
        CompanyVerification.objects.create(
            company=active,
            applicant_user=self.creator,
            application_id=APP_ID,
            status=CompanyVerification.STATUS_PENDING,
        )
        with self.assertRaises(VerificationFlowError) as captured:
            start_company_verification(
                company_id=active.cid,
                user=self.creator,
                provider=provider,
            )
        self.assertEqual(captured.exception.code, 'verification_in_progress')
        self.assertEqual(len(provider.console_calls), 0)

    def test_provider_failure_rejects_attempt_and_retry_reuses_both_open_ids(self):
        failed_provider = FakeVerificationProvider(
            create_error=VerificationProviderUnavailableError(
                'failed', code='InternalError', request_id='failed-request'
            )
        )
        with self.assertRaises(VerificationFlowError) as captured:
            start_company_verification(
                company_id=self.company.cid,
                user=self.creator,
                provider=failed_provider,
            )
        self.assertEqual(captured.exception.code, 'provider_unavailable')
        failed = CompanyVerification.objects.get()
        self.assertEqual(failed.status, CompanyVerification.STATUS_REJECTED)
        self.assertEqual(failed.failure_reason, 'start_failed')
        first_ids = (
            failed.proxy_organization_open_id,
            failed.proxy_operator_open_id,
        )

        _, _, retried = self._start(FakeVerificationProvider())
        self.assertEqual(
            (retried.proxy_organization_open_id, retried.proxy_operator_open_id),
            first_ids,
        )

    def test_continue_reuses_verification_and_operator_but_generates_new_url(self):
        provider, first, verification = self._start()
        second = continue_company_verification(
            company_id=self.company.cid,
            user=self.creator,
            provider=provider,
        )

        self.assertNotEqual(first.verification_url, second.verification_url)
        self.assertEqual(first.verification_id, second.verification_id)
        self.assertEqual(CompanyVerification.objects.count(), 1)
        verification.refresh_from_db()
        self.assertEqual(
            provider.console_calls[0].proxy_operator_open_id,
            provider.console_calls[1].proxy_operator_open_id,
        )
        self.assertEqual(verification.status, CompanyVerification.STATUS_PENDING)

    def test_refresh_intermediate_statuses_remain_pending(self):
        for index, provider_status in enumerate(
            [
                'UNVERIFIED',
                'VERIFYINGLEGALPENDINGAUTHORIZATION',
                'VERIFYINGAUTHORIZATIONFILEPENDING',
                'VERIFYINGAUTHORIZATIONFILEREJECT',
                'VERIFYING',
            ]
        ):
            company = self._company(f'phase4-pending-{index}', account=self.creator)
            provider, _, verification = self._start(company=company)
            provider.organization = OrganizationResult(
                provider_status=provider_status,
                organization_open_id=company.proxy_organization_open_id,
                organization_name=company.company_name,
                unified_social_credit_code=company.registration_no,
                request_id=f'pending-{index}',
            )
            payload = refresh_company_verification_for_user(
                company_id=company.cid,
                user=self.creator,
                provider=provider,
            )
            verification.refresh_from_db()
            company.refresh_from_db()
            self.assertEqual(payload['status'], CompanyVerification.STATUS_PENDING)
            self.assertEqual(verification.provider_status, provider_status)
            self.assertEqual(company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)

    def test_verified_requires_all_identity_fields_and_grants_applicant_highest_access(self):
        provider, _, verification = self._start()
        self.company.refresh_from_db()
        provider.organization = OrganizationResult(
            provider_status='VERIFIED',
            organization_open_id=self.company.proxy_organization_open_id,
            organization_name=f'  {self.company.company_name}  ',
            unified_social_credit_code=self.company.registration_no.lower(),
            request_id='verified-request',
        )

        payload = refresh_company_verification_for_user(
            company_id=self.company.cid,
            user=self.creator,
            provider=provider,
        )

        verification.refresh_from_db()
        self.company.refresh_from_db()
        self.assertEqual(payload['status'], CompanyVerification.STATUS_VERIFIED)
        self.assertEqual(verification.status, CompanyVerification.STATUS_VERIFIED)
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_VERIFIED)
        self.assertEqual(self.company.verification_provider, 'tencent_ess')
        self.assertIsNotNone(self.company.verified_at)
        self.assertFalse(CompanyMember.objects.filter(company=self.company, role='owner').exists())
        resource = Resource.objects.get(external_id=self.company.cid)
        grant = Grant.objects.get(resource=resource, principal__external_id=self.creator.uid)
        self.assertEqual(grant.permissions, Grant.PERM_SUPER_ADMIN)
        self.assertIsNone(grant.expires_at)

    def test_verified_identity_mismatches_are_rejected_and_audited(self):
        mismatches = {
            'openid': {'organization_open_id': 'different-org'},
            'name': {'organization_name': '另一家公司'},
            'uscc': {'unified_social_credit_code': '91350211M000100Y45'},
        }
        for index, overrides in enumerate(mismatches.values()):
            company = self._company(f'phase4-mismatch-{index}', account=self.creator)
            provider, _, verification = self._start(company=company)
            company.refresh_from_db()
            values = {
                'provider_status': 'VERIFIED',
                'organization_open_id': company.proxy_organization_open_id,
                'organization_name': company.company_name,
                'unified_social_credit_code': company.registration_no,
                'request_id': f'mismatch-{index}',
            }
            values.update(overrides)
            provider.organization = OrganizationResult(**values)

            with self.assertRaises(VerificationFlowError) as captured:
                refresh_company_verification_for_user(
                    company_id=company.cid,
                    user=self.creator,
                    provider=provider,
                )
            self.assertEqual(captured.exception.code, 'provider_identity_mismatch')
            verification.refresh_from_db()
            company.refresh_from_db()
            self.assertEqual(verification.status, CompanyVerification.STATUS_PENDING)
            self.assertEqual(verification.failure_reason, 'provider_identity_mismatch')
            self.assertEqual(company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)

    def test_get_verification_returns_only_public_diagnostics(self):
        _, _, verification = self._start()
        payload = get_company_verification(company_id=self.company.cid, user=self.creator)
        self.assertEqual(
            set(payload),
            {'status', 'provider', 'started_at', 'finished_at', 'failure_reason'},
        )
        self.assertNotIn(verification.proxy_operator_open_id, json.dumps(payload))

    @override_settings(TENCENT_ESS_INITIATING_TIMEOUT_SECONDS=1)
    def test_initiating_timeout_is_configurable(self):
        verification = CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.creator,
            application_id=APP_ID,
        )
        CompanyVerification.objects.filter(pk=verification.pk).update(
            started_at=timezone.now() - timedelta(seconds=5)
        )
        self.assertEqual(expire_stale_initiating_verifications(), 1)
        verification.refresh_from_db()
        self.assertEqual(verification.status, CompanyVerification.STATUS_EXPIRED)
        self.assertEqual(verification.failure_reason, 'initiation_timeout')

    @override_settings(COMPANY_VERIFICATION_ESS_START_ENABLED=True)
    @patch('apps.company.verification.get_enterprise_verification_provider')
    def test_start_and_get_http_endpoints(self, get_provider):
        get_provider.return_value = FakeVerificationProvider()
        token = sign_token({'user_id': self.creator.id})
        start_response = self.client.post(
            f'/api/company/{self.company.cid}/verification/start',
            HTTP_ACCESS_TOKEN=token,
        )
        detail_response = self.client.get(
            f'/api/company/{self.company.cid}/verification',
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(start_response.json()['data']['status'], 'pending')
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()['data']['status'], 'pending')

    @override_settings(
        TENCENT_ESS_REFRESH_RATE_LIMIT_SECONDS=60,
        COMPANY_VERIFICATION_ESS_RECONCILE_ENABLED=True,
    )
    @patch('apps.company.verification.get_enterprise_verification_provider')
    def test_refresh_http_endpoint_is_rate_limited(self, get_provider):
        provider, _, _ = self._start()
        get_provider.return_value = provider
        token = sign_token({'user_id': self.creator.id})

        first = self.client.post(
            f'/api/company/{self.company.cid}/verification/refresh',
            HTTP_ACCESS_TOKEN=token,
        )
        second = self.client.post(
            f'/api/company/{self.company.cid}/verification/refresh',
            HTTP_ACCESS_TOKEN=token,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()['code'], 'refresh_rate_limited')

    @override_settings(COMPANY_VERIFICATION_ESS_RECONCILE_ENABLED=True)
    @patch('apps.company.tasks.get_enterprise_verification_provider')
    def test_pending_reconciliation_uses_same_refresh_path(self, get_provider):
        provider, _, verification = self._start()
        self.company.refresh_from_db()
        provider.organization = OrganizationResult(
            provider_status='VERIFIED',
            organization_open_id=self.company.proxy_organization_open_id,
            organization_name=self.company.company_name,
            unified_social_credit_code=self.company.registration_no,
            request_id='scheduled-refresh',
        )
        get_provider.return_value = provider

        result = reconcile_pending_company_verifications.run(limit=10)

        verification.refresh_from_db()
        self.assertEqual(result['processed'], 1)
        self.assertEqual(verification.status, CompanyVerification.STATUS_VERIFIED)


class Phase4ExternalCallBoundaryTests(TransactionTestCase):
    reset_sequences = True

    def test_tencent_call_occurs_after_initiating_transaction_commits(self):
        now = str(time.time())[:20]
        user = User.objects.create(
            uid='phase4-transaction-user',
            username='phase4-transaction-user',
            created_at=now,
            updated_at=now,
        )
        company = Company.objects.create(
            account=user,
            cid='phase4-transaction-company',
            company_name='事务边界测试企业',
            registration_no=VALID_REGISTRATION_NO,
            created_at=now,
            updated_at=now,
        )

        def assert_committed(_request):
            self.assertFalse(connection.in_atomic_block)
            verification = CompanyVerification.objects.get(company=company)
            self.assertEqual(verification.status, CompanyVerification.STATUS_INITIATING)

        provider = FakeVerificationProvider(on_create=assert_committed)
        result = start_company_verification(
            company_id=company.cid,
            user=user,
            provider=provider,
        )
        self.assertEqual(result.status, CompanyVerification.STATUS_PENDING)


@override_settings(
    TENCENT_ESS_APP_ID=APP_ID,
    TENCENT_ESS_SECRET_ID='secret-id-for-test',
    TENCENT_ESS_SECRET_KEY='secret-key-for-test',
    TENCENT_ESS_ENDPOINT='essbasic.tencentcloudapi.com',
    TENCENT_ESS_REGION='ap-guangzhou',
    TENCENT_ESS_CONSOLE_ENDPOINT='H5',
    TENCENT_ESS_AUTHORIZATION_TYPES=[2, 5],
)
class TencentEssProviderContractTests(TestCase):
    def test_create_console_url_maps_locked_identity_fields(self):
        provider = TencentEssVerificationProvider()
        provider._client = Mock()
        provider._client.CreateConsoleLoginUrl.return_value = SimpleNamespace(
            ConsoleUrl='https://ess.example/console',
            RequestId='request-1',
        )
        request = SimpleNamespace(
            proxy_organization_open_id='org-stable',
            proxy_operator_open_id='operator-stable',
            organization_name='测试企业',
            unified_social_credit_code=VALID_REGISTRATION_NO,
        )

        result = provider.create_console_url(request)

        sdk_request = provider._client.CreateConsoleLoginUrl.call_args.args[0]
        self.assertEqual(sdk_request.Agent.AppId, APP_ID)
        self.assertEqual(sdk_request.Agent.ProxyOrganizationOpenId, 'org-stable')
        self.assertEqual(sdk_request.Agent.ProxyOperator.OpenId, 'operator-stable')
        self.assertEqual(sdk_request.ProxyOrganizationName, '测试企业')
        self.assertEqual(sdk_request.UniformSocialCreditCode, VALID_REGISTRATION_NO)
        self.assertTrue(sdk_request.OrganizationAuthorizationOptions.OrganizationNameSame)
        self.assertTrue(
            sdk_request.OrganizationAuthorizationOptions.UniformSocialCreditCodeSame
        )
        self.assertEqual(sdk_request.AuthorizationTypes, [2, 5])
        self.assertEqual(result.verification_url, 'https://ess.example/console')

    def test_describe_organization_maps_sdk_result_to_domain_result(self):
        provider = TencentEssVerificationProvider()
        provider._client = Mock()
        provider._client.DescribeChannelOrganizations.return_value = SimpleNamespace(
            ChannelOrganizationInfos=[
                SimpleNamespace(
                    AuthorizationStatus='VERIFIED',
                    OrganizationOpenId='org-stable',
                    OrganizationName='测试企业',
                    UnifiedSocialCreditCode=VALID_REGISTRATION_NO,
                )
            ],
            RequestId='request-2',
        )

        result = provider.describe_organization(
            proxy_organization_open_id='org-stable',
            proxy_operator_open_id='operator-stable',
        )

        sdk_request = provider._client.DescribeChannelOrganizations.call_args.args[0]
        self.assertEqual(sdk_request.OrganizationOpenId, 'org-stable')
        self.assertEqual(sdk_request.Agent.ProxyOperator.OpenId, 'operator-stable')
        self.assertEqual(result.provider_status, 'VERIFIED')
        self.assertEqual(result.organization_name, '测试企业')


@override_settings(
    TENCENT_ESS_APP_ID=APP_ID,
    TENCENT_ESS_CALLBACK_VERIFY_TOKEN='callback-token',
    TENCENT_ESS_CALLBACK_ENCRYPT_KEY='',
)
class TencentEssCallbackTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid='phase4-callback-user',
            username='phase4-callback-user',
            created_at=now,
            updated_at=now,
        )
        self.company = Company.objects.create(
            account=self.user,
            cid='phase4-callback-company',
            company_name='回调测试企业',
            registration_no=VALID_REGISTRATION_NO,
            proxy_organization_open_id='org-callback',
            created_at=now,
            updated_at=now,
        )
        self.verification = CompanyVerification.objects.create(
            company=self.company,
            applicant_user=self.user,
            application_id=APP_ID,
            proxy_organization_open_id='org-callback',
            proxy_operator_open_id='operator-callback',
            status=CompanyVerification.STATUS_PENDING,
        )

    @staticmethod
    def _raw(msg_id, msg_type, **extra):
        payload = {
            'ApplicationId': APP_ID,
            'MsgId': msg_id,
            'MsgType': msg_type,
            'MsgVersion': '1.0',
            'ProxyOrganizationOpenId': 'org-callback',
        }
        payload.update(extra)
        return json.dumps(payload, separators=(',', ':')).encode()

    @staticmethod
    def _signature(raw):
        return hmac.new(b'callback-token', raw, hashlib.sha256).hexdigest()

    def test_missing_and_wrong_signatures_are_rejected(self):
        raw = self._raw('msg-signature', 'OrgAuth')
        for signature in ('', 'wrong'):
            with self.assertRaises(CallbackValidationError) as captured:
                process_tencent_ess_callback(
                    raw_body=raw,
                    content_signature=signature,
                )
            self.assertEqual(captured.exception.code, 'invalid_signature')
        self.assertFalse(TencentEssEvent.objects.exists())

    def test_org_auth_is_accepted_idempotently_without_verifying_company(self):
        raw = self._raw('msg-org-auth', 'OrgAuth', AuthSuccess=True)
        first = process_tencent_ess_callback(
            raw_body=raw,
            content_signature=self._signature(raw),
        )
        second = process_tencent_ess_callback(
            raw_body=raw,
            content_signature=self._signature(raw),
        )

        self.company.refresh_from_db()
        self.verification.refresh_from_db()
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(TencentEssEvent.objects.count(), 1)
        self.assertTrue(TencentEssEvent.objects.get().auth_success)
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)
        self.assertEqual(self.verification.status, CompanyVerification.STATUS_PENDING)

    def test_organization_authorization_create_records_id_and_invalid_expires(self):
        create_raw = self._raw(
            'msg-auth-create',
            'OrganizationAuthorization',
            Operation='Create',
            AuthorizationId='authorization-1',
        )
        process_tencent_ess_callback(
            raw_body=create_raw,
            content_signature=self._signature(create_raw),
        )
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.authorization_id, 'authorization-1')
        self.assertEqual(self.verification.status, CompanyVerification.STATUS_PENDING)

        invalid_raw = self._raw(
            'msg-auth-invalid',
            'OrganizationAuthorization',
            Operation='Invalid',
            AuthorizationId='authorization-1',
        )
        process_tencent_ess_callback(
            raw_body=invalid_raw,
            content_signature=self._signature(invalid_raw),
        )
        self.verification.refresh_from_db()
        self.company.refresh_from_db()
        self.assertEqual(self.verification.status, CompanyVerification.STATUS_EXPIRED)
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_UNVERIFIED)

    def test_authorization_invalid_requires_matching_authorization_id(self):
        self.verification.authorization_id = 'authorization-current'
        self.verification.save(update_fields=['authorization_id'])
        raw = self._raw(
            'msg-auth-invalid-mismatch',
            'OrganizationAuthorization',
            Operation='Invalid',
            AuthorizationId='authorization-stale',
        )

        process_tencent_ess_callback(
            raw_body=raw,
            content_signature=self._signature(raw),
        )

        self.verification.refresh_from_db()
        event = TencentEssEvent.objects.get(msg_id='msg-auth-invalid-mismatch')
        self.assertEqual(self.verification.status, CompanyVerification.STATUS_PENDING)
        self.assertEqual(event.process_status, TencentEssEvent.PROCESS_STATUS_IGNORED)
        self.assertEqual(event.failure_reason, 'authorization_id_mismatch')

    def test_org_open_business_triggers_trusted_refresh(self):
        provider = FakeVerificationProvider(
            organization=OrganizationResult(
                provider_status='VERIFIED',
                organization_open_id='org-callback',
                organization_name=self.company.company_name,
                unified_social_credit_code=VALID_REGISTRATION_NO,
                request_id='callback-refresh',
            )
        )
        raw = self._raw('msg-open', 'OrgOpenTsignBiz', OpenSuccess=True)

        process_tencent_ess_callback(
            raw_body=raw,
            content_signature=self._signature(raw),
            provider=provider,
        )

        self.company.refresh_from_db()
        self.verification.refresh_from_db()
        self.assertEqual(self.company.verification_status, Company.VERIFICATION_STATUS_VERIFIED)
        self.assertEqual(self.verification.status, CompanyVerification.STATUS_VERIFIED)
        self.assertEqual(len(provider.describe_calls), 1)
        self.assertFalse(CompanyMember.objects.filter(company=self.company, role='owner').exists())
        self.assertEqual(
            Grant.objects.get(resource__external_id=self.company.cid).permissions,
            Grant.PERM_SUPER_ADMIN,
        )

    @override_settings(
        TENCENT_ESS_CALLBACK_ENCRYPT_KEY='0123456789abcdef0123456789abcdef'
    )
    def test_encrypted_callback_is_verified_before_decryption(self):
        key = b'0123456789abcdef0123456789abcdef'
        inner = self._raw('msg-encrypted', 'OrgAuth')
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(inner) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        raw = json.dumps(
            {'encrypt': base64.b64encode(ciphertext).decode()},
            separators=(',', ':'),
        ).encode()

        result = process_tencent_ess_callback(
            raw_body=raw,
            content_signature=self._signature(raw),
        )

        self.assertFalse(result.duplicate)
        event = TencentEssEvent.objects.get(msg_id='msg-encrypted')
        self.assertEqual(event.payload_digest, hashlib.sha256(raw).hexdigest())

    def test_callback_http_endpoint_accepts_valid_signature(self):
        raw = self._raw('msg-http', 'OrgAuth')
        response = self.client.post(
            '/integrations/tencent-ess/callback',
            data=raw,
            content_type='application/json',
            HTTP_CONTENT_SIGNATURE=self._signature(raw),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['message'], 'ok')
