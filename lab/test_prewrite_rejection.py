"""Actual adapter and graph checks with substituted auth/storage; no DB or model."""
import ast
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bootstrap import ROOT, setup
setup()

from asgiref.sync import sync_to_async
from apps.agent_runtime.contracts import ModelEvent, RunRequest, ToolContext, ToolRequest, ToolResult
from apps.agent_runtime.enums import ErrorCode
from apps.agent_runtime.orchestration.graph import Phase2AgentRuntimeFacade
from apps.agent_runtime.orchestration.tool_nodes import Phase2Nodes
from apps.agent_runtime.tools.executors import ToolExecutionError
from apps.agent_runtime.tools.registry import default_tool_registry
from common.immudbIO.errors import UnconfirmedWriteError
from test_table_versions import project_adapter, read_receipt


def workspace_executor(service):
    # Compile the original adapter method without importing production settings.
    path = ROOT / "backend/apps/agent_runtime/tools/workspace.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if getattr(node, "name", None) in {"_failure", "_success"}:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "DjangoWorkspaceBackendToolExecutor":
            node.bases = []
            node.body = [method for method in node.body if getattr(method, "name", None) == "execute"]
            body.append(node)
    namespace = {"json": json, "ToolResult": ToolResult, "ErrorCode": ErrorCode,
        "ToolExecutionError": ToolExecutionError, "UnconfirmedWriteError": UnconfirmedWriteError,
        "ProjectTablePermissionDenied": type("Denied", (PermissionError,), {}),
        "sync_to_async": sync_to_async, "logger": Mock(),
        "PROJECT_TABLE_TOOLS": {"company.table.write", "company.table.clear_cells"},
        "execute_project_table_tool": project_adapter(service, SimpleNamespace(tid="t", num_columns=2),
                                                       type("Conflict", (Exception,), {}))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
    return namespace["DjangoWorkspaceBackendToolExecutor"]()


def storage():
    return SimpleNamespace(upsert_table_cells=Mock(return_value=(True, {})),
                           clear_table_cells=Mock(return_value=(True, 1)))


def execute_node(name, cells, service):
    arguments = {"tid": "t", "cells": cells}
    definition = default_tool_registry().get(name)
    state = {"status": "RUNNING", "run_id": "r", "tool_results": [read_receipt(0)],
        "pending_tool_call": {"name": name, "id": "call", "dedupe_key": "call", "arguments": arguments,
            "definition": definition, "request": ToolRequest(name, arguments, 5),
            "context": ToolContext("r", "call", "c", "a", "u", frozenset())}}
    asyncio.run(Phase2Nodes(None, backend_executor=workspace_executor(service)).execute_tool(state))
    return state


class PrewriteRejectionTests(unittest.TestCase):
    def test_all_explicit_local_cell_checks_report_not_dispatched_without_storage_calls(self):
        bad_write = [[], [{"r": 0, "c": 0}], [{"r": -1, "c": 0, "value": 1}],
                     [{"r": 0, "c": 0, "value": {"nested": 1}}]]
        bad_clear = [[], [{"r": "0", "c": 0}], [{"r": 0, "c": 2}],
                     [{"r": 0, "c": 0}, {"r": 0, "c": 0}]]
        for name, cases in (("company.table.write", bad_write), ("company.table.clear_cells", bad_clear)):
            for cells in cases:
                with self.subTest(name=name, cells=cells):
                    service = storage()
                    state = execute_node(name, cells, service)
                    service.upsert_table_cells.assert_not_called()
                    service.clear_table_cells.assert_not_called()
                    result = state["tool_results"][-1]
                    self.assertEqual(result["error_code"], str(ErrorCode.TOOL_INVALID_ARGUMENTS))
                    self.assertEqual((result["execution_status"], result["business_outcome"]),
                                     ("not_dispatched", "not_attempted"))
                    self.assertFalse(result["success"])
                    self.assertFalse(result["retryable"])
                    self.assertEqual(state["status"], "RUNNING")

    def test_service_value_error_after_effect_remains_unknown_for_write_and_clear(self):
        for name, method in (("company.table.write", "upsert_table_cells"),
                             ("company.table.clear_cells", "clear_table_cells")):
            with self.subTest(name=name):
                service, effects = storage(), []
                def partial_effect(**_):
                    effects.append("committed")
                    raise ValueError("same exception class after a possible remote effect")
                getattr(service, method).side_effect = partial_effect
                state = execute_node(name, [{"r": 0, "c": 0, "value": 1}], service)
                self.assertEqual(effects, ["committed"])
                self.assertEqual(state["tool_results"][-1]["source_error_code"], str(ErrorCode.TOOL_INVALID_ARGUMENTS))
                self.assertEqual(state["tool_results"][-1]["error_code"], str(ErrorCode.OPERATION_OUTCOME_UNKNOWN))
                self.assertEqual(state["status"], "FAILED")
                self.assertFalse(state["error"].retryable)

    def graph(self, cells, service, *, queued=False):
        class Gateway:
            calls = 0
            async def stream(self, request):
                self.calls += 1
                if self.calls == 1:
                    for call_id in (("first", "queued") if queued else ("first",)):
                        yield ModelEvent("tool_call_completed", {"name": "company.table.write", "id": call_id,
                            "arguments": {"tid": "t", "cells": cells}})
                else:
                    yield ModelEvent("text_delta", {"delta": "The input was rejected before any write."})
        gateway = Gateway()
        facade = Phase2AgentRuntimeFacade(gateway, backend_executor=workspace_executor(service),
            permissions=frozenset({"tool.project_table.write"}), context_resolver=lambda _: {
                "config": {"model_provider": "scripted", "model_name": "scripted", "credential_ref": "none",
                           "user_text": "Update the requested cells.", "permission_facts": {}},
                "model_messages": [{"role": "user", "content": "Update the requested cells."}]})
        request = RunRequest("r", "c", "a", "u", "s", "agent_test", "m", "en", "trace")
        state = facade._initial_state(request)
        state["tool_results"] = [read_receipt(0)]
        return asyncio.run(facade.graph.ainvoke(state)), gateway

    def test_actual_graph_can_report_prewrite_rejection_without_unknown_hard_stop(self):
        service = storage()
        final, gateway = self.graph([{"r": -1, "c": 0, "value": 1}], service)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["tool_results"][-1]["error_code"], str(ErrorCode.TOOL_INVALID_ARGUMENTS))
        self.assertIn("before any write", final["output_content"])
        self.assertEqual(gateway.calls, 2)
        service.upsert_table_cells.assert_not_called()

    def test_actual_graph_unknown_service_value_error_stops_queued_mutations(self):
        service, effects = storage(), []
        def partial_effect(**_):
            effects.append("committed")
            raise ValueError("response processing failed after effect")
        service.upsert_table_cells.side_effect = partial_effect
        final, gateway = self.graph([{"r": 0, "c": 0, "value": 1}], service, queued=True)
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(final["error"].code, str(ErrorCode.OPERATION_OUTCOME_UNKNOWN))
        self.assertEqual(final["tool_results"][-1]["source_error_code"], str(ErrorCode.TOOL_INVALID_ARGUMENTS))
        self.assertEqual(final["output_content"], "")
        self.assertEqual((gateway.calls, service.upsert_table_cells.call_count), (1, 1))
        self.assertEqual(effects, ["committed"])


if __name__ == "__main__":
    unittest.main()
