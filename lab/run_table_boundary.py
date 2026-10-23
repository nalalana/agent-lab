"""Source-extracted table writer + real Django ORM + substituted remote SQLite.

Not a full platform/IAM/immudb integration test. No production settings imported.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from bootstrap import ROOT, setup
from durable_counter import transaction
from legacy_write_guard import LegacyWriteGuard, OutcomeUnknown

SOURCE = ROOT / "backend/apps/opengate/services.py"
CLIENT_SOURCE = ROOT / "backend/common/immudbIO/client.py"


class RemoteFixture:
    def __init__(self, path, fault=None):
        self.path, self.fault, self.calls = path, fault, 0
        with transaction(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS cells (tid TEXT, r INTEGER, c INTEGER, value TEXT, PRIMARY KEY(tid,r,c))")

    def upsert_data(self, gid, tid, payload):
        self.calls += 1
        if self.fault == "second_rejected" and self.calls == 2:
            self.fault = None
            return False, "remote refused the second cell"
        with transaction(self.path) as db:
            db.execute("INSERT OR REPLACE INTO cells VALUES (?, ?, ?, ?)", (tid, payload["r"], payload["c"], payload["cts"]))
        if self.fault in {"lost_ack", "typed_lost_ack"}:
            fault, self.fault = self.fault, None
            if fault == "typed_lost_ack":
                from common.immudbIO.errors import UnconfirmedWriteError
                raise UnconfirmedWriteError()
            raise TimeoutError("remote committed before the response was lost")
        return True, "written"

    def values(self, tid):
        with transaction(self.path) as db:
            return list(db.execute("SELECT r,c,value FROM cells WHERE tid=? ORDER BY r,c", (tid,)))

    def clear_cells(self, gid, tid, cells):
        self.calls += 1
        cleared = []
        with transaction(self.path) as db:
            for cell in cells:
                changed = db.execute("UPDATE cells SET value='' WHERE tid=? AND r=? AND c=?",
                    (tid, cell["r"], cell["c"])).rowcount
                if changed:
                    cleared.append(cell)
        if self.fault == "lost_ack":
            self.fault = None
            raise TimeoutError("clear committed before response loss")
        return True, {"cleared_cells": cleared}


def load_writer(remote):
    from django.db import transaction as django_transaction
    from django.db.models import Max
    from apps.opengate.models import GatesTable, TableAuditEvent
    from common.immudbIO.models import CellCtfaChoices, CellCttChoices
    from common.immudbIO.serializers import WriteDataSerializer
    from common.immudbIO.errors import UnconfirmedWriteError
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    helpers = {"TableWriteConflict", "TableCellValidationError", "_cell_versions", "_table_version", "_start_table_audit",
               "_finish_table_audit", "_ip_suffix", "_table_write_text_items"}
    methods = {"get_table_data", "upsert_table_cells", "clear_table_cells", "_clear_table_cells_locked",
               "_ensure_cell_dict", "_normalize_cell_value", "_prepare_cell_payload"}
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if getattr(node, "name", None) in helpers:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "OpenGateService":
            node.body = [m for m in node.body if getattr(m, "name", None) in methods]
            body.append(node)
    namespace = {"transaction": django_transaction, "Max": Max, "GatesTable": GatesTable,
        "TableAuditEvent": TableAuditEvent, "UnconfirmedWriteError": UnconfirmedWriteError, "time": time, "logger": logging.getLogger("isolated-table-boundary"),
        "CellCtfaChoices": CellCtfaChoices, "CellCttChoices": CellCttChoices, "WriteDataSerializer": WriteDataSerializer,
        "TextModerationAsset": SimpleNamespace(SOURCE_EXTERNAL_API_TABLE_UPSERT="fixture"),
        "_moderate_opengate_texts": lambda **_: (True, ""), "ImmudbService": lambda: remote}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SOURCE), "exec"), namespace)
    writer = namespace["OpenGateService"]
    writer.can_write_table = staticmethod(lambda user, table: True)  # IAM is outside this diagnostic.
    writer.can_read_table = staticmethod(lambda user, table: True)
    return writer


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    setup()
    from common.immudbIO.errors import UnconfirmedWriteError
    logger = logging.getLogger("isolated-table-boundary")
    logger.addHandler(logging.FileHandler(output / "expected_faults.log", encoding="utf-8"))
    logger.propagate = False
    from django.db import connection
    from apps.opengate.models import Gates, GatesTable, APIKey, TableAuditEvent
    from apps.user.models import User
    connection.close()
    connection.settings_dict["NAME"] = str(output / "orm.sqlite")
    with connection.schema_editor() as editor:
        for model in (User, Gates, GatesTable, APIKey, TableAuditEvent):
            editor.create_model(model)
    user = User.objects.create(uid="boundary-user", username="Fixture", created_at="1", updated_at="1")
    gate = Gates.objects.create(gid="boundary-project", owner=user, gatename="Fixture", created_at="1", updated_at="1")
    manifest = {"kind": "source_extracted_cross_database_contract_diagnostic",
        "time": datetime.now(timezone.utc).isoformat(), "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "client_source": str(CLIENT_SOURCE.relative_to(ROOT)),
        "client_sha256": hashlib.sha256(CLIENT_SOURCE.read_bytes()).hexdigest(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "substitutions": ["IAM allow fixture", "moderation allow fixture", "immudb replaced by independent SQLite"],
        "real_orm_and_audit": True, "postgresql_lock_behavior_tested": False, "model_calls": 0}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []

    def table(name):
        return GatesTable.objects.create(tid=name, gate=gate, name=name, num_columns=2, created_at="1", updated_at="1")

    def record(name, check):
        row = {"case": name}
        try:
            row.update(check(), checks_passed=True)
        except Exception as exc:
            row.update(checks_passed=False, error_type=type(exc).__name__, error=str(exc)[-500:])
        rows.append(row)
        print(f"{name}: checks_passed={row['checks_passed']}", flush=True)

    def lost_ack():
        t = table("lost-ack")
        remote = RemoteFixture(output / "remote.sqlite", "lost_ack")
        writer = load_writer(remote)
        cells = [{"r": 0, "c": 0, "value": 100}]
        ok, _ = writer.upsert_table_cells(user, t, cells)
        assert ok is False
        assert [tuple(r) for r in remote.values(t.tid)] == [(0, 0, "100")]
        assert TableAuditEvent.objects.filter(table=t).count() == 0
        writer.upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 200}])
        writer.upsert_table_cells(user, t, cells)  # Blind retry overwrites an intervening write.
        assert remote.values(t.tid)[0]["value"] == "100"
        return {"returned_success": False, "remote_committed": True, "audit_after_lost_ack": 0,
                "intervening_value": 200, "blind_retry_value": 100}

    def partial():
        t = table("partial")
        remote = RemoteFixture(output / "remote.sqlite", "second_rejected")
        ok, _ = load_writer(remote).upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 10}, {"r": 0, "c": 1, "value": 20}])
        assert ok is False
        assert [tuple(r) for r in remote.values(t.tid)] == [(0, 0, "10")]
        results = list(TableAuditEvent.objects.filter(table=t).order_by("id").values_list("result", flat=True))
        assert results == ["success", "failed"]
        return {"returned_success": False, "remote_cells_changed": 1, "audit_results": results}

    def guarded():
        t = table("guarded")
        remote = RemoteFixture(output / "remote.sqlite", "lost_ack")
        writer = load_writer(remote)
        guard = LegacyWriteGuard(output / "guard.sqlite", scope="company:user:agent:task")
        args = {"tid": t.tid, "cells": [{"r": 0, "c": 0, "value": 100}]}
        execute = lambda: writer.upsert_table_cells(user, t, args["cells"])
        try:
            guard.invoke("edit", "company.table.write", args, authorize=lambda: True, execute=execute)
        except OutcomeUnknown:
            pass
        else:
            raise AssertionError("Lost acknowledgement was treated as safe")
        writer.upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 200}])
        calls = remote.calls
        for intent in ("edit", "new-model-intent"):
            try:
                LegacyWriteGuard(output / "guard.sqlite", scope="company:user:agent:task").invoke(
                    intent, "company.table.write", args, authorize=lambda: True, execute=execute)
            except OutcomeUnknown:
                pass
            else:
                raise AssertionError("Unresolved task was retried")
        assert remote.calls == calls and remote.values(t.tid)[0]["value"] == "200"
        # A fresh interpreter must also refuse to reach an executor.
        code = """import json,sys
from legacy_write_guard import LegacyWriteGuard, OutcomeUnknown
args=json.load(sys.stdin)
def forbidden(): raise AssertionError('executor was reached')
try: LegacyWriteGuard(sys.argv[1],scope='company:user:agent:task').invoke('edit','company.table.write',args,authorize=lambda:True,execute=forbidden)
except OutcomeUnknown: print('blocked')
else: raise AssertionError('retry accepted')
"""
        child = subprocess.run([sys.executable, "-c", code, str(output / "guard.sqlite")], cwd=ROOT / "lab",
            input=json.dumps(args), capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert child.returncode == 0 and child.stdout.strip() == "blocked", child.stderr
        return {"journal": guard.status("edit", authorize=lambda: True), "retry_and_new_intent_blocked": True,
                "fresh_process_blocked": True, "intervening_value_preserved": 200}

    def replay():
        t = table("replay")
        remote = RemoteFixture(output / "remote.sqlite")
        writer = load_writer(remote)
        guard = LegacyWriteGuard(output / "guard.sqlite", scope="company:user:agent:other-task")
        args = {"tid": t.tid, "cells": [{"r": 0, "c": 0, "value": 10}]}
        execute = lambda: writer.upsert_table_cells(user, t, args["cells"])
        original = guard.invoke("edit", "company.table.write", args, authorize=lambda: True, execute=execute)
        repeated = guard.invoke("edit", "company.table.write", args, authorize=lambda: True, execute=execute)
        assert original["operation_id"] == repeated["operation_id"] and repeated["replayed"] and remote.calls == 1
        try:
            guard.invoke("edit", "company.table.write", args, authorize=lambda: False, execute=execute)
        except PermissionError:
            pass
        else:
            raise AssertionError("Revoked authorization replayed a receipt")
        try:
            guard.invoke("edit", "company.table.write", {**args, "tid": "changed"}, authorize=lambda: True, execute=execute)
        except ValueError:
            pass
        else:
            raise AssertionError("Changed payload replayed old result")
        return {"remote_calls": remote.calls, "historical_receipt_replayed": True, "revocation_and_payload_conflict_blocked": True}

    def stale_version():
        from test_table_versions import project_adapter, read_receipt
        from apps.agent_runtime.contracts import ToolContext, ToolRequest
        from apps.agent_runtime.orchestration.tool_nodes import _table_read_version
        from apps.agent_runtime.tools.executors import ToolExecutionError
        t = table("version-boundary")
        remote = RemoteFixture(output / "remote.sqlite")
        writer = load_writer(remote)
        cells = [{"r": 0, "c": 0, "value": 100}]
        ok, original = writer.upsert_table_cells(user, t, cells)
        assert ok
        writer.upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 200}])
        calls = remote.calls
        try:
            writer.upsert_table_cells(user, t, cells, expected_table_version=original["table_version"])
        except Exception as exc:
            assert getattr(exc, "code", None) == "TABLE_WRITE_CONFLICT"
        else:
            raise AssertionError("Stale version was accepted")
        assert remote.calls == calls and remote.values(t.tid)[0]["value"] == "200"
        execute = project_adapter(writer, t, writer.upsert_table_cells.__globals__["TableWriteConflict"], user)
        version = _table_read_version([read_receipt(original["table_version"], t.tid)], t.tid)
        context = ToolContext("r", "call", "c", "a", "u", frozenset(), version)
        request = ToolRequest("company.table.write", {"tid": t.tid, "cells": cells}, 5)
        try:
            execute(request, context)
        except ToolExecutionError as exc:
            assert exc.code == "TABLE_WRITE_CONFLICT" and not exc.retryable
        else:
            raise AssertionError("Agent adapter overwrote an intervening write")
        assert remote.calls == calls and remote.values(t.tid)[0]["value"] == "200"
        from dataclasses import replace
        latest = writer.upsert_table_cells.__globals__["_table_version"](t)
        assert execute(request, replace(context, expected_table_version=latest))["updated_cells"] == 1
        assert remote.calls == calls + 1 and remote.values(t.tid)[0]["value"] == "100"
        return {"original_version_rejected_after_intervening_write": True,
                "agent_adapter_conflict_before_remote_call": True,
                "fresh_read_version_allows_authorized_write": True}

    def hidden_client_retry():
        tree = ast.parse(CLIENT_SOURCE.read_text(encoding="utf-8"))
        body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"RETRY_MAX_COUNT", "SLEEP_MIN", "SLEEP_MAX"} for t in node.targets):
                body.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "toumingClient":
                node.body = [m for m in node.body if getattr(m, "name", None) == "upsertToTable"]
                body.append(node)
        remote = RemoteFixture(output / "remote.sqlite", "lost_ack")
        tid = "client-retry"
        sql_calls = []
        def sql_exec(query, params):
            sql_calls.append(query)
            remote.upsert_data("gid", tid, params)
            return SimpleNamespace(txs=[1], ongoingTx=False)
        def intervening_write(_seconds):
            remote.upsert_data("gid", tid, {"r": 0, "c": 0, "cts": "200"})
        namespace = {"UnconfirmedWriteError": UnconfirmedWriteError, "time": SimpleNamespace(sleep=intervening_write),
            "random": SimpleNamespace(uniform=lambda *_: 0),
            "print": lambda *args: logger.warning(" ".join(str(x) for x in args))}
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(CLIENT_SOURCE), "exec"), namespace)
        client = namespace["toumingClient"]()
        client.currentTx = None
        client.client = SimpleNamespace(sqlExec=sql_exec)
        client.prepareUpsertQuery = lambda *_: "fixture-upsert"
        outer_calls = []
        def execute():
            outer_calls.append(1)
            response = client.upsertToTable(tid, [], [], {"r": 0, "c": 0, "cts": "100"})
            return True, {"confirmed_txs": len(response.txs)}
        guard = LegacyWriteGuard(output / "guard.sqlite", scope="company:user:agent:sdk-task")
        try:
            guard.invoke("edit", "company.table.write", {"tid": tid}, authorize=lambda: True, execute=execute)
        except OutcomeUnknown:
            pass
        else:
            raise AssertionError("Unconfirmed SDK response was accepted")
        assert len(outer_calls) == 1 and len(sql_calls) == 1 and remote.calls == 1
        intervening_write(0)
        try:
            guard.invoke("edit", "company.table.write", {"tid": tid}, authorize=lambda: True, execute=execute)
        except OutcomeUnknown:
            pass
        else:
            raise AssertionError("Unknown write was retried")
        assert len(sql_calls) == 1 and remote.values(tid)[0]["value"] == "200"
        assert guard.status("edit", authorize=lambda: True)["state"] == "unknown"
        return {"outer_guard_executions": 1, "client_sql_attempts": 1,
                "intervening_value": 200, "final_value": 200, "unknown_preserved": True}

    def typed_service_error():
        t = table("typed-unknown")
        remote = RemoteFixture(output / "remote.sqlite", "typed_lost_ack")
        try:
            load_writer(remote).upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 10}])
        except UnconfirmedWriteError:
            pass
        else:
            raise AssertionError("Service swallowed the typed unknown outcome")
        assert remote.values(t.tid)[0]["value"] == "10"
        assert TableAuditEvent.objects.filter(table=t).count() == 0
        return {"typed_error_preserved": True, "remote_committed": True, "local_audit_rows": 0}

    def clear_version():
        from test_table_versions import project_adapter
        from apps.agent_runtime.contracts import ToolContext, ToolRequest
        from apps.agent_runtime.tools.executors import ToolExecutionError
        from dataclasses import replace
        t = table("clear-version")
        remote = RemoteFixture(output / "remote.sqlite")
        writer = load_writer(remote)
        ok, original = writer.upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 100}])
        assert ok
        ok, latest = writer.upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 200}])
        assert ok
        adapter = project_adapter(writer, t, writer.upsert_table_cells.__globals__["TableWriteConflict"], user)
        request = ToolRequest("company.table.clear_cells", {"tid": t.tid, "cells": [{"r": 0, "c": 0}]}, 5)
        context = ToolContext("r", "call", "c", "a", "u", frozenset(), original["table_version"])
        before = remote.calls
        audits = TableAuditEvent.objects.filter(table=t).count()
        try:
            adapter(request, context)
        except ToolExecutionError as exc:
            assert exc.code == "TABLE_WRITE_CONFLICT"
        else:
            raise AssertionError("Stale clear erased an intervening write")
        assert remote.calls == before and remote.values(t.tid)[0]["value"] == "200"
        assert TableAuditEvent.objects.filter(table=t).count() == audits
        result = adapter(request, replace(context, expected_table_version=latest["table_version"]))
        assert result["cleared_cells"] == 1 and remote.values(t.tid)[0]["value"] == ""
        current = writer.upsert_table_cells.__globals__["_table_version"](t)
        assert current > latest["table_version"]
        before = remote.calls
        try:
            writer.upsert_table_cells(user, t, [{"r": 0, "c": 0, "value": 300}],
                expected_table_version=latest["table_version"])
        except Exception as exc:
            assert getattr(exc, "code", None) == "TABLE_WRITE_CONFLICT"
        else:
            raise AssertionError("Stale write ignored the clear revision")
        assert remote.calls == before
        remote.fault = "lost_ack"
        try:
            adapter(request, replace(context, expected_table_version=current))
        except UnconfirmedWriteError:
            pass
        else:
            raise AssertionError("Lost clear acknowledgement was treated as confirmed")
        assert remote.calls == before + 1
        return {"stale_clear_blocked_before_remote_and_audit": True,
                "fresh_clear_advances_revision": True, "write_rejects_pre_clear_revision": True,
                "clear_lost_ack_remains_unknown": True}

    record("clear_read_version_and_unknown", clear_version)
    record("cross_database_lost_ack", lost_ack)
    record("multi_cell_partial_failure", partial)
    record("persistent_unknown_guard", guarded)
    record("acknowledged_replay_authorization", replay)
    record("original_version_precondition", stale_version)
    record("storage_client_stops_unknown", hidden_client_retry)
    record("service_preserves_typed_unknown", typed_service_error)
    (output / "trials.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    summary = {"selected": len(rows), "checks_passed": sum(r["checks_passed"] for r in rows),
               "errors": sum(not r["checks_passed"] for r in rows), "not_a_full_platform_integration": True}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    connection.close()
    print(json.dumps(summary, indent=2))
    return int(summary["errors"] > 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output.resolve()))
