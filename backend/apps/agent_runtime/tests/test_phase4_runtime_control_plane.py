from __future__ import annotations

import asyncio
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.agent_runtime.contracts import ExecutionResult, ModelEvent, RunRequest, ToolContext, ToolRequest
from apps.agent_runtime.enums import ModelEventType, RunEventType
from apps.agent_runtime.models import AgentRun, AgentRunEvent, SandboxExecution
from apps.agent_runtime.operations import serialize_run_detail
from apps.agent_runtime.orchestration.graph import Phase2AgentRuntimeFacade
from apps.agent_runtime.providers.sandbox.fake import FakeSandboxProvider
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.agent_runtime.tools.definitions import ToolDefinition
from apps.agent_runtime.tools.executors import SandboxToolExecutor
from apps.company.models import TransparentCompanyAgentTestMessage


class Phase4RuntimeControlPlaneTests(SimpleTestCase):
    def test_facade_cancel_checker_interrupts_long_model_stream(self):
        checks = {"count": 0}

        def cancel_checker(_state):
            checks["count"] += 1
            return checks["count"] >= 3

        async def collect():
            facade = Phase2AgentRuntimeFacade(
                SlowModelGateway(),
                cancel_checker=cancel_checker,
                max_execution_seconds=5,
            )
            return [event async for event in facade.stream(_run_request())]

        events = asyncio.run(collect())

        self.assertEqual(events[0].event_type, str(RunEventType.RUN_STARTED))
        self.assertEqual(events[-1].event_type, str(RunEventType.RUN_CANCELLED))
        self.assertNotIn(str(RunEventType.MESSAGE_DELTA), [event.event_type for event in events])

    def test_sandbox_executor_emits_lifecycle_audit_without_external_id(self):
        provider = FakeSandboxProvider()
        provider.enqueue_execution(ExecutionResult(0, "ok", "", (), 1, False))
        audit_events = []
        executor = SandboxToolExecutor(provider, audit_sink=lambda event_type, payload: audit_events.append((event_type, payload)))

        async def run_tool():
            await executor.execute(
                ToolDefinition(
                    name="python.execute",
                    executor="sandbox",
                    required_permission="tool.code.execute",
                    timeout_seconds=3,
                    args_schema={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
                ),
                ToolRequest(name="python.execute", arguments={"code": "print('ok')"}, timeout_seconds=3),
                ToolContext(
                    run_id="phase4-run",
                    tool_call_id="tool-1",
                    company_id=1,
                    agent_id=2,
                    user_id=3,
                    permissions=frozenset({"tool.code.execute"}),
                ),
            )
            await executor.terminate_run("phase4-run")

        asyncio.run(run_tool())

        self.assertEqual([event_type for event_type, _payload in audit_events], [
            str(RunEventType.SANDBOX_STARTED),
            str(RunEventType.SANDBOX_COMPLETED),
        ])
        self.assertNotIn("external_id", audit_events[0][1])


class Phase4OperationsLifecycleTests(TestCase):
    def test_operations_lifecycle_prefers_persisted_sandbox_events(self):
        fixture = create_memory_fixture("phase4-ops")
        input_message = TransparentCompanyAgentTestMessage.objects.create(
            session=fixture["session_a1"],
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content="run sandbox",
            created_at="2026-08-14",
            updated_at="2026-08-14",
        )
        run = AgentRun.objects.create(
            company=fixture["company_a"],
            request_id="phase4-ops-run",
            agent=fixture["agent_a1"],
            user=fixture["owner"],
            session=fixture["session_a1"],
            source=AgentRun.SOURCE_AGENT_TEST,
            engine=AgentRun.ENGINE_UNIFIED,
            status=AgentRun.STATUS_COMPLETED,
            input_message=input_message,
            model_provider="openai",
            model_name="gpt-test",
        )
        SandboxExecution.objects.create(
            run=run,
            company=fixture["company_a"],
            external_id="agsx-secret-provider-id",
            status=SandboxExecution.STATUS_TERMINATED,
            template="touming-code-v1",
            cpu_cores=2,
            memory_mib=2048,
            network_policy="isolated",
            expires_at=timezone.now() + timedelta(minutes=5),
            ready_at=timezone.now(),
            terminated_at=timezone.now(),
            billed_seconds=2,
        )
        AgentRunEvent.objects.create(
            run=run,
            sequence=1,
            event_type=str(RunEventType.SANDBOX_STARTED),
            payload={"sandbox_id": "internal-id"},
        )
        AgentRunEvent.objects.create(
            run=run,
            sequence=2,
            event_type=str(RunEventType.SANDBOX_COMPLETED),
            payload={"sandbox_id": "internal-id"},
        )

        data = serialize_run_detail(run)

        self.assertEqual(
            [item["status"] for item in data["sandbox"]["lifecycle"]],
            ["running", "terminated"],
        )
        self.assertNotIn("agsx-secret-provider-id", str(data))


class SlowModelGateway:
    async def stream(self, _request):
        await asyncio.sleep(1)
        yield ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "late"})


def _run_request() -> RunRequest:
    return RunRequest(
        request_id="phase4-cancel",
        company_id=1,
        agent_id=2,
        user_id=3,
        session_id=4,
        source="agent_test",
        user_message_id=5,
        locale="zh-CN",
        trace_id="trace-phase4",
    )
