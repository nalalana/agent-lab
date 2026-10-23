"""Real serializers and extracted HTTP handlers; auth/storage are fixtures."""
import ast
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bootstrap import ROOT, setup
setup()

from django.http import JsonResponse
from apps.opengate.serializers import UpsertTableDataSerializer, DeleteTableRowSerializer
from common.immudbIO.errors import UnconfirmedWriteError
from test_table_write_protocol import guarded_writer, handlers as managed_handlers

RAW = {"cell_id": "legacy-cell", "r": 2, "c": 1, "ctfa": "General", "ctt": "s", "cts": "raw value",
       "bg": "#123456", "ff": "3", "fc": "#654321", "fs": 14, "bl": True}
HANDLERS = {"upsert_table_data": "upsert_table_data_full", "delete_table_row": "delete_table_row"}


def legacy_handlers(payload):
    writer, remote, audits = guarded_writer()
    types = writer.upsert_table_cells.__globals__
    service = SimpleNamespace(can_write_table=Mock(return_value=True),
        upsert_table_data_full=Mock(return_value=(True, "写入成功")), delete_table_row=Mock(return_value=(True, 2)))
    table = SimpleNamespace(tid="t", num_columns=2)
    manager = Mock()
    manager.get.return_value = table
    manager.filter.return_value.first.return_value = table
    user = SimpleNamespace(username="Fixture")
    namespace = {"JsonResponse": JsonResponse, "OpenGateService": service, "logger": Mock(),
        "TableCellValidationError": types["TableCellValidationError"],
        "TableWriteConflict": types["TableWriteConflict"], "TableWriteDurabilityError": types["TableWriteDurabilityError"],
        "UnconfirmedWriteError": UnconfirmedWriteError, "DeleteTableRowSerializer": DeleteTableRowSerializer,
        "_optional_user_from_request": lambda _: user, "_read_json_body": lambda _: (payload, None),
        "get_client_ip": lambda _: "127.0.0.1",
        "GatesTable": SimpleNamespace(objects=manager, DoesNotExist=type("Missing", (Exception,), {}))}
    path = ROOT / "backend/apps/opengate/views.py"
    body = [n for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name in {*HANDLERS, "_table_write_error_response"}]
    for node in body:
        node.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
    return namespace, service, SimpleNamespace(path="/fixture"), writer, remote, audits


class LegacyWriteCallerTests(unittest.TestCase):
    def test_real_serializers_keep_raw_values_and_optional_nonnegative_revision(self):
        for cls, data in ((UpsertTableDataSerializer, {"tid": "t", "values": RAW}),
                          (DeleteTableRowSerializer, {"tid": "t", "r": 2})):
            for revision in (None, 0, 7):
                selected = data if revision is None else {**data, "expected_table_version": revision}
                serializer = cls(data=selected)
                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(serializer.validated_data.get("expected_table_version"), revision)
                if cls is UpsertTableDataSerializer:
                    self.assertEqual(serializer.validated_data["values"], RAW)
            for revision in (-1, True, 1.5, "bad"):
                serializer = cls(data={**data, "expected_table_version": revision})
                self.assertFalse(serializer.is_valid())

    def test_actual_handlers_forward_revisions_preserve_raw_payload_and_success_shapes(self):
        for handler, method in HANDLERS.items():
            for revision in (None, 0, 7):
                data = {"tid": "t", "r": 2, "values": RAW}
                if revision is not None:
                    data["expected_table_version"] = revision
                views, service, request, *_ = legacy_handlers(data)
                response = views[handler](request)
                self.assertEqual(response.status_code, 200)
                kwargs = getattr(service, method).call_args.kwargs
                self.assertEqual(kwargs["expected_table_version"], revision)
                if handler == "upsert_table_data":
                    self.assertEqual(kwargs["values"], RAW)
                    self.assertEqual(json.loads(response.content), {"result": 1, "message": "写入成功"})
                else:
                    self.assertEqual(kwargs["row_index"], 2)
                    self.assertEqual(json.loads(response.content), {"result": 1, "message": "删除成功", "data": {"deleted_cells": 2}})

    def test_permission_and_bad_revision_reject_without_service_dispatch(self):
        for handler, method in HANDLERS.items():
            views, service, request, *_ = legacy_handlers({"tid": "t", "r": 2, "values": RAW})
            service.can_write_table.return_value = False
            self.assertEqual(views[handler](request).status_code, 403)
            getattr(service, method).assert_not_called()
            views["_optional_user_from_request"] = lambda _: None
            self.assertEqual(views[handler](request).status_code, 401)
            getattr(service, method).assert_not_called()
            views, service, request, *_ = legacy_handlers({"tid": "t", "r": 2, "values": RAW, "expected_table_version": -1})
            self.assertEqual(json.loads(views[handler](request).content)["result"], 0)
            getattr(service, method).assert_not_called()

    def test_typed_preflight_and_conflict_keep_their_own_contracts(self):
        for handler, method in HANDLERS.items():
            views, service, request, writer, *_ = legacy_handlers({"tid": "t", "r": 2, "values": RAW})
            types = writer.upsert_table_cells.__globals__
            getattr(service, method).side_effect = types["TableCellValidationError"]({"cts": ["blank"]})
            response = views[handler](request)
            result = json.loads(response.content)
            self.assertEqual(response.status_code, 400)
            self.assertEqual((result["code"], result["execution_status"], result["business_outcome"]),
                             ("TOOL_INVALID_ARGUMENTS", "not_dispatched", "not_attempted"))
            self.assertEqual(result["message"], {"cts": ["blank"]})
            getattr(service, method).side_effect = types["TableWriteConflict"]("TABLE_WRITE_CONFLICT", "changed", latest_table_version=9)
            response = views[handler](request)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.content)["data"], {"latest_table_version": 9})

    def test_actual_outer_atomic_guard_maps_to_predispatch_refusal_without_remote_calls(self):
        for handler, method in HANDLERS.items():
            views, service, request, writer, remote, audits = legacy_handlers({"tid": "t", "r": 2, "values": RAW})
            getattr(service, method).side_effect = lambda **_: writer.upsert_table_cells.__globals__["_require_table_autocommit"]()
            response = views[handler](request)
            result = json.loads(response.content)
            self.assertEqual(response.status_code, 409)
            self.assertEqual((result["code"], result["execution_status"], result["business_outcome"]),
                             ("TABLE_WRITE_DURABILITY_REQUIRED", "not_dispatched", "not_attempted"))
            self.assertFalse(result["retryable"])
            self.assertIn("联系管理员", result["message"])
            self.assertEqual((remote.mock_calls, audits.mock_calls), ([], []))

    def test_effect_then_unknown_or_untyped_false_never_claims_not_attempted(self):
        for handler, method in HANDLERS.items():
            for typed in (False, True):
                views, service, request, *_ = legacy_handlers({"tid": "t", "r": 2, "values": RAW})
                effects = []
                def dispatched(**_):
                    effects.append("possible effect")
                    if typed:
                        raise UnconfirmedWriteError()
                    return False, "legacy ambiguous failure"
                getattr(service, method).side_effect = dispatched
                response = views[handler](request)
                result = json.loads(response.content)
                self.assertEqual(response.status_code, 409)
                self.assertEqual((result["code"], result["execution_status"], result["business_outcome"]),
                                 ("OPERATION_OUTCOME_UNKNOWN", "unconfirmed", "unknown"))
                self.assertFalse(result["retryable"])
                self.assertNotIn("未执行", result["message"])
                self.assertEqual(effects, ["possible effect"])
                getattr(service, method).assert_called_once()

    def test_live_app_definition_still_calls_managed_cells_not_shadowed_raw_handler(self):
        writer, *_ = guarded_writer()
        service = SimpleNamespace(upsert_table_cells=Mock(return_value=(True, {"table_version": 8})),
                                  upsert_table_data_full=Mock())
        types = writer.upsert_table_cells.__globals__
        views, request = managed_handlers(service, types["TableWriteDurabilityError"], types["TableCellValidationError"])
        self.assertEqual(views["app_upsert_table_data"](request).status_code, 200)
        service.upsert_table_cells.assert_called_once()
        service.upsert_table_data_full.assert_not_called()
        for error in (types["TableWriteDurabilityError"]("outer atomic"), UnconfirmedWriteError()):
            payload = json.loads(views["_table_write_error_response"](error).content)
            self.assertEqual(payload["execution_status"], "unconfirmed" if isinstance(error, UnconfirmedWriteError) else "not_dispatched")


if __name__ == "__main__":
    unittest.main()
