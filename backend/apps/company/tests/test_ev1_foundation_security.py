import json
import threading
import time
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.company.models import (
    Company,
    CompanyMember,
    CompanyVerification,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
)
from apps.company.services import CompanyService
from apps.follow.models import FollowingCompany
from apps.follow.services import FollowingService
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import (
    get_or_create_principal,
    get_or_create_resource,
    grant_permissions,
)
from apps.runtime.services import OpenClawRuntimeService
from apps.sso.models import SSOAuthorizationCode, SSOClient
from apps.user.models import User
from common.utils.Login_utils import sign_token


VALID_REGISTRATION_NO = "91350211M000100Y46"


class EV1FoundationSecurityTests(TestCase):
    def setUp(self):
        self.now = str(time.time())[:20]
        self.creator = self._user("ev1-creator")
        self.other = self._user("ev1-other")

    def _user(self, uid):
        return User.objects.create(
            uid=uid,
            username=uid,
            role="user",
            created_at=self.now,
            updated_at=self.now,
        )

    def _company(self, cid, *, owner=None, verified=False):
        return Company.objects.create(
            account=owner,
            cid=cid,
            company_name=cid,
            registration_no=None,
            status=Company.COMPANY_STATUS_COMMON,
            verification_status=(
                Company.VERIFICATION_STATUS_VERIFIED
                if verified
                else Company.VERIFICATION_STATUS_UNVERIFIED
            ),
            verification_provider=(
                Company.VERIFICATION_PROVIDER_TENCENT_ESS if verified else ""
            ),
            verified_at=timezone.now() if verified else None,
            created_at=self.now,
            updated_at=self.now,
        )

    def _grant(self, company, user, permission=Grant.PERM_ADMIN):
        principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, company.cid)
        grant_permissions(
            principal,
            resource,
            permission,
            granter_user=user,
        )

    def test_company_creation_has_no_implicit_owner_member_or_grant(self):
        with (
            patch(
                "apps.company.services._moderate_company_texts",
                return_value=(True, ""),
            ),
            patch(
                "apps.company.services.ImageModerationService.require_approved_public_image",
                return_value=(True, "", ""),
            ),
        ):
            ok, company = CompanyService.create_company(
                company_name="EV1 Draft",
                owner=self.creator,
                registration_no=VALID_REGISTRATION_NO,
            )

        self.assertTrue(ok)
        self.assertEqual(
            company.verification_status,
            Company.VERIFICATION_STATUS_UNVERIFIED,
        )
        self.assertTrue(
            Resource.objects.filter(
                type=Resource.TYPE_COMPANY,
                external_id=company.cid,
            ).exists()
        )
        self.assertFalse(
            Principal.objects.filter(
                type=Principal.TYPE_USER,
                external_id=self.creator.uid,
            ).exists()
        )
        self.assertFalse(CompanyMember.objects.filter(company=company).exists())
        self.assertFalse(Grant.objects.exists())

    def test_formal_access_requires_verified_and_explicit_iam_without_side_effects(self):
        verified = self._company(
            "ev1-verified-no-grant",
            owner=self.creator,
            verified=True,
        )
        before = (
            Principal.objects.count(),
            Resource.objects.count(),
            Grant.objects.count(),
        )

        service_ok, _ = CompanyService._get_company_for_admin(
            verified.cid,
            self.creator,
        )
        runtime_ok, _ = OpenClawRuntimeService.assert_company_admin(
            verified,
            self.creator,
        )

        self.assertFalse(service_ok)
        self.assertFalse(runtime_ok)
        self.assertEqual(
            before,
            (
                Principal.objects.count(),
                Resource.objects.count(),
                Grant.objects.count(),
            ),
        )

        self._grant(verified, self.other)
        service_ok, returned = CompanyService._get_company_for_admin(
            verified.cid,
            self.other,
        )
        self.assertTrue(service_ok)
        self.assertEqual(returned.pk, verified.pk)

        draft = self._company("ev1-unverified-with-grant", owner=self.creator)
        self._grant(draft, self.other)
        draft_ok, _ = CompanyService._get_company_for_admin(
            draft.cid,
            self.other,
        )
        self.assertFalse(draft_ok)

    def test_pending_verification_freezes_identity_but_allows_profile_update(self):
        company = self._company("ev1-pending", owner=self.creator)
        company.registration_no = VALID_REGISTRATION_NO
        company.establishment_date = "2020-01-01"
        company.address = "Original address"
        company.save(
            update_fields=[
                "registration_no",
                "establishment_date",
                "address",
            ]
        )
        CompanyVerification.objects.create(
            company=company,
            applicant_user=self.creator,
            status=CompanyVerification.STATUS_PENDING,
            application_id="ev1-pending-app",
        )

        with patch(
            "apps.company.services._moderate_company_texts",
            return_value=(True, ""),
        ) as moderation:
            identity_ok, identity_message = CompanyService.update_company(
                cid=company.cid,
                user=self.creator,
                company_name="Changed identity",
            )
            profile_ok, _ = CompanyService.update_company(
                cid=company.cid,
                user=self.creator,
                description="Allowed profile update",
            )

        company.refresh_from_db()
        self.assertFalse(identity_ok)
        self.assertIn("认证申请进行中", identity_message)
        self.assertTrue(profile_ok)
        self.assertEqual(company.company_name, "ev1-pending")
        self.assertEqual(company.description, "Allowed profile update")
        self.assertEqual(
            company.verification_status,
            Company.VERIFICATION_STATUS_UNVERIFIED,
        )
        self.assertEqual(moderation.call_count, 1)

    def test_failed_moderation_leaves_transparent_promotion_state_unchanged(self):
        company = self._company(
            "ev1-transparent-moderation",
            owner=self.creator,
            verified=True,
        )
        self._grant(company, self.creator, Grant.PERM_SUPER_ADMIN)

        with (
            patch(
                "apps.company.services.ImageModerationService.require_approved_public_image",
                return_value=(True, "", ""),
            ),
            patch(
                "apps.company.services._moderate_company_texts",
                return_value=(False, "blocked"),
            ),
        ):
            create_ok, _ = CompanyService.create_transparent_company(
                cid=company.cid,
                avatar="",
                prompts="blocked prompt",
                user=self.creator,
            )
            upgrade_ok, _ = CompanyService.upgrade_to_transparent_company(
                cid=company.cid,
                philosophy="blocked prompt",
                user=self.creator,
            )

        company.refresh_from_db()
        self.assertFalse(create_ok)
        self.assertFalse(upgrade_ok)
        self.assertEqual(company.status, str(Company.COMPANY_STATUS_COMMON))
        self.assertFalse(TransparentCompany.objects.filter(company=company).exists())

    def test_transparent_update_endpoint_uses_current_admin(self):
        company = self._company(
            "ev1-transparent-update",
            owner=self.creator,
            verified=True,
        )
        transparent = TransparentCompany.objects.create(
            company=company,
            prompts="prompt",
            avatar="old-avatar",
        )
        self._grant(company, self.creator, Grant.PERM_ADMIN)

        with patch(
            "apps.company.services.ImageModerationService.require_approved_public_image",
            return_value=(True, "approved-avatar", ""),
        ):
            response = self.client.post(
                "/api/company/transparent/update",
                data=json.dumps(
                    {
                        "cid": company.cid,
                        "avatar": "candidate-avatar",
                    }
                ),
                content_type="application/json",
                HTTP_ACCESS_TOKEN=sign_token({"user_id": self.creator.id}),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], 1)
        transparent.refresh_from_db()
        self.assertEqual(transparent.avatar, "approved-avatar")

    def test_duplicate_uscc_only_reuses_the_same_users_private_draft(self):
        with (
            patch(
                "apps.company.services._moderate_company_texts",
                return_value=(True, ""),
            ),
            patch(
                "apps.company.services.ImageModerationService.require_approved_public_image",
                return_value=(True, "", ""),
            ),
        ):
            first_ok, first = CompanyService.create_company(
                company_name="First draft",
                owner=self.creator,
                registration_no=VALID_REGISTRATION_NO,
            )
            retry_ok, retry = CompanyService.create_company(
                company_name="Retry draft",
                owner=self.creator,
                registration_no=VALID_REGISTRATION_NO,
            )
            other_ok, other_message = CompanyService.create_company(
                company_name="Other draft",
                owner=self.other,
                registration_no=VALID_REGISTRATION_NO,
            )

        self.assertTrue(first_ok)
        self.assertTrue(retry_ok)
        self.assertEqual(first.pk, retry.pk)
        self.assertFalse(other_ok)
        self.assertEqual(other_message, "企业已存在")
        self.assertEqual(
            Company.objects.filter(registration_no=VALID_REGISTRATION_NO).count(),
            1,
        )

    def test_public_company_follow_and_agent_payloads_hide_drafts_and_creator(self):
        verified = self._company("ev1-public-verified", verified=True)
        draft = self._company("ev1-public-draft", owner=self.creator)
        verified_transparent = TransparentCompany.objects.create(
            company=verified,
            prompts="verified",
        )
        draft_transparent = TransparentCompany.objects.create(
            company=draft,
            prompts="draft",
        )
        for transparent, agent_id in (
            (verified_transparent, "verified-agent"),
            (draft_transparent, "draft-agent"),
        ):
            TransparentCompanyAgentDefinition.objects.create(
                transparentCompany=transparent,
                agentId=agent_id,
                name=agent_id,
                role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
                status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
                visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
                clientVisibility=(
                    TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE
                ),
                exposedToClient=True,
                enabled=True,
                created_at=self.now,
                updated_at=self.now,
            )
        TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=verified_transparent,
            agentId="internal-agent",
            name="internal-agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL,
            clientVisibility=(
                TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE
            ),
            exposedToClient=True,
            enabled=True,
            created_at=self.now,
            updated_at=self.now,
        )
        FollowingCompany.objects.create(
            user=self.creator,
            following_company=verified,
            updated_at=self.now,
        )

        companies = CompanyService.list_companies()
        detail_ok, detail = CompanyService.get_company_detail(verified.cid)
        draft_detail_ok, _ = CompanyService.get_company_detail(draft.cid)
        public_agents_ok, public_agents = (
            CompanyService.list_transparent_company_agents(
                verified.cid,
                public_only=True,
            )
        )
        draft_agents_ok, _ = CompanyService.list_transparent_company_agents(
            draft.cid,
            public_only=True,
        )
        following_ok, following = FollowingService.get_following_companies(
            self.creator
        )

        self.assertEqual(
            [item["cid"] for item in companies["items"]],
            [verified.cid],
        )
        self.assertTrue(detail_ok)
        self.assertNotIn("account", detail)
        self.assertFalse(draft_detail_ok)
        self.assertTrue(public_agents_ok)
        self.assertEqual(len(public_agents), 1)
        self.assertNotIn(
            "account",
            public_agents[0]["transparentCompany"]["company"],
        )
        self.assertFalse(draft_agents_ok)
        self.assertTrue(following_ok)
        self.assertNotIn("account", following["companies"][0])

    def test_follow_list_ignores_spoofed_uid_and_uses_login_identity(self):
        mine = self._company("ev1-follow-mine", verified=True)
        theirs = self._company("ev1-follow-theirs", verified=True)
        FollowingCompany.objects.create(
            user=self.creator,
            following_company=mine,
            updated_at=self.now,
        )
        FollowingCompany.objects.create(
            user=self.other,
            following_company=theirs,
            updated_at=self.now,
        )

        response = self.client.post(
            "/api/follow/following/companies",
            data=json.dumps(
                {
                    "uid": self.other.uid,
                    "limit": 20,
                    "offset": 0,
                }
            ),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=sign_token({"user_id": self.creator.id}),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(
            [item["cid"] for item in payload["data"]["companies"]],
            [mine.cid],
        )

    def test_sso_requires_verified_company_read_grant(self):
        company = self._company(
            "ev1-sso-company",
            owner=self.creator,
            verified=True,
        )
        TransparentCompany.objects.create(company=company, prompts="sso")
        SSOClient.objects.create(
            client_id="ev1-sso-client",
            client_secret="secret",
            name="EV1 SSO",
            redirect_uris="https://client.example/callback",
        )
        query = {
            "client_id": "ev1-sso-client",
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "company_id": company.cid,
        }
        token = sign_token({"user_id": self.creator.id})

        denied = self.client.get(
            "/api/sso/authorize",
            data=query,
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(SSOAuthorizationCode.objects.exists())

        self._grant(company, self.creator, Grant.PERM_READ)
        allowed = self.client.get(
            "/api/sso/authorize",
            data=query,
            HTTP_ACCESS_TOKEN=token,
        )
        self.assertEqual(allowed.status_code, 302)
        self.assertTrue(allowed["Location"].startswith("https://client.example/callback?code="))
        self.assertEqual(SSOAuthorizationCode.objects.count(), 1)


class EV1PostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.now = str(time.time())[:20]
        self.owner_a = User.objects.create(
            uid="ev1-race-owner-a",
            username="ev1-race-owner-a",
            role="user",
            created_at=self.now,
            updated_at=self.now,
        )
        self.owner_b = User.objects.create(
            uid="ev1-race-owner-b",
            username="ev1-race-owner-b",
            role="user",
            created_at=self.now,
            updated_at=self.now,
        )

    def _run_threads(self, workers):
        threads = [threading.Thread(target=worker) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_concurrent_same_uscc_creates_one_company_without_database_error_leak(self):
        self.assertEqual(connection.vendor, "postgresql")
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results = []
        original_create = Company.objects.create

        def synchronized_create(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_create(*args, **kwargs)

        def worker(user_id):
            def run():
                close_old_connections()
                try:
                    user = User.objects.get(pk=user_id)
                    ok, value = CompanyService.create_company(
                        company_name=f"Race {user.uid}",
                        owner=user,
                        registration_no=VALID_REGISTRATION_NO,
                    )
                    payload = value.cid if ok else str(value)
                    with result_lock:
                        results.append((ok, payload))
                except Exception as exc:
                    with result_lock:
                        results.append((False, f"exception:{type(exc).__name__}"))
                finally:
                    close_old_connections()

            return run

        with (
            patch(
                "apps.company.services._moderate_company_texts",
                return_value=(True, ""),
            ),
            patch(
                "apps.company.services.ImageModerationService.require_approved_public_image",
                return_value=(True, "", ""),
            ),
            patch(
                "apps.company.services.Company.objects.create",
                side_effect=synchronized_create,
            ),
        ):
            self._run_threads(
                [
                    worker(self.owner_a.pk),
                    worker(self.owner_b.pk),
                ]
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for ok, _ in results if ok), 1)
        self.assertEqual(
            Company.objects.filter(registration_no=VALID_REGISTRATION_NO).count(),
            1,
        )
        losing_payloads = [payload for ok, payload in results if not ok]
        self.assertEqual(losing_payloads, ["企业已存在"])
        self.assertFalse(any("IntegrityError" in payload for _, payload in results))

    def test_update_rereads_after_moderation_and_does_not_overwrite_verification(self):
        self.assertEqual(connection.vendor, "postgresql")
        company = Company.objects.create(
            account=self.owner_a,
            cid="ev1-stale-update",
            company_name="EV1 stale update",
            registration_no=VALID_REGISTRATION_NO,
            verification_status=Company.VERIFICATION_STATUS_UNVERIFIED,
            created_at=self.now,
            updated_at=self.now,
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.owner_a.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, company.cid)
        grant_permissions(
            principal,
            resource,
            Grant.PERM_ADMIN,
            granter_user=self.owner_a,
        )

        moderation_started = threading.Event()
        allow_update = threading.Event()
        result = []
        moderation_atomic_states = []

        def moderation(*_args, **_kwargs):
            moderation_atomic_states.append(connection.in_atomic_block)
            moderation_started.set()
            self.assertTrue(allow_update.wait(timeout=10))
            return True, ""

        def update_worker():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.owner_a.pk)
                result.append(
                    CompanyService.update_company(
                        cid=company.cid,
                        user=user,
                        description="Profile update after verification",
                    )
                )
            finally:
                close_old_connections()

        with patch(
            "apps.company.services._moderate_company_texts",
            side_effect=moderation,
        ):
            thread = threading.Thread(target=update_worker)
            thread.start()
            self.assertTrue(moderation_started.wait(timeout=10))
            Company.objects.filter(pk=company.pk).update(
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_provider=Company.VERIFICATION_PROVIDER_TENCENT_ESS,
                verified_at=timezone.now(),
            )
            allow_update.set()
            thread.join(timeout=15)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [(True, "更新成功（非透明公司，未触发运行时同步）")])
        company.refresh_from_db()
        self.assertEqual(
            company.verification_status,
            Company.VERIFICATION_STATUS_VERIFIED,
        )
        self.assertEqual(
            company.verification_provider,
            Company.VERIFICATION_PROVIDER_TENCENT_ESS,
        )
        self.assertIsNotNone(company.verified_at)
        self.assertEqual(
            company.description,
            "Profile update after verification",
        )
        self.assertEqual(moderation_atomic_states, [False])
