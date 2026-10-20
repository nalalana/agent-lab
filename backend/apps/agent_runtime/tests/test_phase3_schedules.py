"""Phase 3-A scheduled Agent data source and runner tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.agent_runtime.models import AgentRun, AgentSchedule
from apps.agent_runtime.schedules import compute_next_run_at, run_due_schedule, scan_due_agent_schedules
from apps.agent_runtime.views import schedule_list
from apps.company.models import TransparentCompanyAgentTestMessage, TransparentCompanyAgentTestSession
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from common.utils.Login_utils import sign_token


class AgentScheduleTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.fixture = create_memory_fixture("phase3-schedule")
        self.company = self.fixture["company_a"]
        self.agent = self.fixture["agent_a1"]
        self.owner = self.fixture["owner"]

    def test_schedule_create_api_initializes_due_time(self):
        request = self.factory.post(
            "/api/agent-runtime/schedules",
            {
                "cid": self.company.cid,
                "name": "daily digest",
                "schedule_type": "every",
                "schedule_value": "5m",
                "message": "summarize the workspace",
                "agent_id": self.agent.agentId,
                "enabled": "true",
            },
            HTTP_ACCESS_TOKEN=sign_token({"user_id": self.owner.pk}),
        )

        with patch("apps.agent_runtime.views.get_user_from_request", return_value=self.owner), patch(
            "apps.agent_runtime.views.CompanyService._get_company_for_admin",
            return_value=(True, self.company),
        ):
            response = schedule_list(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["data"]["schedule_type"], AgentSchedule.SCHEDULE_EVERY)
        self.assertTrue(payload["data"]["next_run_at"])
        self.assertTrue(payload["data"]["session"].startswith("schedule:"))
        self.assertEqual(AgentSchedule.objects.count(), 1)

    def test_due_schedule_creates_cron_run_and_single_assistant_message(self):
        schedule = AgentSchedule.objects.create(
            company=self.company,
            agent=self.agent,
            operator=self.owner,
            name="digest",
            schedule_type=AgentSchedule.SCHEDULE_EVERY,
            schedule_value="5m",
            message="run digest",
            enabled=True,
            next_run_at=timezone.now() - timedelta(minutes=1),
        )

        runtime = FakeRuntimeService()
        result = run_due_schedule(schedule, runtime_service=runtime)

        self.assertEqual(result.status, "completed")
        run = AgentRun.objects.get(pk=result.run_id)
        self.assertEqual(run.source, AgentRun.SOURCE_CRON)
        self.assertEqual(run.input_message.content, "run digest")
        self.assertIn("系统已经到期并触发", runtime.seen_content)
        self.assertIsNotNone(run.output_message_id)
        self.assertEqual(
            TransparentCompanyAgentTestMessage.objects.filter(session=run.session).count(),
            2,
        )
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.next_run_at)
        self.assertEqual(schedule.consecutive_failures, 0)

        first_session_id = run.session_id
        AgentSchedule.objects.filter(pk=schedule.pk).update(next_run_at=timezone.now() - timedelta(minutes=1))
        second = run_due_schedule(schedule, runtime_service=FakeRuntimeService())
        second_run = AgentRun.objects.get(pk=second.run_id)
        self.assertEqual(second_run.session_id, first_session_id)
        self.assertEqual(TransparentCompanyAgentTestSession.objects.filter(sessionKey=schedule.session_key).count(), 1)
        self.assertEqual(TransparentCompanyAgentTestMessage.objects.filter(session_id=first_session_id).count(), 4)

    def test_scanner_skips_retry_when_last_run_completed_side_effect_tool(self):
        schedule = AgentSchedule.objects.create(
            company=self.company,
            agent=self.agent,
            operator=self.owner,
            name="side effect",
            schedule_type=AgentSchedule.SCHEDULE_EVERY,
            schedule_value="5m",
            message="run mutation",
            enabled=True,
            next_run_at=timezone.now() - timedelta(minutes=1),
        )
        run = FakeRuntimeService.create_run(self.company, self.agent, self.fixture["session_a1"], self.owner)
        schedule.last_run = run
        schedule.save(update_fields=["last_run"])

        with patch("apps.agent_runtime.schedules.SIDE_EFFECT_TOOL_NAMES", frozenset({"external.write"})):
            run.tool_calls.create(
                model_call_id="call-1",
                tool_name="external.write",
                executor="backend",
                status="completed",
                arguments_redacted={},
                arguments_hash="hash",
            )
            result = scan_due_agent_schedules(limit=10)

        self.assertEqual(result["runs"][0]["skipped_reason"], "side_effect_retry_blocked")
        schedule.refresh_from_db()
        self.assertGreater(schedule.consecutive_failures, 0)

    def test_one_time_schedule_is_paused_after_manual_execution(self):
        schedule = AgentSchedule.objects.create(
            company=self.company,
            agent=self.agent,
            operator=self.owner,
            name="one time reminder",
            schedule_type=AgentSchedule.SCHEDULE_AT,
            schedule_value=(timezone.now() + timedelta(hours=2)).isoformat(),
            message="remind me once",
            enabled=True,
            status=AgentSchedule.STATUS_ACTIVE,
            next_run_at=timezone.now() - timedelta(minutes=1),
        )

        result = run_due_schedule(schedule, runtime_service=FakeRuntimeService())

        self.assertEqual(result.status, "completed")
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        self.assertEqual(schedule.status, AgentSchedule.STATUS_PAUSED)
        self.assertIsNone(schedule.next_run_at)

    def test_celery_beat_registers_schedule_and_knowledge_tasks(self):
        beat = settings.CELERY_BEAT_SCHEDULE
        self.assertEqual(
            beat["agent-runtime-schedule-runner"]["task"],
            "apps.agent_runtime.tasks.run_due_agent_schedules",
        )
        self.assertEqual(
            beat["agent-runtime-knowledge-file-scanner"]["task"],
            "apps.agent_runtime.knowledge.tasks.enqueue_pending_knowledge_files",
        )
        self.assertEqual(
            beat["agent-runtime-knowledge-retry"]["task"],
            "apps.agent_runtime.knowledge.tasks.retry_failed_knowledge_jobs",
        )

    def test_cron_next_run_supports_five_field_expressions(self):
        base = timezone.datetime(2026, 8, 13, 10, 0, tzinfo=timezone.get_current_timezone())
        self.assertEqual(
            compute_next_run_at("cron", "*/15 * * * *", now=base).minute,
            15,
        )

    @override_settings(USE_TZ=False, TIME_ZONE="Asia/Shanghai")
    def test_at_schedule_uses_local_datetime_when_timezone_support_is_disabled(self):
        base = datetime(2026, 8, 31, 10, 0)

        self.assertEqual(
            compute_next_run_at("at", "2026-08-31T17:00", now=base),
            datetime(2026, 8, 31, 17, 0),
        )
        self.assertEqual(
            compute_next_run_at("at", "2026-08-31T17:00:00+08:00", now=base),
            datetime(2026, 8, 31, 17, 0),
        )


class FakeRuntimeService:
    def stream(self, *, company, agent, session, input_message, source):
        self.seen_content = input_message.content
        run = self.create_run(company, agent, session, session.operator, input_message=input_message, source=source)
        yield {"event": "start", "run_id": str(run.pk)}
        yield {"event": "delta", "run_id": str(run.pk), "delta": "scheduled"}
        yield {"event": "complete", "run_id": str(run.pk), "content": "scheduled", "token_count": 1}

    @staticmethod
    def create_run(company, agent, session, user, input_message=None, source=AgentRun.SOURCE_CRON):
        if input_message is None:
            input_message = TransparentCompanyAgentTestMessage.objects.create(
                session=session,
                role=TransparentCompanyAgentTestMessage.ROLE_USER,
                content="previous",
                created_at="2026-08-13",
                updated_at="2026-08-13",
            )
        return AgentRun.objects.create(
            company=company,
            request_id=f"schedule-test:{input_message.pk}",
            agent=agent,
            user=user,
            session=session,
            source=source,
            engine=AgentRun.ENGINE_UNIFIED,
            input_message=input_message,
        )
