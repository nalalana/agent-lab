"""Summarize recorded branch trials without making model calls."""
import argparse
import hashlib
import json
from pathlib import Path


def paired(baseline, candidate):
    old, new = {r["id"]: r for r in baseline}, {r["id"]: r for r in candidate}
    if old.keys() != new.keys():
        raise ValueError("Paired comparison requires identical IDs")
    return {"improved": [k for k in sorted(old) if not old[k]["correct"] and new[k]["correct"]],
        "regressed": [k for k in sorted(old) if old[k]["correct"] and not new[k]["correct"]]}


def summarize(root):
    results = {}
    for path in sorted(root.glob("*/*/summary.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))
        trials = [json.loads(s) for s in (path.parent / "trials.jsonl").read_text(encoding="utf-8").splitlines()]
        name = path.parent.parent.name + "/" + path.parent.name
        durations = sorted(r["seconds"] for r in trials)
        compact = {k: summary[k] for k in ("completed", "correct", "errors", "tp", "tn", "fp", "fn", "model_calls", "usage",
            "trials", "deterministic_passed", "normal_completion", "contract_passed", "harness_errors") if k in summary}
        compact.update(p50_seconds=durations[(len(durations)-1)//2], p95_seconds=durations[min(len(durations)-1, int(len(durations)*.95))],
            trials_sha256=hashlib.sha256((path.parent / "trials.jsonl").read_bytes()).hexdigest())
        manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
        compact.update(revision=manifest["revision"], dirty=manifest["dirty"], identity=manifest.get("identity", {}))
        phase = path.parent.parent.name.partition("-")[2]
        baseline = root / ("baseline-" + phase) / path.parent.name
        if "correct" in summary and baseline != path.parent and (baseline / "trials.jsonl").exists():
            previous_manifest = json.loads((baseline / "manifest.json").read_text(encoding="utf-8"))
            for key in ("corpus_file_sha256", "selected_ids_sha256", "model", "endpoint_sha256"):
                if previous_manifest["identity"][key] != manifest["identity"][key]:
                    raise ValueError("Unmatched evaluation identity: " + key)
            compact["paired"] = paired([json.loads(s) for s in (baseline / "trials.jsonl").read_text(encoding="utf-8").splitlines()], trials)
        results[name] = compact
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    result = summarize(args.root)
    (args.root / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    for name, row in result.items():
        print(name, {k: v for k, v in row.items() if k not in {"identity", "paired", "trials_sha256"}})
