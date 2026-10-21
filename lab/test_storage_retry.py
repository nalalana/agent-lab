import ast
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bootstrap import ROOT, setup
setup()
from common.immudbIO.client import toumingClient
from common.immudbIO.errors import UnconfirmedWriteError
from apps.agent_runtime.contracts import ModelEvent, RunRequest, ToolRequest, ToolResult
from apps.agent_runtime.enums import ErrorCode
from apps.agent_runtime.orchestration.graph import Phase2AgentRuntimeFacade
from apps.agent_runtime.tools.definitions import ToolDefinition
from apps.agent_runtime.tools.executors import BackendToolExecutor, InMemoryToolCallRecorder, ToolExecutionError
from apps.agent_runtime.tools.registry import ToolRegistry, ToolRouter


class StorageRetryTests(unittest.TestCase):
    def test_uncertain_write_is_sent_once_and_keeps_cause(self):
        rpc = Mock()
        cause = TimeoutError("response lost after commit")
        rpc.sqlExec.side_effect = cause
        with patch("common.immudbIO.client.time.sleep") as sleep:
            with self.assertRaises(UnconfirmedWriteError) as caught:
                toumingClient(rpc).upsertToTable("cells", ["value"], ["@value"], {"value": 1})
        self.assertIs(caught.exception.__cause__, cause)
        self.assertEqual(rpc.sqlExec.call_count, 1)
        sleep.assert_not_called()

    def test_unconfirmed_response_is_not_accepted_even_without_exception(self):
        for response in (None, SimpleNamespace(txs=[], ongoingTx=False), SimpleNamespace(txs=[1], ongoingTx=True)):
            rpc = Mock()
            rpc.sqlExec.return_value = response
            with self.assertRaises(UnconfirmedWriteError):
                toumingClient(rpc).upsertToTable("cells", [], [], {})
            self.assertEqual(rpc.sqlExec.call_count, 1)

    def test_normal_write_and_active_transaction_keep_their_return_contract(self):
        rpc = Mock()
        committed = SimpleNamespace(txs=[1], ongoingTx=False)
        rpc.sqlExec.return_value = committed
        client = toumingClient(rpc)
        self.assertIs(client.upsertToTable("cells", [], [], {}), committed)
        transaction = Mock()
        staged = SimpleNamespace(txs=[], ongoingTx=True)
        transaction.sqlExec.return_value = staged
        client.currentTx = transaction
        self.assertIs(client.upsertToTable("cells", [], [], {}), staged)
        self.assertEqual(rpc.sqlExec.call_count, 1)
        self.assertEqual(transaction.sqlExec.call_count, 1)

    def test_read_still_retries_transient_failure(self):
        rpc = Mock()
        rpc.sqlQuery.side_effect = [TimeoutError("read timeout"), [[1]]]
        with patch("common.immudbIO.client.time.sleep"), patch("builtins.print"):
            self.assertEqual(toumingClient(rpc).simpleSelect("cells", ["value"], {}), [[1]])
        self.assertEqual(rpc.sqlQuery.call_count, 2)

    def test_workspace_executor_preserves_typed_unknown_code(self):
        # Exercise the original adapter method without importing production settings.
        from asgiref.sync import sync_to_async
        path = ROOT / "backend/apps/agent_runtime/tools/workspace.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in tree.body:
            if getattr(node, "name", None) in {"_failure", "_success"}:
                body.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "DjangoWorkspaceBackendToolExecutor":
                node.bases = []
                node.body = [m for m in node.body if getattr(m, "name", None) == "execute"]
                body.append(node)
        def unconfirmed(*_):
            raise UnconfirmedWriteError()
        namespace = {"json": json, "ToolResult": ToolResult, "UnconfirmedWriteError": UnconfirmedWriteError, "ToolExecutionError": ToolExecutionError,
            "PROJECT_TABLE_TOOLS": {"company.table.write"}, "sync_to_async": sync_to_async,
            "execute_project_table_tool": unconfirmed}
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
        executor = namespace["DjangoWorkspaceBackendToolExecutor"]()
        result = asyncio.run(executor.execute(None, SimpleNamespace(name="company.table.write"), None))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, UnconfirmedWriteError.code)
        self.assertFalse(result.retryable)

    def test_mutation_false_is_unknown_but_preflight_validation_stays_invalid(self):
        path = ROOT / "backend/apps/agent_runtime/tools/project_tables.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        body.extend(n for n in tree.body if getattr(n, "name", None) == "execute_project_table_tool")
        table = SimpleNamespace(tid="t", num_columns=2)
        manager = Mock()
        manager.filter.return_value.select_related.return_value.first.return_value = table
        service = SimpleNamespace(upsert_table_cells=Mock(return_value=(False, "partial or unconfirmed")),
            clear_table_cells=Mock(return_value=(False, "partial or unconfirmed")),
            get_table_data=Mock(return_value=(False, "read unavailable")))
        namespace = {"_resolve_context": lambda _: (object(), object(), object(), object(), "agent"),
            "GatesTable": SimpleNamespace(objects=manager), "Gates": SimpleNamespace(LABEL_COMPANY=1),
            "Resource": SimpleNamespace(TYPE_TABLE="table"), "_resource": lambda *_: object(),
            "runtime_action_allowed": lambda *_: True, "OpenGateService": service,
            "UnconfirmedWriteError": UnconfirmedWriteError, "ToolExecutionError": ToolExecutionError,
            "TableWriteConflict": type("TableWriteConflict", (Exception,), {})}
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
        execute = namespace["execute_project_table_tool"]
        for name in ("company.table.write", "company.table.clear_cells"):
            with self.assertRaises(UnconfirmedWriteError):
                execute(ToolRequest(name, {"tid": "t", "cells": [{"r": 0, "c": 0, "value": 1}]}, 5), SimpleNamespace(expected_table_version=0))
        before = service.upsert_table_cells.call_count
        with self.assertRaises(ValueError):
            execute(ToolRequest("company.table.write", {"tid": "t", "cells": []}, 5), None)
        self.assertEqual(service.upsert_table_cells.call_count, before)
        with self.assertRaises(ValueError):
            execute(ToolRequest("company.table.read", {"tid": "t"}, 5), None)

    def test_production_graph_stops_before_queued_write_or_another_model_call(self):
        async def run(mode):
            class Gateway:
                calls = 0
                async def stream(self, request):
                    self.calls += 1
                    if self.calls != 1:
                        raise AssertionError("Model resumed after an unknown write")
                    for call_id in ("first", "queued"):
                        yield ModelEvent("tool_call_completed", {"name": ("company.table.write" if mode == "timeout" else "runtime.write"), "id": call_id, "arguments": {}})
            class Executor(BackendToolExecutor):
                calls = 0
                async def execute(self, *_):
                    self.calls += 1
                    if mode == "timeout":
                        raise TimeoutError("outer executor deadline")
                    if mode == "typed":
                        raise ToolExecutionError(code=UnconfirmedWriteError.code, message=str(UnconfirmedWriteError()))
                    return ToolResult(False, json.dumps({"error": "unconfirmed"}), (), UnconfirmedWriteError.code, False)
            gateway, executor, recorder = Gateway(), Executor(), InMemoryToolCallRecorder()
            name = "company.table.write" if mode == "timeout" else "runtime.write"
            definition = ToolDefinition(name, "backend", "write", 5,
                args_schema={"type": "object", "properties": {}, "additionalProperties": False})
            facade = Phase2AgentRuntimeFacade(gateway, context_resolver=lambda _: {
                "config": {"model_provider": "scripted", "model_name": "scripted", "credential_ref": "none"},
                "model_messages": [{"role": "user", "content": "Write once."}]},
                permissions=frozenset({"write"}), tool_router=ToolRouter(ToolRegistry({name: definition})),
                backend_executor=executor, tool_recorder=recorder)
            request = RunRequest("r", "c", "a", "u", "s", "agent_test", "m", "en", "t")
            state = await facade.graph.ainvoke(facade._initial_state(request))
            self.assertEqual(state["status"], "FAILED")
            self.assertEqual(state["error"].code, str(ErrorCode.OPERATION_OUTCOME_UNKNOWN))
            self.assertEqual(state["error"].stage, "reconcile_tool")
            self.assertFalse(state["error"].retryable)
            self.assertEqual((gateway.calls, executor.calls), (1, 1))
            self.assertEqual(state["output_content"], "")
            self.assertFalse(state["completed_tool_call_keys"])
            self.assertTrue(state["tool_results"][0]["tool_call_id"])
            self.assertEqual(recorder.records[0].error_code, UnconfirmedWriteError.code)
        for mode in ("result", "typed", "timeout"):
            asyncio.run(run(mode))


if __name__ == "__main__":
    unittest.main()
