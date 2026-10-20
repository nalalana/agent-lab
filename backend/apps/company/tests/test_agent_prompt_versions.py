import json
import time
from unittest.mock import patch

from django.test import TestCase

from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentPromptVersion,
)
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User
from common.utils.Login_utils import sign_token


class AgentPromptVersionApiTests(TestCase):
    maxDiff = None

    def setUp(self):
        now = str(time.time())[:20]
        self.admin_user = User.objects.create(
            uid="agent-prompt-version-admin",
            username="Prompt Admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.admin_user,
            cid="company-agent-prompt-versions",
            company_name="Prompt Version Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Transparent company prompts",
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.admin_user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.admin_user)

    def _token(self) -> str:
        return sign_token({"user_id": self.admin_user.id})

    def _post_agent_upsert(self, payload: dict):
        return self.client.post(
            "/api/company/transparent/agents/upsert",
            data=json.dumps({"cid": self.company.cid, **payload}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=self._token(),
        )

    def _get_prompt_versions(self, agent_id: str, *, token: bool = True):
        headers = {"HTTP_ACCESS_TOKEN": self._token()} if token else {}
        return self.client.get(
            f"/api/backend/agents/{agent_id}/prompt-versions?cid={self.company.cid}",
            **headers,
        )

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_agent_upsert_saves_prompt_version_contract_shape(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test", "status": "published"}})

        create_response = self._post_agent_upsert(
            {
                "agent_id": "support-agent",
                "name": "Support Agent",
                "role": TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
                "description": "Answers support questions",
                "system_prompt": "You are helpful.",
                "visibility": TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
                "client_visibility": TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
                "exposed_to_client": True,
                "route_key": "support",
                "enabled": True,
                "status": TransparentCompanyAgentDefinition.STATUS_ACTIVE,
                "version_label": "v1.5",
                "change_reason": "Add transparent support prompt",
                "change_type": TransparentCompanyAgentPromptVersion.CHANGE_TYPE_FEATURE,
            }
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["result"], 1)

        update_response = self._post_agent_upsert(
            {
                "agent_id": "support-agent",
                "name": "Support Agent",
                "role": TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
                "description": "Answers support questions",
                "system_prompt": "You are helpful.\nExplain data usage.",
                "visibility": TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
                "client_visibility": TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
                "exposed_to_client": True,
                "route_key": "support",
                "enabled": True,
                "status": TransparentCompanyAgentDefinition.STATUS_ACTIVE,
                "version_label": "v2.2",
                "change_reason": "Explain data retention",
                "change_type": TransparentCompanyAgentPromptVersion.CHANGE_TYPE_SECURITY,
            }
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["result"], 1)

        response = self._get_prompt_versions("support-agent")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["message"], "success")
        self.assertEqual(set(payload["data"].keys()), {"agent_id", "agent_name", "versions", "total"})
        self.assertEqual(payload["data"]["agent_id"], "support-agent")
        self.assertEqual(payload["data"]["agent_name"], "Support Agent")
        self.assertEqual(payload["data"]["total"], 2)

        versions = payload["data"]["versions"]
        self.assertEqual(len(versions), 2)
        required_keys = {
            "version_id",
            "version",
            "content",
            "change_reason",
            "change_type",
            "created_at",
            "created_by",
            "is_current",
            "diff_summary",
        }
        self.assertEqual(set(versions[0].keys()), required_keys)
        self.assertEqual(versions[0]["version"], "v2.2")
        self.assertEqual(versions[0]["content"], "You are helpful.\nExplain data usage.")
        self.assertEqual(versions[0]["change_reason"], "Explain data retention")
        self.assertEqual(versions[0]["change_type"], TransparentCompanyAgentPromptVersion.CHANGE_TYPE_SECURITY)
        self.assertEqual(versions[0]["created_by"], self.admin_user.username)
        self.assertTrue(versions[0]["is_current"])
        self.assertEqual(versions[0]["diff_summary"]["added_lines"], 1)
        self.assertEqual(versions[0]["diff_summary"]["removed_lines"], 0)

        self.assertEqual(versions[1]["version"], "v1.5")
        self.assertEqual(versions[1]["change_reason"], "Add transparent support prompt")
        self.assertEqual(versions[1]["change_type"], TransparentCompanyAgentPromptVersion.CHANGE_TYPE_FEATURE)
        self.assertFalse(versions[1]["is_current"])

    def test_prompt_version_endpoint_allows_public_agent_without_login(self):
        now = str(time.time())[:20]
        agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="public-agent",
            name="Public Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            systemPrompt="Public prompt",
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
            exposedToClient=True,
            routeKey="public",
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
        )
        TransparentCompanyAgentPromptVersion.objects.create(
            agent=agent,
            versionLabel="v1.0",
            content="Public prompt",
            changeReason="Initial public prompt",
            changeType=TransparentCompanyAgentPromptVersion.CHANGE_TYPE_INITIAL,
            createdBy="Prompt Admin",
            isCurrent=True,
            diffSummary={"added_lines": 1, "removed_lines": 0, "key_changes": []},
            created_at=now,
        )

        response = self._get_prompt_versions("public-agent", token=False)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["data"]["versions"][0]["change_type"], "initial")

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_agent_upsert_rejects_invalid_change_type(self, mock_sync):
        response = self._post_agent_upsert(
            {
                "agent_id": "invalid-change-type-agent",
                "name": "Invalid Agent",
                "system_prompt": "Prompt",
                "change_type": "breaking",
            }
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 0)
        self.assertIn("change_type", payload["message"])
        mock_sync.assert_not_called()

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_agent_upsert_rejects_duplicate_prompt_version_label(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test", "status": "published"}})

        base_payload = {
            "agent_id": "duplicate-version-agent",
            "name": "Duplicate Version Agent",
            "system_prompt": "Prompt one",
            "version_label": "v1.0",
            "change_reason": "Initial prompt",
            "change_type": TransparentCompanyAgentPromptVersion.CHANGE_TYPE_INITIAL,
        }
        first_response = self._post_agent_upsert(base_payload)
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["result"], 1)

        duplicate_response = self._post_agent_upsert({
            **base_payload,
            "system_prompt": "Prompt two",
            "change_reason": "Duplicate version",
        })
        self.assertEqual(duplicate_response.status_code, 200)
        payload = duplicate_response.json()
        self.assertEqual(payload["result"], 0)
        self.assertIn("版本号已存在", payload["message"])
