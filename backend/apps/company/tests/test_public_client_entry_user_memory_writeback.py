import json
import time
from unittest.mock import patch

from django.test import TestCase

from apps.company.models import (
    Company,
    CompanyUserMemoryItem,
    CompanyUserMemoryPolicy,
    CompanyUserMemoryWriteLog,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentUserMemoryBinding,
)
from apps.company.services import CompanyService
from apps.user.models import User


class PublicClientEntryUserMemoryWritebackTests(TestCase):
    def setUp(self):
        now = str(time.time())[:20]
        self.user = User.objects.create(
            uid="public-client-entry-writeback-user",
            username="public-client-entry-writeback-user",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="company-public-client-entry-writeback",
            company_name="Public Client Entry Writeback Inc",
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
            name="Customer Support",
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
        CompanyUserMemoryPolicy.objects.create(
            company=self.company,
            enabled=True,
            status=CompanyUserMemoryPolicy.STATUS_ACTIVE,
            maxRecallItems=5,
            maxRecallTokens=320,
            allowSensitive=False,
            writebackEnabled=True,
            writebackMode=CompanyUserMemoryPolicy.WRITEBACK_MODE_SHADOW,
            writeMinConfidence=0.9,
            created_at=now,
            updated_at=now,
        )

    def _stream_events(self):
        return iter(
            [
                {"event": "start"},
                {"event": "complete", "content": "assistant reply", "token_count": 8},
            ]
        )

    def test_public_client_entry_shadow_extractor_logs_candidates_without_mutating_memory(self):
        with patch(
            "apps.company.services.CompanyService._public_client_entry_user_memory_mainline_enabled",
            return_value=True,
        ), patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            return_value=(True, self._stream_events()),
        ), patch(
            "apps.company.services.CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime",
            return_value=(
                True,
                {
                    "assistant_content": json.dumps(
                        {
                            "items": [
                                {
                                    "action": "create",
                                    "memory_type": "preference",
                                    "scope": "company",
                                    "content": "Prefers concise answers",
                                    "confidence": 0.96,
                                    "reason": "stable preference",
                                }
                            ]
                        }
                    ),
                    "token_count": 12,
                },
            ),
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="Please keep answers short",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            list(stream)

        self.assertEqual(
            CompanyUserMemoryItem.objects.filter(company=self.company, user=self.user).count(),
            0,
        )
        log = CompanyUserMemoryWriteLog.objects.get(company=self.company, user=self.user)
        self.assertEqual(log.action, CompanyUserMemoryWriteLog.ACTION_SKIP)
        self.assertEqual(log.resultPayload.get("reason"), "shadow_mode")
        self.assertTrue(log.resultPayload.get("shadowMode"))
        self.assertEqual(log.resultPayload.get("writebackMode"), "shadow")
        self.assertEqual(log.resultPayload.get("extractorAction"), "create")
        self.assertEqual(log.resultPayload.get("scope"), CompanyUserMemoryItem.SCOPE_COMPANY)
        self.assertEqual(log.resultPayload.get("memoryType"), CompanyUserMemoryItem.TYPE_PREFERENCE)
        self.assertEqual(log.candidatePayload.get("writebackMode"), None)
        self.assertEqual(log.candidatePayload.get("content"), "Prefers concise answers")

    def test_public_client_entry_shadow_extractor_low_confidence_only_logs_skip(self):
        with patch(
            "apps.company.services.CompanyService._public_client_entry_user_memory_mainline_enabled",
            return_value=True,
        ), patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            return_value=(True, self._stream_events()),
        ), patch(
            "apps.company.services.CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime",
            return_value=(
                True,
                {
                    "assistant_content": json.dumps(
                        {
                            "items": [
                                {
                                    "action": "create",
                                    "memory_type": "preference",
                                    "scope": "company",
                                    "content": "May prefer concise answers",
                                    "confidence": 0.4,
                                    "reason": "weak signal",
                                }
                            ]
                        }
                    ),
                    "token_count": 8,
                },
            ),
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="Maybe keep it short",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            list(stream)

        self.assertFalse(
            CompanyUserMemoryItem.objects.filter(company=self.company, user=self.user).exists()
        )
        log = CompanyUserMemoryWriteLog.objects.get(company=self.company, user=self.user)
        self.assertEqual(log.action, CompanyUserMemoryWriteLog.ACTION_SKIP)
        self.assertEqual(log.resultPayload.get("reason"), "low_confidence")
        self.assertTrue(log.resultPayload.get("shadowMode"))
        self.assertEqual(log.resultPayload.get("writebackMode"), "shadow")
        self.assertEqual(log.resultPayload.get("extractorAction"), "create")

    def test_public_client_entry_shadow_extractor_invalid_json_is_safe(self):
        with patch(
            "apps.company.services.CompanyService._public_client_entry_user_memory_mainline_enabled",
            return_value=True,
        ), patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            return_value=(True, self._stream_events()),
        ), patch(
            "apps.company.services.CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime",
            return_value=(True, {"assistant_content": "not-json", "token_count": 4}),
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="Remember my preference",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            list(stream)

        self.assertFalse(
            CompanyUserMemoryItem.objects.filter(company=self.company, user=self.user).exists()
        )
        log = CompanyUserMemoryWriteLog.objects.get(company=self.company, user=self.user)
        self.assertEqual(log.action, CompanyUserMemoryWriteLog.ACTION_SKIP)
        self.assertEqual(log.resultPayload.get("reason"), "extractor_invalid_json")
        self.assertEqual(log.resultPayload.get("writebackMode"), "shadow")
        self.assertEqual(log.candidatePayload.get("writebackMode"), "shadow")

    def test_public_client_entry_shadow_extractor_runtime_failure_is_safe(self):
        with patch(
            "apps.company.services.CompanyService._public_client_entry_user_memory_mainline_enabled",
            return_value=True,
        ), patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            return_value=(True, self._stream_events()),
        ), patch(
            "apps.company.services.CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime",
            return_value=(False, "runtime unavailable"),
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="Remember this for later",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            list(stream)

        self.assertFalse(
            CompanyUserMemoryItem.objects.filter(company=self.company, user=self.user).exists()
        )
        log = CompanyUserMemoryWriteLog.objects.get(company=self.company, user=self.user)
        self.assertEqual(log.action, CompanyUserMemoryWriteLog.ACTION_SKIP)
        self.assertEqual(log.resultPayload.get("reason"), "extractor_runtime_error")
        self.assertEqual(log.resultPayload.get("writebackMode"), "shadow")
        self.assertEqual(log.candidatePayload.get("writebackMode"), "shadow")

    def test_public_client_entry_shadow_extractor_clamps_scope_for_agent_user_binding(self):
        now = str(time.time())[:20]
        TransparentCompanyAgentUserMemoryBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            memoryScope=TransparentCompanyAgentUserMemoryBinding.SCOPE_AGENT_USER,
            enabled=True,
            inheritCompanyPolicy=True,
            status=TransparentCompanyAgentUserMemoryBinding.STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
        )

        with patch(
            "apps.company.services.CompanyService._public_client_entry_user_memory_mainline_enabled",
            return_value=True,
        ), patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            return_value=(True, self._stream_events()),
        ), patch(
            "apps.company.services.CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime",
            return_value=(
                True,
                {
                    "assistant_content": json.dumps(
                        {
                            "items": [
                                {
                                    "action": "create",
                                    "memory_type": "constraint",
                                    "scope": "company",
                                    "content": "Do not use marketing language",
                                    "confidence": 0.95,
                                    "reason": "stable constraint",
                                }
                            ]
                        }
                    ),
                    "token_count": 10,
                },
            ),
        ):
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="Please avoid marketing language",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            list(stream)

        self.assertFalse(
            CompanyUserMemoryItem.objects.filter(company=self.company, user=self.user).exists()
        )
        log = CompanyUserMemoryWriteLog.objects.get(company=self.company, user=self.user)
        self.assertEqual(log.action, CompanyUserMemoryWriteLog.ACTION_SKIP)
        self.assertEqual(log.resultPayload.get("reason"), "shadow_mode")
        self.assertEqual(log.resultPayload.get("writebackMode"), "shadow")
        self.assertEqual(log.resultPayload.get("scope"), CompanyUserMemoryItem.SCOPE_AGENT)
        self.assertEqual(log.resultPayload.get("memoryType"), CompanyUserMemoryItem.TYPE_CONSTRAINT)
        self.assertEqual(log.candidatePayload.get("scope"), CompanyUserMemoryItem.SCOPE_AGENT)

    def test_public_client_entry_disabled_writeback_mode_skips_extractor_entirely(self):
        policy = CompanyUserMemoryPolicy.objects.get(company=self.company)
        policy.writebackEnabled = True
        policy.writebackMode = CompanyUserMemoryPolicy.WRITEBACK_MODE_DISABLED
        policy.save(update_fields=["writebackEnabled", "writebackMode"])

        with patch(
            "apps.company.services.CompanyService._public_client_entry_user_memory_mainline_enabled",
            return_value=True,
        ), patch(
            "apps.company.services.CompanyService._stream_client_entry_message_via_runtime",
            return_value=(True, self._stream_events()),
        ), patch(
            "apps.company.services.CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime",
        ) as extractor_mock:
            ok, stream = CompanyService.stream_transparent_company_client_entry_message(
                cid=self.company.cid,
                content="Please remember this preference",
                user=self.user,
                entry_key="__default__",
            )
            self.assertTrue(ok)
            list(stream)

        extractor_mock.assert_not_called()
        self.assertFalse(
            CompanyUserMemoryWriteLog.objects.filter(company=self.company, user=self.user).exists()
        )
