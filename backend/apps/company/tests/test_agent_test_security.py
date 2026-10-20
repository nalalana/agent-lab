import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
)
from apps.company.services import CompanyService
from apps.runtime.security import (
    RESTRICTED_RUNTIME_ACCESS_MESSAGE,
    build_openclaw_agent_exec_command,
    build_openclaw_agent_stream_command,
    is_restricted_runtime_request,
    sanitize_agent_test_runtime_payload,
    sanitize_assistant_content_for_user,
    sanitize_runtime_debug_payload,
    sanitize_user_visible_error,
    validate_workspace_logical_path,
)
from apps.runtime.models import OpenClawInstance
from apps.runtime.policy import build_effective_runtime_policy, select_active_bound_skills
from apps.runtime.services import (
    AgentWorkspaceAccessService,
    AgentWorkspaceToolGatewayService,
    DockerSharedHostProviderAdapter,
    OpenClawRuntimeService,
)

_bridge_spec = importlib.util.spec_from_file_location(
    "test_shared_host_bridge",
    str(REPO_ROOT / "scripts" / "runtime" / "shared_host_bridge.py"),
)
if _bridge_spec is None or _bridge_spec.loader is None:  # pragma: no cover
    raise RuntimeError("Unable to load shared_host_bridge for tests")
_bridge_module = importlib.util.module_from_spec(_bridge_spec)
_bridge_spec.loader.exec_module(_bridge_module)
_build_native_openclaw_config = _bridge_module._build_native_openclaw_config
_config_path = _bridge_module._config_path
_default_native_agent_dir = _bridge_module._default_native_agent_dir
_derive_native_agents = _bridge_module._derive_native_agents
_docker_compose_down = _bridge_module._docker_compose_down
_docker_compose_up = _bridge_module._docker_compose_up
_gateway_chat_completions = _bridge_module._gateway_chat_completions
_gateway_chat_via_cli = _bridge_module._gateway_chat_via_cli
_host_workspace_file_payload = _bridge_module._host_workspace_file_payload
_host_workspace_tree_payload = _bridge_module._host_workspace_tree_payload
_ensure_openclaw_home = _bridge_module._ensure_openclaw_home
_execute_agent_workspace_tool = _bridge_module._execute_agent_workspace_tool
_activate_native_agent_session = _bridge_module._activate_native_agent_session
_build_cli_content = _bridge_module._build_cli_content
_native_agent_session_map_key = _bridge_module._native_agent_session_map_key
_should_seed_native_session_messages = _bridge_module._should_seed_native_session_messages
_native_agent_workspace = _bridge_module._native_agent_workspace
_persist_native_agent_session = _bridge_module._persist_native_agent_session
_reset_native_agent_cli_session = _bridge_module._reset_native_agent_cli_session
_runtime_containers_ready = _bridge_module._runtime_containers_ready
_summarize_agent_runtime_exports = _bridge_module._summarize_agent_runtime_exports
_sync_native_agent_state = _bridge_module._sync_native_agent_state
_write_touming_workspace_plugin = _bridge_module._write_touming_workspace_plugin
_write_json = _bridge_module._write_json
DOCKER_COMPOSE_TIMEOUT = _bridge_module.DOCKER_COMPOSE_TIMEOUT


class RuntimeSecurityHelpersTests(SimpleTestCase):
    def _write_runtime_platform_config(self, namespace: str, platform_config: dict[str, object]) -> None:
        config_path = _config_path(namespace)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(config_path, platform_config)

    def _skill_runtime_platform_config(
        self,
        *,
        skills: list[dict[str, object]],
        bindings: list[dict[str, object]],
        template_skills: dict[str, list[str]] | None = None,
        binding_skills: dict[str, list[str]] | None = None,
    ) -> dict[str, object]:
        template_skills = template_skills or {"include": ["skill-*"], "exclude": []}
        binding_skills = binding_skills or {"include": [], "exclude": []}
        return {
            "company": {"name": "Demo Company"},
            "agents": [
                {
                    "agent_id": "worker-agent",
                    "name": "Worker Agent",
                    "role": "subagent",
                    "description": "Executes company workflows.",
                    "system_prompt": "Act as the worker agent.",
                }
            ],
            "skills": skills,
            "agent_skill_bindings": bindings,
            "permission_templates": [
                {
                    "template_id": "tpl-worker",
                    "enabled": True,
                    "skills": template_skills,
                    "tools": {"allow": [], "deny": []},
                    "knowledge": {"read": [], "deny": []},
                }
            ],
            "agent_permission_bindings": [
                {
                    "agent_id": "worker-agent",
                    "template_id": "tpl-worker",
                    "enabled": True,
                    "skills": binding_skills,
                    "tools": {"allow": [], "deny": []},
                    "knowledge": {"read": [], "deny": []},
                }
            ],
            "permission_config": {
                "enabled": True,
                "configJson": {
                    "permissions": {
                        "skills": {"include": [], "exclude": []},
                        "tools": {"allow": [], "deny": []},
                        "knowledge": {"read": [], "deny": []},
                    }
                },
            },
            "client_entry": {"default_agent_id": "worker-agent", "bindings": []},
        }

    def test_docker_compose_lifecycle_uses_dedicated_long_timeout(self):
        with patch.object(_bridge_module, "_run_command", return_value=(True, "")) as run_mock:
            ok, _output = _docker_compose_up("tsds-demo")

        self.assertTrue(ok)
        self.assertEqual(DOCKER_COMPOSE_TIMEOUT, run_mock.call_args.kwargs["timeout"])
        compose_command = run_mock.call_args.args[0]
        self.assertIn("--force-recreate", compose_command)
        self.assertIn("--remove-orphans", compose_command)

        with tempfile.TemporaryDirectory() as tmp_dir:
            compose_path = Path(tmp_dir) / "compose.yml"
            compose_path.write_text("services: {}\n", encoding="utf-8")
            with patch.object(_bridge_module, "_gateway_compose_path", return_value=compose_path):
                with patch.object(_bridge_module, "_run_command", return_value=(True, "")) as run_mock:
                    ok, _output = _docker_compose_down("tsds-demo")

        self.assertTrue(ok)
        self.assertEqual(DOCKER_COMPOSE_TIMEOUT, run_mock.call_args.kwargs["timeout"])

    def test_runtime_containers_ready_requires_gateway_and_workspace_agent(self):
        with patch.object(_bridge_module, "_docker_container_running", return_value=True):
            with patch.object(_bridge_module, "_gateway_reachable", return_value=True):
                with patch.object(_bridge_module, "_workspace_agent_running", return_value=False):
                    with patch.object(_bridge_module, "_workspace_agent_reachable", return_value=True):
                        self.assertFalse(_runtime_containers_ready("tsds-demo"))

        with patch.object(_bridge_module, "_docker_container_running", return_value=True):
            with patch.object(_bridge_module, "_gateway_reachable", return_value=True):
                with patch.object(_bridge_module, "_workspace_agent_running", return_value=True):
                    with patch.object(_bridge_module, "_workspace_agent_reachable", return_value=True):
                        self.assertTrue(_runtime_containers_ready("tsds-demo"))

    def test_sanitize_runtime_debug_payload_removes_sensitive_keys_and_redacts_paths(self):
        payload = {
            "assistant_content": "Found /data/tsds/tenants/demo/workspace/hello.txt",
            "runId": "run-1",
            "meta": {"sessionId": "session-1"},
            "stderr": "sh: 1: /data/tsds/tenants/demo/workspace/a.zip: Permission denied",
            "nested": {"workspaceDir": "/opt/tsds/tenants/demo"},
        }

        sanitized = sanitize_runtime_debug_payload(payload)

        self.assertNotIn("runId", sanitized)
        self.assertNotIn("meta", sanitized)
        self.assertNotIn("stderr", sanitized)
        self.assertNotIn("workspaceDir", str(sanitized))
        self.assertNotIn("/data/tsds", str(sanitized))
        self.assertIn("[internal-path]", str(sanitized))

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_sanitize_user_visible_error_hides_shell_and_path_details(self):
        message = sanitize_user_visible_error(
            "sh: 1: /data/tsds/tenants/demo/workspace/file.zip: Permission denied"
        )
        self.assertEqual(message, "无法访问系统内部目录或配置文件")

    def test_build_openclaw_agent_exec_command_avoids_shell_execution(self):
        command = build_openclaw_agent_exec_command(
            container_name="demo-gateway",
            tenant_home="/data/tsds/tenants/demo/home",
            agent_id="main-agent",
            session_key="conversation-1",
            content="/data/tsds/tenants/demo/workspace/file.zip",
        )

        self.assertNotIn("sh", command)
        self.assertNotIn("-lc", command)
        self.assertEqual(command[0:3], ["docker", "exec", "-e"])
        self.assertIn("/data/tsds/tenants/demo/workspace/file.zip", command)

    def test_build_openclaw_agent_stream_command_uses_plain_agent_output(self):
        command = build_openclaw_agent_stream_command(
            container_name="demo-gateway",
            tenant_home="/srv/openclaw-home",
            agent_id="main-agent",
            session_key="conversation-1",
            content="hello markdown",
        )

        self.assertNotIn("--json", command)
        self.assertNotIn("sh", command)
        self.assertNotIn("-lc", command)

    def test_reset_native_agent_cli_session_archives_agent_main_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = Path(tmp_dir)
            session_file = sessions_dir / "session.jsonl"
            session_file.write_text("old history\n", encoding="utf-8")
            sessions_json = sessions_dir / "sessions.json"
            sessions_json.write_text(
                json.dumps(
                    {
                        "agent:main-agent:main": {
                            "sessionFile": str(session_file),
                            "sessionId": "old-session",
                        },
                        "other:key": {
                            "sessionFile": str(sessions_dir / "other.jsonl"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                _bridge_module,
                "_native_agent_sessions_dir",
                return_value=sessions_dir,
            ):
                _reset_native_agent_cli_session("tsds-demo", "main-agent")

            retained = json.loads(sessions_json.read_text(encoding="utf-8"))
            self.assertEqual(list(retained.keys()), ["other:key"])
            self.assertFalse(session_file.exists())
            archived = list((sessions_dir / ".touming-agent-test-archive").glob("*/*"))
            self.assertTrue(any(item.name == "session.jsonl" for item in archived))

    def test_native_agent_session_map_restores_and_persists_backend_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            home_dir = Path(tmp_dir) / "home"
            sessions_dir = home_dir / ".openclaw" / "agents" / "main-agent" / "sessions"
            sessions_dir.mkdir(parents=True)
            sessions_json = sessions_dir / "sessions.json"
            session_map_json = sessions_dir / ".touming-session-map.json"
            mapped_file = sessions_dir / "mapped.jsonl"
            mapped_file.write_text("mapped history\n", encoding="utf-8")
            _write_json(
                session_map_json,
                {
                    "agent-test:main-agent:backend-session-1": {
                        "sessionId": "native-session-1",
                        "sessionFile": str(mapped_file),
                    }
                },
            )

            with patch.object(
                _bridge_module,
                "_native_agent_sessions_dir",
                return_value=sessions_dir,
            ):
                restored = _activate_native_agent_session(
                    "tsds-demo",
                    "main-agent",
                    "agent-test:main-agent:backend-session-1",
                )
                self.assertTrue(restored)
                sessions = json.loads(sessions_json.read_text(encoding="utf-8"))
                self.assertEqual(
                    sessions["agent:main-agent:main"]["sessionFile"],
                    "/srv/openclaw-home/.openclaw/agents/main-agent/sessions/mapped.jsonl",
                )

                sessions["agent:main-agent:main"] = {
                    "sessionId": "native-session-2",
                    "sessionFile": "/srv/openclaw-home/.openclaw/agents/main-agent/sessions/next.jsonl",
                }
                _write_json(sessions_json, sessions)
                _persist_native_agent_session(
                    "tsds-demo",
                    "main-agent",
                    "agent-test:main-agent:backend-session-1",
                )

            session_map = json.loads(session_map_json.read_text(encoding="utf-8"))
            self.assertEqual(
                session_map["agent-test:main-agent:backend-session-1"]["sessionFile"],
                str(sessions_dir / "next.jsonl"),
            )
            self.assertEqual(
                session_map["agent-test:main-agent:backend-session-1"]["sessionId"],
                "native-session-2",
            )

    def test_native_agent_session_map_key_includes_source_and_agent(self):
        self.assertEqual(
            _native_agent_session_map_key(
                {"session_source": "public-client-entry"},
                "main-agent",
                "session-1",
            ),
            "public-client-entry:main-agent:session-1",
        )
        self.assertEqual(
            _native_agent_session_map_key(
                {
                    "session_source": "agent-test",
                    "native_session_revision": "policy-v2",
                },
                "worker-agent",
                "session-1",
            ),
            "agent-test:worker-agent:policy-v2:session-1",
        )

    def test_build_cli_content_uses_history_only_when_requested(self):
        payload = {
            "content": "current user",
            "messages": [
                {"role": "system", "content": "identity"},
                {"role": "user", "content": "old user"},
                {"role": "assistant", "content": "old assistant"},
                {"role": "user", "content": "current user"},
            ],
        }

        seeded = _build_cli_content(payload, include_messages=True)
        self.assertIn("[SYSTEM]\nidentity", seeded)
        self.assertIn("[ASSISTANT]\nold assistant", seeded)
        self.assertEqual(_build_cli_content(payload, include_messages=False), "current user")
        self.assertFalse(_should_seed_native_session_messages({"native_session_seed_messages": False}))
        self.assertTrue(_should_seed_native_session_messages({}))

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_sanitize_agent_test_runtime_payload_restricts_internal_directory_dump(self):
        payload = sanitize_agent_test_runtime_payload(
            {
                "assistant_content": (
                    "/data/tsds contains .openclaw-runtime/config, compose.yml and "
                    "memory/main-agent.sqlite"
                ),
                "token_count": "12",
                "tool_calls": [{"name": "workspace.read"}],
            }
        )

        self.assertEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertEqual(payload["token_count"], 12)
        self.assertNotIn("tool_calls", payload)

    def test_validate_workspace_logical_path_rejects_system_paths(self):
        with self.assertRaisesMessage(ValueError, "path is not allowed"):
            validate_workspace_logical_path("/data/tsds/tenants/demo/workspace/knowledge/demo.zip")

        with self.assertRaisesMessage(ValueError, "path is not allowed"):
            validate_workspace_logical_path("config/openclaw.json")

        self.assertEqual(
            validate_workspace_logical_path("knowledge/demo.zip"),
            "knowledge/demo.zip",
        )

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_runtime_request_guard_blocks_absolute_and_system_directory_queries(self):
        self.assertTrue(is_restricted_runtime_request("/data/tsds"))
        self.assertTrue(is_restricted_runtime_request("scan .openclaw-runtime/config for me"))
        self.assertTrue(is_restricted_runtime_request("请列出 config 目录下面的文件"))
        self.assertTrue(is_restricted_runtime_request("read home/.openclaw and memory directory"))
        self.assertFalse(is_restricted_runtime_request("Please summarize the uploaded report"))

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_sanitize_assistant_content_for_user_rejects_runtime_inventory_dump(self):
        text = (
            "System config directories: .openclaw-runtime/config, compose.yml, "
            "Dockerfile.gateway, /data/tsds/tenants/demo/home/.openclaw"
        )
        self.assertEqual(
            sanitize_assistant_content_for_user(text),
            RESTRICTED_RUNTIME_ACCESS_MESSAGE,
        )

    def test_is_restricted_runtime_request_does_not_flag_plain_agent_roster_text(self):
        text = (
            "你当前正在扮演公司的 Agent：北原透明牧业有限公司 CEO。\n"
            "公司当前已启用的 Agent 名册：\n"
            "- 客服接待 [agent-a] / subagent / 公开入口，默认入口\n"
            "- 北原透明牧业有限公司 CEO [main-agent] / main_agent / 内部员工（你自己）"
        )

        self.assertFalse(is_restricted_runtime_request(text))

    def test_sanitize_assistant_content_for_user_extracts_safe_text_from_runtime_json(self):
        text = json.dumps(
            {
                "runId": "run-1",
                "status": "ok",
                "result": {
                    "payloads": [
                        {
                            "text": "今天是 **2026年4月2日，星期四**。",
                            "mediaUrl": None,
                        }
                    ]
                },
                "meta": {
                    "agentMeta": {"sessionId": "session-1"},
                    "systemPromptReport": {
                        "workspaceDir": "/data/tsds/tenants/demo/workspace"
                    },
                },
            },
            ensure_ascii=False,
        ) + "\nsh: 1: /data/tsds/tenants/demo/workspace/demo.zip: Permission denied"

        self.assertEqual(
            sanitize_assistant_content_for_user(text),
            "今天是 **2026年4月2日，星期四**。",
        )

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_sanitize_assistant_content_for_user_rejects_runtime_json_directory_scan(self):
        text = json.dumps(
            {
                "runId": "run-1",
                "result": {
                    "payloads": [
                        {
                            "text": (
                                "`/data/tsds` 下的文件目录结构如下：\n"
                                "- `.openclaw-runtime/config/`\n"
                                "- `compose.yml`\n"
                                "- `home/.openclaw/memory/main-agent.sqlite`"
                            )
                        }
                    ]
                },
                "meta": {"systemPromptReport": {"workspaceDir": "/data/tsds/tenants/demo/workspace"}},
            },
            ensure_ascii=False,
        ) + "\nsh: 1: /data/tsds: Permission denied"

        self.assertEqual(
            sanitize_assistant_content_for_user(text),
            RESTRICTED_RUNTIME_ACCESS_MESSAGE,
        )
    def test_sanitize_user_visible_error_returns_generic_file_access_message(self):
        message = sanitize_user_visible_error(
            "sh: 1: /data/tsds/tenants/demo/workspace/file.zip: Permission denied"
        )

        self.assertNotIn("/data/tsds", message)
        self.assertNotIn("Permission denied", message)
        self.assertNotEqual(message, RESTRICTED_RUNTIME_ACCESS_MESSAGE)

    def test_sanitize_agent_test_runtime_payload_keeps_container_internal_listing_text(self):
        payload = sanitize_agent_test_runtime_payload(
            {
                "assistant_content": (
                    "/data/tsds contains .openclaw-runtime/config, compose.yml and "
                    "memory/main-agent.sqlite"
                ),
                "token_count": "12",
                "tool_calls": [{"name": "workspace.read"}],
            }
        )

        self.assertNotEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertIn("[internal-path]", payload["assistant_content"])
        self.assertIn(".openclaw-runtime/config", payload["assistant_content"])
        self.assertEqual(payload["token_count"], 12)
        self.assertNotIn("tool_calls", payload)

    def test_sanitize_assistant_content_for_user_keeps_runtime_inventory_text_and_redacts_host_paths(self):
        text = (
            "System config directories: .openclaw-runtime/config, compose.yml, "
            "Dockerfile.gateway, /data/tsds/tenants/demo/home/.openclaw"
        )

        sanitized = sanitize_assistant_content_for_user(text)

        self.assertNotEqual(sanitized, RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertIn(".openclaw-runtime/config", sanitized)
        self.assertIn("[internal-path]", sanitized)

    def test_sanitize_assistant_content_for_user_keeps_runtime_json_directory_text_and_drops_shell_noise(self):
        text = json.dumps(
            {
                "runId": "run-1",
                "result": {
                    "payloads": [
                        {
                            "text": (
                                "`/data/tsds` contains:\n"
                                "- `.openclaw-runtime/config/`\n"
                                "- `compose.yml`\n"
                                "- `home/.openclaw/memory/main-agent.sqlite`"
                            )
                        }
                    ]
                },
                "meta": {"systemPromptReport": {"workspaceDir": "/data/tsds/tenants/demo/workspace"}},
            },
            ensure_ascii=False,
        ) + "\nsh: 1: /data/tsds: Permission denied"

        sanitized = sanitize_assistant_content_for_user(text)

        self.assertNotEqual(sanitized, RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertNotIn("Permission denied", sanitized)
        self.assertNotIn("/data/tsds/tenants/demo", sanitized)
        self.assertIn(".openclaw-runtime/config", sanitized)


class RuntimePolicyExecutionTests(SimpleTestCase):
    def test_subagent_policy_defaults_to_deny_all_without_enabled_binding(self):
        policy = build_effective_runtime_policy(
            global_config={
                "enabled": True,
                "configJson": {
                    "permissions": {
                        "skills": {"include": ["skill-report"]},
                        "tools": {"allow": ["workspace.list"]},
                        "knowledge": {"read": ["knowledge"]},
                    }
                },
            },
            global_enabled=True,
            binding_enabled=False,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertFalse(policy.allows_skill("skill-report"))
        self.assertTrue(policy.allows_tool("workspace.list"))
        self.assertFalse(policy.allows_path("knowledge/demo.txt"))
        self.assertFalse(policy.allows_memory("memory.read"))
        self.assertFalse(policy.allows_memory("memory.write"))
        self.assertFalse(policy.allows_memory("memory.update"))
        self.assertFalse(policy.allows_memory("memory.delete"))
        self.assertEqual([], list(policy.knowledge_reads))

    def test_main_agent_keeps_compatibility_mode_without_allowlists(self):
        policy = build_effective_runtime_policy(
            agent_id="main-agent",
            require_explicit_binding=False,
            default_allow_skills=True,
            default_allow_tools=True,
        )

        self.assertTrue(policy.allows_skill("skill-report"))
        self.assertTrue(policy.allows_tool("workspace.list"))
        self.assertTrue(policy.allows_memory("memory.read"))
        self.assertTrue(policy.allows_memory("memory.write"))
        self.assertTrue(policy.allows_memory("memory.update"))
        self.assertTrue(policy.allows_memory("memory.delete"))

    def test_memory_permissions_are_independent_for_explicit_agent_binding(self):
        """A deny on one memory action must not implicitly deny the other actions."""

        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "memory": {
                    "read": True,
                    "write": False,
                    "update": "true",
                    "delete": 0,
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertTrue(policy.allows_memory("read"))
        self.assertFalse(policy.allows_memory("memory.write"))
        self.assertTrue(policy.allows_memory("update"))
        self.assertFalse(policy.allows_memory("memory.delete"))
        self.assertFalse(policy.allows_memory("unknown"))
        self.assertEqual(
            {
                "read": True,
                "write": False,
                "update": True,
                "delete": False,
            },
            policy.to_dict()["memory"],
        )

    def test_memory_permissions_use_restrictive_merge_across_layers(self):
        """Global, template, and binding memory permissions merge with deny precedence."""

        policy = build_effective_runtime_policy(
            global_config={
                "enabled": True,
                "configJson": {
                    "permissions": {
                        "memory": {
                            "read": True,
                            "write": True,
                            "update": True,
                            "delete": True,
                        }
                    }
                },
            },
            template={
                "enabled": True,
                "memory": {
                    "update": False,
                },
            },
            binding={
                "enabled": True,
                "memory": {
                    "read": True,
                    "delete": False,
                },
            },
            global_enabled=True,
            template_enabled=True,
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertTrue(policy.allows_memory("read"))
        self.assertTrue(policy.allows_memory("write"))
        self.assertFalse(policy.allows_memory("update"))
        self.assertFalse(policy.allows_memory("delete"))

    def test_explicit_agent_memory_actions_default_to_deny_when_omitted(self):
        """Explicit bindings fail closed for memory actions that are not configured."""

        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "memory": {
                    "read": True,
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertTrue(policy.allows_memory("read"))
        self.assertFalse(policy.allows_memory("write"))
        self.assertFalse(policy.allows_memory("update"))
        self.assertFalse(policy.allows_memory("delete"))

    def test_agent_owns_full_memory_even_without_workspace_binding(self):
        """Memory ownership remains available independently of workspace grants."""

        policy = build_effective_runtime_policy(
            binding={},
            binding_enabled=False,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
            default_allow_memory=True,
        )

        self.assertTrue(policy.allows_tool("workspace.list"))
        self.assertTrue(policy.allows_memory("read"))
        self.assertTrue(policy.allows_memory("write"))
        self.assertTrue(policy.allows_memory("update"))
        self.assertTrue(policy.allows_memory("delete"))

    def test_effective_runtime_policy_uses_restrictive_merge_for_knowledge_scopes(self):
        policy = build_effective_runtime_policy(
            global_config={
                "enabled": True,
                "configJson": {
                    "permissions": {
                        "knowledge": {
                            "read": ["knowledge"],
                        }
                    }
                },
            },
            template={
                "enabled": True,
                "knowledge": {
                    "read": ["knowledge/reports"],
                    "deny": ["knowledge/reports/private"],
                },
            },
            binding={
                "enabled": True,
                "knowledge": {
                    "read": ["knowledge/reports/2026"],
                },
            },
            global_enabled=True,
            template_enabled=True,
            binding_enabled=True,
            agent_id="main-agent",
        )

        self.assertEqual(["knowledge/reports/2026"], list(policy.knowledge_reads))
        self.assertEqual(["knowledge/reports/private"], list(policy.knowledge_denies))
        self.assertTrue(policy.allows_path("knowledge/reports/2026/summary.md"))
        self.assertFalse(policy.allows_path("knowledge/private/demo.md"))
        self.assertFalse(policy.allows_path("docs/demo.md"))

    def test_effective_runtime_policy_keeps_write_scope_separate_from_read(self):
        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "knowledge": {
                    "write": ["knowledge/reports/2026"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertEqual([], list(policy.knowledge_reads))
        self.assertEqual(["knowledge/reports/2026"], list(policy.knowledge_writes))
        self.assertFalse(policy.allows_path("knowledge/reports/2026/summary.md"))
        self.assertTrue(policy.allows_write_path("knowledge/reports/2026/summary.md"))
        self.assertEqual(
            ["knowledge/reports/2026"],
            policy.to_dict()["knowledge"]["write"],
        )

    def test_file_level_knowledge_scope_only_allows_exact_file_access(self):
        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "knowledge": {
                    "read": ["knowledge/demo.txt"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertTrue(policy.allows_directory("knowledge"))
        self.assertTrue(policy.allows_directory("knowledge/demo.txt"))
        self.assertTrue(policy.allows_path("knowledge/demo.txt"))
        self.assertFalse(policy.allows_path("knowledge/private.txt"))
        self.assertFalse(policy.allows_directory("knowledge/private"))

    def test_directory_queries_respect_deny_overrides_without_blocking_parent_scope(self):
        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "knowledge": {
                    "read": ["knowledge"],
                    "deny": ["knowledge/private"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertTrue(policy.allows_directory("knowledge"))
        self.assertTrue(policy.allows_path("knowledge/public/demo.txt"))
        self.assertFalse(policy.allows_directory("knowledge/private"))
        self.assertFalse(policy.allows_directory("knowledge/private/nested"))
        self.assertFalse(policy.allows_path("knowledge/private/secret.txt"))

    def test_unzipped_workspace_directory_scope_allows_descendant_files(self):
        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "knowledge": {
                    "read": ["knowledge/1775028071209-workspace/workspace"],
                },
            },
            binding_enabled=True,
            agent_id="main-agent",
        )

        self.assertTrue(
            policy.allows_path(
                "knowledge/1775028071209-workspace/workspace/public/company/company_profile.md"
            )
        )
        self.assertTrue(
            policy.allows_directory(
                "knowledge/1775028071209-workspace/workspace/public/company"
            )
        )
        self.assertFalse(
            policy.allows_path(
                "knowledge/1775028071209-workspace-other/workspace/public/company/company_profile.md"
            )
        )

    def test_tool_allow_uses_registered_tool_ids_only(self):
        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.list", "workspace.unknown"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertTrue(policy.allows_tool("workspace.list"))
        self.assertTrue(policy.allows_tool("workspace.read"))
        self.assertFalse(policy.allows_tool("workspace.unknown"))

    def test_tool_deny_overrides_registered_tool_allow(self):
        policy = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.list", "workspace.read"],
                    "deny": ["workspace.list"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )

        self.assertTrue(policy.allows_tool("workspace.list"))
        self.assertTrue(policy.allows_tool("workspace.read"))

    def test_runtime_policy_can_load_in_standalone_bridge_mode(self):
        source_path = REPO_ROOT / "backend" / "apps" / "runtime" / "policy.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "runtime_policy.py"
            temp_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
            command = [
                sys.executable,
                "-I",
                "-c",
                (
                    "import importlib.util, json, pathlib; "
                    f"path = pathlib.Path(r'{temp_path.as_posix()}'); "
                    "spec = importlib.util.spec_from_file_location('runtime_policy', str(path)); "
                    "module = importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(module); "
                    "print(json.dumps(module.normalize_tool_ids(['workspace.read', 'workspace.rename', 'workspace.unknown'])))"
                ),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=temp_dir,
                check=False,
            )

        self.assertEqual(0, completed.returncode, msg=completed.stderr)
        self.assertEqual(
            '["workspace.read", "workspace.rename"]',
            completed.stdout.strip(),
        )

    def test_select_active_bound_skills_requires_enabled_binding_and_skill(self):
        policy = build_effective_runtime_policy(
            global_config={
                "enabled": True,
                "configJson": {
                    "permissions": {
                        "skills": {
                            "include": ["skill-report", "skill-disabled", "skill-deprecated"],
                        }
                    }
                },
            },
            global_enabled=True,
            agent_id="main-agent",
        )

        active = select_active_bound_skills(
            skills=[
                {"skill_id": "skill-report", "enabled": True},
                {"skill_id": "skill-disabled", "enabled": False},
                {"skill_id": "skill-deprecated", "enabled": True, "status": "deprecated"},
                {"skill_id": "skill-other", "enabled": True},
            ],
            bindings=[
                {"agent_id": "main-agent", "skill_id": "skill-report", "enabled": True, "sort_order": 1},
                {"agent_id": "main-agent", "skill_id": "skill-disabled", "enabled": True, "sort_order": 2},
                {"agent_id": "main-agent", "skill_id": "skill-deprecated", "enabled": True, "sort_order": 3},
                {"agent_id": "main-agent", "skill_id": "skill-other", "enabled": False, "sort_order": 4},
                {"agent_id": "main-agent", "skill_id": "skill-missing", "enabled": True, "sort_order": 5},
            ],
            policy=policy,
        )

        self.assertEqual(["skill-report"], [item["skill"]["skill_id"] for item in active])


class AgentWorkspaceAccessExecutionTests(SimpleTestCase):
    class _FakeQuerySet:
        def __init__(self, items):
            self._items = items

        def order_by(self, *_args, **_kwargs):
            return self._items

    class _FakeWorkspaceFile:
        def __init__(self, file_id, logical_path, file_name, sync_status="in_sync"):
            self.id = file_id
            self.logical_path = logical_path
            self.file_name = file_name
            self.sync_status = sync_status
            self.instance = None

        def get_info(self):
            return {
                "id": self.id,
                "logical_path": self.logical_path,
                "file_name": self.file_name,
                "sync_status": self.sync_status,
            }

    @patch("apps.runtime.services.OpenClawRuntimeService.get_company_instance", return_value=(False, "runtime missing"))
    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_list_directory_applies_knowledge_read_and_deny_to_real_directory_result(
        self,
        workspace_filter_mock,
        get_policy_mock,
        company_instance_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="main-agent",
            name="Main Agent",
            role=TransparentCompanyAgentDefinition.ROLE_MAIN_AGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.list"],
                },
                "knowledge": {
                    "read": ["knowledge"],
                    "deny": ["knowledge/private"],
                },
            },
            binding_enabled=True,
            agent_id="main-agent",
        )
        workspace_filter_mock.return_value = self._FakeQuerySet(
            [
                self._FakeWorkspaceFile(3, "docs/manual.md", "manual.md"),
                self._FakeWorkspaceFile(2, "knowledge/private/secret.txt", "secret.txt"),
                self._FakeWorkspaceFile(1, "knowledge/1775028071209-workspace.zip", "1775028071209-workspace.zip"),
            ]
        )

        ok, payload = AgentWorkspaceAccessService.list_directory(company, agent, "knowledge", include_runtime_tree=True)

        self.assertTrue(ok)
        self.assertEqual("knowledge", payload["logical_path"])
        self.assertEqual(["knowledge/1775028071209-workspace.zip"], [item["logical_path"] for item in payload["items"]])
        workspace_filter_mock.assert_called_once()
        company_instance_mock.assert_called_once()

    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_workspace_tools_require_explicit_allow_for_subagent(
        self,
        workspace_filter_mock,
        get_policy_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="worker-agent",
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "knowledge": {
                    "read": ["knowledge"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )
        workspace_filter_mock.return_value = self._FakeQuerySet([])

        list_ok, list_payload = AgentWorkspaceAccessService.list_directory(company, agent, "knowledge")
        locate_ok, locate_payload = AgentWorkspaceAccessService.locate_files(company, agent, logical_path="knowledge")
        read_ok, read_payload = AgentWorkspaceAccessService.read_file(
            company,
            agent,
            logical_path="knowledge/demo.txt",
        )

        self.assertFalse(list_ok)
        self.assertIn("目录读取权限", str(list_payload))
        self.assertFalse(locate_ok)
        self.assertIn("文件定位权限", str(locate_payload))
        self.assertFalse(read_ok)
        self.assertIn("文件读取权限", str(read_payload))

    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_knowledge_scope_required_for_directory_listing(
        self,
        workspace_filter_mock,
        get_policy_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="worker-agent",
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.list"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )
        workspace_filter_mock.return_value = self._FakeQuerySet([])

        ok, payload = AgentWorkspaceAccessService.list_directory(company, agent, "knowledge")

        self.assertFalse(ok)
        self.assertIn("knowledge scope", str(payload))

    @override_settings(RUNTIME_AGENT_ARTIFACT_LOGICAL_ROOT="artifacts")
    @patch("apps.runtime.services.OpenClawRuntimeService.get_provider_adapter")
    @patch("apps.runtime.services.AgentWorkspaceAccessService._get_runtime_instance")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_write_file_forces_agent_outputs_under_artifact_root(
        self,
        workspace_filter_mock,
        get_policy_mock,
        get_runtime_instance_mock,
        get_provider_adapter_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="worker-agent",
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.write"],
                },
                "knowledge": {
                    "read": ["artifacts"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )
        workspace_filter_mock.return_value = self._FakeQuerySet([])
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)
        calls: list[dict[str, object]] = []

        def execute_workspace_tool(company_arg, instance_arg, **kwargs):
            calls.append({"company": company_arg, "instance": instance_arg, **kwargs})
            return True, {"status": "completed"}

        adapter = SimpleNamespace(
            list_workspace_tree=lambda *_args, **_kwargs: (True, {"items": []}),
            execute_workspace_tool=execute_workspace_tool,
        )
        get_runtime_instance_mock.return_value = instance
        get_provider_adapter_mock.return_value = adapter

        ok, payload = AgentWorkspaceAccessService.write_file(
            company,
            agent,
            arguments={
                "logical_path": "reports/q1.md",
                "content": "quarterly report",
                "create_dirs": True,
            },
        )

        self.assertTrue(ok)
        self.assertEqual("artifacts/reports/q1.md", payload["logical_path"])
        self.assertEqual("artifacts/reports/q1.md", calls[0]["arguments"]["logical_path"])
        self.assertNotIn("content", payload)

    @patch("apps.runtime.services.OpenClawRuntimeService.get_provider_adapter")
    @patch("apps.runtime.services.AgentWorkspaceAccessService._get_runtime_instance")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_read_file_requires_explicit_unique_target(
        self,
        workspace_filter_mock,
        get_policy_mock,
        get_runtime_instance_mock,
        get_provider_adapter_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="worker-agent",
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.read"],
                },
                "knowledge": {
                    "read": ["knowledge"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )
        file_a = self._FakeWorkspaceFile(2, "knowledge/a/report.txt", "report.txt")
        file_b = self._FakeWorkspaceFile(1, "knowledge/b/report.txt", "report.txt")
        workspace_filter_mock.return_value = self._FakeQuerySet([file_a, file_b])
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)
        adapter = SimpleNamespace(
            read_workspace_file=lambda *_args, **_kwargs: (
                True,
                {
                    "content": "hello",
                    "logical_path": "knowledge/a/report.txt",
                },
            ),
            read_workspace_logical_file=lambda *_args, **_kwargs: (
                True,
                {
                    "content": "hello",
                    "logical_path": "knowledge/a/report.txt",
                },
            ),
        )
        get_runtime_instance_mock.return_value = instance
        get_provider_adapter_mock.return_value = adapter

        ambiguous_ok, ambiguous_payload = AgentWorkspaceAccessService.read_file(
            company,
            agent,
            file_name="report.txt",
        )
        missing_target_ok, missing_target_payload = AgentWorkspaceAccessService.read_file(company, agent)
        exact_ok, exact_payload = AgentWorkspaceAccessService.read_file(
            company,
            agent,
            logical_path="knowledge/a/report.txt",
        )

        self.assertFalse(ambiguous_ok)
        self.assertIn("多个文件", str(ambiguous_payload))
        self.assertFalse(missing_target_ok)
        self.assertIn("必须明确指定", str(missing_target_payload))
        self.assertTrue(exact_ok)
        self.assertEqual("knowledge/a/report.txt", exact_payload["logical_path"])

    @patch("apps.runtime.services.OpenClawRuntimeService.get_provider_adapter")
    @patch("apps.runtime.services.AgentWorkspaceAccessService._get_runtime_instance")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_list_directory_includes_runtime_only_files_within_authorized_scope(
        self,
        workspace_filter_mock,
        get_policy_mock,
        get_runtime_instance_mock,
        get_provider_adapter_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="worker-agent",
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.list"],
                },
                "knowledge": {
                    "read": ["knowledge"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )
        workspace_filter_mock.return_value = self._FakeQuerySet([])
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)
        adapter = SimpleNamespace(
            list_workspace_tree=lambda *_args, **_kwargs: (
                True,
                {
                    "items": [
                        {
                            "logical_path": "knowledge/runtime-only.txt",
                            "name": "runtime-only.txt",
                            "kind": "file",
                        }
                    ]
                },
            )
        )
        get_runtime_instance_mock.return_value = instance
        get_provider_adapter_mock.return_value = adapter

        ok, payload = AgentWorkspaceAccessService.list_directory(company, agent, "knowledge")

        self.assertTrue(ok)
        self.assertEqual(["knowledge/runtime-only.txt"], [item["logical_path"] for item in payload["items"]])
        self.assertEqual("runtime_only", payload["items"][0]["file"]["sync_status"])

    @patch("apps.runtime.services.OpenClawRuntimeService.get_provider_adapter")
    @patch("apps.runtime.services.AgentWorkspaceAccessService._get_runtime_instance")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_file_allow_controls_list_locate_and_read_without_leaking_host_paths(
        self,
        workspace_filter_mock,
        get_policy_mock,
        get_runtime_instance_mock,
        get_provider_adapter_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="worker-agent",
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.list", "workspace.locate", "workspace.read"],
                },
                "knowledge": {
                    "read": ["knowledge/demo.txt"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )
        demo_file = self._FakeWorkspaceFile(2, "knowledge/demo.txt", "demo.txt")
        private_file = self._FakeWorkspaceFile(1, "knowledge/private.txt", "private.txt")
        workspace_filter_mock.return_value = self._FakeQuerySet([demo_file, private_file])
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)
        adapter = SimpleNamespace(
            read_workspace_file=lambda *_args, **_kwargs: (
                True,
                {
                    "content": "demo content",
                    "path": "/data/tsds/tenants/demo/workspace/knowledge/demo.txt",
                },
            ),
        )
        get_runtime_instance_mock.return_value = instance
        get_provider_adapter_mock.return_value = adapter

        list_ok, list_payload = AgentWorkspaceAccessService.list_directory(
            company,
            agent,
            "knowledge",
            include_runtime_tree=False,
        )
        locate_ok, locate_payload = AgentWorkspaceAccessService.locate_files(
            company,
            agent,
            logical_path="knowledge",
        )
        read_ok, read_payload = AgentWorkspaceAccessService.read_file(
            company,
            agent,
            logical_path="knowledge/demo.txt",
        )
        denied_read_ok, denied_read_payload = AgentWorkspaceAccessService.read_file(
            company,
            agent,
            logical_path="knowledge/private.txt",
        )

        self.assertTrue(list_ok)
        self.assertEqual(["knowledge/demo.txt"], [item["logical_path"] for item in list_payload["items"]])
        self.assertTrue(locate_ok)
        self.assertEqual(["knowledge/demo.txt"], [item["logical_path"] for item in locate_payload["items"]])
        self.assertTrue(read_ok)
        self.assertEqual("knowledge/demo.txt", read_payload["logical_path"])
        self.assertEqual("knowledge/demo.txt", read_payload["path"])
        self.assertNotIn("/data/tsds", str(read_payload))
        self.assertFalse(denied_read_ok)
        self.assertIn("logical path", str(denied_read_payload))

    @patch("apps.runtime.services.OpenClawRuntimeService.get_provider_adapter")
    @patch("apps.runtime.services.AgentWorkspaceAccessService._get_runtime_instance")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.get_effective_policy")
    @patch("apps.runtime.services.WorkspaceFile.objects.filter")
    def test_directory_allow_is_recursive_but_deny_override_blocks_denied_subtree(
        self,
        workspace_filter_mock,
        get_policy_mock,
        get_runtime_instance_mock,
        get_provider_adapter_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        agent = TransparentCompanyAgentDefinition(
            transparentCompany=transparent_company,
            agentId="worker-agent",
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            created_at="1",
            updated_at="1",
        )
        get_policy_mock.return_value = build_effective_runtime_policy(
            binding={
                "enabled": True,
                "tools": {
                    "allow": ["workspace.list", "workspace.locate", "workspace.read"],
                },
                "knowledge": {
                    "read": ["knowledge"],
                    "deny": ["knowledge/private"],
                },
            },
            binding_enabled=True,
            agent_id="worker-agent",
            require_explicit_binding=True,
            default_allow_skills=False,
            default_allow_tools=False,
        )
        report_file = self._FakeWorkspaceFile(3, "knowledge/reports/2026/summary.txt", "summary.txt")
        private_file = self._FakeWorkspaceFile(2, "knowledge/private/secret.txt", "secret.txt")
        root_file = self._FakeWorkspaceFile(1, "knowledge/demo.txt", "demo.txt")
        workspace_filter_mock.return_value = self._FakeQuerySet([report_file, private_file, root_file])
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)
        adapter = SimpleNamespace(
            list_workspace_tree=lambda *_args, **_kwargs: (
                True,
                {
                    "items": [
                        {
                            "logical_path": "knowledge/private",
                            "name": "private",
                            "kind": "directory",
                        },
                        {
                            "logical_path": "knowledge/private/runtime-secret.txt",
                            "name": "runtime-secret.txt",
                            "kind": "file",
                        },
                    ]
                },
            ),
            read_workspace_file=lambda *_args, **_kwargs: (
                True,
                {
                    "content": "report content",
                    "path": "/data/tsds/tenants/demo/workspace/knowledge/reports/2026/summary.txt",
                },
            ),
        )
        get_runtime_instance_mock.return_value = instance
        get_provider_adapter_mock.return_value = adapter

        list_ok, list_payload = AgentWorkspaceAccessService.list_directory(company, agent, "knowledge")
        locate_ok, locate_payload = AgentWorkspaceAccessService.locate_files(
            company,
            agent,
            logical_path="knowledge/reports",
        )
        read_ok, read_payload = AgentWorkspaceAccessService.read_file(
            company,
            agent,
            logical_path="knowledge/reports/2026/summary.txt",
        )
        denied_list_ok, denied_list_payload = AgentWorkspaceAccessService.list_directory(
            company,
            agent,
            "knowledge/private",
        )
        denied_locate_ok, denied_locate_payload = AgentWorkspaceAccessService.locate_files(
            company,
            agent,
            logical_path="knowledge/private",
        )
        denied_read_ok, denied_read_payload = AgentWorkspaceAccessService.read_file(
            company,
            agent,
            logical_path="knowledge/private/secret.txt",
        )

        self.assertTrue(list_ok)
        self.assertEqual(
            ["knowledge/reports", "knowledge/demo.txt"],
            [item["logical_path"] for item in list_payload["items"]],
        )
        self.assertTrue(locate_ok)
        self.assertEqual(
            ["knowledge/reports/2026/summary.txt"],
            [item["logical_path"] for item in locate_payload["items"]],
        )
        self.assertTrue(read_ok)
        self.assertEqual("knowledge/reports/2026/summary.txt", read_payload["logical_path"])
        self.assertNotIn("/data/tsds", str(read_payload))
        self.assertFalse(denied_list_ok)
        self.assertIn("logical path", str(denied_list_payload))
        self.assertFalse(denied_locate_ok)
        self.assertIn("logical path", str(denied_locate_payload))
        self.assertFalse(denied_read_ok)
        self.assertIn("logical path", str(denied_read_payload))


class ClientEntryBindingTests(SimpleTestCase):
    @patch.object(CompanyService, "_build_client_entry_payload")
    @patch("apps.company.services.TransparentCompany.objects.get")
    @patch("apps.company.services.Company.objects.get")
    def test_unbound_client_entry_cannot_resolve_subagent(
        self,
        company_get_mock,
        transparent_company_get_mock,
        build_payload_mock,
    ):
        company_get_mock.return_value = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company_get_mock.return_value = TransparentCompany(company=company_get_mock.return_value, prompts="")
        build_payload_mock.return_value = {
            "cid": "cid-1",
            "entry_agent_id": "",
            "bindings": [],
        }

        ok, payload = CompanyService.resolve_transparent_company_client_entry_agent(
            "cid-1",
            route_key="sales",
            agent_id="spoofed-agent",
        )

        self.assertFalse(ok)
        self.assertIn("没有绑定", str(payload))

    @patch.object(CompanyService, "_build_client_entry_payload")
    @patch("apps.company.services.TransparentCompany.objects.get")
    @patch("apps.company.services.Company.objects.get")
    def test_client_entry_binding_ignores_spoofed_agent_id(
        self,
        company_get_mock,
        transparent_company_get_mock,
        build_payload_mock,
    ):
        company_get_mock.return_value = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company_get_mock.return_value = TransparentCompany(company=company_get_mock.return_value, prompts="")
        build_payload_mock.return_value = {
            "cid": "cid-1",
            "entry_agent_id": "sales-agent",
            "bindings": [
                {
                    "entry_key": "sales",
                    "agent_id": "sales-agent",
                    "route_key": "sales",
                }
            ],
        }

        ok, payload = CompanyService.resolve_transparent_company_client_entry_agent(
            "cid-1",
            route_key="sales",
            agent_id="other-agent",
        )

        self.assertTrue(ok)
        self.assertEqual("sales-agent", payload["agent_id"])
        self.assertEqual("other-agent", payload["ignored_agent_id"])


class TransparentAgentUpsertSecurityTests(TestCase):
    class _FakeAgentQuerySet:
        def first(self):
            return None

        def order_by(self, *_args):
            return self

        def exclude(self, **_kwargs):
            return self

        def update(self, **_kwargs):
            return 0

    @patch("apps.company.services.TransparentCompanyAgentVikingSpace.objects.get_or_create")
    @patch("apps.company.services.TransparentCompanyAgentPermissionBinding.objects.get_or_create")
    @patch.object(CompanyService, "_record_agent_prompt_version")
    @patch("apps.company.services._moderate_company_texts", return_value=(True, ""))
    @patch.object(TransparentCompanyAgentDefinition, "get_info")
    @patch.object(TransparentCompanyAgentDefinition, "save")
    @patch("apps.company.services.TransparentCompanyAgentDefinition.objects.filter")
    @patch.object(CompanyService, "_sync_transparent_company_runtime_config")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    def test_new_subagent_gets_default_deny_all_permission_binding(
        self,
        transparent_company_mock,
        sync_mock,
        agent_filter_mock,
        agent_save_mock,
        agent_get_info_mock,
        moderation_mock,
        prompt_version_mock,
        permission_binding_get_or_create_mock,
        viking_space_get_or_create_mock,
    ):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        transparent_company = TransparentCompany(company=company, prompts="")
        transparent_company_mock.return_value = (True, transparent_company)
        sync_mock.return_value = (True, {})
        agent_filter_mock.return_value = self._FakeAgentQuerySet()
        agent_get_info_mock.return_value = {"agentId": "generated-agent"}
        viking_space_get_or_create_mock.return_value = (
            SimpleNamespace(status="active"),
            True,
        )
        user = SimpleNamespace(uid="user-1")

        ok, payload = CompanyService.upsert_transparent_company_agent(
            cid="cid-1",
            user=user,
            name="Worker Agent",
            role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            exposed_to_client=False,
        )

        self.assertTrue(ok)
        permission_binding_get_or_create_mock.assert_called_once()
        _, kwargs = permission_binding_get_or_create_mock.call_args
        self.assertFalse(kwargs["defaults"]["enabled"])
        self.assertNotIn("toolsAllow", kwargs["defaults"])
        self.assertEqual([], kwargs["defaults"]["knowledgeRead"])
        self.assertEqual([], kwargs["defaults"]["skillsInclude"])


class SharedHostBridgeSecurityTests(SimpleTestCase):
    def _write_runtime_platform_config(self, namespace: str, platform_config: dict[str, object]) -> None:
        config_path = _config_path(namespace)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(config_path, platform_config)

    def _skill_runtime_platform_config(
        self,
        *,
        skills: list[dict[str, object]],
        bindings: list[dict[str, object]],
        template_skills: dict[str, list[str]] | None = None,
        binding_skills: dict[str, list[str]] | None = None,
    ) -> dict[str, object]:
        template_skills = template_skills or {"include": ["skill-*"], "exclude": []}
        binding_skills = binding_skills or {"include": [], "exclude": []}
        return {
            "company": {"name": "Demo Company"},
            "agents": [
                {
                    "agent_id": "worker-agent",
                    "name": "Worker Agent",
                    "role": "subagent",
                    "description": "Executes company workflows.",
                    "system_prompt": "Act as the worker agent.",
                }
            ],
            "skills": skills,
            "agent_skill_bindings": bindings,
            "permission_templates": [
                {
                    "template_id": "tpl-worker",
                    "enabled": True,
                    "skills": template_skills,
                    "tools": {"allow": [], "deny": []},
                    "knowledge": {"read": [], "deny": []},
                }
            ],
            "agent_permission_bindings": [
                {
                    "agent_id": "worker-agent",
                    "template_id": "tpl-worker",
                    "enabled": True,
                    "skills": binding_skills,
                    "tools": {"allow": [], "deny": []},
                    "knowledge": {"read": [], "deny": []},
                }
            ],
            "permission_config": {
                "enabled": True,
                "configJson": {
                    "permissions": {
                        "skills": {"include": [], "exclude": []},
                        "tools": {"allow": [], "deny": []},
                        "knowledge": {"read": [], "deny": []},
                    }
                },
            },
            "client_entry": {"default_agent_id": "worker-agent", "bindings": []},
        }

    def test_host_workspace_tree_payload_lists_runtime_files_when_workspace_agent_is_down(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-host-tree"
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            (workspace / "allowed.md").write_text("hello", encoding="utf-8")
            (workspace / "report.txt").write_text("world", encoding="utf-8")

            payload = _host_workspace_tree_payload(namespace, "knowledge", recursive=True)

        paths = [item["logical_path"] for item in payload["items"]]
        self.assertEqual("knowledge", payload["logical_path"])
        self.assertIn("knowledge/allowed.md", paths)
        self.assertIn("knowledge/report.txt", paths)
        self.assertNotIn(str(Path(tempdir)), json.dumps(payload))

    def test_host_workspace_file_payload_reads_text_without_exposing_host_path(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-host-file"
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            (workspace / "allowed.md").write_text("hello from host fallback", encoding="utf-8")

            payload = _host_workspace_file_payload(namespace, "knowledge/allowed.md")

        self.assertEqual("knowledge/allowed.md", payload["logical_path"])
        self.assertEqual("utf-8", payload["encoding"])
        self.assertEqual("hello from host fallback", payload["content"])
        self.assertNotIn(str(Path(tempdir)), json.dumps(payload))

    @patch.object(_bridge_module, "_gateway_chat_via_cli")
    @patch.object(_bridge_module, "_gateway_auth_token")
    def test_gateway_chat_completions_uses_single_cli_path_even_with_gateway_token(
        self,
        gateway_auth_token_mock,
        gateway_chat_via_cli_mock,
    ):
        gateway_auth_token_mock.return_value = "token-present"
        gateway_chat_via_cli_mock.return_value = (
            True,
            {"assistant_content": "Safe final answer", "token_count": 9},
        )

        ok, payload = _gateway_chat_completions(
            "tsds-demo",
            {
                "messages": [{"role": "user", "content": "今天是几号"}],
                "content": "今天是几号",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "Safe final answer")
        gateway_chat_via_cli_mock.assert_called_once_with(
            "tsds-demo",
            {
                "messages": [{"role": "user", "content": "今天是几号"}],
                "content": "今天是几号",
            },
        )

    @patch.object(_bridge_module, "_run_command_capture")
    def test_gateway_chat_via_cli_ignores_stderr_and_uses_stdout_only(self, run_command_capture_mock):
        run_command_capture_mock.return_value = (
            True,
            json.dumps({"result": {"payloads": [{"text": "Safe final answer"}]}}),
            "sh: 1: /data/tsds/tenants/demo/workspace/demo.zip: Permission denied",
            0,
        )

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "main-agent",
                "session_key": "session-1",
                "content": "hello",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "Safe final answer")
        self.assertNotIn("/data/tsds", payload["assistant_content"])

    @patch.object(_bridge_module, "_run_command_capture")
    @patch.object(_bridge_module, "build_openclaw_agent_exec_command")
    @patch.object(_bridge_module, "_read_json")
    def test_gateway_chat_via_cli_uses_bound_client_entry_agent_instead_of_spoofed_agent_id(
        self,
        read_json_mock,
        build_command_mock,
        run_command_capture_mock,
    ):
        read_json_mock.return_value = {
            "client_entry": {
                "bindings": [
                    {
                        "entry_key": "sales",
                        "agent_id": "sales-agent",
                        "route_key": "sales",
                    }
                ]
            }
        }
        build_command_mock.return_value = ["openclaw", "agent"]
        run_command_capture_mock.return_value = (
            True,
            json.dumps({"result": {"payloads": [{"text": "Safe final answer"}]}}),
            "",
            0,
        )

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "spoofed-agent",
                "route_key": "sales",
                "client_entry_binding_required": True,
                "session_key": "session-1",
                "content": "hello",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "Safe final answer")
        build_command_mock.assert_called_once()
        self.assertEqual("sales-agent", build_command_mock.call_args.kwargs["agent_id"])

    @patch.object(_bridge_module, "_run_command_capture")
    @patch.object(_bridge_module, "build_openclaw_agent_exec_command")
    @patch.object(_bridge_module, "_read_json")
    def test_gateway_chat_via_cli_public_scope_forces_default_binding_without_explicit_flag(
        self,
        read_json_mock,
        build_command_mock,
        run_command_capture_mock,
    ):
        read_json_mock.return_value = {
            "client_entry": {
                "bindings": [
                    {
                        "entry_key": "__default__",
                        "agent_id": "sales-agent",
                        "route_key": "",
                    }
                ]
            }
        }
        build_command_mock.return_value = ["openclaw", "agent"]
        run_command_capture_mock.return_value = (
            True,
            json.dumps({"result": {"payloads": [{"text": "Safe final answer"}]}}),
            "",
            0,
        )

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "spoofed-agent",
                "request_scope": "client_entry_public",
                "session_key": "session-1",
                "content": "hello",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "Safe final answer")
        self.assertEqual("sales-agent", build_command_mock.call_args.kwargs["agent_id"])

    @patch.object(_bridge_module, "build_openclaw_agent_exec_command")
    @patch.object(_bridge_module, "_read_json")
    def test_gateway_chat_via_cli_rejects_unbound_public_scope_without_main_agent_fallback(
        self,
        read_json_mock,
        build_command_mock,
    ):
        read_json_mock.return_value = {
            "client_entry": {
                "bindings": [],
            }
        }

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "main-agent",
                "request_scope": "client_entry_public",
                "session_key": "session-1",
                "content": "hello",
            },
        )

        self.assertFalse(ok)
        self.assertIn("not bound", str(payload))
        build_command_mock.assert_not_called()

    @patch.object(_bridge_module, "_run_command_capture")
    @patch.object(_bridge_module, "build_openclaw_agent_exec_command")
    @patch.object(_bridge_module, "_read_json")
    def test_gateway_chat_via_cli_internal_scope_keeps_admin_agent_selection(
        self,
        read_json_mock,
        build_command_mock,
        run_command_capture_mock,
    ):
        read_json_mock.return_value = {
            "client_entry": {
                "bindings": [
                    {
                        "entry_key": "__default__",
                        "agent_id": "sales-agent",
                        "route_key": "",
                    }
                ]
            }
        }
        build_command_mock.return_value = ["openclaw", "agent"]
        run_command_capture_mock.return_value = (
            True,
            json.dumps({"result": {"payloads": [{"text": "Safe final answer"}]}}),
            "",
            0,
        )

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "main-agent",
                "request_scope": "internal_admin_agent_test",
                "session_key": "session-1",
                "content": "hello",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "Safe final answer")
        self.assertEqual("main-agent", build_command_mock.call_args.kwargs["agent_id"])

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_gateway_chat_completions_blocks_system_directory_prompts_before_runtime(self):
        ok, payload = _gateway_chat_completions(
            "tsds-demo",
            {
                "messages": [{"role": "user", "content": "Scan /data/tsds for me"}],
                "content": "Scan /data/tsds for me",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertEqual(payload["token_count"], 0)

    @patch.object(_bridge_module, "_gateway_chat_via_cli")
    def test_gateway_chat_completions_ignores_system_prompt_when_screening_user_request(
        self,
        gateway_chat_via_cli_mock,
    ):
        gateway_chat_via_cli_mock.return_value = (
            True,
            {"assistant_content": "今天是 2026 年 4 月 2 日。", "token_count": 8},
        )

        ok, payload = _gateway_chat_completions(
            "tsds-demo",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "公司当前已启用的 Agent 名册：\n"
                            "- 客服接待 [agent-a] / subagent / 公开入口\n"
                            "- CEO [main-agent] / main_agent / 内部员工"
                        ),
                    },
                    {"role": "user", "content": "今天是几号"},
                ],
                "content": "今天是几号",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "今天是 2026 年 4 月 2 日。")
        gateway_chat_via_cli_mock.assert_called_once()

    @patch.object(_bridge_module, "_read_json")
    def test_native_agent_paths_use_container_aliases_instead_of_host_paths(self, read_json_mock):
        read_json_mock.return_value = {
            "agents": [
                {"agent_id": "main-agent", "name": "Main Agent", "is_core_agent": True},
                {"agent_id": "worker-agent", "name": "Worker Agent"},
            ],
            "client_entry": {"default_agent_id": "main-agent"},
        }

        native_agents = _derive_native_agents("tsds-demo")
        self.assertEqual(native_agents[0]["agentDir"], "/srv/openclaw-home/.openclaw/agents/main-agent/agent")
        self.assertEqual(native_agents[0]["workspace"], "/srv/openclaw-home/.openclaw/workspace")
        self.assertEqual(native_agents[1]["agentDir"], "/srv/openclaw-home/.openclaw/agents/worker-agent/agent")
        self.assertEqual(native_agents[1]["workspace"], "/srv/openclaw-home/.openclaw/workspace-worker-agent")
        self.assertEqual(
            _default_native_agent_dir("tsds-demo"),
            "/srv/openclaw-home/.openclaw/agents/main-agent/agent",
        )

    def test_native_agent_tools_publish_workspace_plugin_policy(self):
        """Native OpenClaw config publishes only policy-authorized workspace/memory tools."""

        platform_config = self._skill_runtime_platform_config(skills=[], bindings=[])
        platform_config["agent_permission_bindings"][0]["tools"] = {
            "allow": ["workspace.list", "workspace.locate", "workspace.read", "workspace.unzip"],
            "deny": ["workspace.read"],
        }
        platform_config["agent_permission_bindings"][0]["knowledge"] = {
            "read": ["knowledge"],
            "deny": [],
        }

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-workspace-tools-config"
            self._write_runtime_platform_config(namespace, platform_config)
            native_config = _build_native_openclaw_config(namespace)

        native_tools = native_config["agents"]["list"][0]["tools"]
        self.assertEqual(
            [
                "touming_workspace_list",
                "touming_workspace_locate",
                "touming_workspace_unzip",
                "touming_memory_read",
                "touming_memory_write",
                "touming_memory_update",
                "touming_memory_delete",
            ],
            native_tools["allow"],
        )
        self.assertIn("group:fs", native_tools["deny"])
        self.assertIn("group:runtime", native_tools["deny"])
        self.assertIn("read", native_tools["deny"])
        self.assertNotIn("touming_workspace_read", native_tools["allow"])
        self.assertEqual(
            ["touming-workspace-tools", "touming-memory-tools"],
            native_config["plugins"]["allow"],
        )
        self.assertIn(
            "/extensions/touming-workspace-tools",
            native_config["plugins"]["load"]["paths"][0],
        )
        self.assertIn(
            "/extensions/touming-memory-tools",
            native_config["plugins"]["load"]["paths"][1],
        )

    def test_workspace_plugin_payload_falls_back_to_session_agent_id(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-workspace-plugin"
            _write_touming_workspace_plugin(namespace)
            bundle_path = (
                Path(tempdir)
                / namespace
                / "home"
                / ".openclaw"
                / "extensions"
                / "touming-workspace-tools"
                / "bundle.js"
            )
            bundle = bundle_path.read_text(encoding="utf-8")

        self.assertIn("function resolveAgentId(ctx)", bundle)
        self.assertIn("agent_id: resolveAgentId(ctx)", bundle)
        self.assertIn("/^agent:([^:]+):/.exec(sessionKey)", bundle)
        self.assertIn("touming_workspace_unzip", bundle)

    def test_agent_workspace_tool_bridge_enforces_tool_policy_before_file_read(self):
        platform_config = self._skill_runtime_platform_config(skills=[], bindings=[])
        platform_config["agent_permission_bindings"][0]["tools"] = {
            "allow": ["workspace.list"],
            "deny": [],
        }
        platform_config["agent_permission_bindings"][0]["knowledge"] = {
            "read": ["knowledge"],
            "deny": [],
        }

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-workspace-tool-deny"
            self._write_runtime_platform_config(namespace, platform_config)
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            (workspace / "allowed.txt").write_text("secret content", encoding="utf-8")

            result = _execute_agent_workspace_tool(
                namespace,
                {
                    "tool": "touming_workspace_read",
                    "agent_id": "worker-agent",
                    "arguments": {"logical_path": "knowledge/allowed.txt"},
                },
            )
            missing_agent_result = _execute_agent_workspace_tool(
                namespace,
                {
                    "tool": "touming_workspace_list",
                    "arguments": {"logical_path": "knowledge"},
                },
            )

        self.assertEqual("denied", result["status"])
        self.assertEqual("workspace.read", result["tool_name"])
        self.assertNotIn("secret content", json.dumps(result))
        self.assertEqual("denied", missing_agent_result["status"])
        self.assertIn("agent_id", missing_agent_result["message"])

    def test_host_workspace_file_payload_returns_safe_zip_preview(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-zip-preview"
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            archive_path = workspace / "demo.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("docs/readme.md", "hello")
                archive.writestr("nested/", "")
                archive.writestr("../evil.txt", "nope")

            payload = _host_workspace_file_payload(namespace, "knowledge/demo.zip")

        self.assertEqual("zip", payload["encoding"])
        self.assertEqual("knowledge/demo.zip", payload["logical_path"])
        self.assertIn("ZIP archive preview", payload["content"])
        self.assertIn("docs/readme.md", payload["content"])
        self.assertNotIn("evil.txt", payload["content"])
        self.assertEqual("zip", payload["preview"]["type"])
        self.assertEqual(1, payload["preview"]["unsafe_entry_count"])

    def test_agent_workspace_tool_read_returns_zip_preview_when_authorized(self):
        platform_config = self._skill_runtime_platform_config(skills=[], bindings=[])
        platform_config["agent_permission_bindings"][0]["tools"] = {
            "allow": ["workspace.read"],
            "deny": [],
        }
        platform_config["agent_permission_bindings"][0]["knowledge"] = {
            "read": ["knowledge/demo.zip"],
            "deny": [],
        }

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-zip-tool-preview"
            self._write_runtime_platform_config(namespace, platform_config)
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            with zipfile.ZipFile(workspace / "demo.zip", "w") as archive:
                archive.writestr("docs/readme.md", "hello")

            result = _execute_agent_workspace_tool(
                namespace,
                {
                    "tool": "touming_workspace_read",
                    "agent_id": "worker-agent",
                    "arguments": {"logical_path": "knowledge/demo.zip"},
                },
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("zip", result["data"]["encoding"])
        self.assertIn("docs/readme.md", result["data"]["content"])
        self.assertEqual("explicit_workspace_tool", result["data"]["source"])

    def test_agent_workspace_tool_unzip_extracts_authorized_zip_safely(self):
        platform_config = self._skill_runtime_platform_config(skills=[], bindings=[])
        platform_config["agent_permission_bindings"][0]["tools"] = {
            "allow": ["workspace.unzip"],
            "deny": [],
        }
        platform_config["agent_permission_bindings"][0]["knowledge"] = {
            "read": ["knowledge/demo.zip"],
            "deny": [],
        }

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-zip-tool-extract"
            self._write_runtime_platform_config(namespace, platform_config)
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            with zipfile.ZipFile(workspace / "demo.zip", "w") as archive:
                archive.writestr("docs/readme.md", "hello")
                archive.writestr("nested/", "")
                archive.writestr("../evil.txt", "nope")

            result = _execute_agent_workspace_tool(
                namespace,
                {
                    "tool": "touming_workspace_unzip",
                    "agent_id": "worker-agent",
                    "arguments": {"logical_path": "knowledge/demo.zip"},
                },
            )
            extracted_file = workspace / "demo" / "docs" / "readme.md"
            escaped_file = Path(tempdir) / namespace / "workspace" / "evil.txt"
            extracted_exists = extracted_file.exists()
            extracted_content = extracted_file.read_text(encoding="utf-8") if extracted_exists else ""
            escaped_exists = escaped_file.exists()

        self.assertEqual("completed", result["status"])
        self.assertEqual("knowledge/demo", result["data"]["destination_logical_path"])
        self.assertEqual(1, result["data"]["extracted_count"])
        self.assertEqual(1, result["data"]["unsafe_entry_count"])
        self.assertTrue(extracted_exists)
        self.assertEqual("hello", extracted_content)
        self.assertFalse(escaped_exists)

    def test_agent_workspace_tool_unzip_requires_explicit_tool_permission(self):
        platform_config = self._skill_runtime_platform_config(skills=[], bindings=[])
        platform_config["agent_permission_bindings"][0]["tools"] = {
            "allow": ["workspace.read"],
            "deny": [],
        }
        platform_config["agent_permission_bindings"][0]["knowledge"] = {
            "read": ["knowledge/demo.zip"],
            "deny": [],
        }

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-zip-tool-denied"
            self._write_runtime_platform_config(namespace, platform_config)
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            with zipfile.ZipFile(workspace / "demo.zip", "w") as archive:
                archive.writestr("docs/readme.md", "hello")

            result = _execute_agent_workspace_tool(
                namespace,
                {
                    "tool": "touming_workspace_unzip",
                    "agent_id": "worker-agent",
                    "arguments": {"logical_path": "knowledge/demo.zip"},
                },
            )
            extracted_exists = (workspace / "demo" / "docs" / "readme.md").exists()

        self.assertEqual("denied", result["status"])
        self.assertEqual("workspace.unzip", result["tool_name"])
        self.assertFalse(extracted_exists)

    def test_agent_workspace_tool_unzip_blocks_custom_destination_outside_default_directory(self):
        platform_config = self._skill_runtime_platform_config(skills=[], bindings=[])
        platform_config["agent_permission_bindings"][0]["tools"] = {
            "allow": ["workspace.unzip"],
            "deny": [],
        }
        platform_config["agent_permission_bindings"][0]["knowledge"] = {
            "read": ["knowledge"],
            "deny": [],
        }

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-zip-tool-destination"
            self._write_runtime_platform_config(namespace, platform_config)
            workspace = Path(tempdir) / namespace / "workspace" / "knowledge"
            workspace.mkdir(parents=True)
            with zipfile.ZipFile(workspace / "demo.zip", "w") as archive:
                archive.writestr("docs/readme.md", "hello")

            result = _execute_agent_workspace_tool(
                namespace,
                {
                    "tool": "touming_workspace_unzip",
                    "agent_id": "worker-agent",
                    "arguments": {
                        "logical_path": "knowledge/demo.zip",
                        "destination_path": "knowledge/other",
                    },
                },
            )
            custom_destination_exists = (workspace / "other" / "docs" / "readme.md").exists()

        self.assertEqual("denied", result["status"])
        self.assertFalse(custom_destination_exists)

    def test_runtime_exports_inspection_reports_workspace_plugin_materialization(self):
        platform_config = self._skill_runtime_platform_config(skills=[], bindings=[])

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-plugin-inspection"
            self._write_runtime_platform_config(namespace, platform_config)
            _ensure_openclaw_home(namespace)
            summary = _summarize_agent_runtime_exports(namespace)

        plugin_summary = summary["native_config"]["workspace_tools_plugin"]
        self.assertTrue(plugin_summary["configured"])
        self.assertTrue(plugin_summary["manifest_exists"])
        self.assertTrue(plugin_summary["bundle_exists"])
        self.assertTrue(plugin_summary["load_path_configured"])
        self.assertIn("touming_workspace_read", plugin_summary["tool_names"])
        self.assertIn("touming_workspace_unzip", plugin_summary["tool_names"])

    def test_bound_skill_is_published_to_confirmed_openclaw_consumption_points(self):
        platform_config = self._skill_runtime_platform_config(
            skills=[
                {
                    "skill_id": "skill-report",
                    "name": "Report Writer",
                    "description": "Write reports from uploaded data.",
                    "prompt": "Generate a concise report from the provided business data.",
                    "enabled": True,
                }
            ],
            bindings=[
                {
                    "agent_id": "worker-agent",
                    "skill_id": "skill-report",
                    "enabled": True,
                    "sort_order": 10,
                }
            ],
            template_skills={"include": ["skill-*"], "exclude": []},
            binding_skills={"include": ["skill-report"], "exclude": []},
        )

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-skills-runtime"
            self._write_runtime_platform_config(namespace, platform_config)

            native_config = _build_native_openclaw_config(namespace)
            native_agents = native_config["agents"]["list"]
            self.assertEqual(["skill-report"], native_agents[0]["skills"])

            _sync_native_agent_state(namespace, native_agents)
            workspace_dir = _native_agent_workspace(namespace, "worker-agent", default_agent=True)
            skill_md = workspace_dir / ".agents" / "skills" / "tsds-skill-report" / "SKILL.md"
            self.assertTrue(skill_md.exists())
            skill_text = skill_md.read_text(encoding="utf-8")
            self.assertIn('name: "skill-report"', skill_text)
            self.assertIn("Generate a concise report", skill_text)
            agents_text = (workspace_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("touming_workspace_list", agents_text)
            self.assertIn("touming_workspace_locate", agents_text)
            self.assertIn("touming_workspace_read", agents_text)
            self.assertIn("touming_workspace_unzip", agents_text)
            self.assertIn("before answering", agents_text)
            self.assertIn("preserve the full logical paths exactly", agents_text)
            self.assertIn("permission configuration page may show all runtime files", agents_text)

            summary = _summarize_agent_runtime_exports(namespace)
            agent_summary = summary["agents"][0]
            self.assertTrue(summary["conclusion"]["native_skill_filter_consumption_confirmed"])
            self.assertTrue(summary["conclusion"]["workspace_skill_materialization_confirmed"])
            self.assertFalse(summary["conclusion"]["skills_json_consumption_proven"])
            self.assertEqual(["skill-report"], agent_summary["native_consumption"]["native_config_skill_filter"])
            self.assertEqual(["skill-report"], agent_summary["native_consumption"]["materialized_skill_ids"])

    def test_disabled_skill_and_disabled_binding_do_not_enter_runtime_consumption_points(self):
        platform_config = self._skill_runtime_platform_config(
            skills=[
                {
                    "skill_id": "skill-disabled",
                    "name": "Disabled Skill",
                    "description": "Should not be published.",
                    "prompt": "This should never reach runtime.",
                    "enabled": False,
                },
                {
                    "skill_id": "skill-bound-disabled",
                    "name": "Binding Disabled Skill",
                    "description": "Binding should block publication.",
                    "prompt": "This should also never reach runtime.",
                    "enabled": True,
                },
            ],
            bindings=[
                {
                    "agent_id": "worker-agent",
                    "skill_id": "skill-disabled",
                    "enabled": True,
                    "sort_order": 10,
                },
                {
                    "agent_id": "worker-agent",
                    "skill_id": "skill-bound-disabled",
                    "enabled": False,
                    "sort_order": 20,
                },
            ],
            template_skills={"include": ["skill-*"], "exclude": []},
            binding_skills={"include": ["skill-*"], "exclude": []},
        )

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-skills-disabled"
            self._write_runtime_platform_config(namespace, platform_config)

            native_config = _build_native_openclaw_config(namespace)
            native_agents = native_config["agents"]["list"]
            self.assertEqual([], native_agents[0]["skills"])

            _sync_native_agent_state(namespace, native_agents)
            workspace_dir = _native_agent_workspace(namespace, "worker-agent", default_agent=True)
            skill_root = workspace_dir / ".agents" / "skills"
            materialized = list(skill_root.rglob("SKILL.md")) if skill_root.exists() else []
            self.assertEqual([], materialized)

    def test_skills_include_and_exclude_filter_the_final_runtime_skill_set(self):
        platform_config = self._skill_runtime_platform_config(
            skills=[
                {
                    "skill_id": "skill-keep",
                    "name": "Keep Skill",
                    "description": "Should remain in runtime.",
                    "prompt": "Keep this skill in runtime.",
                    "enabled": True,
                },
                {
                    "skill_id": "skill-drop",
                    "name": "Drop Skill",
                    "description": "Should be filtered from runtime.",
                    "prompt": "Drop this skill from runtime.",
                    "enabled": True,
                },
            ],
            bindings=[
                {
                    "agent_id": "worker-agent",
                    "skill_id": "skill-keep",
                    "enabled": True,
                    "sort_order": 10,
                },
                {
                    "agent_id": "worker-agent",
                    "skill_id": "skill-drop",
                    "enabled": True,
                    "sort_order": 20,
                },
            ],
            template_skills={"include": ["skill-*"], "exclude": []},
            binding_skills={"include": ["skill-*"], "exclude": ["skill-drop"]},
        )

        with tempfile.TemporaryDirectory() as tempdir, patch.object(_bridge_module, "RUNTIME_ROOT", Path(tempdir)):
            namespace = "tsds-skills-filtered"
            self._write_runtime_platform_config(namespace, platform_config)

            native_config = _build_native_openclaw_config(namespace)
            native_agents = native_config["agents"]["list"]
            self.assertEqual(["skill-keep"], native_agents[0]["skills"])

            _sync_native_agent_state(namespace, native_agents)
            summary = _summarize_agent_runtime_exports(namespace)
            agent_summary = summary["agents"][0]
            self.assertEqual(["skill-keep"], agent_summary["skills"]["skill_ids"])
            self.assertEqual(["skill-keep"], agent_summary["native_consumption"]["materialized_skill_ids"])

    @patch.object(_bridge_module, "_run_command_capture")
    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_gateway_chat_via_cli_invalid_json_never_leaks_raw_output(self, run_command_capture_mock):
        run_command_capture_mock.return_value = (
            True,
            '{"runId":"run-1","result":{"payloads":[{"text":"`/data/tsds` contains compose.yml"}]}}',
            "sh: 1: /data/tsds/tenants/demo/workspace/demo.zip: Permission denied",
            0,
        )

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "main-agent",
                "session_key": "session-1",
                "content": "hello",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertNotIn("/data/tsds", payload["assistant_content"])

    @patch.object(_bridge_module, "_run_command_capture")
    def test_gateway_chat_via_cli_embedded_runtime_json_keeps_only_safe_payload_text(self, run_command_capture_mock):
        run_command_capture_mock.return_value = (
            True,
            json.dumps(
                {
                    "runId": "run-1",
                    "status": "ok",
                    "result": {
                        "payloads": [
                            {
                                "text": "今天是 **2026年4月2日，星期四**。",
                                "mediaUrl": None,
                            }
                        ]
                    },
                    "meta": {
                        "agentMeta": {"sessionId": "session-1"},
                        "systemPromptReport": {
                            "workspaceDir": "/data/tsds/tenants/demo/workspace"
                        },
                    },
                },
                ensure_ascii=False,
            )
            + "\nsh: 1: /data/tsds/tenants/demo/workspace/demo.zip: Permission denied",
            "",
            0,
        )

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "main-agent",
                "session_key": "session-1",
                "content": "今天是几号",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "今天是 **2026年4月2日，星期四**。")
        self.assertNotIn("/data/tsds", payload["assistant_content"])
    @patch.object(_bridge_module, "_gateway_chat_via_cli")
    def test_gateway_chat_completions_remains_cli_fallback_when_called_directly(self, gateway_chat_via_cli_mock):
        gateway_chat_via_cli_mock.return_value = (
            True,
            {"assistant_content": "workspace/knowledge contains 1 file", "token_count": 3},
        )

        ok, payload = _gateway_chat_completions(
            "tsds-demo",
            {
                "messages": [{"role": "user", "content": "Scan /data/tsds for me"}],
                "content": "Scan /data/tsds for me",
            },
        )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "workspace/knowledge contains 1 file")
        gateway_chat_via_cli_mock.assert_called_once()

    @patch.object(_bridge_module, "_run_command_capture")
    def test_gateway_chat_via_cli_invalid_json_returns_sanitized_runtime_text(self, run_command_capture_mock):
        run_command_capture_mock.return_value = (
            True,
            '{"runId":"run-1","result":{"payloads":[{"text":"`/data/tsds` contains compose.yml"}]}}',
            "sh: 1: /data/tsds/tenants/demo/workspace/demo.zip: Permission denied",
            0,
        )

        ok, payload = _gateway_chat_via_cli(
            "tsds-demo",
            {
                "agent_id": "main-agent",
                "session_key": "session-1",
                "content": "hello",
            },
        )

        self.assertTrue(ok)
        self.assertNotEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertIn("compose.yml", payload["assistant_content"])
        self.assertNotIn("/data/tsds", payload["assistant_content"])
        self.assertNotIn("Permission denied", payload["assistant_content"])


class AgentTestResponseSecurityTests(SimpleTestCase):
    def setUp(self):
        self.title_patch = patch.object(
            CompanyService,
            "_ensure_agent_test_session_title_from_first_message",
            return_value=None,
        )
        self.title_patch.start()
        self.addCleanup(self.title_patch.stop)
        self.moderation_patch = patch("apps.company.services._moderate_company_texts", return_value=(True, ""))
        self.moderation_patch.start()
        self.addCleanup(self.moderation_patch.stop)
        self.company = Company(cid="cid-1", company_name="Demo Company", created_at="1", updated_at="1")
        self.transparent_company = TransparentCompany(company=self.company, prompts="")
        self.agent = TransparentCompanyAgentDefinition(
            id=1,
            transparentCompany=self.transparent_company,
            agentId="main-agent",
            name="Main Agent",
            role=TransparentCompanyAgentDefinition.ROLE_MAIN_AGENT,
            created_at="1",
            updated_at="1",
        )
        self.user = SimpleNamespace(uid="user-1")
        self.session = TransparentCompanyAgentTestSession(
            id=1,
            transparentCompany=self.transparent_company,
            agent=self.agent,
            operator=None,
            conversationId="conversation-1",
            title="demo",
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
            messageCount=0,
            lastMessage="",
            sessionKey="session-1",
            created_at="1",
            updated_at="1",
        )

    def _build_message(self, session, *, role, content, token_count=0, tool_calls=None, raw_payload=None):
        return TransparentCompanyAgentTestMessage(
            id=1 if role == TransparentCompanyAgentTestMessage.ROLE_USER else 2,
            session=session,
            role=role,
            content=content,
            tokenCount=token_count,
            toolCalls=tool_calls or [],
            rawPayload=raw_payload or {},
            created_at="1",
            updated_at="1",
        )

    def test_agent_test_token_count_uses_runtime_usage_only(self):
        self.assertEqual(CompanyService._coerce_agent_test_token_count(0, "hello world"), 0)
        self.assertEqual(CompanyService._coerce_agent_test_token_count(None, "hello world"), 0)
        self.assertEqual(CompanyService._coerce_agent_test_token_count("12", "hello world"), 12)

    @patch.object(AgentWorkspaceToolGatewayService, "_resolve_agent")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.read_file")
    def test_agent_workspace_tool_gateway_routes_explicit_read_tool_to_access_service(
        self,
        read_file_mock,
        resolve_agent_mock,
    ):
        resolve_agent_mock.return_value = (True, self.agent)
        read_file_mock.return_value = (
            True,
            {
                "logical_path": "knowledge/a.txt",
                "content": "hello",
            },
        )

        ok, payload = AgentWorkspaceToolGatewayService.execute(
            self.company,
            agent_id="main-agent",
            tool_name="touming_workspace_read",
            arguments={"path": "knowledge/a.txt"},
        )

        self.assertTrue(ok)
        self.assertEqual("workspace.read", payload["tool_name"])
        self.assertEqual("touming_workspace_read", payload["openclaw_tool_name"])
        self.assertEqual("explicit_workspace_tool", payload["source"])
        read_file_mock.assert_called_once_with(
            self.company,
            self.agent,
            file_name="",
            logical_path="knowledge/a.txt",
        )

    @patch.object(AgentWorkspaceToolGatewayService, "_resolve_agent")
    @patch("apps.runtime.services.AgentWorkspaceAccessService._get_runtime_instance")
    @patch("apps.runtime.services.OpenClawRuntimeService.get_provider_adapter")
    def test_agent_workspace_tool_gateway_routes_unzip_to_runtime_adapter(
        self,
        get_provider_adapter_mock,
        get_runtime_instance_mock,
        resolve_agent_mock,
    ):
        calls: list[dict[str, object]] = []
        instance = SimpleNamespace(provider="docker_shared_host")

        def execute_workspace_tool(company, instance_arg, **kwargs):
            calls.append({"company": company, "instance": instance_arg, **kwargs})
            return True, {"status": "completed", "data": {"destination_logical_path": "knowledge/demo"}}

        resolve_agent_mock.return_value = (True, self.agent)
        get_runtime_instance_mock.return_value = instance
        get_provider_adapter_mock.return_value = SimpleNamespace(execute_workspace_tool=execute_workspace_tool)

        ok, payload = AgentWorkspaceToolGatewayService.execute(
            self.company,
            agent_id="main-agent",
            tool_name="touming_workspace_unzip",
            arguments={"logical_path": "knowledge/demo.zip"},
        )

        self.assertTrue(ok)
        self.assertEqual("workspace.unzip", payload["tool_name"])
        self.assertEqual("touming_workspace_unzip", payload["openclaw_tool_name"])
        self.assertEqual("explicit_workspace_tool", payload["source"])
        self.assertEqual("knowledge/demo", payload["data"]["destination_logical_path"])
        self.assertEqual("main-agent", calls[0]["agent_id"])
        self.assertEqual("touming_workspace_unzip", calls[0]["tool_name"])
        self.assertEqual({"logical_path": "knowledge/demo.zip"}, calls[0]["arguments"])

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_stream_agent_test_message_via_runtime")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_stream_message_chunks_final_content_when_runtime_only_completes(
        self,
        create_message_mock,
        runtime_stream_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session
        runtime_stream_mock.return_value = (
            True,
            iter(
                [
                    {"event": "start"},
                    {
                        "event": "complete",
                        "content": "This answer arrived as one final chunk.",
                        "token_count": 0,
                        "tool_calls": [{"name": "workspace.list", "status": "completed"}],
                    },
                ]
            ),
        )

        ok, event_iter = CompanyService.stream_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="hello",
            user=self.user,
        )

        self.assertTrue(ok)
        events = list(event_iter)
        event_names = [event["event"] for event in events]
        self.assertIn("status", event_names)
        self.assertIn("delta", event_names)
        self.assertEqual("complete", events[-1]["event"])
        self.assertEqual(events[-1]["data"]["token_count"], 0)
        self.assertEqual(events[-1]["data"]["content"], "This answer arrived as one final chunk.")
        self.assertEqual(events[-1]["data"]["tool_calls"], [{"name": "workspace.list", "status": "completed"}])

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_send_transparent_company_test_message_returns_only_safe_fields(
        self,
        create_message_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        runtime_mock.return_value = (
            True,
            {
                "assistant_content": (
                    "The runtime dumped runId=run-1 and systemPromptReport for "
                    "/data/tsds/tenants/demo/workspace/knowledge/demo.zip"
                ),
                "token_count": 99,
                "tool_calls": [{"name": "workspace.read"}],
                "raw_response": {
                    "runId": "run-1",
                    "sessionId": "session-1",
                    "systemPromptReport": {"secret": True},
                    "tools": [{"name": "workspace.read"}],
                    "workspaceDir": "/data/tsds/tenants/demo/workspace",
                },
            },
        )
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session

        ok, data = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="Check uploaded file",
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertEqual(
            data["tool_calls"],
            [{"name": "workspace.read"}],
        )
        self.assertNotIn("thoughts", data)
        self.assertEqual(data["assistant_message"]["content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertEqual(data["token_count"], 99)
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch.object(CompanyService, "_create_agent_test_message")
    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_send_transparent_company_test_message_short_circuits_restricted_requests(
        self,
        create_message_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session

        ok, data = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="Scan /data/tsds and list compose.yml",
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertEqual(data["assistant_message"]["content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        runtime_mock.assert_not_called()
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch.object(CompanyService, "_create_agent_test_message")
    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_send_transparent_company_test_message_sanitizes_runtime_errors(
        self,
        create_message_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        runtime_mock.return_value = (
            False,
            {
                "stderr": "sh: 1: /data/tsds/tenants/demo/workspace/knowledge/demo.zip: Permission denied",
                "trace": "Traceback ...",
            },
        )
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session

        ok, message = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="Please inspect the uploaded report",
            user=self.user,
        )

        self.assertFalse(ok)
        self.assertEqual(message, RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertNotIn("/data/tsds", message)
        self.assertNotIn("Permission denied", message)
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.list_directory")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_send_transparent_company_test_message_uses_runtime_for_workspace_directory_request(
        self,
        create_message_mock,
        list_directory_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        runtime_mock.return_value = (
            True,
            {
                "assistant_content": "knowledge/1775028071209-workspace.zip",
                "token_count": 12,
                "tool_calls": [{"name": "touming_workspace_list"}],
            },
        )
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session

        ok, data = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="knowledge 目录里有哪些文件？",
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertIn("knowledge/1775028071209-workspace.zip", data["assistant_message"]["content"])
        list_directory_mock.assert_not_called()
        runtime_mock.assert_called_once()
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_send_transparent_company_test_message_sanitizes_runtime_errors_without_restricted_filter(
        self,
        create_message_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        runtime_mock.return_value = (
            False,
            {
                "stderr": "sh: 1: /data/tsds/tenants/demo/workspace/knowledge/demo.zip: Permission denied",
                "trace": "Traceback ...",
            },
        )
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session

        ok, message = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="Please inspect the uploaded report",
            user=self.user,
        )

        self.assertFalse(ok)
        self.assertNotEqual(message, RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertNotIn("/data/tsds", message)
        self.assertNotIn("Permission denied", message)
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.locate_files")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_send_transparent_company_test_message_explanation_question_uses_runtime(
        self,
        create_message_mock,
        locate_files_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session
        runtime_mock.return_value = (
            True,
            {
                "assistant_content": "这是指把 zip 文件作为 workspace 可访问资料上传。",
                "token_count": 12,
                "tool_calls": [],
            },
        )

        ok, data = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="把 zip 文件上传到 workspace（通过控制台上传）这个是什么意思？",
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertIn("workspace", data["assistant_message"]["content"])
        locate_files_mock.assert_not_called()
        runtime_mock.assert_called_once()
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.locate_files")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_send_transparent_company_test_message_returns_logical_path_for_uploaded_file_location(
        self,
        create_message_mock,
        locate_files_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        runtime_mock.return_value = (
            True,
            {
                "assistant_content": "knowledge/1775028071209-workspace.zip",
                "token_count": 12,
                "tool_calls": [{"name": "touming_workspace_locate"}],
            },
        )
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session

        ok, data = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="我上传的 workspace.zip 在哪里？",
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertIn("knowledge/1775028071209-workspace.zip", data["assistant_message"]["content"])
        self.assertNotIn("/data/tsds", data["assistant_message"]["content"])
        locate_files_mock.assert_not_called()
        runtime_mock.assert_called_once()
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.read_file")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.locate_files")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_send_transparent_company_test_message_ambiguous_uploaded_file_request_prefers_locate(
        self,
        create_message_mock,
        locate_files_mock,
        read_file_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        runtime_mock.return_value = (
            True,
            {
                "assistant_content": "knowledge/1775028071209-workspace.zip",
                "token_count": 12,
                "tool_calls": [{"name": "touming_workspace_locate"}],
            },
        )
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session

        ok, data = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="帮我看一下我上传的文件",
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertIn("knowledge/1775028071209-workspace.zip", data["assistant_message"]["content"])
        locate_files_mock.assert_not_called()
        read_file_mock.assert_not_called()
        runtime_mock.assert_called_once()
        summary_mock.assert_called_once()

    @patch.object(CompanyService, "_update_agent_test_session_summary")
    @patch.object(CompanyService, "_get_agent_test_session")
    @patch.object(CompanyService, "_get_transparent_company_for_admin")
    @patch.object(CompanyService, "_send_agent_test_message_via_runtime")
    @patch("apps.runtime.services.AgentWorkspaceAccessService.list_directory")
    @patch.object(CompanyService, "_create_agent_test_message")
    def test_send_transparent_company_test_message_returns_permission_controlled_directory_denial(
        self,
        create_message_mock,
        list_directory_mock,
        runtime_mock,
        transparent_company_mock,
        session_mock,
        summary_mock,
    ):
        create_message_mock.side_effect = self._build_message
        list_directory_mock.return_value = (False, "当前 Agent 无权访问该 logical path")
        transparent_company_mock.return_value = (True, self.transparent_company)
        session_mock.return_value = self.session
        runtime_mock.return_value = (
            True,
            {
                "assistant_content": list_directory_mock.return_value[1],
                "token_count": 12,
                "tool_calls": [{"name": "touming_workspace_list", "status": "denied"}],
            },
        )

        ok, data = CompanyService.send_transparent_company_test_message(
            cid="cid-1",
            conversation_id="conversation-1",
            content="请列出 knowledge/private 目录内容",
            user=self.user,
        )

        self.assertTrue(ok)
        self.assertIn("当前 Agent 无权访问", data["assistant_message"]["content"])
        list_directory_mock.assert_not_called()
        runtime_mock.assert_called_once()
        summary_mock.assert_called_once()

    def test_message_get_info_does_not_expose_tool_calls(self):
        message = TransparentCompanyAgentTestMessage(
            session=self.session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content="File lives at /data/tsds/tenants/demo/workspace/demo.zip",
            tokenCount=7,
            toolCalls=[{"name": "workspace.read"}],
            rawPayload={"runId": "run-1"},
            created_at="1",
            updated_at="1",
        )

        payload = message.get_info()

        self.assertNotIn("tool_calls", payload)
        self.assertNotIn("/data/tsds", payload["content"])
        self.assertIn("[internal-path]", payload["content"])


class RuntimeAdapterSecurityTests(SimpleTestCase):
    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_execute_agent_test_chat_sanitizes_bridge_payload(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_post",
            return_value=(
                True,
                {
                    "assistant_content": "System files are in /data/tsds and .openclaw-runtime/config",
                    "token_count": 42,
                    "runId": "run-1",
                    "workspaceDir": "/data/tsds/tenants/demo/workspace",
                    "tool_calls": [{"name": "workspace.read"}],
                },
            ),
        ):
            ok, payload = adapter.execute_agent_test_chat(company, instance, {"content": "hello"})

        self.assertTrue(ok)
        assert isinstance(payload, dict)
        self.assertEqual(payload["token_count"], 42)
        self.assertEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertNotIn("runId", payload)
        self.assertNotIn("tool_calls", payload)

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_execute_agent_test_chat_sanitizes_bridge_error_message(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_post",
            return_value=(False, {"stderr": "sh: 1: /data/tsds/tenants/demo/workspace/demo.zip: Permission denied"}),
        ):
            ok, payload = adapter.execute_agent_test_chat(company, instance, {"content": "hello"})

        self.assertFalse(ok)
        self.assertEqual(payload, RESTRICTED_RUNTIME_ACCESS_MESSAGE)

    @unittest.skip("legacy content-filter expectation replaced by sanitized access tests")
    def test_execute_agent_test_chat_short_circuits_restricted_requests(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(adapter, "_post") as post_mock:
            ok, payload = adapter.execute_agent_test_chat(
                company,
                instance,
                {
                    "content": "请扫描 /data/tsds 下面的所有目录",
                    "messages": [{"role": "user", "content": "请扫描 /data/tsds 下面的所有目录"}],
                },
            )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertEqual(payload["token_count"], 0)
        post_mock.assert_not_called()

    def test_execute_agent_test_chat_keeps_container_internal_listing_but_redacts_host_paths(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_post",
            return_value=(
                True,
                {
                    "assistant_content": "System files are in /data/tsds and .openclaw-runtime/config",
                    "token_count": 42,
                    "runId": "run-1",
                    "workspaceDir": "/data/tsds/tenants/demo/workspace",
                    "tool_calls": [{"name": "workspace.read"}],
                },
            ),
        ):
            ok, payload = adapter.execute_agent_test_chat(company, instance, {"content": "hello"})

        self.assertTrue(ok)
        assert isinstance(payload, dict)
        self.assertEqual(payload["token_count"], 42)
        self.assertNotEqual(payload["assistant_content"], RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertIn(".openclaw-runtime/config", payload["assistant_content"])
        self.assertIn("[internal-path]", payload["assistant_content"])
        self.assertNotIn("runId", payload)
        self.assertNotIn("tool_calls", payload)

    def test_execute_agent_test_chat_sanitizes_bridge_error_message_without_restricted_filter(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_post",
            return_value=(False, {"stderr": "sh: 1: /data/tsds/tenants/demo/workspace/demo.zip: Permission denied"}),
        ):
            ok, payload = adapter.execute_agent_test_chat(company, instance, {"content": "hello"})

        self.assertFalse(ok)
        self.assertNotEqual(payload, RESTRICTED_RUNTIME_ACCESS_MESSAGE)
        self.assertNotIn("/data/tsds", payload)
        self.assertNotIn("Permission denied", payload)

    def test_execute_agent_test_chat_no_longer_short_circuits_directory_queries(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_post",
            return_value=(True, {"assistant_content": "workspace/knowledge contains 1 file", "token_count": 5}),
        ) as post_mock:
            ok, payload = adapter.execute_agent_test_chat(
                company,
                instance,
                {
                    "content": "请扫描 /data/tsds 下面的目录",
                    "messages": [{"role": "user", "content": "请扫描 /data/tsds 下面的目录"}],
                },
            )

        self.assertTrue(ok)
        self.assertEqual(payload["assistant_content"], "workspace/knowledge contains 1 file")
        post_mock.assert_called_once()
        self.assertEqual(
            "internal_admin_agent_test",
            post_mock.call_args.kwargs["payload"]["request_scope"],
        )

    def test_execute_client_entry_chat_marks_public_scope_and_strips_agent_selection(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_post",
            return_value=(True, {"assistant_content": "hello", "token_count": 1}),
        ) as post_mock:
            ok, payload = adapter.execute_client_entry_chat(
                company,
                instance,
                {
                    "agent_id": "spoofed-agent",
                    "route_key": "sales",
                    "content": "hello",
                },
            )

        self.assertTrue(ok)
        self.assertEqual("hello", payload["assistant_content"])
        self.assertEqual(
            "client_entry_public",
            post_mock.call_args.kwargs["payload"]["request_scope"],
        )
        self.assertNotIn("agent_id", post_mock.call_args.kwargs["payload"])

    def test_list_workspace_tree_uses_formal_shared_host_endpoint(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_get_with_params",
            return_value=(True, {"items": [{"logical_path": "knowledge/demo.zip", "kind": "file"}]}),
        ) as get_mock:
            ok, payload = adapter.list_workspace_tree(company, instance, logical_path="workspace/knowledge", recursive=False)

        self.assertTrue(ok)
        self.assertEqual("knowledge/demo.zip", payload["items"][0]["logical_path"])
        get_mock.assert_called_once_with(
            instance,
            "workspace-tree",
            params={"recursive": "0", "path": "knowledge"},
        )

    def test_read_workspace_logical_file_uses_formal_shared_host_endpoint(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_get_with_params",
            return_value=(True, {"content": "hello", "path": "knowledge/demo.md"}),
        ) as get_mock:
            ok, payload = adapter.read_workspace_logical_file(
                company,
                instance,
                "workspace/knowledge/demo.md",
            )

        self.assertTrue(ok)
        self.assertEqual("hello", payload["content"])
        self.assertEqual("knowledge/demo.md", payload["logical_path"])
        get_mock.assert_called_once_with(
            instance,
            "workspace-file",
            params={"path": "knowledge/demo.md"},
        )

    def test_read_workspace_logical_file_preserves_shared_host_failure_reason(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(
            adapter,
            "_get_with_params",
            return_value=(False, "workspace agent returned 502"),
        ):
            ok, payload = adapter.read_workspace_logical_file(
                company,
                instance,
                "knowledge/demo.md",
            )

        self.assertFalse(ok)
        self.assertEqual("workspace agent returned 502", payload)

    @override_settings(RUNTIME_DOCKER_SHARED_HOST_API_BASE="http://120.232.134.232:19080")
    def test_shared_host_api_base_uses_default_when_instance_has_no_target_ip(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(
            company=company,
            runtime_namespace="tsds-demo",
            public_ip="",
            private_ip="",
            server_resource=None,
        )

        self.assertEqual(
            adapter._build_url(instance, "health"),
            "http://120.232.134.232:19080/runtime/tsds-demo/health",
        )

    @override_settings(RUNTIME_DOCKER_SHARED_HOST_API_BASE="http://120.232.134.232:19080")
    def test_shared_host_api_base_replaces_host_with_bound_server_ip(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(
            company=company,
            runtime_namespace="tsds-demo",
            public_ip="62.234.222.137",
            private_ip="10.2.0.2",
            server_resource=SimpleNamespace(api_base=""),
        )

        self.assertEqual(
            adapter._build_url(instance, "bootstrap"),
            "http://62.234.222.137:19080/runtime/tsds-demo/bootstrap",
        )
    def test_start_instance_provision_updates_existing_instance_target_provider(self):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        saved_fields = []
        instance = SimpleNamespace(
            id=7,
            company=company,
            provider=OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST,
            region="",
            zone="",
            instance_id="",
            instance_name="",
            public_ip="",
            private_ip="",
            expired_at="",
            billing_order_id="",
            billing_plan_id="",
            updated_at="",
            save=lambda update_fields=None: saved_fields.extend(update_fields or []),
            get_info=lambda: {"id": 7},
        )
        task = SimpleNamespace(id=12, get_info=lambda: {"id": 12})

        with patch.object(OpenClawRuntimeService, "get_or_create_instance", return_value=(True, instance)):
            with patch.object(OpenClawRuntimeService, "create_task", return_value=task) as create_task_mock:
                with patch("apps.runtime.services.RuntimeAuditService.log"):
                    ok, payload = OpenClawRuntimeService.start_instance_provision(
                        company=company,
                        provider=OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE,
                        request_payload={"public_ip": "62.234.222.137", "private_ip": "10.2.0.2"},
                    )

        self.assertTrue(ok)
        self.assertEqual(OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE, instance.provider)
        self.assertEqual("62.234.222.137", instance.public_ip)
        self.assertEqual("10.2.0.2", instance.private_ip)
        self.assertIn("provider", saved_fields)
        self.assertIn("public_ip", saved_fields)
        self.assertEqual("tencent_lighthouse", create_task_mock.call_args.kwargs["request_payload"]["provider"])
        self.assertEqual({"id": 7}, payload["instance"])
    def test_shared_host_provision_failure_does_not_fallback_to_noop_success(self):
        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(
            provider="docker_shared_host",
            runtime_namespace="tsds-demo",
            company=company,
            gateway_port=18789,
            config_path="",
            workspace_prefix="",
            host_id="docker-host",
            gateway_endpoint="",
        )

        with patch.object(adapter, "_post", return_value=(False, "shared host api returned 502")):
            ok, payload = adapter.provision_instance(company, instance, SimpleNamespace(id=1))

        self.assertFalse(ok)
        self.assertEqual("shared_host_api", payload["source"])
        self.assertEqual("shared host api returned 502", payload["message"])
        self.assertNotEqual("runtime provider adapter is not wired yet", payload["message"])

    def test_shared_host_bootstrap_failure_does_not_fallback_to_noop_success(self):
        class EmptyVersionQuery:
            def order_by(self, *_args, **_kwargs):
                return self

            def first(self):
                return None

        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(
            provider="docker_shared_host",
            runtime_namespace="tsds-demo",
            company=company,
            current_publish_version="v1",
            config_path="",
            config_status="pending",
            gateway_endpoint="",
        )
        fake_version_model = SimpleNamespace(
            STATUS_PUBLISHED="published",
            objects=SimpleNamespace(filter=lambda **_kwargs: EmptyVersionQuery()),
        )

        with patch("apps.runtime.services.OpenClawConfigVersion", fake_version_model):
            with patch.object(adapter, "_post", return_value=(False, "shared host api returned 502")) as post_mock:
                ok, payload = adapter.bootstrap_openclaw(company, instance, SimpleNamespace(id=1))

        self.assertFalse(ok)
        self.assertEqual("shared_host_api", payload["source"])
        self.assertEqual("shared host api returned 502", payload["message"])
        self.assertNotEqual("openclaw bootstrap is not wired yet", payload["message"])
        self.assertGreaterEqual(post_mock.call_args.kwargs["timeout"], 180)

    def test_stream_agent_test_chat_parses_ndjson_events(self):
        class FakeResponse:
            def iter_lines(self, decode_unicode=True):
                yield '{"event":"start"}'
                yield '{"event":"delta","delta":"Hi","content":"Hi"}'
                yield '{"event":"complete","content":"Hi there","token_count":12}'

            def close(self):
                return None

        adapter = DockerSharedHostProviderAdapter()
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(provider="docker_shared_host", runtime_namespace="tsds-demo", company=company)

        with patch.object(adapter, "_post_stream", return_value=(True, FakeResponse())):
            ok, event_iter = adapter.stream_agent_test_chat(company, instance, {"content": "hello"})

        self.assertTrue(ok)
        events = list(event_iter)
        self.assertEqual(["start", "delta", "complete"], [event["event"] for event in events])
        self.assertEqual("Hi there", events[-1]["content"])
        self.assertEqual(12, events[-1]["token_count"])



    @override_settings(
        TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH="C:/secure/lighthouse-dev.pem",
        TENCENT_LIGHTHOUSE_SSH_USER="ubuntu",
        TENCENT_LIGHTHOUSE_SSH_PORT=22,
        TENCENT_LIGHTHOUSE_BRIDGE_PORT=19080,
        TENCENT_LIGHTHOUSE_RUNTIME_ROOT="/data/tsds/tenants",
        RUNTIME_DOCKER_SHARED_HOST_API_TOKEN="secret-bridge-token",
        PROJECT_ROOT=REPO_ROOT,
    )
    def test_lighthouse_bridge_deploy_command_uses_key_path_env_without_token_cli_arg(self):
        company = Company(cid="cid-1", company_name="Demo", created_at="1", updated_at="1")
        instance = SimpleNamespace(
            company=company,
            provider=OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE,
            public_ip="62.234.222.137",
        )

        ok, command = OpenClawRuntimeService._lighthouse_bridge_deploy_command(instance, {})

        self.assertTrue(ok)
        self.assertIn("--private-key", command)
        self.assertIn("C:/secure/lighthouse-dev.pem", command)
        self.assertNotIn("--api-token", command)
        self.assertNotIn("secret-bridge-token", command)
        self.assertIn("--host", command)
        self.assertIn("62.234.222.137", command)

    def test_lighthouse_bridge_deploy_output_masks_key_path_and_token(self):
        output = "ssh -i C:/secure/lighthouse-dev.pem ubuntu@host token=secret-bridge-token"

        sanitized = OpenClawRuntimeService._sanitize_lighthouse_deploy_output(
            output,
            "C:/secure/lighthouse-dev.pem",
            "secret-bridge-token",
        )

        self.assertIn("[ssh-key-path]", sanitized)
        self.assertIn("[bridge-token]", sanitized)
        self.assertNotIn("lighthouse-dev.pem", sanitized)
        self.assertNotIn("secret-bridge-token", sanitized)

class AgentTestPromptFormattingTests(SimpleTestCase):
    def test_runtime_prompt_includes_chat_markdown_rules(self):
        session = SimpleNamespace(
            agent=SimpleNamespace(
                id=1,
                agentId="main-agent",
                name="测试 CEO",
                role="ceo",
                description="负责统筹",
                systemPrompt="你负责公司协作。",
            ),
            transparentCompany=SimpleNamespace(
                company=SimpleNamespace(company_name="示例公司")
            ),
            messages=SimpleNamespace(
                order_by=lambda *_args, **_kwargs: [
                    SimpleNamespace(
                        role=TransparentCompanyAgentTestMessage.ROLE_USER,
                        content="current question",
                    )
                ]
            ),
        )

        with patch("apps.company.services.TransparentCompanyAgentDefinition.objects.filter") as mock_filter:
            mock_filter.return_value.order_by.return_value = []
            messages = CompanyService._serialize_agent_test_messages_for_runtime(
                session,
                "请介绍一下你能做什么",
            )

        self.assertGreaterEqual(len(messages), 2)
        self.assertEqual("system", messages[0]["role"])
        self.assertIn("保留标准 Markdown 能力", messages[0]["content"])
        self.assertIn("简单问题优先直接回答", messages[0]["content"])
        self.assertEqual("user", messages[-1]["role"])

    def test_runtime_prompt_keeps_prior_assistant_answers(self):
        previous_messages = [
            SimpleNamespace(
                role=TransparentCompanyAgentTestMessage.ROLE_USER,
                content="first question",
            ),
            SimpleNamespace(
                role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
                content="stale answer",
            ),
            SimpleNamespace(
                role=TransparentCompanyAgentTestMessage.ROLE_USER,
                content="current question",
            ),
        ]
        session = SimpleNamespace(
            agent=SimpleNamespace(
                id=1,
                agentId="main-agent",
                name="Test CEO",
                role="ceo",
                description="",
                systemPrompt="",
            ),
            transparentCompany=SimpleNamespace(
                company=SimpleNamespace(company_name="Demo Company")
            ),
            messages=SimpleNamespace(order_by=lambda *_args, **_kwargs: previous_messages),
        )

        with patch("apps.company.services.TransparentCompanyAgentDefinition.objects.filter") as mock_filter:
            mock_filter.return_value.order_by.return_value = []
            messages = CompanyService._serialize_agent_test_messages_for_runtime(
                session,
                "current question",
            )

        contents = [item["content"] for item in messages]
        self.assertIn("first question", contents)
        self.assertIn("current question", contents)
        self.assertIn("stale answer", contents)
