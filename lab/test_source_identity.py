from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from source_identity import source_identity


class SourceIdentityTests(unittest.TestCase):
    def test_runtime_dependencies_lab_sources_and_prompts_change_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = [root / name for name in ("backend/apps/agent_runtime/table_evidence.py",
                     "lab/run_eval.py", "lab/prompts/verify.txt", "lab/prompts/system.md")]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original", encoding="utf-8")
            original = source_identity(root)["source_sha256"]
            self.assertEqual(original, source_identity(root)["source_sha256"])
            for path in paths:
                with self.subTest(path=path.relative_to(root)):
                    path.write_text("changed", encoding="utf-8")
                    self.assertNotEqual(original, source_identity(root)["source_sha256"])
                    path.write_text("original", encoding="utf-8")

    def test_reports_and_generated_files_do_not_change_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = source_identity(root)["source_sha256"]
            for name in ("lab/README.md", "lab/experiments/report.md", "lab/results/generated.py",
                         "lab/datasets/generated.py", "lab/.venv/site-packages/module.py",
                         "backend/apps/agent_runtime/__pycache__/generated.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("irrelevant", encoding="utf-8")
            self.assertEqual(original, source_identity(root)["source_sha256"])

    def test_python_and_installed_dependency_versions_are_recorded(self):
        installed = [SimpleNamespace(metadata={"Name": "Langchain_Core"}, version="0.3.0"),
                     SimpleNamespace(metadata={"Name": "Django"}, version="4.2.0")]
        with tempfile.TemporaryDirectory() as folder, patch("source_identity.distributions", return_value=installed):
            identity = source_identity(Path(folder))
            self.assertEqual(identity["python"], sys.version)
            self.assertEqual(identity["dependencies"], {"django": "4.2.0", "langchain-core": "0.3.0"})
            installed[0].version = "0.3.1"
            self.assertNotEqual(identity, source_identity(Path(folder)))


if __name__ == "__main__":
    unittest.main()
