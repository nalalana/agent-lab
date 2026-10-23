"""Production writer/adapter contracts with real serializers and no database I/O."""
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bootstrap import setup
setup()

from apps.agent_runtime.contracts import ToolContext, ToolRequest
from apps.agent_runtime.tools.executors import ToolExecutionError
from common.immudbIO.errors import UnconfirmedWriteError
from run_table_boundary import load_writer
from test_prewrite_rejection import execute_node
from test_table_versions import project_adapter


def writer_fixture():
    remote = SimpleNamespace(upsert_data=Mock(return_value=(True, "written")),
        insert_row=Mock(return_value=(True, "written")),
        get_data=Mock(return_value=(True, {"max_row_index": 0})))
    writer = load_writer(remote)
    table = SimpleNamespace(pk=1, tid="t", num_columns=2, gate=SimpleNamespace(gid="test_fixture"), save=Mock())
    manager, service = Mock(), Mock(return_value=remote)
    manager.select_for_update.return_value.get.return_value = table
    created = []
    def start_audit(table, user, action, *args, **kwargs):
        event = SimpleNamespace(pk=len(created) + 1, id=len(created) + 1,
            row_index=kwargs.get("row_index"), column_index=kwargs.get("column_index"), action=action)
        created.append(event)
        return event
    audit = Mock(side_effect=start_audit)
    audit.created = created
    namespace = writer.upsert_table_cells.__globals__
    # This fixture isolates payload checks; durable state is tested with a real
    # ORM in test_durable_table_fence and with both servers in the safety oracle.
    namespace.update(transaction=SimpleNamespace(atomic=lambda **_: nullcontext(),
        get_connection=lambda: SimpleNamespace(in_atomic_block=False), get_autocommit=lambda: True),
        GatesTable=SimpleNamespace(objects=manager), ImmudbService=service,
        _table_version=Mock(return_value=0),
        _cell_versions=Mock(side_effect=lambda table, cells: {(c["r"], c["c"]): 0 for c in cells}),
        _start_table_audit=audit, _finish_table_audit=Mock(), _assert_table_write_fence=Mock(), logger=Mock())
    return writer, remote, table, namespace


class TablePreflightTests(unittest.TestCase):
    def test_invalid_second_payload_has_no_remote_audit_or_lock_effect_for_update_and_append(self):
        for append in (False, True):
            for value in ("", None):
                with self.subTest(append=append, value=value):
                    writer, remote, table, namespace = writer_fixture()
                    cells = [{"r": 1, "c": 0, "value": 100}, {"r": 1, "c": 1, "value": value}]
                    with self.assertRaises(namespace["TableCellValidationError"]) as caught:
                        writer.upsert_table_cells(SimpleNamespace(), table, cells, append=append,
                            expected_max_row_index=0 if append else None, expected_table_version=0)
                    self.assertIn("cts", caught.exception.errors)
                    namespace["ImmudbService"].assert_not_called()
                    namespace["GatesTable"].objects.select_for_update.assert_not_called()
                    namespace["_cell_versions"].assert_not_called()
                    namespace["_table_version"].assert_not_called()
                    namespace["_start_table_audit"].assert_not_called()
                    table.save.assert_not_called()
                    remote.upsert_data.assert_not_called()
                    remote.insert_row.assert_not_called()

    def test_valid_payloads_are_prepared_once_and_reused_by_each_write_path(self):
        for append in (False, True):
            with self.subTest(append=append):
                writer, remote, table, namespace = writer_fixture()
                prepare = Mock(wraps=writer._prepare_cell_payload)
                writer._prepare_cell_payload = prepare
                cells = [{"r": 1, "c": 0, "value": 100}, {"r": 1, "c": 1, "value": True}]
                ok, receipt = writer.upsert_table_cells(SimpleNamespace(), table, cells, append=append,
                    expected_max_row_index=0 if append else None, expected_table_version=0)
                self.assertTrue(ok)
                self.assertEqual(prepare.call_count, 2)
                self.assertEqual(receipt["table_version"], max(event.id for event in
                    namespace["_start_table_audit"].created if event.action == "cell.upsert"))
                if append:
                    payloads = remote.insert_row.call_args.args[2]
                    self.assertEqual(remote.insert_row.call_count, 1)
                    remote.upsert_data.assert_not_called()
                else:
                    payloads = [call.args[2] for call in remote.upsert_data.call_args_list]
                    self.assertEqual(remote.upsert_data.call_count, 2)
                    remote.insert_row.assert_not_called()
                self.assertEqual([(p["cts"], p["ctt"]) for p in payloads], [("100", "n"), ("1", "b")])

    def test_adapter_marks_only_typed_preflight_rejection_as_not_attempted(self):
        writer, remote, table, namespace = writer_fixture()
        adapter = project_adapter(writer, table, namespace["TableWriteConflict"])
        request = ToolRequest("company.table.write", {"tid": "t", "cells": [
            {"r": 0, "c": 0, "value": 100}, {"r": 0, "c": 1, "value": ""}]}, 5)
        context = ToolContext("r", "call", "company", "agent", "user", frozenset(), 0)
        with self.assertRaises(ToolExecutionError) as caught:
            adapter(request, context)
        self.assertEqual(caught.exception.code, "TOOL_INVALID_ARGUMENTS")
        self.assertEqual((caught.exception.execution_status, caught.exception.business_outcome),
                         ("not_dispatched", "not_attempted"))
        self.assertFalse(caught.exception.retryable)
        remote.upsert_data.assert_not_called()

    def test_false_and_exceptions_after_remote_dispatch_remain_unknown_without_retry(self):
        for failure in (False, ValueError("response failed after effect"), UnconfirmedWriteError()):
            with self.subTest(failure=type(failure).__name__):
                writer, remote, table, _ = writer_fixture()
                effects = []
                def partial_effect(*_):
                    effects.append("committed")
                    if isinstance(failure, Exception):
                        raise failure
                    return False, "partial remote effect"
                remote.upsert_data.side_effect = partial_effect
                def write(**kwargs):
                    kwargs["table"] = table
                    return writer.upsert_table_cells(**kwargs)
                state = execute_node("company.table.write", [{"r": 0, "c": 0, "value": 100}],
                                     SimpleNamespace(upsert_table_cells=write))
                self.assertEqual(effects, ["committed"])
                self.assertEqual(remote.upsert_data.call_count, 1)
                self.assertEqual(state["status"], "FAILED")
                self.assertEqual(state["error"].code, "OPERATION_OUTCOME_UNKNOWN")
                self.assertFalse(state["error"].retryable)


if __name__ == "__main__":
    unittest.main()
