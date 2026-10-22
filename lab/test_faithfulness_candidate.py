"""Offline contract checks for the fixed-draft prompt ablation, not model scores."""
import asyncio
import json
import unittest

from bootstrap import setup
setup()

from apps.agent_runtime.response_verification import (
    ModelResponseVerifier, VerifierContractError, VERIFICATION_PROMPT,
    VERIFICATION_REPAIR_PROMPT, VERIFICATION_SCHEMA,
)
from apps.agent_runtime.tests.test_response_verification import state, claim, verdict
from faithfulness_candidate import FaithfulnessResponseVerifier, FAITHFULNESS_PROMPT
from test_verifier_repair import Gateway, table_state


PATH = "knowledge/reference.txt"
DRAFT = "The company launched in 2018. Source: " + PATH
VALID = claim("The company launched in 2018.", "company_fact", "tool:reference:body", "Founded in 2018.")


def file_state():
    return state("Summarize the supplied reference", tool_results=[{
        "name": "workspace.read", "tool_call_id": "reference", "success": True,
        "content": json.dumps({"tool_name": "workspace.read", "logical_path": PATH, "content": "Founded in 2018."}),
    }])


class FaithfulnessCandidateTests(unittest.TestCase):
    def test_production_policy_and_request_contract_stay_unchanged(self):
        normal_gateway = Gateway([("response_verification", verdict(claims=[VALID]))])
        candidate_gateway = Gateway([("response_verification", verdict(claims=[VALID]))])
        normal = ModelResponseVerifier(normal_gateway)
        candidate = FaithfulnessResponseVerifier(candidate_gateway)
        normal_result = asyncio.run(normal.verify(file_state(), DRAFT))
        candidate_result = asyncio.run(candidate.verify(file_state(), DRAFT))
        self.assertEqual(ModelResponseVerifier.policy_prompt, VERIFICATION_PROMPT)
        first, second = normal_gateway.requests[0], candidate_gateway.requests[0]
        self.assertEqual(first.messages[0]["content"], VERIFICATION_PROMPT)
        self.assertEqual(second.messages[0]["content"], FAITHFULNESS_PROMPT)
        self.assertEqual(first.messages[1:], second.messages[1:])
        for attribute in ("tools", "temperature", "max_output_tokens", "timeout_seconds", "response_format", "purpose"):
            self.assertEqual(getattr(first, attribute), getattr(second, attribute))
        self.assertEqual(second.response_format["json_schema"]["schema"], VERIFICATION_SCHEMA)
        self.assertEqual(candidate_result, normal_result)
        self.assertEqual(candidate.strategy, normal.strategy)

    def test_candidate_cannot_bypass_local_schema_claim_source_quote_or_path_contracts(self):
        bad_claims = [
            claim("The company launched in 2019.", "company_fact", "tool:reference:body", "Founded in 2018."),
            claim(VALID["text"], "company_fact", "invented", "Founded in 2018."),
            claim(VALID["text"], "company_fact", "tool:reference:body", "Launched in 2018."),
            claim(VALID["text"], "operation", "tool:reference:body", "Founded in 2018."),
            {**VALID, "evidence": []},
        ]
        cases = [(verdict(claims=[bad]), DRAFT) for bad in bad_claims]
        cases.extend([(verdict(claims=[VALID]), VALID["text"]),
                      (json.dumps({"verdict": "supported", "issues": [], "claims": "invalid"}), DRAFT)])
        for output, draft in cases:
            with self.subTest(output=output, draft=draft):
                gateway = Gateway([("response_verification", output)] * 2)
                with self.assertRaises(VerifierContractError):
                    asyncio.run(FaithfulnessResponseVerifier(gateway).verify(file_state(), draft))
                self.assertEqual(len(gateway.requests), 2)

    def test_user_attachment_source_cannot_be_promoted_to_company_fact(self):
        current = state()
        current["config"]["attachments"] = [{"name": "statement.txt", "extracted_text": "Founded in 2018."}]
        draft = "The attachment says the company was founded in 2018."
        for kind in ("user_fact", "company_fact"):
            with self.subTest(kind=kind):
                output = verdict(claims=[claim(draft, kind, "attachment:0", "Founded in 2018.")])
                gateway = Gateway([("response_verification", output)] * (1 if kind == "user_fact" else 2))
                if kind == "user_fact":
                    result = asyncio.run(FaithfulnessResponseVerifier(gateway).verify(current, draft))
                    self.assertEqual(result.verdict, "supported")
                else:
                    with self.assertRaises(VerifierContractError):
                        asyncio.run(FaithfulnessResponseVerifier(gateway).verify(current, draft))

    def test_historical_approval_still_runs_temporal_audit_of_the_whole_draft(self):
        draft = "Previously observed 20; the current value is unconfirmed."
        proof = claim("Previously observed 20", "historical_company_fact", "tool:read:table", "20")
        for audit in (verdict(), verdict("revise", ["The conclusion asserts a current value without current evidence"])):
            with self.subTest(audit=audit):
                gateway = Gateway([("response_verification", verdict(claims=[proof])),
                                   ("response_temporal_verification", audit)])
                result = asyncio.run(FaithfulnessResponseVerifier(gateway).verify(table_state(True), draft))
                self.assertEqual(result.verdict, json.loads(audit)["verdict"])
                self.assertEqual([r.purpose for r in gateway.requests], ["response_verification", "response_temporal_verification"])
                audit_input = json.loads(gateway.requests[-1].messages[1]["content"])
                self.assertEqual(audit_input["draft"], draft)
                self.assertTrue(any(s["kind"] == "historical_table_content" for s in audit_input["sources"]))
                self.assertEqual(result.usage["total_tokens"], 6)
        stale = verdict(claims=[claim("Currently 20", "company_fact", "tool:read:table", "20")])
        gateway = Gateway([("response_verification", stale)] * 2)
        with self.assertRaises(VerifierContractError):
            asyncio.run(FaithfulnessResponseVerifier(gateway).verify(table_state(True), "Currently 20"))
        self.assertEqual([r.purpose for r in gateway.requests], ["response_verification"] * 2)

    def test_candidate_keeps_repair_policy_and_two_call_budget_for_both_reask_modes(self):
        for mode in ("evidence_scope", "combined"):
            with self.subTest(mode=mode):
                gateway = Gateway([("response_verification", "not JSON"),
                                   ("response_verification", verdict(claims=[VALID]))])
                result = asyncio.run(FaithfulnessResponseVerifier(gateway, strategy=mode).verify(file_state(), DRAFT))
                self.assertEqual(result.verdict, "supported")
                first, repair = gateway.requests
                self.assertEqual(first.messages[0]["content"], FAITHFULNESS_PROMPT)
                self.assertEqual(repair.messages[0]["content"],
                    (VERIFICATION_REPAIR_PROMPT if mode == "combined" else "") + FAITHFULNESS_PROMPT)
                self.assertEqual(first.messages[1], repair.messages[1])
                self.assertTrue(all(r.tools == [] and r.max_output_tokens == 4096 for r in gateway.requests))
                self.assertEqual(result.usage["total_tokens"], 6)
                self.assertEqual(len(result.metrics), 2)
                gateway = Gateway([("response_verification", "not JSON")] * 2)
                with self.assertRaises(VerifierContractError):
                    asyncio.run(FaithfulnessResponseVerifier(gateway, strategy=mode).verify(file_state(), DRAFT))
                self.assertEqual(len(gateway.requests), 2)

    def test_actual_rejection_is_not_repaired_or_automatically_approved(self):
        output = verdict("revise", ["Unknown outcome cannot establish that no mutation committed"])
        gateway = Gateway([("response_verification", output)])
        result = asyncio.run(FaithfulnessResponseVerifier(gateway).verify(file_state(), "No mutation committed."))
        self.assertEqual(result.verdict, "revise")
        self.assertEqual(result.issues, ("Unknown outcome cannot establish that no mutation committed",))
        self.assertEqual(len(gateway.requests), 1)


if __name__ == "__main__":
    unittest.main()
