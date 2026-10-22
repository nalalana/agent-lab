import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from build_eval_corpus import synthetic, audit_rows
from run_corpus_eval import score, run, load_rows


class CorpusTests(unittest.TestCase):
    def test_deterministic_grouped_balanced_synthetic_splits(self):
        rows = list(synthetic())
        self.assertEqual(rows, list(synthetic()))
        self.assertEqual(len(rows), 3200)
        self.assertEqual(audit_rows(rows)["groups"], 100)
        self.assertEqual(len({r["family"] for r in rows}), 8)
        for split, count in (("dev", 2240), ("validation", 480), ("test", 480)):
            selected = [r for r in rows if r["split"] == split]
            self.assertEqual(len(selected), count)
            self.assertEqual(sum(r["supported"] for r in selected), count // 2)
            self.assertTrue(all("supported" not in r["state"]["config"] for r in selected))

    def test_audit_rejects_group_leakage(self):
        row = next(synthetic())
        with self.assertRaises(ValueError):
            audit_rows([row, {**row, "id": "new-id", "split": "test" if row["split"] != "test" else "dev"}])

    def test_test_split_refuses_execution_without_explicit_unsealing(self):
        with patch("run_corpus_eval.model_config") as config:
            with self.assertRaises(ValueError):
                asyncio.run(run(SimpleNamespace(split="test", unseal_test=False)))
            config.assert_not_called()

    def test_metrics_keep_contract_errors_separate_and_count_them_wrong(self):
        base = {"group": "g", "model_calls": 1, "temporal_calls": 0,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        rows = [{**base, "supported": False, "correct": True, "verdict": {"verdict": "revise"}},
            {**base, "supported": True, "correct": False, "error_type": "ValueError"}]
        result = score(rows)
        self.assertEqual((result["errors"], result["accuracy_errors_wrong"], result["macro_group_accuracy"]), (1, .5, .5))
        self.assertEqual(result["false_reject_rate"], None)

    def test_fingerprint_changes_are_rejected_before_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "manifest.json").write_text(json.dumps({"files": {"synthetic-validation.jsonl": {"sha256": "wrong"}}}))
            (path / "synthetic-validation.jsonl").write_text("{}\n")
            with self.assertRaises(ValueError):
                load_rows(path, "synthetic", "validation")


if __name__ == "__main__":
    unittest.main()
