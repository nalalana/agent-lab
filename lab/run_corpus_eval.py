"""Resumable grouped-corpus verifier evaluation; test split sealed by default."""
import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
from bootstrap import ROOT, setup
from run_eval import model_config, EnvironmentCredentials, TracedGateway


def score(rows):
    resolved = [r for r in rows if "verdict" in r]
    tp = sum(not r["supported"] and r["verdict"]["verdict"] == "revise" for r in resolved)
    fp = sum(r["supported"] and r["verdict"]["verdict"] == "revise" for r in resolved)
    fn = sum(not r["supported"] and r["verdict"]["verdict"] == "supported" for r in resolved)
    tn = sum(r["supported"] and r["verdict"]["verdict"] == "supported" for r in resolved)
    groups = defaultdict(list)
    for row in rows:
        groups[row["group"]].append(row["correct"])
    divide = lambda a, b: a / b if b else None
    return {"completed": len(rows), "resolved": len(resolved), "errors": len(rows) - len(resolved),
        "correct": tp + tn, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "accuracy_errors_wrong": divide(tp + tn, len(rows)), "hallucination_recall": divide(tp, tp + fn),
        "false_approve_rate": divide(fn, tp + fn), "false_reject_rate": divide(fp, fp + tn),
        "groups": len(groups), "macro_group_accuracy": divide(sum(sum(v) / len(v) for v in groups.values()), len(groups)),
        "model_calls": sum(r["model_calls"] for r in rows), "temporal_calls": sum(r["temporal_calls"] for r in rows),
        "usage": {k: sum(r["usage"][k] for r in rows) for k in ("input_tokens", "output_tokens", "total_tokens")}}


def load_rows(corpus, dataset, split, limit=None):
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    name = f"{dataset}-{split}.jsonl"
    path = corpus / name
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]["sha256"]:
        raise ValueError("Corpus fingerprint changed")
    rows = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]
    if any(r["split"] != split or r["dataset"] != dataset for r in rows):
        raise ValueError("Wrong split or dataset in file")
    rows.sort(key=lambda r: hashlib.sha256(r["id"].encode()).hexdigest())
    return rows[:limit], manifest["files"][name]


async def run(args):
    if args.split == "test" and not args.unseal_test:
        raise ValueError("Test is sealed; use --unseal-test only after final architecture/model freeze")
    selected, file_info = load_rows(args.corpus, args.dataset, args.split, args.limit)
    if args.dry_run:
        print(json.dumps({"selected": len(selected), "available": file_info["cases"], "groups": len({r["group"] for r in selected}),
            "split": args.split, "model_calls": 0}))
        return
    cfg = model_config()
    setup()
    from apps.agent_runtime.providers.model.openai_compatible import OpenAICompatibleModelGateway
    from apps.agent_runtime.response_verification import ModelResponseVerifier
    verifier_path = ROOT / "backend/apps/agent_runtime/response_verification.py"
    identity = {"corpus_file_sha256": file_info["sha256"], "selected_ids_sha256": hashlib.sha256(json.dumps([r["id"] for r in selected]).encode()).hexdigest(),
        "dataset": args.dataset, "split": args.split, "model": cfg["MODEL"],
        "endpoint_sha256": hashlib.sha256(cfg["BASE_URL"].encode()).hexdigest(),
        "verifier_sha256": hashlib.sha256(verifier_path.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    rows = []
    if args.resume:
        previous = json.loads((args.output / "manifest.json").read_text(encoding="utf-8"))
        if previous["identity"] != identity:
            raise ValueError("Resume refused: corpus/selection/model/verifier/runner changed")
        path = args.output / "trials.jsonl"
        rows = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        if len({r["id"] for r in rows}) != len(rows) or not {r["id"] for r in rows} <= {r["id"] for r in selected}:
            raise ValueError("Invalid resume records")
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        manifest = {"identity": identity, "time": datetime.now(timezone.utc).isoformat(),
            "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
            "selected": len(selected), "available": file_info["cases"], "concurrency": args.concurrency,
            "endpoint_sha256": hashlib.sha256(cfg["BASE_URL"].encode()).hexdigest()}
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    done = {r["id"] for r in rows}
    semaphore = asyncio.Semaphore(args.concurrency)
    def summarize():
        summary = {**score(rows), "selected": len(selected), "remaining": len(selected) - len(rows),
            "by_family": {f: score([r for r in rows if r["family"] == f]) for f in sorted({r["family"] for r in rows})}}
        temporary = args.output / "summary.tmp"
        temporary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        temporary.replace(args.output / "summary.json")
        return summary
    async def trial(case):
        async with semaphore:
            traced = TracedGateway(OpenAICompatibleModelGateway(credential_loader=EnvironmentCredentials(cfg), max_retries=0))
            state = case["state"]
            state["config"].update(model_provider="lab-compatible", model_name=cfg["MODEL"], credential_ref="lab:env")
            row = {k: case[k] for k in ("id", "group", "family", "supported")}
            row["language"] = case.get("language", "source_language")
            start = time.perf_counter()
            try:
                verdict = await asyncio.wait_for(ModelResponseVerifier(traced).verify(state, case["draft"]), 120)
                if verdict.verdict not in {"supported", "revise"}:
                    raise ValueError("Unexpected permission verdict")
                row.update(verdict=asdict(verdict), correct=(verdict.verdict == "supported") == case["supported"])
            except Exception as exc:
                row.update(error_type=type(exc).__name__, correct=False)
            row.update(seconds=time.perf_counter() - start, model_calls=len(traced.calls),
                temporal_calls=sum(c["request"]["purpose"] == "response_temporal_verification" for c in traced.calls),
                usage={k: sum(int(e["data"].get(k) or 0) for c in traced.calls for e in c["events"] if e["event_type"] == "usage")
                       for k in ("input_tokens", "output_tokens", "total_tokens")})
            if not row["correct"]:
                (args.output / "failures").mkdir(exist_ok=True)
                trace = args.output / "failures" / (hashlib.sha256(row["id"].encode()).hexdigest() + ".json")
                trace.write_text(json.dumps({"id": row["id"], "calls": traced.calls}, ensure_ascii=False, default=str).replace(cfg["API_KEY"], "[REDACTED]"), encoding="utf-8")
            encoded = json.dumps(row, ensure_ascii=False, default=str).replace(cfg["API_KEY"], "[REDACTED]")
            with (args.output / "trials.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
            rows.append(json.loads(encoded))
            if len(rows) % 25 == 0 or len(rows) == len(selected):
                summarize()
                print(f"{len(rows)}/{len(selected)} correct={sum(r['correct'] for r in rows)} errors={sum('error_type' in r for r in rows)}", flush=True)
    summarize()
    await asyncio.gather(*(trial(case) for case in selected if case["id"] not in done))
    print(json.dumps(summarize()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "lab/datasets/agent-eval-v2")
    parser.add_argument("--dataset", choices=["synthetic", "ragtruth"], required=True)
    parser.add_argument("--split", choices=["dev", "validation", "test"], default="validation")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--unseal-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 10 or (args.limit is not None and args.limit < 1) or (not args.dry_run and args.output is None):
        parser.error("Require output, concurrency 1–10 and positive limit")
    asyncio.run(run(args))
