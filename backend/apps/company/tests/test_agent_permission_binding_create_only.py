import json
import time
from unittest.mock import patch

from django.test import TestCase

from apps.agent_runtime.runtime import UnifiedRuntimeService
from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentPermissionBinding,
)
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User
from common.utils.Login_utils import sign_token


class AgentPermissionBindingCreateOnlyTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.admin_user = User.objects.create(
            uid="user-permission-binding-admin",
            username="permission-binding-admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.admin_user,
            cid="company-permission-binding-create-only",
            company_name="Permission Binding Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Transparent company prompts",
        )
        self.agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="main-agent",
            name="Main Agent",
            role=TransparentCompanyAgentDefinition.ROLE_MAIN_AGENT,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=True,
            created_at=now,
            updated_at=now,
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.admin_user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.admin_user)

    def _token(self) -> str:
        return sign_token({"user_id": self.admin_user.id})

    def _post_upsert(self, payload):
        return self.client.post(
            "/api/company/transparent/agent-permission-bindings/upsert",
            data=json.dumps({"cid": self.company.cid, "agent_id": self.agent.agentId, **payload}),
            content_type="application/json",
            HTTP_ACCESS_TOKEN=self._token(),
        )

    def test_list_without_enabled_query_returns_enabled_binding(self):
        TransparentCompanyAgentPermissionBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            knowledgeRead=["knowledge/allowed.txt"],
            knowledgeDeny=[],
            enabled=True,
            status=TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            created_at=str(time.time())[:20],
            updated_at=str(time.time())[:20],
        )

        response = self.client.get(
            (
                "/api/company/transparent/agent-permission-bindings/list"
                f"?cid={self.company.cid}&agent_id={self.agent.agentId}"
            ),
            HTTP_ACCESS_TOKEN=self._token(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(len(payload["data"]), 1)
        self.assertNotIn("tools", payload["data"][0])
        self.assertEqual(payload["data"][0]["knowledge"]["read"], ["knowledge/allowed.txt"])

    def test_list_enabled_query_still_filters_when_explicit(self):
        TransparentCompanyAgentPermissionBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            knowledgeRead=["knowledge/allowed.txt"],
            enabled=True,
            status=TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            created_at=str(time.time())[:20],
            updated_at=str(time.time())[:20],
        )

        inactive_response = self.client.get(
            (
                "/api/company/transparent/agent-permission-bindings/list"
                f"?cid={self.company.cid}&agent_id={self.agent.agentId}&enabled=false"
            ),
            HTTP_ACCESS_TOKEN=self._token(),
        )
        active_response = self.client.get(
            (
                "/api/company/transparent/agent-permission-bindings/list"
                f"?cid={self.company.cid}&agent_id={self.agent.agentId}&enabled=true"
            ),
            HTTP_ACCESS_TOKEN=self._token(),
        )

        self.assertEqual(inactive_response.json()["data"], [])
        self.assertEqual(len(active_response.json()["data"]), 1)

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_create_only_rejects_existing_binding_without_overwriting_knowledge(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test", "status": "published"}})

        first_response = self._post_upsert(
            {
                "create_only": True,
                "knowledge_read": ["knowledge/allowed.txt"],
                "knowledge_deny": [],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["result"], 1)

        duplicate_response = self._post_upsert(
            {
                "create_only": True,
                "knowledge_read": [],
                "knowledge_deny": ["knowledge/denied.txt"],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(duplicate_response.json()["result"], 0)

        binding = TransparentCompanyAgentPermissionBinding.objects.get(
            transparentCompany=self.transparent_company,
            agent=self.agent,
        )
        self.assertEqual(binding.knowledgeRead, ["knowledge/allowed.txt"])
        self.assertEqual(binding.knowledgeDeny, [])

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_regular_upsert_can_edit_existing_binding(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test", "status": "published"}})
        self._post_upsert(
            {
                "create_only": True,
                "knowledge_read": ["knowledge/allowed.txt"],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        update_response = self._post_upsert(
            {
                "knowledge_read": ["knowledge/updated.txt"],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["result"], 1)

        binding = TransparentCompanyAgentPermissionBinding.objects.get(
            transparentCompany=self.transparent_company,
            agent=self.agent,
        )
        self.assertEqual(binding.knowledgeRead, ["knowledge/updated.txt"])

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_memory_permissions_are_persisted_independently(self, mock_sync):
        """Each memory action must survive API validation and persistence independently."""

        mock_sync.return_value = (True, {"version": {"version": "v-test"}})

        response = self._post_upsert(
            {
                "memory_read": True,
                "memory_write": False,
                "memory_update": True,
                "memory_delete": False,
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(response.json()["result"], 1)
        binding = TransparentCompanyAgentPermissionBinding.objects.get(agent=self.agent)
        self.assertTrue(binding.memoryRead)
        self.assertFalse(binding.memoryWrite)
        self.assertTrue(binding.memoryUpdate)
        self.assertFalse(binding.memoryDelete)

        inherit_response = self._post_upsert(
            {
                "memory_write": None,
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(inherit_response.json()["result"], 1)
        binding.refresh_from_db()
        self.assertTrue(binding.memoryRead)
        self.assertIsNone(binding.memoryWrite)
        self.assertTrue(binding.memoryUpdate)
        self.assertFalse(binding.memoryDelete)

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_upsert_keeps_read_write_and_delete_scopes_independent(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test", "status": "published"}})

        response = self._post_upsert(
            {
                "knowledge_writable_scopes": ["knowledge/write-only"],
                "knowledge_deletable_scopes": ["knowledge/delete-only"],
                "knowledge_read": ["knowledge/read"],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], 1)

        binding = TransparentCompanyAgentPermissionBinding.objects.get(
            transparentCompany=self.transparent_company,
            agent=self.agent,
        )
        self.assertEqual(binding.knowledgeRead, ["knowledge/read"])
        self.assertEqual(binding.knowledgeWrite, ["knowledge/write-only"])
        self.assertEqual(binding.knowledgeDelete, ["knowledge/delete-only"])
        permissions = UnifiedRuntimeService()._tool_permissions(
            {
                "knowledge": {
                    "read": binding.knowledgeRead,
                    "write": binding.knowledgeWrite,
                    "delete": binding.knowledgeDelete,
                    "deny": binding.knowledgeDeny,
                }
            }
        )
        self.assertIn("tool.workspace.write", permissions)

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_partial_upsert_preserves_omitted_write_and_delete_scopes(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test", "status": "published"}})
        self._post_upsert(
            {
                "knowledge_read": ["knowledge/original.txt"],
                "knowledge_write": ["knowledge/original.txt"],
                "knowledge_delete": ["knowledge/original.txt"],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        response = self._post_upsert(
            {
                "knowledge_read": ["knowledge/updated.txt"],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(response.json()["result"], 1)
        binding = TransparentCompanyAgentPermissionBinding.objects.get(agent=self.agent)
        self.assertEqual(binding.knowledgeRead, ["knowledge/updated.txt"])
        self.assertEqual(binding.knowledgeWrite, ["knowledge/original.txt"])
        self.assertEqual(binding.knowledgeDelete, ["knowledge/original.txt"])

    @patch("apps.company.services.CompanyService._sync_transparent_company_runtime_config")
    def test_explicit_empty_write_scope_revokes_write_permission(self, mock_sync):
        mock_sync.return_value = (True, {"version": {"version": "v-test", "status": "published"}})
        self._post_upsert(
            {
                "knowledge_read": ["knowledge/allowed.txt"],
                "knowledge_write": ["knowledge/allowed.txt"],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        response = self._post_upsert(
            {
                "knowledge_write": [],
                "enabled": True,
                "status": TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            }
        )

        self.assertEqual(response.json()["result"], 1)
        binding = TransparentCompanyAgentPermissionBinding.objects.get(agent=self.agent)
        self.assertEqual(binding.knowledgeRead, ["knowledge/allowed.txt"])
        self.assertEqual(binding.knowledgeWrite, [])
