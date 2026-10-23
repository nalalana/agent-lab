"""Local contracts with substituted auth/remote store; no real-model scores."""
import ast
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from bootstrap import ROOT, setup
setup()
from apps.agent_runtime.contracts import ToolContext, ToolRequest, ToolResult
from apps.agent_runtime.orchestration.tool_nodes import Phase2Nodes, _table_read_version
from apps.agent_runtime.tools.executors import BackendToolExecutor, ToolExecutionError
from common.immudbIO.errors import UnconfirmedWriteError


def project_adapter(service, table, conflict_type, user=None):
    path = ROOT / "backend/apps/agent_runtime/tools/project_tables.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(n for n in tree.body if getattr(n, "name", None) == "execute_project_table_tool")
    manager = Mock()
    manager.filter.return_value.select_related.return_value.first.return_value = table
    namespace = {"_resolve_context": lambda _: (object(), object(), user or object(), object(), "agent"),
        "GatesTable": SimpleNamespace(objects=manager), "Gates": SimpleNamespace(LABEL_COMPANY=1),
        "Resource": SimpleNamespace(TYPE_TABLE="table"), "_resource": lambda *_: object(),
        "runtime_action_allowed": lambda *_: True, "OpenGateService": service,
        "UnconfirmedWriteError": UnconfirmedWriteError, "ToolExecutionError": ToolExecutionError,
        "TableWriteConflict": conflict_type,
        "TableCellValidationError": getattr(getattr(service, "upsert_table_cells", None), "__globals__", {}).get(
            "TableCellValidationError", type("TableCellValidationError", (ValueError,), {}))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
    return namespace["execute_project_table_tool"]


def read_receipt(version=3, tid="t"):
    return {"name": "company.table.read", "arguments": {"tid": tid}, "success": True,
        "content": json.dumps({"tool_name": "company.table.read", "tid": tid,
            "data": {"table_version": version}})}


class TableVersionTests(unittest.TestCase):
    def test_clear_requires_trusted_version_through_graph_and_adapter(self):
        service = SimpleNamespace(clear_table_cells=Mock(return_value=(True, 1)))
        adapter = project_adapter(service, SimpleNamespace(tid="t", num_columns=2), RuntimeError)
        class Executor(BackendToolExecutor):
            async def execute(self, definition, request, context):
                return ToolResult(True, json.dumps(adapter(request, context)), (), None, False)
        nodes = Phase2Nodes(None, backend_executor=Executor())
        state = {"status": "RUNNING", "run_id": "r", "tool_results": []}
        def dispatch():
            arguments = {"tid": "t", "cells": [{"r": 0, "c": 0}], "expected_table_version": 999}
            state["pending_tool_call"] = {"name": "company.table.clear_cells", "id": "clear", "dedupe_key": "clear",
                "arguments": arguments, "definition": SimpleNamespace(executor="backend"),
                "request": ToolRequest("company.table.clear_cells", arguments, 5),
                "context": ToolContext("r", "clear", "c", "a", "u", frozenset(), 999)}
            asyncio.run(nodes.execute_tool(state))
        dispatch()
        self.assertEqual(state["tool_results"][-1]["error_code"], "TABLE_READ_REQUIRED")
        service.clear_table_cells.assert_not_called()
        state["tool_results"].append(read_receipt(0))
        dispatch()
        self.assertTrue(state["tool_results"][-1]["success"])
        self.assertEqual(service.clear_table_cells.call_args.kwargs["expected_table_version"], 0)
        dispatch()
        self.assertEqual(state["tool_results"][-1]["error_code"], "TABLE_READ_REQUIRED")
        self.assertEqual(service.clear_table_cells.call_count, 1)

    def test_latest_matching_read_only(self):
        self.assertEqual(_table_read_version([read_receipt(0)], "t"), 0)
        self.assertEqual(_table_read_version([read_receipt(2), read_receipt(8, "other")], "t"), 2)
        for last in (read_receipt(True), read_receipt(-1), read_receipt("3"),
                {**read_receipt(), "success": False}, {**read_receipt(), "content": "bad"},
                {**read_receipt(), "content": "[]"},
                {**read_receipt(), "content": read_receipt(9, "other")["content"]},
                {**read_receipt(), "name": "company.table.write"},
                {**read_receipt(), "name": "company.table.clear_cells"}):
            self.assertIsNone(_table_read_version([read_receipt(), last], "t"))
        self.assertIsNone(_table_read_version([], "t"))
        self.assertEqual(_table_read_version([read_receipt(), {**read_receipt(),
            "name": "company.table.write"}, read_receipt(7)], "t"), 7)

    def test_adapter_requires_and_forwards_runtime_version(self):
        service = SimpleNamespace(upsert_table_cells=Mock(return_value=(True, {})))
        execute = project_adapter(service, SimpleNamespace(tid="t", num_columns=2), RuntimeError)
        request = ToolRequest("company.table.write", {"tid": "t", "expected_table_version": 999,
            "cells": [{"r": 0, "c": 0, "value": 1}]}, 5)
        context = ToolContext("r", "call", "c", "a", "u", frozenset())
        with self.assertRaises(ToolExecutionError) as caught:
            execute(request, context)
        self.assertEqual(caught.exception.code, "TABLE_READ_REQUIRED")
        service.upsert_table_cells.assert_not_called()
        from dataclasses import replace
        execute(request, replace(context, expected_table_version=3))
        self.assertEqual(service.upsert_table_cells.call_args.kwargs["expected_table_version"], 3)

    def test_actual_graph_dispatch_uses_receipt_not_model_or_old_context(self):
        class Executor(BackendToolExecutor):
            async def execute(self, definition, request, context):
                self.context = context
                return ToolResult(True, "{}", (), None, False)
        for results, expected in (([read_receipt(4)], 4), ([], None)):
            executor = Executor()
            nodes = Phase2Nodes(None, backend_executor=executor)
            context = ToolContext("r", "call", "c", "a", "u", frozenset(), 999)
            state = {"status": "RUNNING", "run_id": "r", "tool_results": results,
                "pending_tool_call": {"name": "company.table.write", "id": "call", "dedupe_key": "x",
                    "arguments": {"tid": "t"}, "definition": SimpleNamespace(executor="backend"),
                    "request": ToolRequest("company.table.write", {"tid": "t", "expected_table_version": 999}, 5),
                    "context": context}}
            asyncio.run(nodes.execute_tool(state))
            self.assertEqual(executor.context.expected_table_version, expected)

    def test_graph_and_adapter_require_new_read_after_each_attempt(self):
        service = SimpleNamespace(upsert_table_cells=Mock(return_value=(True, {})))
        adapter = project_adapter(service, SimpleNamespace(tid="t", num_columns=2), RuntimeError)
        class Executor(BackendToolExecutor):
            async def execute(self, definition, request, context):
                return ToolResult(True, json.dumps(adapter(request, context)), (), None, False)
        nodes = Phase2Nodes(None, backend_executor=Executor())
        state = {"status": "RUNNING", "run_id": "r", "tool_results": []}
        def dispatch():
            arguments = {"tid": "t", "cells": [{"r": 0, "c": 0, "value": 1}]}
            state["pending_tool_call"] = {"name": "company.table.write", "id": "call", "dedupe_key": "x",
                "arguments": arguments, "definition": SimpleNamespace(executor="backend"),
                "request": ToolRequest("company.table.write", arguments, 5),
                "context": ToolContext("r", "call", "c", "a", "u", frozenset())}
            asyncio.run(nodes.execute_tool(state))
        dispatch()
        self.assertEqual(state["tool_results"][-1]["error_code"], "TABLE_READ_REQUIRED")
        service.upsert_table_cells.assert_not_called()
        state["tool_results"].append(read_receipt(4))
        dispatch()
        self.assertTrue(state["tool_results"][-1]["success"])
        dispatch()
        self.assertEqual(state["tool_results"][-1]["error_code"], "TABLE_READ_REQUIRED")
        self.assertEqual(service.upsert_table_cells.call_count, 1)


if __name__ == "__main__":
    unittest.main()
