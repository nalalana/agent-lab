from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.company.models import Company, TransparentCompany
from apps.company.services import CompanyService
from apps.runtime.models import OpenClawInstance
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User


class CompanyUpgradeRuntimeProvisionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(
            uid="upgrade-admin",
            username="upgrade-admin",
            role="user",
            created_at="1",
            updated_at="1",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="upgrade-company",
            company_name="Upgrade Company",
            status=Company.COMPANY_STATUS_COMMON,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at="1",
            updated_at="1",
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(
            principal,
            resource,
            Grant.PERM_SUPER_ADMIN,
            granter_user=self.user,
        )

    def _instance(self):
        return SimpleNamespace(
            refresh_from_db=lambda: None,
            get_info=lambda: {"id": 7, "provider": OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST},
        )

    @patch("apps.runtime.services.OpenClawRuntimeService.enqueue_bootstrap")
    @patch("apps.runtime.services.OpenClawRuntimeService.enqueue_instance_provision")
    @patch("apps.runtime.services.OpenClawRuntimeService.get_or_create_instance")
    @patch.object(CompanyService, "ensure_default_transparent_agent")
    @patch("apps.company.services._moderate_company_texts", return_value=(True, ""))
    @patch("apps.company.services.ImageModerationService.require_approved_public_image", return_value=(True, "", ""))
    @patch("apps.company.services.is_granted", return_value=True)
    def test_upgrade_defaults_to_shared_host_runtime(
        self,
        _grant_mock,
        _image_mock,
        _moderation_mock,
        _agent_mock,
        get_instance_mock,
        provision_mock,
        bootstrap_mock,
    ):
        get_instance_mock.return_value = (True, self._instance())
        provision_mock.return_value = (True, {"task": {"id": 12}})
        bootstrap_mock.return_value = (True, {"task": {"id": 13}})

        ok, payload = CompanyService.upgrade_to_transparent_company(
            cid=self.company.cid,
            philosophy="hello",
            user=self.user,
            runtime_request={},
        )

        self.assertTrue(ok)
        get_instance_mock.assert_called_once_with(
            company=self.company,
            provider=OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST,
            region="",
            zone="",
        )
        provision_kwargs = provision_mock.call_args.kwargs
        self.assertEqual(OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST, provision_kwargs["provider"])
        self.assertEqual({}, provision_kwargs["request_payload"])
        bootstrap_mock.assert_called_once()
        self.assertEqual({"task": {"id": 13}}, payload["bootstrap"])

    @patch("apps.runtime.services.OpenClawRuntimeService.enqueue_bootstrap")
    @patch("apps.runtime.services.OpenClawRuntimeService.enqueue_instance_provision")
    @patch("apps.runtime.services.OpenClawRuntimeService.get_or_create_instance")
    @patch.object(CompanyService, "ensure_default_transparent_agent")
    @patch("apps.company.services._moderate_company_texts", return_value=(True, ""))
    @patch("apps.company.services.ImageModerationService.require_approved_public_image", return_value=(True, "", ""))
    @patch("apps.company.services.is_granted", return_value=True)
    def test_upgrade_ignores_external_runtime_request(
        self,
        _grant_mock,
        _image_mock,
        _moderation_mock,
        _agent_mock,
        get_instance_mock,
        provision_mock,
        bootstrap_mock,
    ):
        get_instance_mock.return_value = (True, self._instance())
        provision_mock.return_value = (True, {"task": {"id": 12}})
        bootstrap_mock.return_value = (True, {"task": {"id": 13}})

        ok, _payload = CompanyService.upgrade_to_transparent_company(
            cid=self.company.cid,
            philosophy="hello",
            user=self.user,
            runtime_request={
                "provider": "unexpected_provider",
                "region": "ap-beijing",
                "runtime_option": "ignored",
            },
        )

        self.assertTrue(ok)
        provision_kwargs = provision_mock.call_args.kwargs
        self.assertEqual(OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST, provision_kwargs["provider"])
        self.assertEqual("", provision_kwargs["region"])
        self.assertEqual({}, provision_kwargs["request_payload"])
        bootstrap_mock.assert_called_once()

    def test_transparent_status_uses_transparent_company_record_not_legacy_runtime(self):
        TransparentCompany.objects.create(company=self.company, prompts="hello")

        ok, payload = CompanyService.is_transparent_company(self.company.cid)
        info = Company.objects.get(pk=self.company.pk).get_info()

        self.assertTrue(ok)
        self.assertTrue(payload["isTransparent"])
        self.assertTrue(payload["is_transparent"])
        self.assertEqual("ready", payload["smart_company_status"])
        self.assertTrue(info["is_transparent"])
