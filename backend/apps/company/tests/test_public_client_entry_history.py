import time
from unittest.mock import patch

from django.test import TestCase

from apps.company.models import (
    Company,
    CompanySessionMemoryPolicy,
    CompanyUserMemoryItem,
    CompanyUserMemoryPolicy,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
)
from apps.company.services import CompanyService
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User
from common.utils.Login_utils import sign_token


class PublicClientEntryHistoryTests(TestCase):
    maxDiff = None

    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="public-client-entry-user",
            username="public-client-entry-user",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.other_user = User.objects.create(
            uid="public-client-entry-other-user",
            username="public-client-entry-other-user",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="company-public-client-entry-history",
            company_name="Public Client Entry History Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.transparent_company = TransparentCompany.objects.create(
            company=self.company,
            prompts="Public client entry prompts",
        )
        self.agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=self.transparent_company,
            agentId="support-agent",
            name="Support Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT,
            exposedToClient=True,
            isDefaultEntry=True,
            routeKey="",
            exposureOrder=100,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            isCoreAgent=False,
            created_at=now,
            updated_at=now,
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.user)

    def _token(self, user: User) -> str:
        return sign_token({"user_id": user.id})

    def _create_public_session(self, user: User, *, suffix: str) -> TransparentCompanyAgentTestSession:
        now = str(time.time())[:20]
        return TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            operator=user,
            conversationId=f"conversation-{suffix}",
            title="Public session",
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
            messageCount=0,
            lastMessage="",
            sessionKey=f"session-{suffix}",
            source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
            created_at=now,
            updated_at=now,
        )

    def _create_agent_test_session(self, user: User, *, suffix: str) -> TransparentCompanyAgentTestSession:
        now = str(time.time())[:20]
        return TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            operator=user,
            conversationId=f"agent-test-conversation-{suffix}",
            title="Agent test session",
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
            messageCount=0,
            lastMessage="",
            sessionKey=f"agent-test-session-{suffix}",
            source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
            created_at=now,
            updated_at=now,
        )
    def _append_message(
        self,
        session: TransparentCompanyAgentTestSession,
        *,
        role: str,
        content: str,
    ) -> TransparentCompanyAgentTestMessage:
        now = str(time.time())[:20]
        return TransparentCompanyAgentTestMessage.objects.create(
            session=session,
            role=role,
            content=content,
            tokenCount=0,
            toolCalls=[],
            rawPayload={},
            created_at=now,
            updated_at=now,
        )

    def test_company_admin_conversation_list_includes_public_client_entry_sessions(self):
        agent_test_session = self._create_agent_test_session(self.user, suffix="owner")
        public_session = self._create_public_session(self.other_user, suffix="other-user")

        ok, payload = CompanyService.list_company_conversations(
            cid=self.company.cid,
            user=self.user,
        )

        self.assertTrue(ok)
        conversation_ids = {item["conversation_id"] for item in payload["items"]}
        self.assertIn(agent_test_session.conversationId, conversation_ids)
        self.assertIn(public_session.conversationId, conversation_ids)
        self.assertEqual(payload["total"], 2)

    def test_company_admin_conversation_list_can_filter_by_source(self):
        self._create_agent_test_session(self.user, suffix="owner")
        public_session = self._create_public_session(self.other_user, suffix="other-user")

        ok, payload = CompanyService.list_company_conversations(
            cid=self.company.cid,
            user=self.user,
            source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
        )

        self.assertTrue(ok)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["conversation_id"], public_session.conversationId)
        self.assertEqual(payload["items"][0]["source"], TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY)

    def test_company_admin_can_read_public_client_entry_session_messages(self):
        public_session = self._create_public_session(self.other_user, suffix="other-user")
        self._append_message(public_session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="Hello")

        ok, payload = CompanyService.get_company_conversation_messages(
            cid=self.company.cid,
            conversation_id=public_session.conversationId,
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertEqual(payload["conversation_id"], public_session.conversationId)
        self.assertEqual(payload["source"], TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY)
        self.assertEqual(payload["messages"][0]["content"], "Hello")

    def _create_active_l1_policy(
        self,
        *,
        recent_window: int = 2,
        summary_trigger_tokens: int = 1,
        summary_max_tokens: int = 160,
        max_tokens: int = 4000,
    ) -> CompanySessionMemoryPolicy:
        now = str(time.time())[:20]
        return CompanySessionMemoryPolicy.objects.create(
            company=self.company,
            mode=CompanySessionMemoryPolicy.MODE_SESSION,
            status=CompanySessionMemoryPolicy.STATUS_ACTIVE,
            enabled=True,
            recentMessageWindow=recent_window,
            maxTokens=max_tokens,
            summaryTriggerTokens=summary_trigger_tokens,
            summaryMaxTokens=summary_max_tokens,
            summaryStyle=CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE,
            sessionTtlDays=30,
            redactPii=False,
            created_at=now,
            updated_at=now,
        )

    def test_client_entry_conversation_list_requires_login(self):
        response = self.client.get(
            f"/api/company/transparent/client-entry/conversations/list?cid={self.company.cid}"
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["result"], 0)

    def test_stream_public_client_entry_message_persists_messages_and_reuses_history(self):
        captured_payloads: list[dict] = []

        def fake_runtime_stream(_transparent_company, request_payload):
            captured_payloads.append(request_payload)
            content = str(request_payload.get("content") or "")
            return True, iter(
                [
                    {"event": "start"},
                    {
                        "event": "complete",
                        "content": f"received:{content}",
                        "token_count": 18,
                    },
                ]
            )

        with patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            side_effect=fake_runtime_stream,
        ):
            ok, first_stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="who are you?",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            first_events = list(first_stream)

            ok, second_stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="tell me about your products",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            second_events = list(second_stream)

        sessions = TransparentCompanyAgentTestSession.objects.filter(
            transparentCompany=self.transparent_company,
            operator=self.user,
            source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
        )
        self.assertEqual(sessions.count(), 1)
        session = sessions.first()
        assert session is not None

        stored_messages = list(session.messages.order_by("id"))
        self.assertEqual(len(stored_messages), 4)
        self.assertEqual([item.role for item in stored_messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(stored_messages[0].content, "who are you?")
        self.assertEqual(stored_messages[1].content, "received:who are you?")
        self.assertEqual(stored_messages[2].content, "tell me about your products")
        self.assertEqual(stored_messages[3].content, "received:tell me about your products")

        self.assertEqual(len(captured_payloads), 2)
        self.assertEqual(captured_payloads[0]["messages"], [])
        self.assertEqual(
            captured_payloads[1]["messages"],
            [
                {"role": "user", "content": "who are you?"},
                {"role": "assistant", "content": "received:who are you?"},
            ],
        )

        first_complete = next(item for item in first_events if item["event"] == "complete")
        second_complete = next(item for item in second_events if item["event"] == "complete")
        self.assertEqual(first_complete["data"]["conversation_id"], session.conversationId)
        self.assertEqual(second_complete["data"]["conversation_id"], session.conversationId)
        self.assertEqual(first_complete["data"]["session_key"], session.sessionKey)
        self.assertEqual(second_complete["data"]["session_key"], session.sessionKey)

    def test_client_entry_history_endpoints_are_scoped_to_current_user(self):
        my_session = self._create_public_session(self.user, suffix="mine")
        other_session = self._create_public_session(self.other_user, suffix="other")
        now = str(time.time())[:20]
        TransparentCompanyAgentTestMessage.objects.create(
            session=my_session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content="my question",
            tokenCount=0,
            toolCalls=[],
            rawPayload={},
            created_at=now,
            updated_at=now,
        )
        TransparentCompanyAgentTestMessage.objects.create(
            session=other_session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content="other question",
            tokenCount=0,
            toolCalls=[],
            rawPayload={},
            created_at=now,
            updated_at=now,
        )
        CompanyService._update_agent_test_session_summary(
            my_session,
            last_message="my question",
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
        )
        CompanyService._update_agent_test_session_summary(
            other_session,
            last_message="other question",
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
        )

        list_response = self.client.get(
            (
                "/api/company/transparent/client-entry/conversations/list"
                f"?cid={self.company.cid}&agent_id={self.agent.agentId}"
            ),
            HTTP_ACCESS_TOKEN=self._token(self.user),
        )
        self.assertEqual(list_response.status_code, 200)
        payload = list_response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(len(payload["data"]["items"]), 1)
        self.assertEqual(payload["data"]["items"][0]["conversation_id"], my_session.conversationId)
        self.assertEqual(
            payload["data"]["items"][0]["source"],
            TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
        )

        message_response = self.client.get(
            (
                "/api/company/transparent/client-entry/conversations/messages"
                f"?cid={self.company.cid}&conversation_id={my_session.conversationId}"
            ),
            HTTP_ACCESS_TOKEN=self._token(self.user),
        )
        self.assertEqual(message_response.status_code, 200)
        message_payload = message_response.json()
        self.assertEqual(message_payload["result"], 1)
        self.assertEqual(message_payload["data"]["session_key"], my_session.sessionKey)
        self.assertEqual(message_payload["data"]["source"], my_session.source)
        self.assertEqual(len(message_payload["data"]["messages"]), 1)
        self.assertEqual(message_payload["data"]["messages"][0]["content"], "my question")

        forbidden_response = self.client.get(
            (
                "/api/company/transparent/client-entry/conversations/messages"
                f"?cid={self.company.cid}&conversation_id={other_session.conversationId}"
            ),
            HTTP_ACCESS_TOKEN=self._token(self.user),
        )
        self.assertEqual(forbidden_response.status_code, 403)
        self.assertEqual(forbidden_response.json()["result"], 0)

    def test_public_client_entry_uses_summary_and_recent_window_when_threshold_reached(self):
        self._create_active_l1_policy()

        session = self._create_public_session(self.user, suffix="summary")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-2")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-2")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-3")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-3")

        captured_payloads: list[dict] = []

        def fake_runtime_stream(_transparent_company, request_payload):
            captured_payloads.append(request_payload)
            return True, iter(
                [
                    {"event": "start"},
                    {"event": "complete", "content": "assistant-4", "token_count": 12},
                ]
            )

        with patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            side_effect=fake_runtime_stream,
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="user-4",
                user=self.user,
                entry_key="__default__",
                conversation_id=session.conversationId,
            )
            self.assertTrue(ok)
            list(stream)

        self.assertEqual(len(captured_payloads), 1)
        runtime_messages = captured_payloads[0]["messages"]
        self.assertEqual(runtime_messages[0]["role"], "system")
        self.assertIn("user-1", runtime_messages[0]["content"])
        self.assertEqual(
            runtime_messages[1:],
            [
                {"role": "user", "content": "user-3"},
                {"role": "assistant", "content": "assistant-3"},
            ],
        )

        session.refresh_from_db()
        self.assertTrue(session.memorySummary)
        self.assertEqual(session.memorySummaryMessageCount, 6)
        self.assertTrue(session.memorySummaryUpdatedAt)

    def test_public_client_entry_can_fallback_to_full_history_when_memory_binding_is_disabled(self):
        now = str(time.time())[:20]
        self._create_active_l1_policy()
        TransparentCompanyAgentMemoryBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            memoryScope=TransparentCompanyAgentMemoryBinding.SCOPE_SESSION,
            enabled=False,
            inheritCompanyPolicy=True,
            status=TransparentCompanyAgentMemoryBinding.STATUS_INACTIVE,
            created_at=now,
            updated_at=now,
        )

        session = self._create_public_session(self.user, suffix="legacy")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-2")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-2")

        captured_payloads: list[dict] = []

        def fake_runtime_stream(_transparent_company, request_payload):
            captured_payloads.append(request_payload)
            return True, iter(
                [
                    {"event": "start"},
                    {"event": "complete", "content": "assistant-3", "token_count": 9},
                ]
            )

        with patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            side_effect=fake_runtime_stream,
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="user-3",
                user=self.user,
                entry_key="__default__",
                conversation_id=session.conversationId,
            )
            self.assertTrue(ok)
            list(stream)

        self.assertEqual(
            captured_payloads[0]["messages"],
            [
                {"role": "user", "content": "user-1"},
                {"role": "assistant", "content": "assistant-1"},
                {"role": "user", "content": "user-2"},
                {"role": "assistant", "content": "assistant-2"},
            ],
        )
        session.refresh_from_db()
        self.assertEqual(session.memorySummary, "")
        self.assertEqual(session.memorySummaryMessageCount, 0)

    def test_public_client_entry_without_memory_policy_keeps_legacy_full_history_replay(self):
        session = self._create_public_session(self.user, suffix="no-policy")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-2")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-2")

        captured_payloads: list[dict] = []

        def fake_runtime_stream(_transparent_company, request_payload):
            captured_payloads.append(request_payload)
            return True, iter(
                [
                    {"event": "start"},
                    {"event": "complete", "content": "assistant-3", "token_count": 9},
                ]
            )

        with patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            side_effect=fake_runtime_stream,
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="user-3",
                user=self.user,
                entry_key="__default__",
                conversation_id=session.conversationId,
            )
            self.assertTrue(ok)
            list(stream)

        self.assertEqual(
            captured_payloads[0]["messages"],
            [
                {"role": "user", "content": "user-1"},
                {"role": "assistant", "content": "assistant-1"},
                {"role": "user", "content": "user-2"},
                {"role": "assistant", "content": "assistant-2"},
            ],
        )
        session.refresh_from_db()
        self.assertEqual(session.memorySummary, "")
        self.assertEqual(session.memorySummaryMessageCount, 0)

    def test_public_client_entry_summary_mode_does_not_mix_other_users_history(self):
        self._create_active_l1_policy()

        my_session = self._create_public_session(self.user, suffix="mine-summary")
        self._append_message(my_session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="mine-user-1")
        self._append_message(
            my_session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content="mine-assistant-1",
        )
        self._append_message(my_session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="mine-user-2")
        self._append_message(
            my_session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content="mine-assistant-2",
        )

        other_session = self._create_public_session(self.other_user, suffix="other-summary")
        self._append_message(other_session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="other-user-1")
        self._append_message(
            other_session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content="other-assistant-1",
        )
        self._append_message(other_session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="other-user-2")
        self._append_message(
            other_session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content="other-assistant-2",
        )

        captured_payloads: list[dict] = []

        def fake_runtime_stream(_transparent_company, request_payload):
            captured_payloads.append(request_payload)
            return True, iter(
                [
                    {"event": "start"},
                    {"event": "complete", "content": "mine-assistant-3", "token_count": 9},
                ]
            )

        with patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            side_effect=fake_runtime_stream,
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="mine-user-3",
                user=self.user,
                entry_key="__default__",
                conversation_id=my_session.conversationId,
            )
            self.assertTrue(ok)
            list(stream)

        runtime_messages = captured_payloads[0]["messages"]
        serialized_payload = str(runtime_messages)
        self.assertIn("mine-user", serialized_payload)
        self.assertNotIn("other-user", serialized_payload)
        self.assertNotIn("other-assistant", serialized_payload)

    def test_public_client_entry_default_config_does_not_inject_long_term_memory_block(self):
        now = str(time.time())[:20]
        CompanyUserMemoryPolicy.objects.create(
            company=self.company,
            enabled=True,
            status=CompanyUserMemoryPolicy.STATUS_ACTIVE,
            maxRecallItems=3,
            maxRecallTokens=320,
            allowSensitive=False,
            writebackEnabled=True,
            created_at=now,
            updated_at=now,
        )
        CompanyUserMemoryItem.objects.create(
            company=self.company,
            user=self.user,
            agent=None,
            scope=CompanyUserMemoryItem.SCOPE_COMPANY,
            memoryKey="pref-source",
            memoryType=CompanyUserMemoryItem.TYPE_PREFERENCE,
            content="Prefer verifiable public data sources.",
            confidence=0.96,
            status=CompanyUserMemoryItem.STATUS_ACTIVE,
            lastSeenAt=now,
            lastConfirmedAt=now,
            created_at=now,
            updated_at=now,
        )

        captured_payloads: list[dict] = []

        def fake_runtime_stream(_transparent_company, request_payload):
            captured_payloads.append(request_payload)
            return True, iter(
                [
                    {"event": "start"},
                    {"event": "complete", "content": "assistant-1", "token_count": 9},
                ]
            )

        with patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            side_effect=fake_runtime_stream,
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="show me your public projects",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            list(stream)

        self.assertEqual(captured_payloads[0]["messages"], [])

    def test_public_client_entry_rebuilds_summary_when_stored_summary_is_missing_or_count_mismatch(self):
        self._create_active_l1_policy()

        session = self._create_public_session(self.user, suffix="summary-rebuild")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-1")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-2")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-2")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_USER, content="user-3")
        self._append_message(session, role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT, content="assistant-3")
        session.memorySummary = "stale-summary"
        session.memorySummaryMessageCount = 3
        session.save(update_fields=["memorySummary", "memorySummaryMessageCount"])

        captured_payloads: list[dict] = []

        def fake_runtime_stream(_transparent_company, request_payload):
            captured_payloads.append(request_payload)
            return True, iter(
                [
                    {"event": "start"},
                    {"event": "complete", "content": "assistant-4", "token_count": 11},
                ]
            )

        with patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            side_effect=fake_runtime_stream,
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="user-4",
                user=self.user,
                entry_key="__default__",
                conversation_id=session.conversationId,
            )
            self.assertTrue(ok)
            list(stream)

        runtime_messages = captured_payloads[0]["messages"]
        self.assertEqual(runtime_messages[0]["role"], "system")
        self.assertNotEqual(runtime_messages[0]["content"], "stale-summary")
        self.assertIn("user-1", runtime_messages[0]["content"])
        self.assertEqual(
            runtime_messages[1:],
            [
                {"role": "user", "content": "user-3"},
                {"role": "assistant", "content": "assistant-3"},
            ],
        )

        session.refresh_from_db()
        self.assertTrue(session.memorySummary)
        self.assertNotEqual(session.memorySummary, "stale-summary")
        self.assertEqual(session.memorySummaryMessageCount, 6)
        self.assertTrue(session.memorySummaryUpdatedAt)
