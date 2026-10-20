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
    args = parser.parse_args()
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
    plan = {"kind": "tau2_native_scaffold_reference_not_production_graph", "domain": args.domain,
            "split": args.split, "task_ids": [t.id for t in tasks], "seed": args.seed, "max_steps": args.max_steps,
            "native_environment_and_evaluator": True, "official_score": False,
            "agent_user_judge_same_model": True}
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    config = model_config()
    # Credentials stay in process environment, never inside serialized llm_args.
    os.environ["OPENAI_API_KEY"] = config["API_KEY"]
    os.environ["OPENAI_API_BASE"] = config["BASE_URL"]
    model = "openai/" + config["MODEL"]
    llm_args = {"temperature": 0, "max_tokens": 2048, "timeout": 60, "num_retries": 0}
    run_config = TextRunConfig(domain=args.domain, task_split_name=args.split, agent="llm_agent",
        llm_agent=model, llm_args_agent=llm_args, user="user_simulator", llm_user=model,
        llm_args_user=llm_args, num_trials=1, max_steps=args.max_steps, max_errors=5,
        seed=args.seed, max_concurrency=1, workers=0, timeout=180, auto_review=False,
        communicate_scoring=CommunicateScoring(judge=SemanticCommunicateSettings(model=model, model_args=llm_args)))
    output = args.output or ROOT / "lab/results" / ("tau2-native-" + uuid4().hex[:10])
    output.mkdir(parents=True, exist_ok=False)
    plan.update({"model": config["MODEL"], "time": datetime.now(timezone.utc).isoformat(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "dataset_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=DATASET, text=True).strip(),
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
        "kind": plan["kind"], "not_production_graph_score": True}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Report: {output}")
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
