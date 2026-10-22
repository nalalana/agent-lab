"""Run a frozen checkout against common subsets with explicit local credentials."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from bootstrap import ROOT


def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / "lab/.env.local", override=False)
    checkout = args.checkout.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.phase in {"dev", "validation"}:
        for dataset in ("synthetic", "ragtruth"):
            command = [sys.executable, str(checkout / "lab/run_corpus_eval.py"), "--corpus", str(args.corpus.resolve()),
                "--dataset", dataset, "--split", args.phase, "--concurrency", "4", "--output", str((args.output / dataset).resolve())]
            if args.resume:
                command.append("--resume")
            subprocess.run(command, cwd=checkout, env=os.environ, check=True)
    else:
        script = "run_eval.py" if args.phase == "business" else "run_conflict_recovery.py"
        command = [sys.executable, str(checkout / "lab" / script), "--output", str((args.output / args.phase).resolve())]
        if args.phase == "business":
            command.extend(["--concurrency", "4", "--repeat", "1", "--timeout", "120"])
        completed = subprocess.run(command, cwd=checkout, env=os.environ)
        # A business grade failure is a result, not permission to hide subsequent results.
        if completed.returncode and not (args.output / args.phase / "summary.json").exists():
            raise subprocess.CalledProcessError(completed.returncode, command)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--phase", choices=["dev", "validation", "business", "conflict"], required=True)
    parser.add_argument("--corpus", type=Path, default=ROOT / "lab/results/harness-study-20261022/corpus")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    run(parser.parse_args())
