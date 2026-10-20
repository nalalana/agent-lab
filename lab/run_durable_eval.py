"""Real child-process crash checks; optional real-model recovery via Phase2 graph."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from bootstrap import ROOT, setup
from durable_counter import DurableCounter


def child(directory, action, *, amount=1, crash_at=None, scope="lab-company:lab-user", intent="increment-once"):
    command = [sys.executable, str(ROOT / "lab/durable_counter.py"), "--directory", str(directory),
               "--scope", scope, "--intent", intent, "--amount", str(amount), "--action", action]
    if crash_at:
        command += ["--crash-at", crash_at]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if crash_at:
        assert result.returncode == {"before_dispatch": 70, "before_commit": 71, "after_commit": 72}[crash_at], result.stderr
        return {"crash_exit": result.returncode}
    if result.returncode:
        raise RuntimeError(result.stderr[-500:])
    return json.loads(result.stdout)


def crash_check(directory, crash_at, amount=1):
    prepared = child(directory, "prepare", amount=amount)
    operation_id = prepared["result"]["operation_id"]
    crash = child(directory, "dispatch", amount=amount, crash_at=crash_at)
    fresh = DurableCounter(directory, scope="lab-company:lab-user")
    before = fresh.snapshot(operation_id)
    assert before["journal_state"] == "unknown"
    try:
        fresh.dispatch(operation_id)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Unconfirmed operation was dispatched without reconciliation")
    # This query runs in another process and uses the exact operation ID ledger.
    reconciled = child(directory, "reconcile", amount=amount)
    expected = ("rejected" if amount > 5 else "committed") if crash_at == "after_commit" else "unknown"
    assert reconciled["result"]["status"] == expected
    assert child(directory, "prepare", amount=amount)["result"]["operation_id"] == operation_id
    if expected == "unknown":
        assert reconciled["journal_state"] == "unknown"  # Missing ledger is not proof of no effect.
    retried = child(directory, "retry", amount=amount)
    repeated = child(directory, "retry", amount=amount)
    assert retried["result"] == repeated["result"]
    assert repeated["counter_value"] == (0 if amount > 5 else amount)
    assert repeated["effect_records"] == 1
    return {"crash_at": crash_at, "amount": amount, "operation_id": operation_id,
            "crash": crash, "before_reconciliation": before, "reconciled": reconciled, "after_retry": repeated}


def boundary_checks(directory):
    counter = DurableCounter(directory, scope="lab-company:lab-user")
    op = counter.prepare("shared-intent")
    try:
        counter.prepare("shared-intent", 2)
    except ValueError:
        pass
    else:
        raise AssertionError("Changed payload reused a logical intent")
    other = DurableCounter(directory, scope="other-company:other-user")
    try:
        other.status(op)
    except PermissionError:
        pass
    else:
        raise AssertionError("Cross-scope operation status was readable")
    # Concurrent restart attempts all use the original trusted intent/key.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: child(directory, "retry", intent="shared-intent"), range(8)))
    assert len({r["result"]["operation_id"] for r in results}) == 1
    assert counter.snapshot(op) == {"journal_state": "committed", "counter_value": 1, "effect_records": 1}
    counter._apply(op, 1)  # A late original request still reuses the same backend key.
    assert counter.snapshot(op)["counter_value"] == 1
    second = counter.prepare("different-authorized-intent", 2)
    counter.dispatch(second)
    assert counter.snapshot(op)["counter_value"] == 3
    assert counter.dispatch(op)["counter_value_after_this_operation"] == 1
    # A reused business key cannot change payload, even if bypassing the journal API.
    try:
        counter._apply(op, 2)
    except ValueError:
        pass
    else:
        raise AssertionError("Backend accepted a conflicting key payload")
    assert counter.snapshot(op)["counter_value"] == 3
    return {"concurrent_attempts": 8, "first_operation_commits": 1, "final_counter": 3,
            "changed_payload_rejected": True, "cross_scope_rejected": True, "historical_result_preserved": True}


async def model_recovery(directory, crash_at, config):
    from tau_graph_worker import Engine
    child(directory, "dispatch", crash_at=crash_at)
    counter = DurableCounter(directory, scope="lab-company:lab-user")
    op = counter.prepare("increment-once")
    before = counter.snapshot(op)
    # Restarted recovery exposes only an authorized operation-specific status query.
    tool = {"type": "function", "function": {"name": "get_operation_status",
        "description": "Query the authoritative ledger for this task's earlier operation. Missing records leave status unknown.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    engine = Engine({"tools": [tool], "read_names": ["get_operation_status"], "carry_read_evidence": True,
        "stop_on_unknown_operation": True, "trace_dir": str(directory / "graph_traces"),
        "policy": "This is an authorized recovery task for an earlier counter increment. Tool scope and operation ID are fixed by the runtime, not chosen by the model. Query that operation only. Committed means this operation committed; unknown does not prove failure or absence of effects. Do not report a historical counter value as necessarily current. Do not initiate or retry a mutation."}, config)
    reply = await engine.step({"kind": "user", "content": "Recover the earlier operation after restart. Query its exact status and explain what is known, without writing anything."})
    queries = 0
    while reply["kind"] == "tool":
        assert reply["name"] == "get_operation_status"
        queries += 1
        if queries > 8:
            raise RuntimeError("Recovery query budget exhausted")
        receipt = counter.reconcile(op)
        reply = await engine.step({"kind": "tool", "id": reply["id"], "error": False,
            "execution_status": "executed", "content": json.dumps(receipt)})
    assert queries >= 1, "Recovery answered without querying the operation"
    after = counter.snapshot(op)
    assert before["counter_value"] == after["counter_value"]
    assert before["effect_records"] == after["effect_records"]
    return {"operation_id": op, "before": before, "after": after, "queries": queries, "reply": reply,
            "no_business_mutation_during_recovery": True, "graph_completed": reply["kind"] == "answer", "model_calls": len(engine.traced.calls)}


async def run(output, real_model):
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"kind": "controlled_durable_counter_recovery", "time": datetime.now(timezone.utc).isoformat(),
        "real_model_recovery": real_model, "python": sys.version, "sqlite": sqlite3.sqlite_version,
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))}
    config = None
    if real_model:
        setup()
        from run_eval import model_config
        config = model_config()
        manifest["model"] = config["MODEL"]
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []

    async def record(name, function):
        row = {"case": name}
        try:
            value = function()
            if inspect.isawaitable(value):
                value = await value
            row.update(result=value, checks_passed=True)
        except Exception as exc:
            row.update(checks_passed=False, error_type=type(exc).__name__, error=str(exc)[-500:])
        rows.append(row)
        encoded = json.dumps(row, ensure_ascii=False, default=str)
        if config:
            encoded = encoded.replace(config["API_KEY"], "[REDACTED]")
        with (output / "trials.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        print(f"{name}: checks_passed={row['checks_passed']}", flush=True)

    for stage in ("before_dispatch", "before_commit", "after_commit"):
        await record(stage, lambda stage=stage: crash_check(output / stage, stage))
    await record("rejected_after_commit", lambda: crash_check(output / "rejected", "after_commit", 6))
    await record("concurrency_and_boundaries", lambda: boundary_checks(output / "boundaries"))
    if real_model:
        for stage in ("before_commit", "after_commit"):
            await record("model_recovery_" + stage, lambda stage=stage: model_recovery(output / ("model-" + stage), stage, config))
    summary = {"selected": len(rows), "checks_passed": sum(r["checks_passed"] for r in rows),
        "errors": sum(not r["checks_passed"] for r in rows),
        "model_answered": sum(bool(r.get("result", {}).get("graph_completed")) for r in rows if r["case"].startswith("model_recovery_")),
        "model_graph_failed": sum(r.get("result", {}).get("graph_completed") is False for r in rows if r["case"].startswith("model_recovery_")),
        "ordinary_model_answers_require_semantic_review": True, "not_a_public_benchmark": True}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return int(summary["errors"] > 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--real-model", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.output.resolve(), args.real_model)))
