"""Run existing production-graph regression suites without platform services."""
import argparse
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "apps.agent_runtime.tests.test_phase1_graph",
    "apps.agent_runtime.tests.test_phase2_tools",
    "apps.agent_runtime.tests.test_response_verification",
)


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.records.append({"test": test.id(), "seconds": time.perf_counter() - self.started})
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--suite", action="append", help="Dotted unittest suite; repeatable")
    parser.add_argument("--output", type=Path, default=ROOT / "lab/results/baseline.json")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    sys.path.insert(0, str(ROOT / "backend"))
    from django.conf import settings
    settings.configure(SECRET_KEY="offline-agent-lab", USE_TZ=True,
                       INSTALLED_APPS=["apps.user", "apps.opengate", "apps.company", "apps.iam",
                                       "apps.runtime", "apps.agent_runtime"],
                       DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})
    import django
    django.setup()
    runs = []
    for _ in range(args.repeat):
        suite = unittest.defaultTestLoader.loadTestsFromNames(args.suite or SUITES)
        runner = unittest.TextTestRunner(verbosity=1, resultclass=RecordedResult)
        result = runner.run(suite)
        runs.append({"tests": result.testsRun, "success": result.wasSuccessful(),
                     "failures": [(t.id(), detail) for t, detail in result.failures],
                     "errors": [(t.id(), detail) for t, detail in result.errors],
                     "skipped": [(t.id(), reason) for t, reason in result.skipped],
                     "records": result.records})
    report = {"kind": "offline_regression_not_model_evaluation",
              "created_at": datetime.now(timezone.utc).isoformat(),
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
              "python": sys.version, "runs": runs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {args.output}")
    return 0 if all(run["success"] for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
