"""Regression tests for Agent test API security boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from apps.company import views_agent_test
from apps.company.models import (
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentTestSession,
)
from apps.user.models import User


class AgentTestJsonInputSecurityTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_malformed_json_returns_uniform_400_for_all_post_endpoints(self):
        endpoints = (
            (
                views_agent_test.transparent_company_agent_test_session_create,
                "/api/company/transparent/agent-test/session/create",
            ),
            (
                views_agent_test.transparent_company_agent_test_message_send,
                "/api/company/transparent/agent-test/session/message",
            ),
            (
                views_agent_test.transparent_company_agent_test_message_stream,
                "/api/company/transparent/agent-test/session/message/stream",
            ),
        )
        for view, path in endpoints:
            with self.subTest(path=path):
                request = self.factory.post(
                    path,
                    data="{",
                    content_type="application/json",
                    HTTP_ACCESS_TOKEN="test-token",
                )
                with patch(
                    "common.utils.Login_utils.check_token",
                    return_value={"user_id": 1},
                ), patch.object(
                    views_agent_test,
                    "get_user_from_request",
                    return_value=SimpleNamespace(uid="audit-user"),
                ):
                    response = view(request)

                payload = json.loads(response.content)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(payload["result"], 0)
                self.assertEqual(
                    payload["message"],
                    views_agent_test.INVALID_JSON_MESSAGE,
                )
                serialized = response.content.decode("utf-8")
                self.assertNotIn("Traceback", serialized)
                self.assertNotIn("SECRET_KEY", serialized)
                self.assertNotIn("POSTGRES_PASSWORD", serialized)

    def test_non_object_json_is_rejected(self):
        request = self.factory.post(
            "/api/company/transparent/agent-test/session/create",
            data="[]",
            content_type="application/json",
            HTTP_ACCESS_TOKEN="test-token",
        )
        with patch(
            "common.utils.Login_utils.check_token",
            return_value={"user_id": 1},
        ), patch.object(
            views_agent_test,
            "get_user_from_request",
            return_value=SimpleNamespace(uid="audit-user"),
        ):
            response = views_agent_test.transparent_company_agent_test_session_create(
                request
            )

        self.assertEqual(response.status_code, 400)

    def test_resource_existence_errors_are_uniform(self):
        for message in (
            "公司不存在",
            "透明公司不存在",
            "Agent不存在",
            "Agent 不存在",
            "测试会话不存在",
            "工作会话不存在",
        ):
            self.assertEqual(
                views_agent_test._service_error_message(message),
                views_agent_test.RESOURCE_UNAVAILABLE_MESSAGE,
            )


class AgentTestResponsePrivacyTests(SimpleTestCase):
    def test_session_payload_excludes_pii_and_internal_session_fields(self):
        operator = User(
            uid="internal-user-id",
            username="",
            phone="13800000000",
            email="private@example.test",
        )
        agent = TransparentCompanyAgentDefinition(
            agentId="agent-1",
            name="Audit Agent",
        )
        session = TransparentCompanyAgentTestSession(
            operator=operator,
            agent=agent,
            conversationId="conversation-1",
            title="Audit",
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
            messageCount=1,
            lastMessage="hello",
            sessionKey="internal-session-key",
            source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
            created_at="2026-08-28",
            updated_at="2026-08-28",
        )

        payload = session.get_info()
        serialized = json.dumps(payload)

        for key in (
            "user",
            "user_id",
            "session_key",
            "memory_summary_present",
            "memory_summary_message_count",
            "memory_summary_updated_at",
        ):
            self.assertNotIn(key, payload)
        self.assertNotIn(operator.phone, serialized)
        self.assertNotIn(operator.email, serialized)
        self.assertNotIn(operator.uid, serialized)
