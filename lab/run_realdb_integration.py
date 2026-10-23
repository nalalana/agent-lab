"""Local PostgreSQL + immudb diagnostic using production service method bodies.

No production settings, IAM/moderation integration, model calls, or alternate
agent loop. The persistent unknown journal remains a lab-only SQLite guard.
"""
import argparse
import ast
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
from importlib.metadata import version as package_version
from pathlib import Path
from uuid import uuid4

from bootstrap import ROOT
from run_postgres_locks import config as pg_config, require, run as run_locks

SERVICE_SOURCE = ROOT / "backend/common/immudbIO/services.py"
CLIENT_SOURCE = ROOT / "backend/common/immudbIO/client.py"


def config(path):
    if path is None:
        raise ValueError("An explicit local JSON configuration is required")
    values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for key in ("AGENT_LAB_PG_ENABLED", "AGENT_LAB_IMMUDB_ENABLED"):
        if values.get(key) != "1":
            raise ValueError("Both local test databases must be explicitly enabled")
    for prefix in ("PG", "IMMUDB"):
        for suffix in ("HOST", "PORT", "USER", "PASSWORD"):
            if not values.get(f"AGENT_LAB_{prefix}_{suffix}"):
                raise ValueError(f"Missing explicit AGENT_LAB_{prefix}_{suffix}")
        if values[f"AGENT_LAB_{prefix}_HOST"] not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Real database diagnostics require loopback hosts")
        port = int(values[f"AGENT_LAB_{prefix}_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid local {prefix} port")
    if not values.get("AGENT_LAB_PG_DATABASE"):
        raise ValueError("An explicit PostgreSQL test database is required")
    pg_config(path)  # Keep the existing PostgreSQL TLS and test database checks.
    host = values["AGENT_LAB_IMMUDB_HOST"]
    if host == "::1":
        host = "[::1]"
    return {"address": f"{host}:{values['AGENT_LAB_IMMUDB_PORT']}",
            "user": values["AGENT_LAB_IMMUDB_USER"],
            "password": values["AGENT_LAB_IMMUDB_PASSWORD"]}


def load_service(client):
    """Extract original methods without importing properties or their constructor."""
    from common.immudbIO.client import toumingClient
    from common.immudbIO.models import Worksheet, Cell, CellCttChoices
    tree = ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ImmudbService")
    methods = {"create_db", "delete_db", "create_table", "get_data", "upsert_data", "clear_cells"}
    cls.body = [n for n in cls.body if getattr(n, "name", None) in methods]
    namespace = {"hashlib": hashlib, "Worksheet": Worksheet, "Cell": Cell,
                 "CellCttChoices": CellCttChoices, "toumingClient": toumingClient}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(SERVICE_SOURCE), "exec"), namespace)
    service = namespace["ImmudbService"]()
    service.immudb_client = client
    return service


class CommitThenLoseAcknowledgement:
    """Fault at the SDK response boundary, after the actual SQL commit."""
    def __init__(self, client):
        self.client, self.armed, self.committed_then_lost = client, None, 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def sqlExec(self, query, *args, **kwargs):
        response = self.client.sqlExec(query, *args, **kwargs)
        if self.armed and query.lstrip().upper().startswith(self.armed):
            self.armed = None
            require(bool(getattr(response, "txs", None)) and not getattr(response, "ongoingTx", False),
                    "Fault injection requires an acknowledged real transaction first")
            self.committed_then_lost += 1
            raise TimeoutError("Injected response loss after confirmed local immudb commit")
        return response


class RealRemote:
    def __init__(self, client, gid, tid):
        self.gid, self.calls = gid, 0
        self.rpc = CommitThenLoseAcknowledgement(client)
        self.service = load_service(self.rpc)
        require(self.service.create_table(gid, tid, 2)[0] is True, "Local immudb table creation failed")

    def upsert_data(self, *args):
        self.calls += 1
        return self.service.upsert_data(*args)

    def clear_cells(self, *args):
        self.calls += 1
        return self.service.clear_cells(*args)

    def get_data(self, *args):
        return self.service.get_data(*args)

    def values(self, tid):
        from common.immudbIO.client import toumingClient
        self.rpc.useDatabase(self.gid.encode("utf-8"))
        rows = toumingClient(self.rpc).simpleSelect(tid, ["r", "c", "cts"], {})
        require(isinstance(rows, list), "Local immudb read did not return rows")
        return sorted(({"r": r[0], "c": r[1], "value": r[2]} for r in rows), key=lambda r: (r["r"], r["c"]))


def additional_checks(output, user, gate, factory):
    from apps.opengate.models import GatesTable, TableAuditEvent
    from apps.agent_runtime.contracts import ToolContext, ToolRequest
    from common.immudbIO.errors import UnconfirmedWriteError
    from legacy_write_guard import LegacyWriteGuard, OutcomeUnknown
    from run_table_boundary import load_writer
    from test_table_versions import project_adapter

    rows = []
    def record(row):
        rows.append(row)
        (output / "realdb-trials.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    for case in ("normal_write_clear", "write_ack_lost", "clear_ack_lost", "partial_validation_failure"):
        (output / "active-case.json").write_text(json.dumps({"case": case}), encoding="utf-8")
        table = GatesTable.objects.create(tid="t" + uuid4().hex, gate=gate, name=case,
            num_columns=2, created_at="1", updated_at="1")
        remote = factory(gate, table)
        writer = load_writer(remote)
        version = lambda: writer.upsert_table_cells.__globals__["_table_version"](table)
        cells = [{"r": 0, "c": 0, "value": 100}]
        if case in {"normal_write_clear", "clear_ack_lost"}:
            ok, receipt = writer.upsert_table_cells(user, table, cells, expected_table_version=0)
            require(ok is True, "Initial real write failed")
            before = receipt["table_version"]
        else:
            before = 0
        if case == "normal_write_clear":
            ok, _ = writer.clear_table_cells(user, table, [{"r": 0, "c": 0}], expected_table_version=before)
            require(ok is True and remote.values(table.tid)[0]["value"] == "", "Real clear failed")
            require(version() > before and remote.calls == 2, "Normal clear lost its audit revision")
            record({"case": case, "remote_calls": remote.calls, "acknowledged": True})
            continue
        tool = "company.table.clear_cells" if case == "clear_ack_lost" else "company.table.write"
        if case == "partial_validation_failure":
            # Real serializer rejects blank cts after the first real cell commit.
            cells.append({"r": 0, "c": 1, "value": ""})
        else:
            remote.rpc.armed = "UPDATE " if case == "clear_ack_lost" else "UPSERT INTO "
        arguments = {"tid": table.tid, "cells": cells}
        adapter = project_adapter(writer, table, writer.upsert_table_cells.__globals__["TableWriteConflict"], user)
        request = ToolRequest(tool, arguments, 5)
        context = ToolContext("realdb", case, "fixture", "fixture", user.uid, frozenset(), before)
        guard_path = output / (case + "-unknown.sqlite")
        guard = LegacyWriteGuard(guard_path, scope="local:fixture:" + table.tid)
        try:
            guard.invoke("edit", tool, arguments, authorize=lambda: True,
                         execute=lambda: (True, adapter(request, context)))
        except OutcomeUnknown as exc:
            require(isinstance(exc.__cause__, UnconfirmedWriteError), "Production adapter did not preserve unknown")
        else:
            raise AssertionError("Unconfirmed real mutation was acknowledged")
        observed = remote.values(table.tid)
        expected = "" if case == "clear_ack_lost" else "100"
        require(observed == [{"r": 0, "c": 0, "value": expected}], "Expected real partial/committed data missing")
        audits = list(TableAuditEvent.objects.filter(table=table).order_by("id").values_list("result", flat=True))
        if case == "write_ack_lost":
            require(audits == [] and version() == before, "Lost write acknowledgement did not roll back PG audit")
        elif case == "clear_ack_lost":
            require(audits == ["success", "failed"] and version() == before, "Unexpected clear failure audit contract")
        else:
            require(audits == ["success", "failed"] and version() > before, "Partial validation failure audit contract changed")
        calls = remote.calls
        for intent in ("edit", "new-intent"):
            try:
                LegacyWriteGuard(guard_path, scope=guard.scope).invoke(intent, tool, arguments,
                    authorize=lambda: True, execute=lambda: (True, adapter(request, context)))
            except OutcomeUnknown:
                pass
            else:
                raise AssertionError("Persistent unknown guard allowed another mutation")
        require(remote.calls == calls, "Unknown guard retried the remote write")
        record({"case": case, "production_adapter_outcome": "unknown", "remote_values": observed,
            "remote_calls": calls, "audit_results": audits, "version_before": before, "version_after": version(),
            "real_commit_ack_dropped": remote.rpc.committed_then_lost,
            "pg_version_unchanged_despite_remote_change": version() == before,
            "lab_persistent_guard_blocks_retry_and_new_intent": True,
            "cross_database_atomicity": False})
    return rows


def run(output, config_path=None, *, extra_checks=None, manifest_updates=None):
    if output.exists():
        raise FileExistsError("Diagnostic output must be new")
    client, service, databases = None, None, []
    code = 2
    summary = {"status": "unavailable", "checks_passed": 0, "model_calls": 0, "real_immudb": False,
               "postgresql_lock_behavior_tested": False}
    try:
        cfg = config(config_path)
        from immudb import ImmudbClient
        client = ImmudbClient(cfg["address"], timeout=5)
        client.login(cfg["user"], cfg["password"])
        server_version = client.health().version
        def factory(gate, table):
            nonlocal service
            if service is None:
                service = load_service(client)
                databases.append(gate.gid)
                require(service.create_db(gate.gid)[0] is True, "Local immudb database creation failed")
            return RealRemote(client, gate.gid, table.tid)
        manifest = {"kind": "local_real_postgresql_immudb_service_diagnostic", "real_immudb": True,
            "time": datetime.now(timezone.utc).isoformat(), "model_calls": 0,
            "immudb_server_version": server_version, "immudb_python_sdk_version": package_version("immudb-py"),
            "substitutions": ["source-extracted production methods (constructor replaced)",
                "IAM allow fixture", "moderation allow fixture", "lab-only SQLite unknown journal",
                "SDK acknowledgement fault after actual commit"],
            "not_tested": ["full platform runtime/SSE/recorder", "production IAM/moderation", "production durable unknown reconciliation"],
            "shared_sdk_scope": "one test database; competing RPCs serialized by the table row lock; reads after thread join",
            "immudb_service_sha256": hashlib.sha256(SERVICE_SOURCE.read_bytes()).hexdigest(),
            "immudb_client_sha256": hashlib.sha256(CLIENT_SOURCE.read_bytes()).hexdigest()}
        manifest.update(manifest_updates or {})
        # Existing read retry logging is suppressed; database exceptions may expose
        # connection details. Only sanitized summaries and controlled trial data persist.
        with contextlib.redirect_stdout(io.StringIO()):
            code = run_locks(output, config_path, remote_factory=factory,
                extra_checks=lambda user, gate: (extra_checks or additional_checks)(output, user, gate, factory),
                manifest_updates=manifest)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    except Exception as exc:
        summary.update(error_type=type(exc).__name__)
        if databases:
            summary["status"] = "failed"
            code = 1
    finally:
        if databases:
            summary["immudb_test_databases"] = databases
            removed = True
            for database in databases:
                try:
                    client.useDatabase(b"defaultdb")
                    removed = service.delete_db(database)[0] is True and removed
                except Exception:
                    removed = False
            summary["immudb_databases_removed"] = removed
            if not removed:
                summary["status"], code = "failed", 1
        if client is not None:
            client.channel.close()
        output.mkdir(parents=True, exist_ok=True)
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="Explicit ignored local JSON configuration")
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve(), args.config))
