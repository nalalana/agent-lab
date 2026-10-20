"""Contract tests for the frontend-facing memory console endpoint."""

import json
import time
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from apps.company.console_memory import console_memory
from apps.company.models import Company, TransparentCompany, TransparentCompanyAgentDefinition
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User
from common.utils.Login_utils import sign_token


class ConsoleMemoryEndpointTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="memory-console-admin",
            username="memory-console-admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="memory-console-company",
            company_name="Memory Console Company",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Company prompt",
        )
        TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="support-agent",
            name="客服助手",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.user)

    @patch("apps.company.console_memory.MemoryBankService.list_company")
    def test_returns_native_long_term_memories(self, mock_memory_bank):
        mock_memory_bank.return_value = {
                "list": [
                    {
                        "id": "ov-1",
                        "uri": "viking://user/support/memories/entities/a.md",
                        "title": "供应商 A",
                        "content": "供应商 A 是核心供应商。",
                        "memory_type": "entities",
                        "group": "people_events",
                        "owner_agent_id": "support-agent",
                        "owner_agent_name": "客服助手",
                        "updated_at": "",
                    }
                ],
                "agents": [{"id": "1", "agent_id": "support-agent", "name": "客服助手"}],
                "groups": [],
                "type_counts": {"entities": 1},
                "source": "native_openviking_commit",
            }
        token = sign_token({"user_id": self.user.id})
        request = RequestFactory().get(
            "/api/backend/console/memory",
            {"cid": self.company.cid},
            HTTP_ACCESS_TOKEN=token,
        )

        response = console_memory(request)
        payload = json.loads(response.content.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["data"]["type_counts"], {"entities": 1})
        self.assertEqual(payload["data"]["list"][0]["owner_agent_name"], "客服助手")
        self.assertEqual(payload["data"]["list"][0]["content"], "供应商 A 是核心供应商。")
        mock_memory_bank.assert_called_once_with(self.company, self.transparent_company)
