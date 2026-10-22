import copy
import json
import unittest
from prepare_verifier_followup import diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_frozen_pairs_no_gold_in_model_state_and_group_isolation(self):
        rows = list(diagnostics())
        self.assertEqual(rows, list(diagnostics()))
        self.assertEqual(len(rows), 32)
        for split in ("dev", "validation"):
            selected = [r for r in rows if r["split"] == split]
            self.assertEqual((len(selected), sum(r["supported"] for r in selected)), (16, 8))
        groups = [{r["group"] for r in rows if r["split"] == split} for split in ("dev", "validation")]
        self.assertFalse(groups[0] & groups[1])
        for row in rows:
            self.assertNotIn("supported", row["state"])
            self.assertNotIn("supported", row["state"]["config"])
        for family in {r["family"] for r in rows}:
            pair = [r for r in rows if r["family"] == family and r["language"] == "en"]
            states = [copy.deepcopy(r["state"]) for r in pair]
            for state in states:
                state.pop("run_id")
            self.assertEqual(states[0], states[1])
            self.assertNotEqual(pair[0]["draft"], pair[1]["draft"])


if __name__ == "__main__":
    unittest.main()
