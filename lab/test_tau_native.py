"""Run with the isolated tau2 Python; no model calls or worker processes."""
import unittest
from unittest.mock import patch

from tau_graph_agent import GraphAgent, create_graph_agent
from operation_outcome import UNKNOWN_CODE, UNKNOWN_MESSAGE
from tau2.data_model.message import AssistantMessage, UserMessage


class NativeBoundaryTests(unittest.TestCase):
    def test_graph_failure_is_explicit_terminal_message_not_scoring_exception(self):
        agent = object.__new__(GraphAgent)
        agent.exchange = lambda _: {"kind": "error", "status": "FAILED", "error": {"stage": "verify_response"}}
        message, _ = agent.generate_next_message(UserMessage(role="user", content="Help"), {})
        self.assertTrue(agent.is_stop(message))
        self.assertEqual(message.raw_data["graph_runtime_failure"]["status"], "FAILED")
        self.assertNotIn("success", message.content.lower())

    def test_unknown_outcome_uses_fixed_uncertainty_text_and_stops(self):
        agent = object.__new__(GraphAgent)
        agent.exchange = lambda _: {"kind": "error", "status": "FAILED", "execution_outcome": "unknown",
                                    "error": {"code": UNKNOWN_CODE, "message": "do not trust arbitrary content"}}
        message, _ = agent.generate_next_message(UserMessage(role="user", content="Retry"), {})
        self.assertEqual(message.content, UNKNOWN_MESSAGE)
        self.assertTrue(agent.is_stop(message))
        self.assertEqual(message.raw_data["graph_runtime_failure"]["execution_outcome"], "unknown")

    def test_tool_request_remains_real_tool_call_and_visible_history_is_preserved(self):
        agent = object.__new__(GraphAgent)
        commands = []
        def exchange(command):
            commands.append(command)
            return {"kind": "tool", "id": "c", "name": "lookup", "arguments": {"id": "12"}}
        agent.exchange = exchange
        agent.get_init_state([AssistantMessage.text("Hi")])
        self.assertEqual(commands[0]["kind"], "history")
        message, _ = agent.generate_next_message(UserMessage(role="user", content="Look up"), {})
        self.assertEqual(message.tool_calls[0].name, "lookup")
        self.assertFalse(agent.is_stop(message))

    def test_factory_drops_hidden_task_and_other_benchmark_internals(self):
        with patch("tau_graph_agent.GraphAgent") as constructor:
            create_graph_agent([], "visible policy", task={"hidden_answer": "oracle"}, evaluator_state="private")
            constructor.assert_called_once_with([], "visible policy")


if __name__ == "__main__":
    unittest.main()
