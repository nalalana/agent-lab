"""Scripted contract/graph checks, not a real-model effectiveness evaluation."""
import asyncio
import json
import unittest

from bootstrap import setup
setup()

from apps.agent_runtime.contracts import ModelEvent, RunRequest
from apps.agent_runtime.orchestration.graph import Phase2AgentRuntimeFacade
from apps.agent_runtime.response_verification import (
    ModelResponseVerifier, VerifierContractError, VerifierModelError,
    VERIFICATION_PROMPT, VERIFICATION_REPAIR_PROMPT, TABLE_SCOPE_GUIDANCE,
)
from apps.agent_runtime.tests.test_response_verification import claim, config, state, verdict


class Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        purpose, response = self.responses.pop(0)
        assert request.purpose == purpose, (request.purpose, purpose)
        if isinstance(response, Exception):
            raise response
        yield response if isinstance(response, ModelEvent) else ModelEvent("text_delta", {"delta": response})
        yield ModelEvent("usage", {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})
        yield ModelEvent("model_call_metrics", {"call_number": len(self.requests)})


def table_state(historical=False):
    result = state("报告人数", tool_results=[{
        "name": "company.table.read", "tool_call_id": "read", "arguments": {"tid": "stats"},
        "success": True, "content": json.dumps({"tool_name": "company.table.read", "tid": "stats", "rows": [["人数", 20]]}),
    }])
    if historical:
        result["tool_results"].append({"name": "company.table.write", "tool_call_id": "conflict",
            "arguments": {"tid": "stats"}, "success": False, "error_code": "TABLE_WRITE_CONFLICT", "content": "conflict"})
    return result


class VerifierRepairTests(unittest.TestCase):
    def verify(self, responses, draft="**人数20**。", current=None, suffix=""):
        gateway = Gateway(responses)
        result = asyncio.run(ModelResponseVerifier(gateway, prompt_suffix=suffix, strategy="combined").verify(current or table_state(), draft))
        return result, gateway

    def test_local_excerpt_source_and_quote_faults_can_be_repaired_without_changing_input(self):
        draft = "**人数20**。"
        valid = claim("**人数20**", "company_fact", "tool:read:table", "20")
        for bad in (claim("人数为20", "company_fact", "tool:read:table", "20"),
                    claim("**人数20**", "company_fact", "invented", "20"),
                    claim("**人数20**", "company_fact", "tool:read:table", "人数为20")):
            with self.subTest(bad=bad):
                result, gateway = self.verify([
                    ("response_verification", verdict(claims=[bad])),
                    ("response_verification", verdict(claims=[valid])),
                ], draft, suffix="\nExtra operation attribution policy.")
                self.assertEqual(result.verdict, "supported")
                self.assertEqual(result.claims, (valid,))
                self.assertEqual(result.usage["total_tokens"], 6)
                self.assertEqual(len(result.metrics), 2)
                first, repaired = gateway.requests
                self.assertEqual(first.messages[0]["content"], VERIFICATION_PROMPT + "\nExtra operation attribution policy.\n" + TABLE_SCOPE_GUIDANCE)
                self.assertTrue(repaired.messages[0]["content"].startswith(VERIFICATION_REPAIR_PROMPT))
                self.assertIn(first.messages[0]["content"], repaired.messages[0]["content"])
                self.assertEqual(first.messages[1], repaired.messages[1])
                feedback = json.loads(repaired.messages[2]["content"])
                self.assertEqual(feedback["contract_error"]["code"], "verifier_contract_error")
                self.assertEqual(feedback["previous_output"], verdict(claims=[bad]))

    def test_repeated_forged_approval_exhausts_two_calls_without_fallback(self):
        invalid = verdict(claims=[claim("**人数20**", "company_fact", "tool:read:table", "999")])
        gateway = Gateway([("response_verification", invalid)] * 2)
        with self.assertRaises(VerifierContractError) as failure:
            asyncio.run(ModelResponseVerifier(gateway, strategy="combined").verify(table_state(), "**人数20**。"))
        self.assertEqual(failure.exception.code, "verifier_contract_error")
        self.assertEqual(len(gateway.requests), 2)

    def test_repair_does_not_relax_source_type_contract(self):
        bad = verdict(claims=[claim("**人数20**", "operation", "tool:read:table", "20")])
        gateway = Gateway([("response_verification", bad)] * 2)
        with self.assertRaises(VerifierContractError):
            asyncio.run(ModelResponseVerifier(gateway, strategy="combined").verify(table_state(), "**人数20**。"))
        first = json.loads(gateway.requests[0].messages[1]["content"])
        second = json.loads(gateway.requests[1].messages[1]["content"])
        self.assertEqual(first["sources"], second["sources"])

    def test_actual_draft_rejection_does_not_enter_contract_repair(self):
        result, gateway = self.verify([("response_verification", verdict("revise", ["未提供文件来源"]))])
        self.assertEqual(result.verdict, "revise")
        self.assertEqual(result.issues, ("未提供文件来源",))
        self.assertEqual(len(gateway.requests), 1)

    def test_repair_can_still_reject_an_unsupported_draft(self):
        result, _ = self.verify([
            ("response_verification", "not JSON"),
            ("response_verification", verdict("revise", ["当前人数未知，来源引用不能支持该结论"])),
        ])
        self.assertEqual(result.verdict, "revise")
        self.assertEqual(result.issues, ("当前人数未知，来源引用不能支持该结论",))

    def test_transport_error_is_not_a_contract_repair_or_a_draft_rejection(self):
        gateway = Gateway([("response_verification", ModelEvent("error", {"code": "unavailable"}))])
        with self.assertRaises(VerifierModelError) as failure:
            asyncio.run(ModelResponseVerifier(gateway, strategy="combined").verify(table_state(), "人数20"))
        self.assertEqual(failure.exception.code, "verifier_model_error")
        self.assertEqual(len(gateway.requests), 1)
        timeout = TimeoutError("transport deadline")
        gateway = Gateway([("response_verification", timeout)])
        with self.assertRaises(TimeoutError) as failure:
            asyncio.run(ModelResponseVerifier(gateway, strategy="combined").verify(table_state(), "人数20"))
        self.assertIs(failure.exception, timeout)
        self.assertEqual(len(gateway.requests), 1)

    def test_tool_call_is_an_output_contract_error(self):
        gateway = Gateway([("response_verification", ModelEvent("tool_call_completed", {"name": "write"}))])
        with self.assertRaises(VerifierContractError):
            asyncio.run(ModelResponseVerifier(gateway, strategy="combined").verify(table_state(), "人数20"))
        self.assertEqual(len(gateway.requests), 1)

    def test_temporal_contract_failure_never_restarts_the_main_verifier(self):
        historical = verdict(claims=[claim("上次读取人数20", "historical_company_fact", "tool:read:table", "20")])
        for invalid in ("not JSON", verdict("permission_report"),
                        verdict(claims=[claim("上次读取人数20", "historical_company_fact", "tool:read:table", "20")])):
            with self.subTest(invalid=invalid):
                gateway = Gateway([("response_verification", historical), ("response_temporal_verification", invalid)])
                with self.assertRaises(VerifierContractError) as failure:
                    asyncio.run(ModelResponseVerifier(gateway, strategy="combined").verify(table_state(True), "上次读取人数20"))
                self.assertIn("Temporal audit", str(failure.exception))
                self.assertEqual([r.purpose for r in gateway.requests], ["response_verification", "response_temporal_verification"])

    def test_repaired_historical_approval_still_requires_temporal_audit(self):
        historical = verdict(claims=[claim("当前人数20", "historical_company_fact", "tool:read:table", "20")])
        result, gateway = self.verify([
            ("response_verification", "not JSON"),
            ("response_verification", historical),
            ("response_temporal_verification", verdict("revise", ["历史读取不能证明当前值"])),
        ], "当前人数20", table_state(True))
        self.assertEqual(result.verdict, "revise")
        self.assertEqual(result.usage["total_tokens"], 9)
        self.assertEqual(len(result.metrics), 3)
        self.assertEqual([r.purpose for r in gateway.requests], ["response_verification"] * 2 + ["response_temporal_verification"])

    def test_permission_replacement_and_image_inputs_survive_repair(self):
        current = state()
        current["config"]["attachments"] = [{"name": "ref.png", "mime_type": "image/png", "data_url": "data:image/png;base64,AA=="}]
        result, gateway = self.verify([
            ("response_verification", "not JSON"),
            ("response_verification", verdict("knowledge_permission_report")),
        ], "权限草稿", current)
        self.assertEqual(result.verdict, "knowledge_permission_report")
        self.assertEqual(gateway.requests[0].messages[1], gateway.requests[1].messages[1])
        self.assertEqual(gateway.requests[1].messages[1]["content"][-1]["type"], "image_url")

    def graph(self, responses):
        gateway = Gateway(responses)
        question = "解释知识库"
        context = {"config": config(question), "model_messages": [{"role": "user", "content": question}]}
        facade = Phase2AgentRuntimeFacade(gateway, response_verifier=ModelResponseVerifier(gateway, strategy="combined"), context_resolver=lambda _: context)
        request = RunRequest("repair-test", "company", "agent", "user", "session", "agent_test", "message", "zh-CN", "trace")

        async def collect():
            return [event async for event in facade.stream(request)]
        return asyncio.run(collect()), gateway

    def test_graph_contract_repair_preserves_draft_without_consuming_a_draft_revision(self):
        draft = "知识库用于整理资料。"
        events, gateway = self.graph([
            ("response_generation", draft),
            ("response_verification", "not JSON"),
            ("response_verification", verdict()),
        ])
        output = "".join(e.data.get("delta", "") for e in events if str(e.event_type) == "message.delta")
        self.assertEqual(output, draft)
        self.assertNotIn("response.retry_required", [str(e.event_type) for e in events])
        self.assertIn("run.completed", [str(e.event_type) for e in events])
        self.assertEqual(len(gateway.requests), 3)

    def test_graph_exhausted_contract_keeps_draft_private_and_does_not_rewrite_it(self):
        events, gateway = self.graph([
            ("response_generation", "未核验的草稿"),
            ("response_verification", "not JSON"),
            ("response_verification", "still not JSON"),
        ])
        types = [str(e.event_type) for e in events]
        self.assertIn("run.failed", types)
        self.assertNotIn("message.delta", types)
        self.assertNotIn("message.completed", types)
        self.assertNotIn("response.retry_required", types)
        self.assertEqual(len(gateway.requests), 3)

    def test_graph_actual_draft_rejection_still_uses_normal_correction_path(self):
        events, gateway = self.graph([
            ("response_generation", "有误的概念说明"),
            ("response_verification", verdict("revise", ["请修正概念定义"])),
            ("response_generation", "知识库用于整理资料。"),
            ("response_verification", verdict()),
        ])
        types = [str(e.event_type) for e in events]
        self.assertIn("response.retry_required", types)
        self.assertIn("run.completed", types)
        self.assertEqual([r.purpose for r in gateway.requests], ["response_generation", "response_verification"] * 2)


if __name__ == "__main__":
    unittest.main()
