"""Compare old/new evidence consumers on frozen fixture traces; no model calls."""
import ast
import hashlib
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
from bootstrap import ROOT, setup
setup()
from apps.agent_runtime.policies import PROJECT_TABLE_WRITE_POLICY
from apps.agent_runtime.orchestration.tool_nodes import _table_read_version
from apps.agent_runtime.response_verification import verification_input

PRIOR = "2259dad"


def old_helpers(path, names, namespace):
    text = subprocess.check_output(["git", "show", f"{PRIOR}:{path}"], cwd=ROOT).decode("utf-8")
    tree = ast.parse(text)
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(node for node in tree.body if getattr(node, "name", None) in names)
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), path, "exec"), namespace)


def run():
    prior = {"json": json, "re": re, "PROJECT_TABLE_WRITE_POLICY": PROJECT_TABLE_WRITE_POLICY}
    old_helpers("backend/apps/agent_runtime/response_verification.py",
        {"_json", "_body_text", "_invalidated_table_reads", "verification_input"}, prior)
    old_helpers("backend/apps/agent_runtime/orchestration/tool_nodes.py", {"_table_read_version"}, prior)
    count = 0
    for split in ("dev", "validation", "test"):
        path = ROOT / f"lab/datasets/agent-eval-v2/synthetic-{split}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            for enriched in (False, True):
                state = deepcopy(case["state"])
                results = state["tool_results"]
                if enriched:
                    for index, result in enumerate(results):
                        if result["name"] == "company.table.read" and result.get("success") is True:
                            payload = json.loads(result["content"])
                            payload["data"]["table_version"] = index
                            result["content"] = json.dumps(payload, ensure_ascii=False)
                if prior["verification_input"](state, case["draft"]) != verification_input(state, case["draft"]):
                    raise AssertionError("Evidence source changed for " + case["id"])
                for tid in {r["arguments"]["tid"] for r in results}:
                    if prior["_table_read_version"](results, tid) != _table_read_version(results, tid):
                        raise AssertionError("Write version changed for " + case["id"])
                count += 1
    result = {"prior_revision": PRIOR, "case_trace_variants": count, "source_and_version_mismatches": 0,
        "model_calls": 0, "not_a_model_score": True,
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "view_sha256": hashlib.sha256((ROOT / "backend/apps/agent_runtime/table_evidence.py").read_bytes()).hexdigest(),
        "corpus_lock_sha256": hashlib.sha256((ROOT / "lab/eval-corpus-v2.lock.json").read_bytes()).hexdigest(),
        "note": "Program-only equivalence over existing templates, including test-file trace structure; no test model evaluation"}
    output = ROOT / "lab/results/shared-evidence-equivalence.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    run()
