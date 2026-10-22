"""Scripted contract checks for a prompt-only candidate, not a model evaluation."""
import asyncio
import json
import unittest

from bootstrap import setup
setup()

from apps.agent_runtime.response_verification import (
    ModelResponseVerifier, VerifierContractError, VERIFICATION_PROMPT,
    TABLE_SCOPE_GUIDANCE, VERIFICATION_SCHEMA, validate_verdict,
)
from short_quotes import SHORT_QUOTE_GUIDANCE, ShortQuoteVerifier
from test_verifier_repair import Gateway, claim, state, table_state, verdict


class ShortQuoteTests(unittest.TestCase):
    def test_full_policy_caller_suffix_schema_and_source_input_are_preserved(self):
        response = verdict(claims=[claim("人数20", "company_fact", "tool:read:table", "20")])
        original, candidate = Gateway([("response_verification", response)]), Gateway([("response_verification", response)])
        suffix = "\nCaller-specific attribution rule."
        for verifier, gateway in ((ModelResponseVerifier, original), (ShortQuoteVerifier, candidate)):
            result = asyncio.run(verifier(gateway, prompt_suffix=suffix).verify(table_state(), "人数20"))
            self.assertEqual(result.verdict, "supported")
        before, after = original.requests[0], candidate.requests[0]
        self.assertEqual(after.messages[0]["content"],
                         VERIFICATION_PROMPT + suffix + "\n" + SHORT_QUOTE_GUIDANCE + "\n" + TABLE_SCOPE_GUIDANCE)
        self.assertEqual(before.messages[1:], after.messages[1:])
        self.assertEqual(before.response_format, after.response_format)
        self.assertEqual(after.response_format["json_schema"]["schema"], VERIFICATION_SCHEMA)
        self.assertEqual(before.max_output_tokens, after.max_output_tokens)
        self.assertEqual(before.timeout_seconds, after.timeout_seconds)
        self.assertEqual(ShortQuoteVerifier(None).strategy, "evidence_scope")
        for strategy in ("baseline", "contract_repair", "combined"):
            self.assertEqual(ShortQuoteVerifier(None, strategy=strategy).strategy, strategy)

    def test_json_quotes_remain_literal_without_decoding_or_normalization(self):
        path = "knowledge/record.json"
        review = 'Reported: "maybe"\nPath C:\\notes; uncertainty remains.'
        body = json.dumps({"nested": [{"review": review}]}, ensure_ascii=False)
        current = state("报告记录", tool_results=[{"name": "workspace.read", "tool_call_id": "read",
            "success": True, "content": json.dumps({"tool_name": "workspace.read", "logical_path": path,
                "content": body, "has_extractable_text": True})}])
        draft = "记录包含带不确定性的报告。 Source: " + path
        data, _ = ShortQuoteVerifier.build_input(current, draft)
        literal = json.dumps(review, ensure_ascii=False)[1:-1]
        good = verdict(claims=[claim("记录包含带不确定性的报告", "company_fact", "tool:read:body", literal)])
        self.assertEqual(validate_verdict(good, data).claims[0]["evidence"][0]["quote"], literal)
        decoded = verdict(claims=[claim("记录包含带不确定性的报告", "company_fact", "tool:read:body", review)])
        gateway = Gateway([("response_verification", decoded)] * 2)
        with self.assertRaises(VerifierContractError):
            asyncio.run(ShortQuoteVerifier(gateway).verify(current, draft))
        self.assertEqual(len(gateway.requests), 2)
        self.assertEqual(gateway.requests[0].messages[1], gateway.requests[1].messages[1])

    def test_forged_quotes_sources_and_wrong_kinds_still_fail_with_two_calls(self):
        for source, kind, quote in (("tool:read:table", "company_fact", "999"),
                                    ("invented", "company_fact", "20"),
                                    ("tool:read:table", "operation", "20")):
            with self.subTest(source=source, kind=kind, quote=quote):
                bad = verdict(claims=[claim("人数20", kind, source, quote)])
                gateway = Gateway([("response_verification", bad)] * 2)
                with self.assertRaises(VerifierContractError):
                    asyncio.run(ShortQuoteVerifier(gateway).verify(table_state(), "人数20"))
                self.assertEqual(len(gateway.requests), 2)
                self.assertEqual(gateway.requests[0].response_format, gateway.requests[1].response_format)

    def test_historical_approval_still_runs_bounded_temporal_audit(self):
        approval = verdict(claims=[claim("当前人数20", "historical_company_fact", "tool:read:table", "20")])
        for audit in (verdict("revise", ["历史读取不能证明当前值"]), "not JSON"):
            with self.subTest(audit=audit):
                gateway = Gateway([("response_verification", "not JSON"),
                    ("response_verification", approval), ("response_temporal_verification", audit)])
                if audit == "not JSON":
                    with self.assertRaisesRegex(VerifierContractError, "Temporal audit"):
                        asyncio.run(ShortQuoteVerifier(gateway).verify(table_state(True), "当前人数20"))
                else:
                    result = asyncio.run(ShortQuoteVerifier(gateway).verify(table_state(True), "当前人数20"))
                    self.assertEqual(result.verdict, "revise")
                    self.assertEqual(result.issues, ("历史读取不能证明当前值",))
                    self.assertEqual(result.usage["total_tokens"], 9)
                self.assertEqual([r.purpose for r in gateway.requests],
                    ["response_verification", "response_verification", "response_temporal_verification"])
                self.assertEqual(gateway.requests[-1].max_output_tokens, 512)

    def test_rejection_and_permission_routes_are_not_automatically_supported(self):
        for response in (verdict("revise", ["未获充分证据"]), verdict("knowledge_permission_report")):
            with self.subTest(response=response):
                gateway = Gateway([("response_verification", response)])
                result = asyncio.run(ShortQuoteVerifier(gateway).verify(table_state(), "待核验草稿"))
                self.assertEqual(result.verdict, json.loads(response)["verdict"])
                self.assertEqual(len(gateway.requests), 1)


if __name__ == "__main__":
    unittest.main()
