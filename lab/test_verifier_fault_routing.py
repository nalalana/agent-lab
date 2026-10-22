"""Offline real-graph fault/accounting regression; no model quality claims."""
import asyncio
import ast
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bootstrap import ROOT, setup
setup()

from apps.agent_runtime.contracts import ModelEvent, RunRequest, ToolResult
from apps.agent_runtime.enums import ErrorCode, RunEventType
from apps.agent_runtime.orchestration.graph import Phase2AgentRuntimeFacade
from apps.agent_runtime.response_verification import ModelResponseVerifier
from apps.agent_runtime.tests.test_response_verification import claim, config, verdict
from apps.agent_runtime.tools.definitions import ToolDefinition
from apps.agent_runtime.tools.executors import BackendToolExecutor
from apps.agent_runtime.tools.registry import ToolRegistry, ToolRouter


class MeteredGateway:
    def __init__(self, steps, unmetered=()):
        self.steps = list(steps)
        self.unmetered = set(unmetered)
        self.requests = []
        self.reported = {}

    async def stream(self, request):
        self.requests.append(request)
        index = len(self.requests)
        purpose, response = self.steps.pop(0)
        assert request.purpose == purpose, (request.purpose, purpose)
        if callable(response):
            response = response(request)
        error = isinstance(response, ModelEvent) and response.event_type == "error"
        if not error:
            for event in response if isinstance(response, list) else [ModelEvent("text_delta", {"delta": response})]:
                yield event
        usage = {} if index in self.unmetered else {"input_tokens": index, "output_tokens": 2 * index, "total_tokens": 3 * index}
        if usage:
            for key, value in usage.items():
                self.reported[key] = self.reported.get(key, 0) + value
            yield ModelEvent("usage", usage)
        yield ModelEvent("model_call_metrics", {"purpose": purpose, "duration_ms": index, **usage})
        if error:
            yield response


class TableBackend(BackendToolExecutor):
    def __init__(self, outcome="succeeded"):
        self.outcome = outcome
        self.calls = []
        self.committed_writes = 0

    async def execute(self, definition, request, context):
        self.calls.append(request.name)
        if request.name == "company.table.read":
            return ToolResult(True, json.dumps({"tool_name": request.name, "tid": "stats",
                "data": {"table_version": 1, "rows": [["count", 20]]}}), (), None, False,
                business_outcome="not_applicable")
        if self.outcome == "unknown":
            raise TimeoutError("lost acknowledgement")
        if self.outcome == "conflict":
            return ToolResult(False, '{"error":"version conflict"}', (), "TABLE_WRITE_CONFLICT", False,
                business_outcome="conflict")
        self.committed_writes += 1
        return ToolResult(True, '{"changed":true}', (), None, False, business_outcome="succeeded")


def tool(name, call_id):
    return ModelEvent("tool_call_completed", {"id": call_id, "name": name, "arguments": {"tid": "stats"}})


def historical_approval(request):
    data = json.loads(request.messages[1]["content"])
    old = next(s for s in data["sources"] if s["kind"] == "historical_table_content")
    return verdict(claims=[claim("Previously observed 20", "historical_company_fact", old["id"], "20")])


def record_without_database(event):
    """Execute the original recorder body with mocked ORM managers, no imports."""
    path = ROOT / "backend/apps/agent_runtime/runtime.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    recorder = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AgentRunRecorder")
    method = next(n for n in recorder.body if isinstance(n, ast.FunctionDef) and n.name == "record")
    method.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
    runs, events, now = Mock(), Mock(), object()
    namespace = {"RunEventType": RunEventType, "ErrorCode": ErrorCode,
        "AgentRun": SimpleNamespace(STATUS_FAILED="FAILED", objects=runs),
        "AgentRunEvent": SimpleNamespace(objects=events), "timezone": SimpleNamespace(now=lambda: now)}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    namespace["record"](SimpleNamespace(run_id=event.run_id, _first_token_recorded=False), event)
    return runs, events, now


class VerifierFaultRoutingTests(unittest.TestCase):
    def graph(self, steps, *, outcome="succeeded", unmetered=()):
        gateway, backend = MeteredGateway(steps, unmetered), TableBackend(outcome)
        registry = ToolRegistry()
        for name, effect in (("company.table.read", "read_only"), ("company.table.write", "mutates")):
            registry.register(ToolDefinition(name, "backend", "test:" + name, 5,
                args_schema={"type": "object", "properties": {"tid": {"type": "string"}}, "required": ["tid"], "additionalProperties": False},
                effect_class=effect))
        question = "Inspect and update the stats table."
        context = {"config": config(question), "model_messages": [{"role": "user", "content": question}]}
        facade = Phase2AgentRuntimeFacade(gateway, response_verifier=ModelResponseVerifier(gateway),
            context_resolver=lambda _: context, backend_executor=backend, tool_router=ToolRouter(registry),
            permissions=frozenset("test:" + name for name in registry.definitions))
        request = RunRequest("fault-test", "company", "agent", "user", "session", "agent_test", "message", "en", "trace")

        async def run():
            return await facade.graph.ainvoke(facade._initial_state(request), config={"recursion_limit": 50})
        return asyncio.run(run()), gateway, backend

    def assert_accounting(self, final, gateway):
        self.assertEqual(final["usage"], gateway.reported)
        metrics = [e.data for e in final["events"] if str(e.event_type) == "model.call.completed"]
        self.assertEqual([m["duration_ms"] for m in metrics], list(range(1, len(gateway.requests) + 1)))
        self.assertEqual([m["purpose"] for m in metrics], [r.purpose for r in gateway.requests])

    def assert_fault(self, final, gateway, code, generation_calls):
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(final["error"].code, str(code))
        self.assertEqual(final["error"].stage, "verify_response")
        self.assertFalse(final["error"].retryable)
        self.assertEqual(final["response_retry_count"], 0)
        self.assertEqual(final["output_content"], "")
        types = [str(e.event_type) for e in final["events"]]
        self.assertNotIn("message.delta", types)
        self.assertNotIn("message.completed", types)
        self.assertNotIn("response.retry_required", types)
        self.assertEqual(sum(r.purpose == "response_generation" for r in gateway.requests), generation_calls)
        self.assertEqual(gateway.steps, [])
        self.assert_accounting(final, gateway)
        failure = next(e for e in final["events"] if str(e.event_type) == "run.failed")
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            self.assertEqual(key in failure.data, key in final["usage"])
            if key in final["usage"]:
                self.assertEqual(failure.data[key], final["usage"][key])

    def test_contract_exhaustion_after_successful_write_does_not_repeat_mutation_or_lose_usage(self):
        final, gateway, backend = self.graph([
            ("response_generation", [tool("company.table.read", "read")]),
            ("response_generation", [tool("company.table.write", "write")]),
            ("response_generation", "The requested write was acknowledged."),
            ("response_verification", "not JSON"),
            ("response_verification", "still not JSON"),
        ])
        self.assert_fault(final, gateway, ErrorCode.VERIFIER_CONTRACT_ERROR, 3)
        self.assertEqual(backend.calls, ["company.table.read", "company.table.write"])
        self.assertEqual(backend.committed_writes, 1)
        self.assertEqual(final["usage"]["total_tokens"], 45)

    def test_temporal_contract_and_model_faults_include_main_repair_usage_exactly_once(self):
        for response, code in (("not JSON", ErrorCode.VERIFIER_CONTRACT_ERROR),
                               (ModelEvent("error", {"code": "upstream_unavailable"}), ErrorCode.VERIFIER_MODEL_ERROR)):
            with self.subTest(code=code):
                final, gateway, backend = self.graph([
                    ("response_generation", [tool("company.table.read", "read")]),
                    ("response_generation", [tool("company.table.write", "write")]),
                    ("response_generation", "Previously observed 20; current value unconfirmed."),
                    ("response_verification", "not JSON"),
                    ("response_verification", historical_approval),
                    ("response_temporal_verification", response),
                ], outcome="conflict")
                self.assert_fault(final, gateway, code, 3)
                self.assertEqual(backend.calls.count("company.table.write"), 1)
                self.assertEqual(final["usage"]["total_tokens"], 63)

    def test_main_model_error_preserves_prior_repair_usage_and_does_not_estimate_missing_usage(self):
        for unmetered, expected in (((), 18), ((3,), 9)):
            with self.subTest(unmetered=unmetered):
                final, gateway, _ = self.graph([
                    ("response_generation", "Knowledge bases organize information."),
                    ("response_verification", "not JSON"),
                    ("response_verification", ModelEvent("error", {"code": "upstream_unavailable"})),
                ], unmetered=unmetered)
                self.assert_fault(final, gateway, ErrorCode.VERIFIER_MODEL_ERROR, 1)
                self.assertEqual(final["usage"]["total_tokens"], expected)

    def test_successful_repair_and_semantic_revision_keep_existing_usage_paths(self):
        for reviews, generations in ((["not JSON", verdict()], 1),
                                     ([verdict("revise", ["Correct the definition"]), verdict()], 2)):
            with self.subTest(generations=generations):
                steps = [("response_generation", "A conceptual explanation."), ("response_verification", reviews[0])]
                if generations == 2:
                    steps.append(("response_generation", "Knowledge bases organize information."))
                steps.append(("response_verification", reviews[1]))
                final, gateway, _ = self.graph(steps)
                self.assertEqual(final["status"], "COMPLETED")
                self.assertEqual(final["response_retry_count"], generations - 1)
                self.assert_accounting(final, gateway)

    def test_failed_event_persists_reported_input_and_output_tokens_without_live_database(self):
        final, gateway, _ = self.graph([
            ("response_generation", "A conceptual explanation."),
            ("response_verification", "not JSON"),
            ("response_verification", "still not JSON"),
        ])
        self.assert_fault(final, gateway, ErrorCode.VERIFIER_CONTRACT_ERROR, 1)
        failure = next(e for e in final["events"] if str(e.event_type) == "run.failed")
        runs, events, now = record_without_database(failure)
        runs.filter.assert_called_once_with(pk=failure.run_id)
        runs.filter.return_value.update.assert_called_once_with(status="FAILED",
            error_code="VERIFIER_CONTRACT_ERROR", error_message=failure.data["message"], completed_at=now,
            input_tokens=6, output_tokens=12)
        self.assertEqual(events.get_or_create.call_args.kwargs["defaults"]["payload"]["total_tokens"], 18)

    def test_unreported_usage_is_absent_from_failure_event_and_database_update(self):
        final, gateway, _ = self.graph([
            ("response_generation", "A conceptual explanation."),
            ("response_verification", ModelEvent("error", {"code": "upstream_unavailable"})),
        ], unmetered=(1, 2))
        self.assert_fault(final, gateway, ErrorCode.VERIFIER_MODEL_ERROR, 1)
        self.assertEqual(final["usage"], {})
        failure = next(e for e in final["events"] if str(e.event_type) == "run.failed")
        runs, _, _ = record_without_database(failure)
        updates = runs.filter.return_value.update.call_args.kwargs
        self.assertNotIn("input_tokens", updates)
        self.assertNotIn("output_tokens", updates)

    def test_unknown_write_still_stops_queued_mutation_and_all_verification(self):
        final, gateway, backend = self.graph([
            ("response_generation", [tool("company.table.read", "read")]),
            ("response_generation", [tool("company.table.write", "write"), tool("company.table.write", "queued")]),
        ], outcome="unknown")
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(final["error"].code, str(ErrorCode.OPERATION_OUTCOME_UNKNOWN))
        self.assertFalse(final["error"].retryable)
        self.assertEqual(backend.calls.count("company.table.write"), 1)
        self.assertTrue(all(r.purpose == "response_generation" for r in gateway.requests))
        self.assertEqual(final["response_retry_count"], 0)
        self.assertEqual(final["output_content"], "")
        self.assert_accounting(final, gateway)


if __name__ == "__main__":
    unittest.main()
