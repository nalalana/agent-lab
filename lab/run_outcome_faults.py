"""Real-model graph with local SQLite fault injection; not an external benchmark."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess

from bootstrap import ROOT, setup
from operation_outcome import UNKNOWN_CODE, UNKNOWN_MESSAGE
from run_eval import model_config
from tau_graph_worker import Engine

MODES = ("normal", "after_commit_timeout", "before_commit_timeout", "rejected_before_execution",
         "business_rejected", "read_timeout")
TOOLS = [{"type": "function", "function": {"name": name, "description": description,
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    for name, description in (("increment_counter", "Increment the SQLite counter by one and return the new value."),
                              ("read_counter", "Read the current SQLite counter value without changes."))]


async def run(output):
    setup()
    config = model_config()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"kind": "real_model_synthetic_sqlite_fault_injection", "model": config["MODEL"],
        "time": datetime.now(timezone.utc).isoformat(), "modes": MODES, "concurrency": 1,
        "attribution_prompt": False, "read_cache": True,
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []
    for mode in MODES:
        for gate in (False, True):
            row = {"mode": mode, "gate": gate, "tool_requests": [], "commits": 0}
            with sqlite3.connect(":memory:") as db:
                db.execute("CREATE TABLE counter(value INTEGER)")
                db.execute("INSERT INTO counter VALUES (0)")
                engine = Engine({"tools": TOOLS, "read_names": ["read_counter"], "carry_read_evidence": True,
                    "stop_on_unknown_operation": gate, "trace_dir": str(output / f"{mode}-{gate}"),
                    "policy": "This is an authorized synthetic counter task. The initial counter is zero. The user has confirmed the requested action. No identity verification is needed. Use only the supplied tools. Do not invent results."}, config)
                try:
                    content = "Read the current counter value only." if mode == "read_timeout" else "Increment the counter by one exactly once now. I confirm. Report what actually happened."
                    reply = await engine.step({"kind": "user", "content": content})
                    while reply["kind"] == "tool":
                        if len(row["tool_requests"]) >= 12:
                            raise RuntimeError("Fault diagnostic tool budget exhausted")
                        name = reply["name"]
                        row["tool_requests"].append(name)
                        command = {"kind": "tool", "id": reply["id"], "execution_status": "executed", "error": False}
                        if name == "read_counter":
                            command["content"] = json.dumps({"value": db.execute("SELECT value FROM counter").fetchone()[0]})
                            if mode == "read_timeout":
                                command.update(error=True, content="read response timed out")
                        elif name == "increment_counter":
                            if mode == "rejected_before_execution":
                                command.update(error=True, execution_status="rejected", content="executor rejected before dispatch")
                            elif mode == "business_rejected":
                                command["content"] = json.dumps({"status": "rejected", "changed": False, "reason": "counter is locked"})
                            else:
                                if mode != "before_commit_timeout":
                                    db.execute("UPDATE counter SET value=value+1")
                                    db.commit()
                                    row["commits"] += 1
                                if mode in {"before_commit_timeout", "after_commit_timeout"}:
                                    command.update(error=True, content="transport response timed out")
                                else:
                                    command["content"] = json.dumps({"changed": True, "value": db.execute("SELECT value FROM counter").fetchone()[0]})
                        else:
                            raise ValueError("Unexpected tool")
                        reply = await engine.step(command)
                    row["reply"] = reply
                    row["unknown_stop"] = (reply.get("error") or {}).get("code") == UNKNOWN_CODE
                    if row["unknown_stop"]:
                        before = len(engine.traced.calls)
                        repeated = await engine.step({"kind": "user", "content": "Retry the increment now."})
                        row["retry_blocked_without_model_call"] = repeated == reply and len(engine.traced.calls) == before
                        row["fixed_uncertainty_message"] = reply["error"]["message"] == UNKNOWN_MESSAGE
                except Exception as exc:
                    row.update(error_type=type(exc).__name__, error=str(exc)[:300])
                row["final_value"] = db.execute("SELECT value FROM counter").fetchone()[0]
                row["model_calls"] = len(engine.traced.calls)
            rows.append(row)
            with (output / "trials.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str).replace(config["API_KEY"], "[REDACTED]") + "\n")
            print(f"mode={mode} gate={gate} value={row['final_value']} unknown={row.get('unknown_stop')} error={row.get('error_type')}", flush=True)
    summary = {"selected": len(rows), "errors": sum("error_type" in r for r in rows),
        "gate_unknown_stops": sum(bool(r.get("unknown_stop")) for r in rows if r["gate"]),
        "duplicate_commits": sum(r["commits"] > 1 for r in rows),
        "ordinary_model_answers_require_semantic_review": True}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
