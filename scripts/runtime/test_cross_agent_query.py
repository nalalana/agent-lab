"""Authorization tests for the CEO-only cross-Agent Viking query broker."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import shared_host_bridge as bridge  # noqa: E402


class _Policy:
    knowledge_reads = ("knowledge/procurement.md",)

    def allows_memory(self, _action):
        return True

    def allows_tool(self, _tool):
        return False


class CrossAgentQueryTests(unittest.TestCase):
    def test_only_main_agent_receives_native_query_tool(self):
        runtime_state = {"effective_policy": _Policy()}
        ceo_tools = bridge._native_workspace_tool_policy(
            runtime_state,
            {"role": "main_agent"},
        )
        procurement_tools = bridge._native_workspace_tool_policy(
            runtime_state,
            {"role": "procurement"},
        )

        self.assertIn(bridge.CROSS_AGENT_QUERY_TOOL, ceo_tools["allow"])
        self.assertNotIn(bridge.CROSS_AGENT_QUERY_TOOL, procurement_tools["allow"])

    @patch.object(bridge, "_active_platform_agents")
    def test_target_is_resolved_from_same_company_config(self, active_agents):
        active_agents.return_value = [
            {"agent_id": "main-agent", "name": "CEO Agent", "role": "main_agent"},
            {"agent_id": "proc-agent", "name": "采购 Agent", "role": "procurement"},
        ]

        target = bridge._resolve_cross_agent_target("company-a", "采购agent")

        self.assertEqual(target["agent_id"], "proc-agent")
        active_agents.assert_called_once_with("company-a")

    @patch.object(bridge, "_active_platform_agents")
    @patch.object(bridge, "resolve_runtime_actor")
    def test_non_ceo_runtime_actor_is_denied(self, runtime_actor, active_agents):
        runtime_actor.return_value = "proc-agent"
        active_agents.return_value = [
            {
                "agent_id": "proc-agent",
                "name": "采购 Agent",
                "role": "procurement",
                "enabled": True,
                "status": "active",
            }
        ]

        result = bridge._execute_agent_query(
            "company-a",
            {
                "tool": bridge.CROSS_AGENT_QUERY_TOOL,
                "session_key": "agent:proc-agent:main",
                "arguments": {"target_agent": "CEO Agent", "query": "secret"},
            },
        )

        self.assertEqual(result["status"], "denied")
        self.assertIn("Only the company CEO", result["message"])


if __name__ == "__main__":
    unittest.main()
