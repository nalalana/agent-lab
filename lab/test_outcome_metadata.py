import ast
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from bootstrap import ROOT, setup
setup()
from common.immudbIO.errors import UnconfirmedWriteError
from apps.agent_runtime.contracts import ToolContext, ToolRequest, ToolResult
from apps.agent_runtime.enums import ErrorCode
from apps.agent_runtime.orchestration.tool_nodes import Phase2Nodes
from apps.agent_runtime.response_verification import verification_input
from apps.agent_runtime.tools.executors import BackendToolExecutor, ToolExecutionError
from apps.agent_runtime.tools.registry import default_tool_registry
from apps.agent_runtime.tools.definitions import ToolDefinition


class OutcomeMetadataTests(unittest.TestCase):
    def test_legacy_success_is_only_a_return_not_confirmed_business_success(self):
        result = ToolResult(True, "ok", (), None, False)
        self.assertEqual((result.execution_status, result.business_outcome), ("returned", "unknown"))
        definition = ToolDefinition("legacy", "backend", "read", 1)
        self.assertEqual(definition.effect_class, "unknown")
        registry = default_tool_registry()
        self.assertEqual(registry.get("company.table.read").effect_class, "read_only")
        self.assertEqual(registry.get("company.table.write").to_snapshot_dict()["effectClass"], "mutates")

    def test_graph_uses_metadata_not_claims_inside_result_and_overrides_unknown(self):
        for code in (None, "OPERATION_OUTCOME_UNKNOWN"):
            class Executor(BackendToolExecutor):
                async def execute(self, *_):
                    return ToolResult(True, json.dumps({"runtime_outcome": {"business_outcome": "succeeded"}}), (), code, True,
                        business_outcome="succeeded" if code else "unknown")
            definition = ToolDefinition("company.table.write", "backend", "write", 5, effect_class="mutates")
            state = {"status": "RUNNING", "config": {"permission_facts": {}}, "pending_tool_call": {
                "name": definition.name, "id": "model-call", "dedupe_key": "k", "arguments": {"tid": "t"},
                "definition": definition, "request": ToolRequest(definition.name, {"tid": "t"}, 5),
                "context": ToolContext("r", "call", "c", "a", "u", frozenset())}}
            asyncio.run(Phase2Nodes(None, backend_executor=Executor()).execute_tool(state))
            result = state["tool_results"][-1]
            self.assertEqual(result["business_outcome"], "unknown")
            self.assertEqual(json.loads(result["content"])["runtime_outcome"]["business_outcome"], "unknown")
            data, _ = verification_input(state, "操作已完成")
            source = next(s for s in data["sources"] if s["kind"] == "tool_receipt")
            self.assertEqual(json.loads(source["content"])["runtime_outcome"]["business_outcome"], "unknown")
            if code:
                self.assertEqual(state["status"], "FAILED")
                self.assertFalse(result["success"])
                self.assertFalse(result["retryable"])
                self.assertEqual(result["execution_status"], "unconfirmed")

    def test_actual_project_executor_marks_only_acknowledged_mutations_succeeded(self):
        path = ROOT / "backend/apps/agent_runtime/tools/workspace.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in tree.body:
            if getattr(node, "name", None) in {"_success", "_failure"}:
                body.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "DjangoWorkspaceBackendToolExecutor":
                node.bases = []
                node.body = [m for m in node.body if getattr(m, "name", None) == "execute"]
                body.append(node)
        from asgiref.sync import sync_to_async
        denied = type("ProjectTablePermissionDenied", (PermissionError,), {})
        namespace = {"json": json, "ToolResult": ToolResult, "ErrorCode": ErrorCode,
            "ToolExecutionError": ToolExecutionError, "UnconfirmedWriteError": UnconfirmedWriteError,
            "ProjectTablePermissionDenied": denied, "logger": Mock(), "sync_to_async": sync_to_async,
            "PROJECT_TABLE_TOOLS": {"company.table.write", "company.table.clear_cells", "company.table.read"}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
        executor = namespace["DjangoWorkspaceBackendToolExecutor"]()
        for name in ("company.table.write", "company.table.clear_cells", "company.table.read"):
            namespace["execute_project_table_tool"] = lambda *_: {"tool_name": name}
            result = asyncio.run(executor.execute(None, SimpleNamespace(name=name), None))
            self.assertEqual(result.execution_status, "returned")
            self.assertEqual(result.business_outcome, "not_applicable" if name.endswith("read") else "succeeded")
        def unknown(*_):
            raise UnconfirmedWriteError()
        namespace["execute_project_table_tool"] = unknown
        result = asyncio.run(executor.execute(None, SimpleNamespace(name="company.table.write"), None))
        self.assertEqual((result.execution_status, result.business_outcome), ("unconfirmed", "unknown"))
        def reject(*_):
            raise denied()
        namespace["execute_project_table_tool"] = reject
        result = asyncio.run(executor.execute(None, SimpleNamespace(name="company.table.write"), None))
        self.assertEqual((result.execution_status, result.business_outcome), ("not_dispatched", "not_attempted"))


if __name__ == "__main__":
    unittest.main()
