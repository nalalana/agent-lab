import asyncio
import json
import unittest

from bootstrap import setup
setup()
from tau_graph_worker import Engine, ReadEvidenceCache, readonly_evidence
from apps.agent_runtime.contracts import ModelEvent


class ScriptedGateway:
    def __init__(self):
        self.generated = False

    async def stream(self, request):
        if request.purpose == "request_classification":
            text = json.dumps({"route": "general"})
        elif request.purpose == "response_verification":
            data = json.loads(request.messages[-1]["content"])
            source = next(s for s in data["sources"] if s["kind"] == "tool_receipt" and '"name": "write_value"' in s["content"])
            text = json.dumps({"verdict": "supported", "issues": [], "claims": [
                {"text": "已更新为20。", "kind": "operation", "evidence": [{"source_id": source["id"], "quote": '"success": true'}]}]})
        elif not self.generated:
            self.generated = True
            for name, call_id, args in (("lookup", "call-a", {}), ("write_value", "call-b", {"value": 20})):
                yield ModelEvent("tool_call_completed", {"name": name, "id": call_id, "arguments": args})
            return
        else:
            text = "已更新为20。"
        yield ModelEvent("text_delta", {"delta": text})


class GraphBridgeTests(unittest.TestCase):
    def test_historical_reads_are_facts_not_operation_receipts_and_expire(self):
        from apps.agent_runtime.response_verification import validate_verdict
        cache = ReadEvidenceCache({"lookup"})
        cache.turn = 1
        receipt = {"name": "lookup", "arguments": {"id": "a"}, "success": True,
                   "tool_call_id": "observed", "content": '{"value":12}'}
        cache.observe(receipt)
        cache.turn = 2
        cache.dispatch("lookup")
        self.assertEqual(len(cache.sources), 1)
        cache.observe({**receipt, "success": False, "content": "error"})
        source = next(iter(cache.sources.values()))
        self.assertEqual(json.loads(source["content"])["observed_turn"], 1)
        self.assertIn('"value": 12', source["content"])
        data = {"sources": [source], "draft": "The value was 12."}
        claim = {"text": data["draft"], "kind": "company_fact", "evidence": [
            {"source_id": source["id"], "quote": "12"}]}
        verdict = {"verdict": "supported", "issues": [], "claims": [claim]}
        validate_verdict(json.dumps(verdict), data)
        claim["kind"] = "operation"
        with self.assertRaises(ValueError):
            validate_verdict(json.dumps(verdict), data)
        cache.dispatch("write_value")  # Before execution, even if it later fails.
        self.assertFalse(cache.sources)
        adapted = readonly_evidence({"tool_results": [receipt]}, {"lookup"}, set())
        self.assertEqual(adapted["tool_results"], [receipt])
        cache.observe({**receipt, "name": "write_value"})
        self.assertFalse(cache.sources)
        self.assertFalse(ReadEvidenceCache({"lookup"}).sources)

    def test_real_graph_verifies_previous_turn_read_without_reexecuting_it(self):
        class Gateway:
            generated = 0
            observed_inputs = []

            async def stream(self, request):
                if request.purpose == "request_classification":
                    text = json.dumps({"route": "general"})
                elif request.purpose == "response_verification":
                    data = json.loads(request.messages[-1]["content"])
                    self.observed_inputs.append(data)
                    source = next(s for s in data["sources"] if s["id"].startswith("historical_read:"))
                    text = json.dumps({"verdict": "supported", "issues": [], "claims": [
                        {"text": "The value is 12.", "kind": "company_fact", "evidence": [
                            {"source_id": source["id"], "quote": "12"}]}]})
                elif self.generated == 0:
                    self.generated += 1
                    yield ModelEvent("tool_call_completed", {"name": "lookup", "id": "read-a", "arguments": {}})
                    return
                else:
                    text = "The value is 12."
                yield ModelEvent("text_delta", {"delta": text})

        async def run():
            gateway = Gateway()
            engine = Engine({"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {
                "type": "object", "properties": {}, "additionalProperties": False}}}],
                "read_names": ["lookup"], "policy": "Read values.", "carry_read_evidence": True},
                {"MODEL": "scripted"}, gateway)
            first = await engine.step({"kind": "user", "content": "Look up the value."})
            self.assertEqual(first["kind"], "tool")
            answer = await engine.step({"kind": "tool", "id": "read-a", "content": '{"value":12}'})
            self.assertEqual(answer["kind"], "answer")
            second = await engine.step({"kind": "user", "content": "Repeat the value."})
            self.assertEqual(second, {"kind": "answer", "content": "The value is 12."})
            self.assertEqual(len(engine.recorder.records), 1)
            sources = gateway.observed_inputs[-1]["sources"]
            self.assertFalse(any(s["kind"] == "tool_receipt" for s in sources))
            self.assertEqual(sum(s["id"].startswith("historical_read:") for s in sources), 1)
        asyncio.run(run())

    def test_model_failure_is_explicit_graph_terminal_state(self):
        class FailingGateway:
            async def stream(self, request):
                yield ModelEvent("error", {"code": "MODEL_TIMEOUT", "message": "unavailable", "retryable": True})
        async def run():
            engine = Engine({"tools": [], "read_names": [], "policy": "Read only."},
                            {"MODEL": "scripted"}, FailingGateway())
            reply = await engine.step({"kind": "user", "content": "Help"})
            self.assertEqual(reply["status"], "FAILED")
            self.assertEqual(reply["error"]["code"], "MODEL_TIMEOUT")
            self.assertFalse(engine.active)
        asyncio.run(run())

    def test_real_graph_pause_resume_and_external_receipt_validation(self):
        async def run():
            tools = [{"type": "function", "function": {"name": name, "parameters": {
                "type": "object", "properties": props, "additionalProperties": False}}}
                for name, props in (("lookup", {}), ("write_value", {"value": {"type": "integer"}}))]
            engine = Engine({"tools": tools, "read_names": ["lookup"], "policy": "Use the authorized tools."},
                            {"MODEL": "scripted"}, ScriptedGateway())
            with self.assertRaises(ValueError):
                await engine.step({"kind": "history", "messages": [{"role": "system", "content": "oracle"}]})
            await engine.step({"kind": "history", "messages": [{"role": "assistant", "content": "Hi"}]})
            first = await engine.step({"kind": "user", "content": "Set the value to 20."})
            self.assertEqual(first, {"kind": "tool", "id": "call-a", "name": "lookup", "arguments": {}})
            with self.assertRaises(ValueError):
                await engine.step({"kind": "tool", "id": "forged", "content": "{}"})
            with self.assertRaises(ValueError):
                await engine.step({"kind": "user", "content": "Skip the pending result"})
            second = await engine.step({"kind": "tool", "id": "call-a", "content": '{"value":12}'})
            self.assertEqual(second["name"], "write_value")
            self.assertEqual(second["arguments"], {"value": 20})
            final = await engine.step({"kind": "tool", "id": "call-b", "content": '{"updated":true}'})
            self.assertEqual(final, {"kind": "answer", "content": "已更新为20。"})
            self.assertEqual([r.status for r in engine.recorder.records], ["completed", "completed"])
            snapshot = (await engine.graph.aget_state(engine.checkpoint)).values
            self.assertTrue(all(event.run_id for event in snapshot["events"]))
            self.assertEqual(len(snapshot["completed_tool_call_keys"]), 2)
            history = [m for m in engine.history if m["role"] == "tool"]
            self.assertEqual([m["tool_call_id"] for m in history], ["call-a", "call-b"])
        asyncio.run(run())

    def test_failed_and_mutating_native_results_are_never_promoted_to_facts(self):
        state = {"tool_results": [
            {"name": name, "success": success, "tool_call_id": name + str(success), "content": '{"value":20}'}
            for name, success in (("lookup", True), ("lookup", False), ("write_value", True))]}
        adapted = readonly_evidence(state, {"lookup"})
        aliases = [r for r in adapted["tool_results"] if r["name"] == "company.table.read"]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(len(state["tool_results"]), 3)


if __name__ == "__main__":
    unittest.main()
