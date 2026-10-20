"""Evaluate the existing response verifier against a pinned RAGTruth subset."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import subprocess
import time
from uuid import uuid4

from bootstrap import ROOT, setup
from run_eval import EnvironmentCredentials, TracedGateway, model_config

DATASET = ROOT / "lab/datasets/RAGTruth"


def select_samples(dataset, limit=200, seed=42, split="test", pool="all"):
    sources = {s["source_id"]: s for s in map(json.loads, (dataset / "dataset/source_info.jsonl").read_text(encoding="utf-8").splitlines())}
    buckets = {}
    for response in map(json.loads, (dataset / "dataset/response.jsonl").read_text(encoding="utf-8").splitlines()):
        if response["split"] != split or response["quality"] != "good":
            continue
        held_out = int(hashlib.sha256(response["source_id"].encode()).hexdigest(), 16) % 5 == 0
        if (pool == "dev" and held_out) or (pool == "holdout" and not held_out):
            continue
        source = sources[response["source_id"]]
        buckets.setdefault((bool(response["labels"]), source["task_type"]), []).append((source, response))
    rng = random.Random(seed)
    for key in sorted(buckets):
        buckets[key].sort(key=lambda pair: pair[1]["id"])
        rng.shuffle(buckets[key])
    selected, used = [], set()
    # Round-robin strata, with at most one response per underlying source.
    while len(selected) < limit:
        progress = False
        for key in sorted(buckets, key=lambda key: (key[1], key[0])):
            bucket = buckets[key]
            while bucket and bucket[-1][0]["source_id"] in used:
                bucket.pop()
            if bucket and len(selected) < limit:
                sample = bucket.pop()
                selected.append(sample)
                used.add(sample[0]["source_id"])
                progress = True
        if not progress:
            raise ValueError("Not enough distinct test sources for requested subset")
    return selected


def verifier_input(source, response):
    reference = source["source_info"]
    if source["task_type"] == "QA":
        question, body = reference["question"], reference["passages"]
    else:
        question = "Summarize the provided source." if source["task_type"] == "Summary" else "Describe the provided structured record."
        body = reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False, sort_keys=True)
    path = f"knowledge/ragtruth/{source['source_id']}.txt"
    # Labels, annotation metadata, quality, generating-model ID, and split never enter model inputs.
    request = "Check every factual claim strictly against the supplied reference; outside knowledge is not evidence.\nTask: " + question
    config = {"user_text": request, "permission_facts": {"rules": {"read": [path], "write": [], "delete": [], "deny": []},
        "readable_files": [{"logical_path": path, "read": True, "write": False, "delete": False}]},
        "memory_policy": {}, "attachments": [], "request_source": "agent_test"}
    state = {"run_id": uuid4().hex, "config": config, "model_messages": [], "max_execution_seconds": 60,
        "tool_results": [{"tool_call_id": "reference", "name": "workspace.read", "success": True,
            "arguments": {"logical_path": path}, "content": json.dumps({"tool_name": "workspace.read",
                "logical_path": path, "content": body, "has_extractable_text": bool(body)}, ensure_ascii=False)}]}
    return state, response["response"] + "\n\nSource: " + path


def metrics(records):
    valid = [r for r in records if r.get("predicted_hallucination") is not None]
    tp = sum(r["gold_hallucination"] and r["predicted_hallucination"] for r in valid)
    fp = sum(not r["gold_hallucination"] and r["predicted_hallucination"] for r in valid)
    fn = sum(r["gold_hallucination"] and not r["predicted_hallucination"] for r in valid)
    tn = sum(not r["gold_hallucination"] and not r["predicted_hallucination"] for r in valid)
    divide = lambda a, b: a / b if b else None
    return {"selected": len(records), "resolved": len(valid), "errors": len(records) - len(valid),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": divide(tp, tp + fp),
        "recall": divide(tp, tp + fn), "f1": divide(2 * tp, 2 * tp + fp + fn),
        "false_positive_rate": divide(fp, fp + tn), "false_negative_rate": divide(fn, fn + tp),
        "accuracy_resolved": divide(tp + tn, len(valid)),
        "accuracy_errors_counted_wrong": divide(tp + tn, len(records))}


async def evaluate(args, samples, config):
    from apps.agent_runtime.providers.model.openai_compatible import OpenAICompatibleModelGateway
    from apps.agent_runtime.response_verification import ModelResponseVerifier
    import apps.agent_runtime.response_verification as verification
    if args.prompt_suffix:
        verification.VERIFICATION_PROMPT += "\n" + args.prompt_suffix.read_text(encoding="utf-8")
    output = args.output or ROOT / "lab/results" / ("ragtruth-" + uuid4().hex[:10])
    output.mkdir(parents=True, exist_ok=False)
    state_manifest = {"kind": "adapted_ragtruth_response_level_verifier_diagnostic", "time": datetime.now(timezone.utc).isoformat(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "dataset_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.dataset, text=True).strip(),
        "dataset_hashes": {name: hashlib.sha256((args.dataset / "dataset" / name).read_bytes()).hexdigest()
                           for name in ("response.jsonl", "source_info.jsonl")},
        "model": config["MODEL"], "seed": args.seed, "concurrency": args.concurrency,
        "sample_ids": [r["id"] for _, r in samples], "source_ids": [s["source_id"] for s, _ in samples],
        "split": args.split, "pool": args.pool,
        "selection": "quality=good, round-robin gold×task, unique source_id; train holdout=SHA256(source_id)%5==0",
        "verifier_prompt_sha256": hashlib.sha256(verification.VERIFICATION_PROMPT.encode()).hexdigest(),
        "gold": "positive iff any annotated span, including implicit_true as in upstream preparation",
        "adaptation": "append Source path to original draft; map reference to successful workspace.read; no labels in model input",
        "not_official_span_level_score": True}
    (output / "manifest.json").write_text(json.dumps(state_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    semaphore, records = asyncio.Semaphore(args.concurrency), []
    with (output / "trials.jsonl").open("w", encoding="utf-8") as stream:
        async def trial(source, response):
            async with semaphore:
                traced = TracedGateway(OpenAICompatibleModelGateway(credential_loader=EnvironmentCredentials(config), max_retries=0))
                state, draft = verifier_input(source, response)
                state["config"].update({"model_provider": "lab-compatible", "model_name": config["MODEL"], "credential_ref": "lab:env"})
                record = {"id": response["id"], "source_id": source["source_id"], "task": source["task_type"],
                    "gold_hallucination": bool(response["labels"]), "predicted_hallucination": None,
                    "implicit_true_labels": sum(bool(label.get("implicit_true")) for label in response["labels"])}
                start = time.perf_counter()
                try:
                    result = await asyncio.wait_for(ModelResponseVerifier(traced).verify(state, draft), 65)
                    if result.verdict not in {"supported", "revise"}:
                        raise ValueError("Unexpected routing verdict for faithfulness diagnostic")
                    record.update({"predicted_hallucination": result.verdict == "revise", "verdict": result.verdict,
                                   "issues": result.issues, "claims": result.claims})
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
                    record["error_reason"] = str(exc)[:300]
                record["seconds"] = time.perf_counter() - start
                record["model_calls"] = traced.calls
                encoded = json.dumps(record, ensure_ascii=False, default=str).replace(config["API_KEY"], "[REDACTED]")
                stream.write(encoded + "\n")
                stream.flush()
                records.append(json.loads(encoded))
                print(f"{len(records)}/{len(samples)} {response['id']}: gold={record['gold_hallucination']} predicted={record['predicted_hallucination']}", flush=True)
        await asyncio.gather(*(trial(*sample) for sample in samples))
    summary = metrics(records)
    summary["by_task"] = {task: metrics([r for r in records if r["task"] == task]) for task in sorted({r["task"] for r in records})}
    summary["usage"] = {key: sum(int(event["data"].get(key) or 0) for r in records
        for call in r["model_calls"] for event in call["events"] if event["event_type"] == "usage")
        for key in ("input_tokens", "output_tokens", "total_tokens")}
    summary["source_faithful_responses_rejected"] = summary["fp"]
    summary["supported_with_empty_claims"] = sum(r.get("verdict") == "supported" and not r.get("claims") for r in records)
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Report: {output}")
    return 0 if summary["errors"] == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split", choices=["train", "test"], default="test")
    parser.add_argument("--pool", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--prompt-suffix", type=Path, help="Lab-only candidate; never modifies production source")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.limit < 1 or not 1 <= args.concurrency <= 10:
        parser.error("limit must be positive; concurrency must be 1–10")
    if args.split == "test" and args.pool != "all":
        parser.error("dev/holdout pools are defined only within train")
    samples = select_samples(args.dataset, args.limit, args.seed, args.split, args.pool)
    if args.dry_run:
        print(json.dumps({"selected": len(samples), "strata": {str(k): v for k, v in Counter(
            (s["task_type"], bool(r["labels"])) for s, r in samples).items()}, "model_calls": 0}, indent=2))
        return 0
    config = model_config()
    setup()
    return asyncio.run(evaluate(args, samples, config))


if __name__ == "__main__":
    raise SystemExit(main())
