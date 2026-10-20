"""Paired synthetic receipt-attribution diagnostic; not a benchmark score."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from bootstrap import ROOT, setup
from run_eval import EnvironmentCredentials, TracedGateway, model_config

CASES = ROOT / "lab/cases/operation-attribution.jsonl"
SUFFIX = ROOT / "lab/prompts/operation-attribution.txt"


def case_state(case, config):
    return {"run_id": case["id"], "max_execution_seconds": 60,
        "config": {"model_provider": "lab-compatible", "model_name": config["MODEL"],
            "credential_ref": "lab:env", "permission_facts": {},
            "user_text": "Report what actually happened to order W1 using the receipts in chronological order. Do not initiate any action. If the outcome is unconfirmed, say so."},
        "model_messages": case.get("history", []), "tool_results": case["tool_results"]}


async def run(output, concurrency, cases_path=CASES):
    setup()
    from apps.agent_runtime.response_verification import ModelResponseVerifier
    from apps.agent_runtime.providers.model.openai_compatible import OpenAICompatibleModelGateway
    config = model_config()
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"kind": "synthetic_operation_attribution_diagnostic", "model": config["MODEL"],
        "time": datetime.now(timezone.utc).isoformat(), "cases": len(cases), "concurrency": concurrency,
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (cases_path, SUFFIX)}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(case, candidate):
        async with semaphore:
            gateway = TracedGateway(OpenAICompatibleModelGateway(credential_loader=EnvironmentCredentials(config), max_retries=0))
            verifier = ModelResponseVerifier(gateway, prompt_suffix=SUFFIX.read_text(encoding="utf-8") if candidate else "")
            record = {"id": case["id"], "candidate": candidate, "expected_supported": case["supported"]}
            try:
                verdict = await asyncio.wait_for(verifier.verify(case_state(case, config), case["draft"]), timeout=150)
                record.update({"verdict": asdict(verdict), "correct": (verdict.verdict == "supported") == case["supported"]})
            except Exception as exc:
                record.update({"error_type": type(exc).__name__, "error": str(exc)[:400], "correct": False})
            record["calls"] = gateway.calls
            return record

    records = await asyncio.gather(*(evaluate(c, candidate) for c in cases for candidate in (False, True)))
    encoded = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records).replace(config["API_KEY"], "[REDACTED]")
    (output / "trials.jsonl").write_text(encoded, encoding="utf-8")
    summary = {}
    for candidate in (False, True):
        rows = [r for r in records if r["candidate"] == candidate]
        counts = Counter()
        for r in rows:
            if "error_type" in r:
                counts["errors"] += 1
            elif not r["correct"]:
                counts["false_reject" if r["expected_supported"] else "false_approve"] += 1
        summary["candidate" if candidate else "baseline"] = {"selected": len(rows),
            "correct": sum(r["correct"] for r in rows), **{k: counts[k] for k in ("errors", "false_reject", "false_approve")}}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.concurrency, args.cases))
