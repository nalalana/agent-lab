import json

from django.test import SimpleTestCase

from apps.company.serializers import (
    TransparentCompanyAgentPermissionBindingUpsertSerializer,
    TransparentCompanyPermissionTemplateUpsertSerializer,
)


class PermissionToolFieldsRetiredTests(SimpleTestCase):
    def test_permission_template_rejects_legacy_tool_fields(self):
        serializer = TransparentCompanyPermissionTemplateUpsertSerializer(data={
            "cid": "company-1",
            "name": "default",
            "tools_allow": ["workspace.read"],
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("tools_allow", serializer.errors)

    def test_agent_binding_rejects_legacy_tool_fields(self):
        serializer = TransparentCompanyAgentPermissionBindingUpsertSerializer(data={
            "cid": "company-1",
            "agent_id": "agent-1",
            "tools_deny": ["python.execute"],
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("tools_deny", serializer.errors)
