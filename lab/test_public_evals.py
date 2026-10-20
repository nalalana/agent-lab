import asyncio
import json
import tempfile
from pathlib import Path
import unittest

from bootstrap import setup
from run_ragtruth import metrics, select_samples, verifier_input


class PublicEvaluationTests(unittest.TestCase):
    def test_adapter_never_exposes_gold_and_preserves_original_response(self):
        source = {"source_id": "42", "task_type": "QA", "source_info": {"question": "When?", "passages": "Founded in 2018."}}
        response = {"response": "Founded in 2018.", "labels": [{"text": "GOLD_MARKER"}], "model": "HIDDEN_MODEL"}
        state, draft = verifier_input(source, response)
        self.assertTrue(draft.startswith(response["response"] + "\n\nSource: "))
        self.assertNotIn("GOLD_MARKER", json.dumps(state) + draft)
        self.assertNotIn("HIDDEN_MODEL", json.dumps(state) + draft)
        self.assertNotIn("labels", json.dumps(state))
        setup()
        from apps.agent_runtime.contracts import ModelEvent
        from apps.agent_runtime.response_verification import ModelResponseVerifier
        state["config"].update(model_provider="fake", model_name="fake", credential_ref="fake")

        class Gateway:
            async def stream(self, request):
                yield ModelEvent("text_delta", {"delta": json.dumps({"verdict": "supported", "issues": [],
                    "claims": [{"text": "Founded in 2018.", "kind": "company_fact", "evidence": [
                        {"source_id": "tool:reference:body", "quote": "Founded in 2018."}]}]})})
        verdict = asyncio.run(ModelResponseVerifier(Gateway()).verify(state, draft))
        self.assertEqual(verdict.verdict, "supported")

    def test_confusion_matrix_keeps_errors_out_of_predictions(self):
        records = [{"gold_hallucination": gold, "predicted_hallucination": prediction}
                   for gold, prediction in [(True, True), (False, True), (True, False), (False, False), (True, None)]]
        result = metrics(records)
        self.assertEqual([result[k] for k in ("tp", "fp", "fn", "tn", "errors")], [1, 1, 1, 1, 1])
        self.assertEqual(result["f1"], .5)
        self.assertEqual(result["accuracy_errors_counted_wrong"], .4)
        self.assertIsNone(metrics([])["f1"])

    def test_selection_is_deterministic_test_only_and_source_disjoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dataset").mkdir()
            sources = [{"source_id": str(i), "task_type": "QA"} for i in range(10)]
            responses = [{"id": str(i), "source_id": str(i // 2), "split": "test", "quality": "good", "labels": [] if i % 2 else [{}]} for i in range(20)]
            responses += [{"id": "train", "source_id": "0", "split": "train", "quality": "good", "labels": []}]
            for name, values in (("source_info.jsonl", sources), ("response.jsonl", responses)):
                (root / "dataset" / name).write_text("\n".join(map(json.dumps, values)), encoding="utf-8")
            first, second = select_samples(root, 6), select_samples(root, 6)
            self.assertEqual(first, second)
            self.assertEqual(len({s["source_id"] for s, _ in first}), 6)
            self.assertTrue(all(r["split"] == "test" for _, r in first))
            with self.assertRaises(ValueError):
                select_samples(root, 11)


if __name__ == "__main__":
    unittest.main()
