"""Run a small tau2 native-scaffold reference baseline in its isolated venv.

This does NOT evaluate the production Phase2 graph and is NOT an official score:
agent/user/judge share the configured model, and the task set is sampled.
"""
import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from bootstrap import ROOT
from run_eval import model_config

DATASET = ROOT / "lab/datasets/tau2-bench"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=["retail", "airline", "telecom"], default="retail")
    parser.add_argument("--split", default="train")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--task", action="append")
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--agent", choices=["native", "graph"], default="native")
    parser.add_argument("--carry-read-evidence", action="store_true", help="Experimental task-local read cache; retail graph only")
    parser.add_argument("--attribute-operation-results", action="store_true", help="Experimental receipt attribution prompt; requires read carry")
    parser.add_argument("--stop-on-unknown-operation", action="store_true", help="Experimental task-local fail-stop for uncertain writes")
    args = parser.parse_args()
    if args.stop_on_unknown_operation and (args.agent != "graph" or args.domain != "retail"):
        parser.error("Unknown outcome gate requires --agent graph --domain retail")
    os.environ["AGENT_LAB_TAU_STOP_UNKNOWN"] = "1" if args.stop_on_unknown_operation else "0"
    if args.attribute_operation_results and not args.carry_read_evidence:
        parser.error("Operation attribution requires --carry-read-evidence")
    os.environ["AGENT_LAB_TAU_ATTRIBUTE_OPERATIONS"] = "1" if args.attribute_operation_results else "0"
    if args.carry_read_evidence and (args.agent != "graph" or args.domain != "retail"):
        parser.error("Read carry experiment requires --agent graph --domain retail")
    os.environ["AGENT_LAB_TAU_CARRY_READS"] = "1" if args.carry_read_evidence else "0"
    if args.limit < 1 or args.max_steps < 1:
        parser.error("limit/max-steps must be positive")
    os.environ["TAU2_DATA_DIR"] = str(DATASET / "data")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from loguru import logger
    logger.remove()
    from tau2.run import get_tasks, run_single_task
    from tau2.data_model.simulation import TextRunConfig
    from tau2.data_model.semantic_communication import CommunicateScoring, SemanticCommunicateSettings
    tasks = get_tasks(args.domain, task_split_name=args.split, task_ids=args.task, num_tasks=args.limit)
    if not tasks:
        parser.error("No tasks selected")
    plan = {"kind": "tau2_native_scaffold_reference_not_production_graph" if args.agent == "native" else "tau2_production_graph_external_execution_adapter", "domain": args.domain,
            "split": args.split, "task_ids": [t.id for t in tasks], "seed": args.seed, "max_steps": args.max_steps,
            "native_environment_and_evaluator": True, "official_score": False,
            "agent_user_judge_same_model": True}
    if args.agent == "graph":
        plan["adapter"] = {"graph": "production Phase2 topology with checkpoint pause/resume",
            "execution": "native tau tools via external handoff; execute_tool node bypassed only for actual native execution",
            "facts": "successful non-mutating READ tool results mapped to typed table evidence for verifier only",
            "prompt": "native domain policy with adapter instructions", "memory": "disabled",
            "per_user_turn_seconds": 120, "per_user_turn_tool_limit": 8,
            "carry_read_evidence": args.carry_read_evidence,
            "attribute_operation_results": args.attribute_operation_results,
            "stop_on_unknown_operation": args.stop_on_unknown_operation,
            "model_request_classifier": True, "model_response_verifier": True}
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    config = model_config()
    if args.agent == "graph":
        from tau2.registry import registry
        from tau_graph_agent import create_graph_agent
        registry.register_agent_factory(create_graph_agent, name="touming_graph")
    # Credentials stay in process environment, never inside serialized llm_args.
    os.environ["OPENAI_API_KEY"] = config["API_KEY"]
    os.environ["OPENAI_API_BASE"] = config["BASE_URL"]
    model = "openai/" + config["MODEL"]
    llm_args = {"temperature": 0, "max_tokens": 2048, "timeout": 60, "num_retries": 0}
    # Upstream exposes communicate-info settings but hard-codes the NL assertion
    # judge in this module. Override its model only; preserve its grading logic.
    import tau2.evaluator.evaluator_nl_assertions as nl_evaluator
    nl_evaluator.DEFAULT_LLM_NL_ASSERTIONS = model
    nl_evaluator.DEFAULT_LLM_NL_ASSERTIONS_ARGS = llm_args
    run_config = TextRunConfig(domain=args.domain, task_split_name=args.split,
        agent="llm_agent" if args.agent == "native" else "touming_graph",
        llm_agent=model, llm_args_agent=llm_args, user="user_simulator", llm_user=model,
        llm_args_user=llm_args, num_trials=1, max_steps=args.max_steps, max_errors=5,
        seed=args.seed, max_concurrency=1, workers=0, timeout=180, auto_review=False,
        communicate_scoring=CommunicateScoring(judge=SemanticCommunicateSettings(model=model, model_args=llm_args)))
    output = args.output or ROOT / "lab/results" / ("tau2-native-" + uuid4().hex[:10])
    output.mkdir(parents=True, exist_ok=False)
    if args.agent == "graph":
        os.environ["AGENT_LAB_TAU_TRACE_DIR"] = str(output.resolve() / "graph_traces")
    plan.update({"model": config["MODEL"], "time": datetime.now(timezone.utc).isoformat(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "dataset_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=DATASET, text=True).strip(),
        "nl_assertion_judge_override": {"model": model, "model_args": llm_args},
        "config": run_config.model_dump(mode="json"),
        "dependencies": {name: version(name) for name in ("tau2", "litellm", "openai", "pydantic")},
        "python": sys.version})
    (output / "manifest.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    records = []
    with (output / "trials.jsonl").open("w", encoding="utf-8") as stream:
        for task in tasks:
            record = {"task_id": task.id}
            start = time.perf_counter()
            try:
                result = run_single_task(run_config, task, seed=args.seed)
                record.update({"simulation": result.model_dump(mode="json"),
                    "reward": result.reward_info.reward if result.reward_info else None,
                    "termination_reason": str(result.termination_reason)})
                record["unknown_operation_outcome"] = any(
                    ((getattr(m, "raw_data", None) or {}).get("graph_runtime_failure") or {}).get("execution_outcome") == "unknown"
                    for m in result.messages)
                record["graph_runtime_failed"] = any(bool((getattr(m, "raw_data", None) or {}).get("graph_runtime_failure"))
                                                     for m in result.messages)
            except Exception as exc:
                record.update({"reward": None, "error_type": type(exc).__name__, "error_reason": str(exc)[:400]})
            record["seconds"] = time.perf_counter() - start
            encoded = json.dumps(record, ensure_ascii=False, default=str).replace(config["API_KEY"], "[REDACTED]")
            stream.write(encoded + "\n")
            stream.flush()
            records.append(json.loads(encoded))
            print(f"task={task.id} reward={record['reward']} error={record.get('error_type')}", flush=True)
    scored = [r for r in records if r["reward"] is not None]
    summary = {"selected": len(records), "scored": len(scored), "errors": len(records) - len(scored),
        "reward_one": sum(r["reward"] == 1 for r in scored),
        "mean_reward": sum(r["reward"] for r in scored) / len(scored) if scored else None,
        "graph_runtime_failures": sum(bool(r.get("graph_runtime_failed")) for r in records),
        "unknown_operation_outcomes": sum(bool(r.get("unknown_operation_outcome")) for r in records),
        "reliable_reward_one": sum(r["reward"] == 1 and not r.get("graph_runtime_failed") for r in records),
        "mean_reward_errors_zero": sum(r["reward"] or 0 for r in records) / len(records),
        "kind": plan["kind"], "not_production_graph_score": args.agent == "native"}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Report: {output}")
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
