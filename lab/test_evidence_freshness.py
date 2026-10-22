import json
import unittest
from bootstrap import setup
setup()
from apps.agent_runtime.response_verification import verification_input, validate_verdict


def receipt(name="company.table.read", tid="stats", value=20, success=True, call_id="read"):
    return {"tool_call_id": call_id, "name": name, "arguments": {"tid": tid}, "success": success,
        "error_code": None if success else "TABLE_WRITE_CONFLICT",
        "content": json.dumps({"tool_name": name, "tid": tid, "data": {"rows": [["人数", value]]}})}


def evidence_input(results, draft):
    return verification_input({"config": {"permission_facts": {}, "user_text": "报告实际数值"},
        "tool_results": results}, draft)[0]


class EvidenceFreshnessTests(unittest.TestCase):
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
