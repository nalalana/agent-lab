"""Fixed opt-in positive-receipt recovery contracts, separate from all old oracles."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from bootstrap import ROOT, setup
import run_realdb_integration as integration
from run_postgres_locks import config as pg_config
from run_realdb_legacy import evaluate
from run_realdb_safety import managed_read
from recovery_fixture import RecoveryRemote, bind_table, load_recovery_writer, prepare_scope, RECEIPT_TABLE
from source_identity import source_identity

OPERATIONS = {"raw": "raw_upsert", "cells": "cells_upsert", "clear": "clear", "delete": "delete_row"}
FAMILIES = {
    "normal_atomic_commit": tuple(OPERATIONS),
    "commit_ack_lost_reconcile": tuple(OPERATIONS),
    "commit_process_exit_reconcile": ("cells", "clear"),
    "reconcile_idempotency": ("new_run_replay", "concurrent_two_callers"),
    "pg_repair_rollback": (None,),
    "unattributed_evidence": ("absent_receipt", "legacy_fence", "matching_current_values"),
    "partial_receipt_rejected": (None,),
    "tampered_receipt_rejected": ("request_digest_mismatch", "operation_id_mismatch", "scope_mismatch"),
    "inflight_reconcile": (None,),
    "scope_rejected_without_rpc": (None,),
    "auth_rejected_without_rpc": (None,),
    "atomic_batch_rollback": (None,),
}
CASE_IDS = [family + (":" + sub if sub else "") for family, subs in FAMILIES.items() for sub in subs]


def attempt(call):
    from common.immudbIO.errors import UnconfirmedWriteError
    try:
        result = call()
        return {"committed": isinstance(result, dict) and result.get("status") == "committed",
                "unknown": False, "permission_denied": False, "result": result}
    except Exception as exc:
        return {"committed": False, "unknown": isinstance(exc, UnconfirmedWriteError)
                    and getattr(exc, "code", None) == "OPERATION_OUTCOME_UNKNOWN",
                "permission_denied": isinstance(exc, PermissionError), "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None), "operation_id": getattr(exc, "operation_id", None)}


def delta(after, before):
    return {key: after[key] - before[key] for key in before}


def child_run(config_path, schema, table_pk, kind):
    if not re.fullmatch(r"agent_lab_[0-9a-f]{32}", schema) or table_pk < 1 or kind not in {"cells", "clear"}:
        raise ValueError("Invalid child scope")
    pg, cfg = pg_config(config_path), integration.config(config_path)
    setup({"ENGINE": "django.db.backends.postgresql", "NAME": pg["dbname"], "USER": pg["user"],
        "PASSWORD": pg["password"], "HOST": pg["host"], "PORT": pg["port"], "OPTIONS": {
            "connect_timeout": 5, "sslmode": pg["sslmode"],
            "options": f"-c search_path={schema} -c lock_timeout=5000 -c statement_timeout=10000"}})
    from immudb import ImmudbClient
    from apps.opengate.models import GatesTable
    table = GatesTable.objects.select_related("gate__owner").get(pk=table_pk)
    if not table.gate.gid.startswith("test_"):
        raise ValueError("Child requires an existing test database")
    client = ImmudbClient(cfg["address"], timeout=5)
    client.login(cfg["user"], cfg["password"])
    remote = RecoveryRemote(client, table.gate.gid)
    remote.immudb_client.fault = "exit"
    writer = load_recovery_writer(remote)
    version = writer.upsert_table_cells.__globals__["_table_version"](table)
    writer.submit_recoverable_write(table.gate.owner, table, OPERATIONS[kind], expected_table_version=version,
        cells=[{"r": 0, "c": 0, **({"value": 100} if kind == "cells" else {})}])
    raise AssertionError("Child returned without a confirmed atomic commit exit")


def recovery_checks(output, user, gate, factory, make_remote, config_path):
    from django.db import connection, connections
    from apps.opengate.models import GatesTable, TableAuditEvent
    from common.immudbIO.client import toumingClient
    from run_table_boundary import load_writer

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(postgresql_server_version=connection.connection.get_parameter_status("server_version"))
    supported = all(callable(getattr(load_recovery_writer(SimpleNamespace()), name, None))
                    for name in ("submit_recoverable_write", "reconcile_committed_write"))
    manifest["recoverable_api_supported"] = supported
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    project = prepare_scope(user, gate)
    rows = []

    def table(label):
        item = GatesTable.objects.create(tid="t" + uuid4().hex, gate=gate, name=label,
            num_columns=2, created_at="1", updated_at="1")
        bind_table(item, project)
        old = factory(gate, item)  # The existing runner owns actual DB/table creation and cleanup.
        return item, old

    def new(label, seed=False):
        item, old = table(label)
        if seed:
            ok, _ = load_writer(old).upsert_table_cells(user, item, [{"r": 0, "c": 0, "value": 100}])
            if ok is not True:
                raise AssertionError("Seed write was not acknowledged")
        remote = make_remote(item)
        return item, old, remote, load_recovery_writer(remote)

    def state(writer, item):
        ns = writer.upsert_table_cells.__globals__
        return {"version": ns["_table_version"](item), "cell_versions": ns["_cell_versions"](item,
            [{"r": 0, "c": 0}, {"r": 0, "c": 1}]),
            "audits": list(TableAuditEvent.objects.filter(table=item).order_by("id").values(
                "id", "action", "result", "row_index", "column_index", "details")),
            "pending": TableAuditEvent.objects.filter(table=item, action="write.fence", result="pending").count()}

    def snapshot(writer, item):
        result = state(writer, item)
        result["cell_versions"] = {f"{r}:{c}": v for (r, c), v in result["cell_versions"].items()}
        return result

    def args(kind):
        return {"values": {"r": 0, "c": 0, "ctfa": "0.00", "ctt": "n", "cts": "100",
                           "bg": "#123456", "fs": 17, "bl": True}} if kind == "raw" else (
            {"row_index": 0} if kind == "delete" else {"cells": [{"r": 0, "c": 0,
                **({"value": 100} if kind == "cells" else {})}]})

    def submit(writer, item, kind="cells", **kwargs):
        return attempt(lambda: writer.submit_recoverable_write(user, item, OPERATIONS[kind],
            expected_table_version=state(writer, item)["version"], **(kwargs or args(kind))))

    def resolve(writer, item, opid):
        return attempt(lambda: writer.reconcile_committed_write(user, item, opid))

    def pending_id(item):
        return TableAuditEvent.objects.get(table=item, action="write.fence", result="pending").details["operation_id"]

    def verify_finish(facts, checks, item, remote, writer, kind, before, result):
        facts["after"] = snapshot(writer, item)
        facts["after_values"] = remote.values(item.tid)
        facts["managed_read"] = managed_read(writer, user, item)
        expected = [] if kind == "delete" else [{"r": 0, "c": 0, "value": "" if kind == "clear" else "100"}]
        opid = result.get("result", {}).get("operation_id")
        events = [event for event in facts["after"]["audits"]
                  if event["action"] != "write.fence" and event["details"].get("operation_id") == opid]
        action = {"raw": "data.upsert", "cells": "cell.upsert", "clear": "cell.clear", "delete": "row.delete"}[kind]
        fences = [e for e in facts["after"]["audits"] if e["action"] == "write.fence"
                  and e["details"].get("operation_id") == opid]
        checks.update(committed=result["committed"], no_pending=facts["after"]["pending"] == 0,
            real_values=facts["after_values"] == expected, managed_read_has_version=facts["managed_read"]["has_version"],
            version_advanced=facts["after"]["version"] > before["version"],
            original_action_repaired=len(events) == 1 and events[0]["action"] == action and events[0]["result"] == "success",
            receipt_transaction_recorded=len(fences) == 1 and type(fences[0]["details"].get("receipt_tx")) is int
                and fences[0]["details"]["receipt_tx"] > 0,
            cell_revision_advanced=facts["after"]["cell_versions"]["0:0"] > before["cell_versions"]["0:0"])
        if kind == "raw":
            remote.immudb_client.useDatabase(gate.gid.encode())
            facts["sql_metadata"] = [list(r) for r in toumingClient(remote.immudb_client).simpleSelect(
                item.tid, ["ctfa", "ctt", "cts", "bg", "fs", "bl"], {})]
            checks["raw_metadata_preserved"] = facts["sql_metadata"] == [["0.00", "n", "100", "#123456", 17, True]]

    def committed(facts, checks, kind, lost=False, crash=False):
        item, old, remote, writer = new(kind, kind in {"clear", "delete"})
        facts["before"] = snapshot(writer, item)
        if crash:
            command = [sys.executable, str(Path(__file__).resolve()), "--child", "--config", str(config_path.resolve()),
                "--schema", manifest["schema"], "--table-pk", str(item.pk), "--kind", kind]
            try:
                child = subprocess.run(command, cwd=ROOT / "lab", capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                facts["child"] = {"exit_code": None, "expected_exit_code": 73, "timed_out": True}
                checks["actual_committed_exit"] = False
                return
            facts["child"] = {"exit_code": child.returncode, "expected_exit_code": 73, "timed_out": False}
            checks["actual_committed_exit"] = child.returncode == 73
            (output / (kind + "-child-stdout.log")).write_bytes(child.stdout)
            (output / (kind + "-child-stderr.log")).write_bytes(child.stderr)
        else:
            remote.immudb_client.fault = "ack_lost" if lost else None
            facts["submit"] = submit(writer, item, kind)
            facts["confirmed_transactions"] = list(remote.immudb_client.confirmed_transactions)
            checks["one_real_atomic_commit"] = len(facts["confirmed_transactions"]) == 1
        if lost or crash:
            facts["pending_state"] = snapshot(writer, item)
            facts["pending_read"] = managed_read(writer, user, item)
            checks.update(pending_preserved=facts["pending_state"]["pending"] == 1,
                business_audit_rolled_back=facts["pending_state"]["version"] == facts["before"]["version"],
                pending_read_no_version=not facts["pending_read"]["has_version"])
            if lost:
                checks["typed_unknown"] = facts["submit"]["unknown"]
            opid = pending_id(item)
            fresh = make_remote(item)
            writer = load_recovery_writer(fresh)
            counters = fresh.immudb_client.counters()
            facts["reconcile"] = resolve(writer, item, opid)
            facts["reconcile_calls"] = delta(fresh.immudb_client.counters(), counters)
            checks["reconcile_no_business_dispatch"] = facts["reconcile_calls"]["business_mutation_calls"] == 0
            checks["receipt_was_read"] = facts["reconcile_calls"]["receipt_reads"] == 1
            checks["reconciled_flag"] = facts["reconcile"].get("result", {}).get("reconciled") is True
            result = facts["reconcile"]
        else:
            result = facts["submit"]
            checks["normal_not_reconciled"] = result.get("result", {}).get("reconciled") is False
        verify_finish(facts, checks, item, remote, writer, kind, facts["before"], result)

    def lose(label="unknown", fault="ack_lost", *, seed=False, two_cells=False):
        item, old, remote, writer = new(label, seed)
        before = snapshot(writer, item)
        remote.immudb_client.fault = fault
        result = submit(writer, item, cells=[{"r": 0, "c": 0, "value": 100}, {"r": 0, "c": 1, "value": 200}]) if two_cells else submit(writer, item)
        return item, old, remote, writer, before, result

    def no_evidence(facts, checks, sub):
        if sub == "legacy_fence":
            item, old = table("legacy_unknown")
            old.rpc.armed = "UPSERT INTO "
            writer = load_writer(old)
            facts["legacy_submit"] = attempt(lambda: writer.upsert_table_cells(user, item, [{"r": 0, "c": 0, "value": 100}]))
            remote = make_remote(item) if supported else None
            writer = load_recovery_writer(remote) if supported else writer
            opid = uuid4().hex
            facts["legacy_actual_commit_ack_dropped"] = old.rpc.committed_then_lost
            checks["legacy_actual_unknown_commit"] = old.rpc.committed_then_lost == 1 and facts["legacy_submit"]["unknown"]
            before = snapshot(writer, item)
        else:
            item, old, remote, writer, before, initial = lose(sub, "before_dispatch", seed=sub == "matching_current_values")
            facts["submit"] = initial
            opid = initial.get("operation_id")
            checks["typed_initial_unknown"] = initial["unknown"]
        facts["before"] = before
        if not supported:
            return
        facts["values_before"] = remote.values(item.tid)
        counters = remote.immudb_client.counters()
        facts["reconcile"] = resolve(writer, item, opid)
        facts["calls"] = delta(remote.immudb_client.counters(), counters)
        facts["after"] = snapshot(writer, item)
        facts["values_after"] = remote.values(item.tid)
        checks.update(no_positive_claim=facts["reconcile"]["unknown"], pending_remains=facts["after"]["pending"] == 1,
            zero_business_dispatch=facts["calls"]["business_mutation_calls"] == 0,
            no_version_repair=facts["after"]["version"] == before["version"], values_unchanged=facts["values_before"] == facts["values_after"])
        if sub == "legacy_fence":
            checks["legacy_zero_receipt_reads"] = facts["calls"]["receipt_reads"] == 0
        if sub == "matching_current_values":
            checks["values_already_match_request"] = facts["values_before"] == [{"r": 0, "c": 0, "value": "100"}]

    def rejected_proof(facts, checks, fault):
        item, old, remote, writer, before, initial = lose(fault, fault, two_cells=fault == "partial")
        facts.update(submit=initial, before=before, confirmed_transactions=remote.immudb_client.confirmed_transactions,
            injected_fixture=remote.immudb_client.last_injection, values_before=remote.values(item.tid))
        counters = remote.immudb_client.counters()
        facts["reconcile"] = resolve(writer, item, initial.get("operation_id"))
        facts["calls"] = delta(remote.immudb_client.counters(), counters)
        facts["after"] = snapshot(writer, item)
        checks.update(initial_unknown=initial["unknown"], real_invalid_receipt_commit=len(facts["confirmed_transactions"]) == 1,
            rejected=facts["reconcile"]["unknown"], pending_remains=facts["after"]["pending"] == 1,
            zero_business_dispatch=facts["calls"]["business_mutation_calls"] == 0,
            no_audit_version_repair=facts["after"]["version"] == before["version"],
            values_unchanged=remote.values(item.tid) == facts["values_before"])
        if fault == "partial":
            checks["actual_partial_fixture"] = facts["values_before"] == [{"r": 0, "c": 0, "value": "100"}]

    def repair_rollback(facts, checks):
        item, old, remote, writer, before, initial = lose()
        opid = initial.get("operation_id")
        ns = writer.submit_recoverable_write.__globals__
        original = ns["_finish_table_audit"]
        def fail(event, success):
            if event.action == "write.fence":
                raise RuntimeError("Injected PG failure after business audit repair")
            return original(event, success)
        counters = remote.immudb_client.counters()
        ns["_finish_table_audit"] = fail
        try:
            facts["failed_repair"] = resolve(writer, item, opid)
        finally:
            ns["_finish_table_audit"] = original
        facts["after_rollback"] = snapshot(writer, item)
        checks.update(repair_unknown=facts["failed_repair"]["unknown"], pending_survives=facts["after_rollback"]["pending"] == 1,
            business_audits_rolled_back=len(facts["after_rollback"]["audits"]) == len(before["audits"]) + 1,
            version_rolled_back=facts["after_rollback"]["version"] == before["version"])
        facts["retry_repair"] = resolve(writer, item, opid)
        facts["calls"] = delta(remote.immudb_client.counters(), counters)
        checks.update(retry_reconciled=facts["retry_repair"]["committed"], no_business_retry=facts["calls"]["business_mutation_calls"] == 0)
        verify_finish(facts, checks, item, remote, writer, "cells", before, facts["retry_repair"])

    def wait_lock(name, future):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock')", [name])
                if cursor.fetchone()[0] and not future.done():
                    return True
            if future.done():
                return False
            time.sleep(0.02)
        return False

    def invoke(writer, item, name, *, opid=None):
        try:
            with connections["default"].cursor() as cursor:
                cursor.execute("SET application_name=%s", [name])
            return resolve(writer, item, opid) if opid else submit(writer, item)
        finally:
            connections.close_all()

    def concurrency(facts, checks, inflight=False):
        if inflight:
            item, old, first_remote, first_writer = new("inflight")
            first_remote.immudb_client.fault = "pause"
        else:
            item, old, original_remote, original_writer, before, initial = lose()
            first_remote = make_remote(item)
            first_writer = load_recovery_writer(first_remote)
        second_remote = make_remote(item)
        second_writer = load_recovery_writer(second_remote)
        entered, release = threading.Event(), threading.Event()
        if inflight:
            first_remote.immudb_client.entered, first_remote.immudb_client.release = entered, release
        else:
            first_remote.immudb_client.proof_entered, first_remote.immudb_client.proof_release = entered, release
        waiter_name = "recover_wait_" + uuid4().hex
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke, first_writer, item, "recover_first_" + uuid4().hex,
                opid=None if inflight else initial.get("operation_id"))
            try:
                if not entered.wait(3):
                    raise AssertionError("First operation did not reach its controlled RPC boundary")
                opid = pending_id(item)
                second = pool.submit(invoke, second_writer, item, waiter_name, opid=opid)
                facts["postgres_lock_wait_observed"] = wait_lock(waiter_name, second)
                facts["while_inflight"] = {"pending": state(first_writer, item)["pending"],
                    "second_calls": second_remote.immudb_client.counters(), "second_done": second.done()}
            finally:
                release.set()
            facts["first"], facts["second"] = first.result(timeout=8), second.result(timeout=8)
        facts["after"] = snapshot(first_writer, item)
        facts["first_calls"], facts["second_calls"] = first_remote.immudb_client.counters(), second_remote.immudb_client.counters()
        checks.update(actual_pg_lock_wait=facts["postgres_lock_wait_observed"],
            did_not_unlock_while_inflight=facts["while_inflight"]["pending"] == 1 and not facts["while_inflight"]["second_done"],
            waiting_reconcile_zero_rpc=facts["while_inflight"]["second_calls"]["total_rpc"] == 0,
            waiting_reconcile_zero_constructors=facts["while_inflight"]["second_calls"]["service_constructions"] == 0,
            both_committed=facts["first"]["committed"] and facts["second"]["committed"],
            same_result=facts["first"].get("result", {}).get("table_version") == facts["second"].get("result", {}).get("table_version"),
            exactly_once_audit=len([e for e in facts["after"]["audits"] if e["action"] == "cell.upsert"]) == 1,
            second_no_rpc=facts["second_calls"]["total_rpc"] == 0,
            recovery_no_business_dispatch=facts["second_calls"]["business_mutation_calls"] == 0
                and (inflight or facts["first_calls"]["business_mutation_calls"] == 0))

    def replay(facts, checks):
        item, old, remote, writer, before, initial = lose()
        opid = initial.get("operation_id")
        facts["first"] = resolve(writer, item, opid)
        facts["after_first"] = snapshot(writer, item)
        fresh = make_remote(item)
        facts["second"] = resolve(load_recovery_writer(fresh), item, opid)
        facts["after_second"] = snapshot(writer, item)
        facts["second_calls"] = fresh.immudb_client.counters()
        checks.update(first_repaired=facts["first"]["committed"], second_replayed=facts["second"]["committed"],
            same_receipt_result=facts["first"].get("result") == facts["second"].get("result"),
            no_duplicate_audits=facts["after_first"] == facts["after_second"], no_new_rpc=facts["second_calls"]["total_rpc"] == 0)

    def access(facts, checks, denied=False):
        item, old, remote, writer, before, initial = lose()
        if denied:
            writer = load_recovery_writer(remote, admin=False)
            target = item
        else:
            target, unused_old = table("wrong_scope")
        counters = remote.immudb_client.counters()
        facts["attempt"] = resolve(writer, target, initial.get("operation_id"))
        facts["calls"] = delta(remote.immudb_client.counters(), counters)
        facts["after"] = snapshot(writer, item)
        checks.update(correct_rejection=facts["attempt"]["permission_denied"] if denied else facts["attempt"]["unknown"],
            zero_all_rpc=facts["calls"]["total_rpc"] == 0, original_pending=facts["after"]["pending"] == 1,
            zero_service_constructors=facts["calls"]["service_constructions"] == 0,
            original_version_unchanged=facts["after"]["version"] == before["version"])

    def aborted(facts, checks):
        item, old, remote, writer, before, initial = lose("abort", "abort", two_cells=True)
        facts.update(submit=initial, injection=remote.immudb_client.last_injection,
            confirmed_transactions=remote.immudb_client.confirmed_transactions, values=remote.values(item.tid))
        counters = remote.immudb_client.counters()
        facts["reconcile"] = resolve(writer, item, initial.get("operation_id"))
        facts["calls"] = delta(remote.immudb_client.counters(), counters)
        facts["after"] = snapshot(writer, item)
        checks.update(initial_unknown=initial["unknown"], actual_atomic_abort=facts["injection"] == "abort"
                and len(facts["confirmed_transactions"]) == 0,
            all_business_cells_rolled_back=facts["values"] == [], no_positive_receipt=facts["reconcile"]["unknown"],
            pending_remains=facts["after"]["pending"] == 1, version_unchanged=facts["after"]["version"] == before["version"],
            no_business_redispatch=facts["calls"]["business_mutation_calls"] == 0)

    if not supported:
        facts, checks = {}, {}
        try:
            no_evidence(facts, checks, "legacy_fence")
        except Exception as exc:
            facts["scenario_error_type"] = type(exc).__name__
        (output / "baseline-legacy-unknown.json").write_text(json.dumps({"facts": facts, "checks": checks,
            "recoverable_api_supported": False, "not_counted_as_recovery_pass": True}, indent=2), encoding="utf-8")
    for family, subs in FAMILIES.items():
        for sub in subs:
            name = family + (":" + sub if sub else "")
            (output / "active-case.json").write_text(json.dumps({"case": name}), encoding="utf-8")
            if not supported:
                row = {"case": name, "family": family, "status": "unsupported", "passed": False,
                    "facts": {"recoverable_api_supported": False}, "checks": {"recoverable_api_supported": False}}
            else:
                def check(facts, checks):
                    if family == "normal_atomic_commit": committed(facts, checks, sub)
                    elif family == "commit_ack_lost_reconcile": committed(facts, checks, sub, lost=True)
                    elif family == "commit_process_exit_reconcile": committed(facts, checks, sub, crash=True)
                    elif family == "reconcile_idempotency": replay(facts, checks) if sub == "new_run_replay" else concurrency(facts, checks)
                    elif family == "pg_repair_rollback": repair_rollback(facts, checks)
                    elif family == "unattributed_evidence": no_evidence(facts, checks, sub)
                    elif family == "partial_receipt_rejected": rejected_proof(facts, checks, "partial")
                    elif family == "tampered_receipt_rejected": rejected_proof(facts, checks, sub)
                    elif family == "inflight_reconcile": concurrency(facts, checks, inflight=True)
                    elif family == "scope_rejected_without_rpc": access(facts, checks)
                    elif family == "auth_rejected_without_rpc": access(facts, checks, denied=True)
                    elif family == "atomic_batch_rollback": aborted(facts, checks)
                row = evaluate(name, check)
                row.update(family=family, status="passed" if row["passed"] else "failed")
            rows.append(row)
            temporary = output / "recovery-trials.tmp"
            temporary.write_text(json.dumps(rows, indent=2), encoding="utf-8")
            temporary.replace(output / "recovery-trials.json")
    return rows


def run(output, config_path):
    paths = ("backend/apps/opengate/services.py", "backend/common/immudbIO/recoverable.py", "lab/recovery_fixture.py")
    manifest = {"kind": "opt_in_positive_receipt_recovery_oracle", "oracle_version": 1,
        "case_ids": CASE_IDS, "family_ids": list(FAMILIES), "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "identity": source_identity(ROOT), "recovery_source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in paths if (ROOT / p).exists()}, "lab_sqlite_unknown_guard_enabled": False,
        "substitutions": ["source-extracted production methods; constructor replaced", "IAM write/admin and moderation allow fixtures",
            "real Tenant/Resource scope rows", "explicit SDK-boundary fault/negative-proof fixtures"],
        "counter_scope": "SDK call entries; business_mutation_calls counts SQL mutation dispatches, not rows",
        "measurement_scope": "operation deltas; fixture login/receipt DDL excluded; service constructors counted inside extracted calls",
        "not_tested": ["full HTTP/IAM/runtime/model integration", "legacy-path automatic recovery", "hostile privileged database administrator"]}
    clients = []
    def extra(folder, user, gate, factory):
        cfg = integration.config(config_path)
        def make_remote(table):
            from immudb import ImmudbClient
            from common.immudbIO.recoverable import ensure_receipt_table
            client = ImmudbClient(cfg["address"], timeout=5)
            client.login(cfg["user"], cfg["password"])
            clients.append(client)
            remote = RecoveryRemote(client, gate.gid)
            ensure_receipt_table(remote.immudb_client, db_name=gate.gid)
            # Provisioning/login are outside the measured API operation.
            remote.immudb_client.total_rpc = 0
            return remote
        try:
            return recovery_checks(folder, user, gate, factory, make_remote, config_path)
        finally:
            for client in clients:
                client.channel.close()
    with contextlib.redirect_stdout(io.StringIO()):
        code = integration.run(output, config_path, extra_checks=extra, manifest_updates=manifest)
    summary_path, trials_path = output / "summary.json", output / "recovery-trials.json"
    summary = json.loads(summary_path.read_text())
    rows = json.loads(trials_path.read_text()) if trials_path.exists() else []
    failures = [{"case": row["case"], "check": key} for row in rows for key, passed in row["checks"].items() if not passed]
    unsupported = sum(row.get("status") == "unsupported" for row in rows)
    summary.update(kind=manifest["kind"], recovery_cases_expected=len(CASE_IDS), recovery_cases_completed=len(rows),
        recovery_cases_passed=sum(row["passed"] for row in rows), recovery_cases_unsupported=unsupported,
        recovery_failed_checks=failures, recovery_families_expected=len(FAMILIES),
        recovery_families_passed=sum(all(r["passed"] for r in rows if r["family"] == family)
            and sum(r["family"] == family for r in rows) == len(subs) for family, subs in FAMILIES.items()), model_calls=0)
    summary["diagnostic_rows_completed"] = summary.get("checks_passed", 0)
    summary["checks_passed"] = int(summary.get("postgresql_lock_behavior_tested", False)) * 2 + summary["recovery_cases_passed"]
    if summary.get("status") == "passed":
        if unsupported:
            summary["status"], code = "unsupported", 2
        elif failures or [r["case"] for r in rows] != CASE_IDS:
            summary["status"], code = "failed", 1
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--schema")
    parser.add_argument("--table-pk", type=int)
    parser.add_argument("--kind", choices=("cells", "clear"))
    arguments = parser.parse_args()
    if arguments.child:
        child_run(arguments.config, arguments.schema, arguments.table_pk, arguments.kind)
    elif arguments.output is None:
        parser.error("--output is required")
    else:
        raise SystemExit(run(arguments.output.resolve(), arguments.config))
