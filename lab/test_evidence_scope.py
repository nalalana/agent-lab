import copy
import json
import unittest
from bootstrap import setup
setup()
from apps.agent_runtime.response_verification import verification_input, validate_verdict
from freshness_cases_v2 import read, failure


class ScopeTests(unittest.TestCase):
    def test_scope_follows_shared_lifecycle_and_keeps_sources_unchanged(self):
        results = [read("old", "a", "stock", 4), failure("conflict", "a"), read("other", "b", "budget", 8)]
        state = {"config": {"permission_facts": {}}, "tool_results": results}
        original = copy.deepcopy(state)
        data, _ = verification_input(state, "Previously 4; current value unknown; budget read 8.")
        self.assertEqual(state, original)
        a, b = data["table_evidence_scope"]["a"], data["table_evidence_scope"]["b"]
        self.assertEqual((a["current_values"], a["historical_sources"], a["observed_source"]),
            ("unconfirmed", ["tool:old:table"], None))
        self.assertEqual(b["observed_source"], "tool:other:table")
        quote = next(s["content"] for s in data["sources"] if s["id"] == "tool:old:table")
        self.assertEqual(json.loads(quote), json.loads(results[0]["content"]))
        invalid = {"verdict": "supported", "issues": [], "claims": [{"text": "Previously 4", "kind": "company_fact",
            "evidence": [{"source_id": "tool:old:table", "quote": "4"}]}]}
        with self.assertRaises(ValueError):
            validate_verdict(json.dumps(invalid), data)
        results.append(read("new", "a", "stock", 6))
        data, _ = verification_input(state, "Latest read 6")
        self.assertEqual(data["table_evidence_scope"]["a"]["observed_source"], "tool:new:table")

    def test_non_table_inputs_get_no_extra_scope(self):
        data, _ = verification_input({"config": {"permission_facts": {}}}, "Hello")
        self.assertNotIn("table_evidence_scope", data)


if __name__ == "__main__":
    unittest.main()
