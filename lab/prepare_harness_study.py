"""Freeze small, balanced dev/validation subsets; never opens the test files."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from bootstrap import ROOT
from run_corpus_eval import load_rows


def select(rows, dataset, count):
    ordered = sorted(rows, key=lambda r: hashlib.sha256(r["id"].encode()).hexdigest())
    if dataset == "synthetic":
        groups = sorted({r["group"] for r in rows}, key=lambda s: hashlib.sha256(s.encode()).hexdigest())[:count]
        return [r for r in ordered if r["group"] in groups]
    selected, seen = [], set()
    for family in sorted({r["family"] for r in rows}):
        for label in (False, True):
            candidates = [r for r in ordered if r["family"] == family and r["supported"] == label]
            chosen = []
            for row in candidates:
                if row["group"] not in seen:
                    seen.add(row["group"])
                    chosen.append(row)
                if len(chosen) == count:
                    break
            if len(chosen) != count:
                raise ValueError("Not enough independent reference groups")
            selected.extend(chosen)
    return sorted(selected, key=lambda r: hashlib.sha256(r["id"].encode()).hexdigest())


def prepare(corpus, output):
    output.mkdir(parents=True, exist_ok=False)
    files = {}
    for dataset in ("synthetic", "ragtruth"):
        for split in ("dev", "validation"):
            rows, original = load_rows(corpus, dataset, split)
            count = (2 if split == "dev" else 3) if dataset == "synthetic" else (4 if split == "dev" else 8)
            chosen = select(rows, dataset, count)
            name = f"{dataset}-{split}.jsonl"
            path = output / name
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in chosen), encoding="utf-8")
            files[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "cases": len(chosen),
                "original_sha256": original["sha256"], "groups": len({r["group"] for r in chosen}),
                "supported": sum(r["supported"] for r in chosen), "families": dict(Counter(r["family"] for r in chosen))}
    manifest = {"kind": "harness_study_subsets_not_new_benchmark", "test_opened": False, "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "lab/datasets/agent-eval-v2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.corpus, args.output)
