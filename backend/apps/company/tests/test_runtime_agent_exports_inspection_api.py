import time
from unittest.mock import patch

from django.test import TestCase

from apps.company.models import Company
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.runtime.models import OpenClawInstance
from apps.user.models import User
from common.utils.Login_utils import sign_token


class RuntimeAgentExportsInspectionApiTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.admin_user = User.objects.create(
            uid="runtime-admin",
            username="runtime-admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.viewer_user = User.objects.create(
            uid="runtime-viewer",
            username="runtime-viewer",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.admin_user,
            cid="company-runtime-exports",
            company_name="Runtime Exports Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.instance = OpenClawInstance.objects.create(
            company=self.company,
            provider=OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST,
            runtime_namespace="tsds-company-runtime",
            status=OpenClawInstance.STATUS_RUNNING,
            openclaw_status=OpenClawInstance.OPENCLAW_STATUS_HEALTHY,
            config_status=OpenClawInstance.CONFIG_STATUS_IN_SYNC,
            created_at=now,
            updated_at=now,
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.admin_user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.admin_user)

    def _token(self, user: User) -> str:
        return sign_token({"user_id": user.id})

    def _get(self, user: User):
        return self.client.get(
            f"/api/runtime/inspection/agent-exports?cid={self.company.cid}",
            HTTP_ACCESS_TOKEN=self._token(user),
        )

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.inspect_agent_runtime_exports")
    def test_admin_can_access_agent_runtime_exports_summary(self, mock_inspect):
        mock_inspect.return_value = (
            True,
            {
                "runtime_namespace": self.instance.runtime_namespace,
                "inspection_mode": "runtime_export_file_summary",
                "platform": {
                    "skills_count": 2,
                    "agent_skill_binding_count": 1,
                    "permission_template_count": 1,
                    "agent_permission_binding_count": 1,
                    "agent_count": 1,
                },
                "native_config": {
                    "exists": True,
                    "top_level_keys": ["agents", "gateway", "models"],
                    "agent_count": 1,
                    "has_skills_section": False,
                },
                "conclusion": {
                    "runtime_exports_materialized": True,
                    "skills_json_written": True,
                    "skills_json_consumption_proven": False,
                    "blocker": "Current repository proves shared_host_bridge writes skills.json, but does not prove native OpenClaw consumes it.",
                },
                "agents": [
                    {
                        "agent_id": "sales-agent",
                        "name": "Sales Agent",
                        "agent_json": {
                            "exists": True,
                            "workspace_alias": "workspace-sales-agent",
                            "source": "platform_runtime_config",
                        },
                        "skills": {
                            "exists": True,
                            "items_count": 1,
                            "skill_ids": ["skill.alpha"],
                        },
                        "permissions": {
                            "exists": True,
                            "effective_mode": "explicit_binding_required",
                        },
                        "knowledge": {
                            "exists": True,
                            "read_count": 1,
                            "deny_count": 0,
                        },
                    }
                ],
            },
        )

        response = self._get(self.admin_user)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["data"]["runtime_namespace"], self.instance.runtime_namespace)
        self.assertEqual(payload["data"]["inspection_mode"], "runtime_export_file_summary")
        self.assertFalse(payload["data"]["conclusion"]["skills_json_consumption_proven"])
        self.assertEqual(payload["data"]["agents"][0]["agent_id"], "sales-agent")
        self.assertEqual(payload["data"]["agents"][0]["skills"]["items_count"], 1)
        self.assertTrue(payload["data"]["agents"][0]["permissions"]["exists"])
        self.assertTrue(payload["data"]["agents"][0]["knowledge"]["exists"])

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.inspect_agent_runtime_exports")
    def test_non_admin_is_denied(self, mock_inspect):
        response = self._get(self.viewer_user)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["result"], 0)
        mock_inspect.assert_not_called()
