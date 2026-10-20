from datetime import timedelta
import json
from types import SimpleNamespace

from django.test import TestCase, override_settings

from apps.company.models import CompanyVerificationReviewer, VerificationAuditEvent, VerificationReview
from apps.company.verification_domain import VerificationDomainError
from apps.company.verification_reviews import (
    _require_any_reviewer,
    _require_reviewer,
    decide_manual_verification,
    get_reviewer_context,
    get_review_application,
    grant_or_update_reviewer,
    list_registered_companies,
    list_reviewers,
    list_verification_audit_events,
    restore_company_service,
    restore_company_verification,
    revoke_reviewer,
    submit_manual_verification,
    suspend_company_service,
    suspend_company_verification,
)
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.news.views import can_publish
from apps.risk_control.views import _require_admin_user
from apps.user.models import User
from apps.user.services import UserService
from common.utils.Login_utils import sign_token

from .ev5_test_support import EV5FixtureMixin


@override_settings(
    COMPANY_VERIFICATION_ENABLED=True,
    COMPANY_VERIFICATION_ALLOW_MOCK=True,
    COMPANY_VERIFICATION_REVIEW_ALLOW_MOCK=True,
    COMPANY_VERIFICATION_AUTHORIZATION_TEMPLATE_VERSION="authorization-v1-draft",
)
class PlatformAdministrationTests(EV5FixtureMixin, TestCase):
    def test_admin_and_root_get_both_management_capabilities_without_extra_grants(self):
        administrator = self.make_user("platform-admin", role="admin")
        initial_count = CompanyVerificationReviewer.objects.count()
        for user in (administrator, self.root):
            with self.subTest(role=user.role):
                context = get_reviewer_context(user=user, now=self.now)
                self.assertTrue(context["enabled"])
                self.assertTrue(context["is_platform_admin"])
                self.assertEqual(context["capabilities"], ["pre_review", "final_review", "suspend_verification"])
                self.assertIsNone(context["expires_at"])
                self.assertTrue(can_publish(user.id))
                headers = {"HTTP_ACCESS_TOKEN": sign_token({"user_id": user.id})}
                response = self.client.get("/api/company/verification/reviewers/me", **headers)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["data"]["is_platform_admin"])
                self.assertTrue(self.client.get("/api/news/access", **headers).json()["can_publish"])
                self.assertEqual(list_registered_companies(user=user, now=self.now)["total"], 1)
                self.assertTrue(_require_any_reviewer(user, now=self.now).can_suspend_verification)
                for capability in context["capabilities"]:
                    self.assertTrue(_require_reviewer(user, capability, now=self.now).enabled)
                operator, error = _require_admin_user(SimpleNamespace(user_id=user.id))
                self.assertIsNone(error)
                self.assertEqual(operator.id, user.id)
        self.assertEqual(CompanyVerificationReviewer.objects.count(), initial_count)

    def test_expired_or_revoked_qualification_does_not_limit_the_platform_role(self):
        administrator = self.make_user("old-qualified-admin", role="admin")
        qualification = self.grant_fixture_reviewer(
            administrator, enabled=False, expires_at=self.now - timedelta(hours=1),
            revoked_at=self.now, revoked_by=self.root,
        )
        self.assertTrue(get_reviewer_context(user=administrator, now=self.now)["is_platform_admin"])
        self.assertTrue(_require_reviewer(administrator, "suspend_verification", now=self.now).can_suspend_verification)
        qualification.refresh_from_db()
        self.assertFalse(qualification.enabled)
        self.assertEqual(qualification.revoked_at, self.now)

    def test_existing_admin_assignment_enables_both_modules_and_removal_is_immediate(self):
        target = self.make_user("new-platform-admin")
        self.assertFalse(can_publish(target.id))
        granted, _ = UserService.add_admin(self.root.id, target.username)
        self.assertTrue(granted)
        # The caller deliberately still holds the user object loaded before promotion.
        self.assertTrue(can_publish(target.id))
        self.assertTrue(get_reviewer_context(user=target, now=self.now)["is_platform_admin"])
        _require_reviewer(target, "final_review", now=self.now)
        removed, _ = UserService.remove_admin(self.root.id, target.username)
        self.assertTrue(removed)
        self.assertFalse(can_publish(target.id))
        self.assertFalse(get_reviewer_context(user=target, now=self.now)["enabled"])
        with self.assertRaises(VerificationDomainError):
            _require_reviewer(target, "final_review", now=self.now)

    def test_scoped_reviewers_and_company_admins_are_not_platform_admins(self):
        self.assertTrue(get_reviewer_context(user=self.reviewer_user, now=self.now)["enabled"])
        self.assertFalse(can_publish(self.reviewer_user.id))
        company_admin = self.make_user("company-admin-only")
        principal = get_or_create_principal(Principal.TYPE_USER, company_admin.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_SUPER_ADMIN, granter_user=self.root)
        self.assertFalse(can_publish(company_admin.id))
        self.assertFalse(get_reviewer_context(user=company_admin, now=self.now)["enabled"])
        _operator, error = _require_admin_user(SimpleNamespace(user_id=company_admin.id))
        self.assertEqual(error.status_code, 403)

    def test_platform_admin_can_manage_reviewer_qualifications(self):
        administrator = self.make_user("qualification-manager", role="admin")
        target = self.make_user("delegated-pre-reviewer")
        granted = grant_or_update_reviewer(
            actor=administrator, target_user_uid=target.uid, capabilities=["pre_review"],
            reason="预审人员授权", now=self.now,
        )
        self.assertEqual(granted["capabilities"], ["pre_review"])
        self.assertTrue(list_reviewers(actor=administrator)["total"])
        revoked = revoke_reviewer(
            actor=administrator, reviewer_id=granted["reviewer_id"], reason="结束轮值", now=self.now,
        )
        self.assertFalse(revoked["enabled"])

    def test_approval_and_all_company_administration_are_audited_and_do_not_leave_a_grant(self):
        administrator = self.make_user("full-administrator", role="admin")
        submitted = submit_manual_verification(
            company_id=self.company.cid, application_id=str(self.application.public_id),
            user=self.applicant, expected_revision=self.application.revision,
            idempotency_key="platform-submit", now=self.now,
        )
        result = decide_manual_verification(
            user=administrator, application_id=str(self.application.public_id),
            expected_revision=submitted["revision"], idempotency_key="platform-approve",
            action="approve", reason_public="认证通过", reason_internal="平台管理员审核",
            checklist=self.approval_checklist(), now=self.now,
        )
        self.assertEqual(result["status"], "verified")
        identity = CompanyVerificationReviewer.objects.get(user=administrator)
        self.assertFalse(identity.enabled)
        self.assertFalse(identity.can_pre_review)
        self.assertFalse(identity.can_final_review)
        self.assertFalse(identity.can_suspend_verification)
        self.assertEqual(VerificationReview.objects.get(application=self.application).reviewer_id, identity.id)

        operations = (
            (suspend_company_verification, "verification_suspended"),
            (restore_company_verification, "verification_restored"),
            (suspend_company_service, "company_service_suspended"),
            (restore_company_service, "company_service_restored"),
        )
        for operation, action in operations:
            operation(
                user=administrator, company_id=self.company.cid, reason_code="admin_test",
                ticket_reference="ADMIN-TEST", idempotency_key=action,
                expected_application_id=self.application.public_id, now=self.now,
            )
            event = VerificationAuditEvent.objects.get(company=self.company, action=action)
            self.assertEqual(event.actor_id, administrator.id)
            self.assertEqual(event.detail["reviewer_id"], identity.id)
        self.assertGreaterEqual(list_verification_audit_events(user=administrator, now=self.now)["total"], 6)

        User.objects.filter(pk=administrator.pk).update(role="user")
        self.assertFalse(can_publish(administrator.id))
        self.assertFalse(get_reviewer_context(user=administrator, now=self.now)["enabled"])
        with self.assertRaises(VerificationDomainError):
            _require_any_reviewer(administrator, now=self.now)
        forbidden = self.client.post(
            "/api/news", json.dumps({"title": "不可发布", "summary": "摘要", "content": "正文"}),
            content_type="application/json", HTTP_ACCESS_TOKEN=sign_token({"user_id": administrator.id}),
        )
        self.assertEqual(forbidden.status_code, 403)
        identity.refresh_from_db()
        self.assertFalse(identity.enabled)

    def test_platform_admin_still_cannot_review_their_own_company(self):
        User.objects.filter(pk=self.applicant.pk).update(role="admin")
        with self.assertRaises(VerificationDomainError) as captured:
            get_review_application(
                user=self.applicant, application_id=str(self.application.public_id), now=self.now,
            )
        self.assertEqual(captured.exception.code, "review_self_forbidden")

    def test_platform_administrator_can_grant_and_remove_all_management_permissions(self):
        administrator = self.make_user("account-manager", role="admin")
        target = self.make_user("managed-platform-account")
        ordinary = self.make_user("ordinary-account")
        denied, _ = UserService.add_admin(ordinary.id, target.username)
        self.assertFalse(denied)
        self.assertFalse(can_publish(target.id))

        response = self.client.post(
            "/api/user/admin/add", json.dumps({"username": target.username}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=sign_token({"user_id": administrator.id}),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["result"], 1, response.content)
        self.assertTrue(can_publish(target.id))
        self.assertTrue(get_reviewer_context(user=target, now=self.now)["is_platform_admin"])

        response = self.client.post(
            "/api/user/admin/delete", json.dumps({"username": target.username}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=sign_token({"user_id": administrator.id}),
        )
        self.assertEqual(response.json()["result"], 1, response.content)
        self.assertFalse(can_publish(target.id))
        self.assertFalse(get_reviewer_context(user=target, now=self.now)["enabled"])

        for method in (UserService.add_admin, UserService.remove_admin):
            allowed, _ = method(administrator.id, self.root.username)
            self.assertFalse(allowed)
        self.root.refresh_from_db()
        self.assertEqual(self.root.role, "root")

        User.objects.filter(pk=administrator.pk).update(role="user")
        allowed, _ = UserService.add_admin(administrator.id, target.username)
        self.assertFalse(allowed)

    def test_retired_root_bootstrap_cannot_bypass_administrator_authorization(self):
        target = self.make_user("xudawei")
        for user in (None, target, self.root):
            headers = {"HTTP_ACCESS_TOKEN": sign_token({"user_id": user.id})} if user else {}
            response = self.client.post("/api/user/quick_init_root", **headers)
            self.assertEqual(response.status_code, 410)
        target.refresh_from_db()
        self.assertEqual(target.role, "user")
        self.assertFalse(can_publish(target.id))
