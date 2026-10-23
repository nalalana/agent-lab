"""Actual extracted callers with substituted IAM/transaction context; no DB/model."""
import ast
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bootstrap import ROOT, setup
setup()

from django.http import JsonResponse
from apps.agent_runtime.contracts import ToolContext, ToolRequest
from apps.agent_runtime.tools.executors import ToolExecutionError
from apps.opengate.serializers import SimpleUpsertTableDataSerializer
from common.immudbIO.errors import UnconfirmedWriteError
from run_table_boundary import load_writer
from test_prewrite_rejection import execute_node
from test_table_versions import project_adapter


def guarded_writer():
    remote = Mock()
    writer = load_writer(remote)
    namespace = writer.upsert_table_cells.__globals__
    audits = Mock()
    namespace["transaction"] = SimpleNamespace(
        get_connection=lambda: SimpleNamespace(in_atomic_block=True),
        get_autocommit=lambda: False,
        atomic=Mock(side_effect=AssertionError("Transaction entered after pre-dispatch refusal")),
    )
    namespace["TableAuditEvent"] = SimpleNamespace(objects=audits)
    return writer, remote, audits


def handlers(service, durability_error):
    path = ROOT / "backend/apps/opengate/views.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {"_table_write_error_response", "upsert_table_cells", "app_upsert_table_data"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
    for node in body:
        node.decorator_list = []  # Authentication is an explicit fixture below.
    user = SimpleNamespace(uid="fixture", username="Fixture", id=1)
    table = SimpleNamespace(tid="t", num_columns=2, gate=SimpleNamespace(gid="test_fixture"))
    manager = Mock()
    manager.get.return_value = table
    payload = {"tid": "t", "values": [{"r": 0, "c": 0, "value": 100}], "expected_table_version": 0}
    namespace = {"JsonResponse": JsonResponse, "logger": Mock(), "OpenGateService": service,
        "TableWriteConflict": type("Conflict", (Exception,), {}),
        "TableWriteDurabilityError": durability_error, "UnconfirmedWriteError": UnconfirmedWriteError,
        "_optional_user_from_request": lambda _: user, "_api_key_principal": lambda _: (user, None),
        "_read_json_body": lambda _: (payload, None), "get_client_ip": lambda _: "127.0.0.1",
        "GatesTable": SimpleNamespace(objects=manager, DoesNotExist=type("Missing", (Exception,), {})),
        "SimpleUpsertTableDataSerializer": SimpleUpsertTableDataSerializer,
        "RiskControlService": SimpleNamespace(evaluate=lambda *a, **kw: SimpleNamespace(allowed=True))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
    request = SimpleNamespace(method="POST", gid="test_fixture", api_key=SimpleNamespace(secret_id="fixture"), path="/fixture")
    return namespace, request


class TableWriteProtocolTests(unittest.TestCase):
    def test_actual_agent_write_and_clear_preserve_pre_dispatch_durability_refusal(self):
        for name in ("company.table.write", "company.table.clear_cells"):
            with self.subTest(tool=name):
                writer, remote, audits = guarded_writer()
                adapter = project_adapter(writer, SimpleNamespace(tid="t", num_columns=2),
                    writer.upsert_table_cells.__globals__["TableWriteConflict"])
                request = ToolRequest(name, {"tid": "t", "cells": [{"r": 0, "c": 0, "value": 100}]}, 5)
                context = ToolContext("r", "call", "c", "a", "u", frozenset(), 0)
                with self.assertRaises(ToolExecutionError) as caught:
                    adapter(request, context)
                error = caught.exception
                self.assertEqual(error.code, "TABLE_WRITE_DURABILITY_REQUIRED")
                self.assertEqual((error.execution_status, error.business_outcome), ("not_dispatched", "not_attempted"))
                self.assertFalse(error.retryable)
                self.assertIn("联系管理员", str(error))
                self.assertEqual(remote.mock_calls, [])
                self.assertEqual(audits.mock_calls, [])

    def test_actual_tool_node_does_not_turn_pre_dispatch_refusal_into_unknown(self):
        for name in ("company.table.write", "company.table.clear_cells"):
            writer, remote, audits = guarded_writer()
            state = execute_node(name, [{"r": 0, "c": 0, "value": 100}], writer)
            result = state["tool_results"][-1]
            self.assertEqual(result["error_code"], "TABLE_WRITE_DURABILITY_REQUIRED")
            self.assertEqual((result["execution_status"], result["business_outcome"]), ("not_dispatched", "not_attempted"))
            self.assertFalse(result["retryable"])
            self.assertEqual(state["status"], "RUNNING")
            self.assertEqual(remote.mock_calls, [])
            self.assertEqual(audits.mock_calls, [])

    def test_both_actual_http_handlers_return_non_retryable_409_without_side_effects(self):
        writer, remote, audits = guarded_writer()
        views, request = handlers(writer, writer.upsert_table_cells.__globals__["TableWriteDurabilityError"])
        for name in ("upsert_table_cells", "app_upsert_table_data"):
            with self.subTest(handler=name):
                response = views[name](request)
                payload = json.loads(response.content)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(payload["code"], "TABLE_WRITE_DURABILITY_REQUIRED")
                self.assertEqual((payload["execution_status"], payload["business_outcome"]), ("not_dispatched", "not_attempted"))
                self.assertFalse(payload["retryable"])
                self.assertIn("联系管理员", payload["message"])
        self.assertEqual(remote.mock_calls, [])
        self.assertEqual(audits.mock_calls, [])

    def test_both_actual_http_handlers_keep_committed_unknown_distinct(self):
        writer, _, _ = guarded_writer()
        effects = []
        def committed_unknown(**_):
            effects.append("committed")
            raise UnconfirmedWriteError()
        service = SimpleNamespace(upsert_table_cells=Mock(side_effect=committed_unknown))
        views, request = handlers(service, writer.upsert_table_cells.__globals__["TableWriteDurabilityError"])
        for name in ("upsert_table_cells", "app_upsert_table_data"):
            response = views[name](request)
            payload = json.loads(response.content)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(payload["code"], "OPERATION_OUTCOME_UNKNOWN")
            self.assertEqual((payload["execution_status"], payload["business_outcome"]), ("unconfirmed", "unknown"))
            self.assertFalse(payload["retryable"])
            self.assertIn("不要重复提交", payload["message"])
            self.assertNotIn("本次未执行", payload["message"])
        self.assertEqual(effects, ["committed", "committed"])


if __name__ == "__main__":
    unittest.main()
