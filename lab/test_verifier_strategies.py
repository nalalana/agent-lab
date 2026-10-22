"""Verify strategy selection does not silently enable the rejected combination."""
import asyncio
import json
import unittest
from bootstrap import setup
setup()
from apps.agent_runtime.response_verification import ModelResponseVerifier, VERIFICATION_PROMPT, VERIFICATION_REPAIR_PROMPT
from test_verifier_repair import Gateway, table_state, verdict


class StrategyTests(unittest.TestCase):
    def test_modes_keep_two_call_budget_and_explicit_prompt_boundaries(self):
        for mode in ("baseline", "contract_repair", "evidence_scope", "combined"):
            with self.subTest(mode=mode):
                gateway = Gateway([("response_verification", "invalid"), ("response_verification", verdict())])
                asyncio.run(ModelResponseVerifier(gateway, strategy=mode).verify(table_state(), "An explanation."))
                first, repair = gateway.requests
                data = json.loads(first.messages[1]["content"])
                self.assertEqual("table_evidence_scope" in data, mode in {"evidence_scope", "combined"})
                self.assertEqual(repair.messages[0]["content"].startswith(VERIFICATION_REPAIR_PROMPT), mode in {"contract_repair", "combined"})
                self.assertEqual(first.messages[1], repair.messages[1])
                if mode == "baseline":
                    self.assertEqual(first.messages[0]["content"], VERIFICATION_PROMPT)
                if mode in {"baseline", "evidence_scope"}:
                    self.assertEqual(repair.messages[2], {"role": "assistant", "content": "invalid"})

    def test_default_is_scope_only_and_unknown_modes_fail_before_call(self):
        verifier = ModelResponseVerifier(None)
        self.assertEqual(verifier.strategy, "evidence_scope")
        with self.assertRaises(ValueError):
            ModelResponseVerifier(None, strategy="unreviewed")


if __name__ == "__main__":
    unittest.main()
