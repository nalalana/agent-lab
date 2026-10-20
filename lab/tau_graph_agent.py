"""Tau participant adapter; hidden task/evaluator information never crosses IPC."""
import json
import os
from pathlib import Path
import subprocess

from bootstrap import ROOT
from tau2.agent.base_agent import HalfDuplexAgent, AgentError
from tau2.data_model.message import AssistantMessage, ToolCall, MultiToolMessage, UserMessage


class GraphAgent(HalfDuplexAgent[dict]):
    def __init__(self, tools, domain_policy):
        super().__init__(tools, domain_policy)
        executable = os.environ.get("AGENT_LAB_BACKEND_PYTHON")
        if not executable or not Path(executable).is_file():
            raise ValueError("Set AGENT_LAB_BACKEND_PYTHON to the backend virtualenv Python executable")
        self.process = subprocess.Popen([executable, "-u", str(ROOT / "lab/tau_graph_worker.py")],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        read_names = [t.name for t in tools if getattr(t._func, "__tool_type__", None) == "read"
                      and not getattr(t._func, "__mutates_state__", True)]
        try:
            reply = self.exchange({"kind": "init", "policy": domain_policy,
                "tools": [t.openai_schema for t in tools], "read_names": read_names,
                "trace_dir": os.environ.get("AGENT_LAB_TAU_TRACE_DIR")})
        except Exception:
            self.stop()
            raise
        if reply["kind"] != "ready":
            self.stop()
            raise AgentError("Production graph worker failed initialization")

    def exchange(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise AgentError("Graph worker terminated without response")
        reply = json.loads(line)
        if reply["kind"] == "error":
            raise AgentError("Graph worker error: " + json.dumps(reply))
        return reply

    def get_init_state(self, message_history=None):
        if message_history:
            from tau2.agent.base_agent import is_valid_agent_history_message
            from tau2.utils.llm_utils import to_litellm_messages
            if not all(is_valid_agent_history_message(message) for message in message_history):
                raise ValueError("Invalid native visible history")
            self.exchange({"kind": "history", "messages": to_litellm_messages(message_history)})
        return {}

    def generate_next_message(self, message, state):
        if isinstance(message, UserMessage):
            reply = self.exchange({"kind": "user", "content": message.content})
        else:
            messages = message.tool_messages if isinstance(message, MultiToolMessage) else [message]
            if len(messages) != 1:
                raise AgentError("Expected one pending native tool result")
            tool = messages[0]
            reply = self.exchange({"kind": "tool", "id": tool.id, "content": tool.content,
                "error": tool.error, "execution_status": tool.execution_status})
        if reply["kind"] == "tool":
            answer = AssistantMessage.text("", tool_calls=[ToolCall(id=reply["id"], name=reply["name"], arguments=reply["arguments"])])
        else:
            answer = AssistantMessage.text(reply["content"])
        return answer, state

    def stop(self, message=None, state=None):
        process = getattr(self, "process", None)
        if process and process.poll() is None:
            try:
                process.stdin.write('{"kind":"stop"}\n')
                process.stdin.flush()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=3)


def create_graph_agent(tools, domain_policy, **_ignored):
    return GraphAgent(tools, domain_policy)
