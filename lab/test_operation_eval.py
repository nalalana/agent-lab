import asyncio
import json
import unittest

from bootstrap import setup
setup()
from apps.agent_runtime.contracts import ModelEvent
from apps.agent_runtime.response_verification import ModelResponseVerifier, VERIFICATION_PROMPT, verification_input
from run_operation_eval import CASES, SUFFIX, case_state


class AttributionTests(unittest.TestCase):
    def test_suffix_is_explicit_and_preserves_receipt_type_contract(self):
        case = json.loads(CASES.read_text(encoding="utf-8").splitlines()[0])
        state = case_state(case, {"MODEL": "scripted"})

        class Gateway:
            async def stream(self, request):
                self.request = request
                yield ModelEvent("text_delta", {"delta": json.dumps({"verdict": "supported", "issues": [],
                    "claims": [{"text": case["draft"], "kind": self.kind,
                        "evidence": [{"source_id": "tool:write-1", "quote": '"status": "exchanged"'}]}]})})

        async def run():
            gateway = Gateway()
            gateway.kind = "operation"
            await ModelResponseVerifier(gateway).verify(state, case["draft"])
            self.assertEqual(gateway.request.messages[0]["content"], VERIFICATION_PROMPT)
            suffix = SUFFIX.read_text(encoding="utf-8")
            candidate = ModelResponseVerifier(gateway, prompt_suffix=suffix)
            await candidate.verify(state, case["draft"])
            self.assertEqual(gateway.request.messages[0]["content"], VERIFICATION_PROMPT + suffix)
            gateway.kind = "company_fact"
            with self.assertRaises(ValueError):
                await candidate.verify(state, case["draft"])
        asyncio.run(run())

    def test_diagnostic_labels_never_enter_verifier_sources(self):
        cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for case in cases:
            data, _ = verification_input(case_state(case, {"MODEL": "scripted"}), case["draft"])
            self.assertNotIn("expected_supported", json.dumps(data))
            self.assertNotIn("supported", data)
            self.assertFalse(any(s["kind"] == "table_content" for s in data["sources"] if s["id"] == "tool:write-1"))


if __name__ == "__main__":
    unittest.main()
