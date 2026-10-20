import asyncio
import json
import unittest

from bootstrap import setup
setup()
from tau_graph_worker import Engine, readonly_evidence
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
