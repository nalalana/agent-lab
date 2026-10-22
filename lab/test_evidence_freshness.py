import json
import asyncio
import unittest
from bootstrap import setup
setup()
from apps.agent_runtime.response_verification import verification_input, validate_verdict, ModelResponseVerifier
from apps.agent_runtime.contracts import ModelEvent


def receipt(name="company.table.read", tid="stats", value=20, success=True, call_id="read"):
    return {"tool_call_id": call_id, "name": name, "arguments": {"tid": tid}, "success": success,
        "error_code": None if success else "TABLE_WRITE_CONFLICT",
        "content": json.dumps({"tool_name": name, "tid": tid, "data": {"rows": [["人数", value]]}})}


def evidence_input(results, draft):
    return verification_input({"config": {"permission_facts": {}, "user_text": "报告实际数值"},
        "tool_results": results}, draft)[0]


class EvidenceFreshnessTests(unittest.TestCase):
    def test_temporal_audit_rejects_first_pass_mislabel_and_preserves_usage(self):
        class Gateway:
            purposes = []
            async def stream(self, request):
                self.purposes.append(request.purpose)
                if request.purpose == "response_verification":
                    value = {"verdict": "supported", "issues": [], "claims": [{"text": "当前人数为20",
                        "kind": "historical_company_fact", "evidence": [{"source_id": "tool:read:table", "quote": "20"}]}]}
                else:
                    value = {"verdict": "revise", "issues": ["历史读取不能证明冲突后的当前人数"], "claims": []}
                yield ModelEvent("text_delta", {"delta": json.dumps(value)})
                yield ModelEvent("usage", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        gateway = Gateway()
        state = {"run_id": "r", "config": {"permission_facts": {}, "model_provider": "fake",
            "model_name": "fake", "credential_ref": "none"}, "tool_results": [receipt(),
                receipt("company.table.write", success=False, call_id="conflict")]}
        result = asyncio.run(ModelResponseVerifier(gateway).verify(state, "当前人数为20"))
        self.assertEqual(result.verdict, "revise")
        self.assertEqual(result.usage["total_tokens"], 4)
        self.assertEqual(gateway.purposes, ["response_verification", "response_temporal_verification"])

    def test_old_current_claim_is_rejected_but_historical_claim_has_evidence(self):
        results = [receipt(), receipt("company.table.write", success=False, call_id="conflict")]
        for kind, draft in (("company_fact", "当前人数为20"), ("historical_company_fact", "上次读取人数为20")):
            data = evidence_input(results, draft)
            raw = json.dumps({"verdict": "supported", "issues": [], "claims": [{"text": draft,
                "kind": kind, "evidence": [{"source_id": "tool:read:table", "quote": "20"}]}]})
            if kind == "company_fact":
                with self.assertRaises(ValueError):
                    validate_verdict(raw, data)
            else:
                self.assertEqual(validate_verdict(raw, data).verdict, "supported")

    def test_all_later_mutation_attempts_and_failed_reads_invalidate(self):
        for name in ("company.table.write", "company.table.clear_cells", "company.table.read"):
            for success in (False, True):
                data = evidence_input([receipt(), receipt(name, success=success, call_id="later")], "test")
                old = next(s for s in data["sources"] if s["id"] == "tool:read:table")
                self.assertEqual(old["kind"], "historical_table_content")
                self.assertEqual(old["invalidated_by"], "tool:later")

    def test_other_table_does_not_invalidate_and_new_read_restores_snapshot(self):
        results = [receipt(), receipt("company.table.write", tid="other", call_id="other")]
        data = evidence_input(results, "test")
        self.assertEqual(next(s for s in data["sources"] if s["id"] == "tool:read:table")["kind"], "table_content")
        results += [receipt("company.table.write", success=False, call_id="conflict"), receipt(value=21, call_id="new")]
        data = evidence_input(results, "test")
        self.assertEqual(next(s for s in data["sources"] if s["id"] == "tool:new:table")["kind"], "table_content")
        self.assertEqual(next(s for s in data["sources"] if s["id"] == "tool:read:table")["kind"], "historical_table_content")


if __name__ == "__main__":
    unittest.main()
