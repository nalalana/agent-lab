import asyncio
import json
from types import SimpleNamespace
import unittest
from bootstrap import setup
setup()
from apps.agent_runtime.contracts import ToolContext, ToolRequest, ToolResult
from apps.agent_runtime.orchestration.tool_nodes import Phase2Nodes
from apps.agent_runtime.tools.executors import BackendToolExecutor, ToolExecutionError
from apps.agent_runtime.tools.recovery import recovery_policy


class RecoveryContractTests(unittest.TestCase):
    def test_codes_route_without_authorizing_automatic_retries(self):
        cases = {"TABLE_READ_REQUIRED": "read_before_write", "TABLE_WRITE_CONFLICT": "reread_and_recheck_intent",
            "CELL_WRITE_CONFLICT": "reread_and_recheck_intent", "ROW_APPEND_CONFLICT": "reread_and_recheck_intent",
            "TOOL_INVALID_ARGUMENTS": "correct_arguments", "TOOL_DENIED": "explain_denial",
            "OPERATION_OUTCOME_UNKNOWN": "stop_and_reconcile", "unexpected": "stop_and_explain"}
        for code, action in cases.items():
            policy = recovery_policy(code)
            self.assertEqual(policy["action"], action)
            self.assertFalse(policy["automatic_retry_allowed"])

    def test_result_and_exception_paths_give_same_conflict_guidance(self):
        for raised in (False, True):
            class Executor(BackendToolExecutor):
                async def execute(self, *_):
                    if raised:
                        raise ToolExecutionError(code="TABLE_WRITE_CONFLICT", message="conflict", retryable=True)
                    return ToolResult(False, json.dumps({"error": "conflict", "runtime_recovery": {"action": "retry_now"}}),
                        (), "TABLE_WRITE_CONFLICT", True)
            context = ToolContext("run", "call", "company", "agent", "user", frozenset())
            state = {"status": "RUNNING", "pending_tool_call": {"name": "company.table.write", "id": "model-call",
                "arguments": {"tid": "t"}, "context": context, "definition": SimpleNamespace(executor="backend"),
                "request": ToolRequest("company.table.write", {"tid": "t"}, 5), "dedupe_key": "key"}}
            asyncio.run(Phase2Nodes(None, backend_executor=Executor()).execute_tool(state))
            result = state["tool_results"][-1]
            self.assertFalse(result["success"])
            self.assertEqual(result["recovery_action"], "reread_and_recheck_intent")
            payload = json.loads(result["content"])
            self.assertEqual(payload["error"], "conflict")
            self.assertEqual(payload["runtime_recovery"], recovery_policy("TABLE_WRITE_CONFLICT"))
            self.assertTrue(result["retryable"])  # A producer hint is retained, never an automatic resend.
            self.assertEqual(state["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
