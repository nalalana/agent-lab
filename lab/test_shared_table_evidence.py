import json
import unittest
from bootstrap import setup
setup()
from apps.agent_runtime.table_evidence import table_observations
from apps.agent_runtime.orchestration.tool_nodes import _table_read_version
from apps.agent_runtime.response_verification import verification_input


def read(call_id="r", tid="table-a", version=0, **fields):
    return {"tool_call_id": call_id, "name": "company.table.read", "arguments": {"tid": tid}, "success": True,
        "content": json.dumps({"tool_name": "company.table.read", "tid": tid.strip(),
            "data": {"table_version": version, "rows": [["value", 20]]}}), **fields}


def sources(results):
    data, _ = verification_input({"config": {"permission_facts": {}}, "tool_results": results}, "report")
    return {s["id"]: s for s in data["sources"] if s["kind"] in {"table_content", "historical_table_content"}}


class SharedTableEvidenceTests(unittest.TestCase):
    def test_error_and_unconfirmed_read_cannot_supply_facts_or_write_version(self):
        for invalid in (read(error_code="TOOL_TIMEOUT"), read(success=1), read(execution_status="unconfirmed"),
                read(execution_status="not_dispatched"), read(business_outcome="rejected"),
                read(content=json.dumps({"tool_name": "company.table.read", "tid": "table-a",
                    "success": False, "data": {"table_version": 7, "rows": [[20]]}}))):
            self.assertIsNone(_table_read_version([invalid], "table-a"))
            self.assertFalse(sources([invalid]))

    def test_whitespace_identity_is_normalized_for_both_consumers(self):
        results = [read(tid=" table-a ")]
        self.assertEqual(_table_read_version(results, "table-a"), 0)
        self.assertEqual(sources(results)["tool:r:table"]["tid"], "table-a")

    def test_legacy_content_can_prove_read_facts_but_cannot_authorize_writing(self):
        legacy = read(content=json.dumps({"tid": "table-a", "rows": [[20]]}))
        self.assertEqual(sources([legacy])["tool:r:table"]["kind"], "table_content")
        self.assertIsNone(_table_read_version([legacy], "table-a"))
        for version in (True, -1, "1", None):
            self.assertIsNone(_table_read_version([read(version=version)], "table-a"))

    def test_failed_refresh_blocks_fallback_but_preserves_history(self):
        for failed in (read("failed", success=False), read("failed", content="invalid JSON"), read("failed", error_code="TOOL_TIMEOUT")):
            results = [read(), failed]
            self.assertIsNone(_table_read_version(results, "table-a"))
            self.assertEqual(sources(results)["tool:r:table"]["kind"], "historical_table_content")
            self.assertEqual(table_observations(results)[0]["invalidated_by"], "tool:failed")

    def test_mutations_invalidate_only_the_target_then_fresh_read_restores_both(self):
        for name in ("company.table.write", "company.table.clear_cells"):
            for success in (True, False):
                results = [read(), read("other", "table-b", 5), {"tool_call_id": "mutate", "name": name,
                    "arguments": {"tid": "table-a"}, "success": success, "content": "{}"}]
                self.assertIsNone(_table_read_version(results, "table-a"))
                self.assertEqual(_table_read_version(results, "table-b"), 5)
                self.assertEqual(sources(results)["tool:r:table"]["kind"], "historical_table_content")
                results.append(read("fresh", version=1))
                self.assertEqual(_table_read_version(results, "table-a"), 1)
                self.assertEqual(sources(results)["tool:fresh:table"]["kind"], "table_content")
                self.assertEqual(sources(results)["tool:other:table"]["kind"], "table_content")


if __name__ == "__main__":
    unittest.main()
