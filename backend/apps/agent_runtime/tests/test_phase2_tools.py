from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from django.test import SimpleTestCase

from apps.agent_runtime.contracts import ExecutionResult, MemoryItem, ModelEvent, RunRequest, ToolRequest, ToolResult
from apps.agent_runtime.enums import ErrorCode, ModelEventType, RunEventType
from apps.agent_runtime.grounding import (
    GROUNDING_REFUSAL,
    GROUNDING_RETRY_INSTRUCTION,
    build_internal_grounding_policy,
)
from apps.agent_runtime.memory.quality import (
    assess_memory_candidate,
    has_meaningful_successful_tool_result,
)
from apps.agent_runtime.orchestration.graph import Phase2AgentRuntimeFacade, build_phase2_graph
from apps.agent_runtime.orchestration.nodes import UNVERIFIED_TOOL_CLAIM_MESSAGE, _enforce_runtime_truthfulness
from apps.agent_runtime.orchestration.state import AgentState
from apps.agent_runtime.providers.sandbox.errors import SandboxCreateError
from apps.agent_runtime.providers.sandbox.fake import FakeSandboxProvider
from apps.agent_runtime.tools.definitions import ToolDefinition, ToolValidationError, validate_arguments
from apps.agent_runtime.tools.executors import BackendToolExecutor, InMemoryToolCallRecorder, SandboxToolExecutor
from apps.agent_runtime.tools.registry import ToolRouter, default_tool_registry


def _echo_router() -> ToolRouter:
    return ToolRouter(default_tool_registry([
        ToolDefinition(
            name="runtime.echo",
            executor="backend",
            required_permission="tool.runtime.echo",
            timeout_seconds=5,
            args_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        )
    ]))


class EchoBackendExecutor(BackendToolExecutor):
    async def execute(self, definition, request, context):
        if request.name == "runtime.echo":
            return ToolResult(True, str(request.arguments.get("text") or ""), (), None, False)
        return await super().execute(definition, request, context)


class GroundedReadBackendExecutor(BackendToolExecutor):
    def __init__(self, *, logical_path: str, content: str):
        self.logical_path = logical_path
        self.content = content

    async def execute(self, definition, request, context):
        if request.name == "workspace.read":
            return ToolResult(
                True,
                json.dumps({
                    "tool_name": "workspace.read",
                    "logical_path": self.logical_path,
                    "content": self.content,
                    "encoding": "pdf-text",
                }, ensure_ascii=False),
                (),
                None,
                False,
            )
        return await super().execute(definition, request, context)


class InternalGroundingPolicyTests(SimpleTestCase):
    def test_own_permission_questions_use_runtime_policy_without_file_evidence(self):
        for query in (
            "你对知识库文件有哪些权限",
            "请问你目前对知识库文件有什么权限？",
            "你有哪些知识库权限？",
            "当前 Agent 的知识库权限范围是什么？",
        ):
            with self.subTest(query=query):
                self.assertFalse(build_internal_grounding_policy(query)["required"])

    def test_permission_terms_do_not_exempt_internal_facts_or_mixed_requests(self):
        for query in (
            "公司员工有哪些知识库权限？",
            "介绍公司知识库中的权限管理制度",
            "你对知识库文件有哪些权限？再介绍一下员工林学承。",
            "你对知识库文件有哪些权限并读取公司薪资资料",
            "读取公司数据业务系统说明.txt的权限配置内容",
        ):
            with self.subTest(query=query):
                self.assertTrue(build_internal_grounding_policy(query)["required"])

    def test_internal_employee_fact_requires_current_run_evidence(self):
        policy = build_internal_grounding_policy("介绍一下单位员工林学承")

        self.assertTrue(policy["required"])
        self.assertEqual(policy["target_terms"], ["林学承"])
        self.assertEqual(policy["attachment_sources"], [])

    def test_unrelated_action_and_greeting_do_not_require_grounding(self):
        self.assertFalse(build_internal_grounding_policy("你好")["required"])
        self.assertFalse(build_internal_grounding_policy(
            "在公司数据业务系统说明.txt最后一行追加今天的日期"
        )["required"])

    def test_relevant_current_attachment_counts_as_evidence(self):
        policy = build_internal_grounding_policy(
            "介绍一下单位员工林学承",
            ({
                "name": "林学承简历.pdf",
                "extracted_text": "林学承，网络空间安全专业。",
            },),
        )

        self.assertEqual(policy["attachment_sources"], ["林学承简历.pdf"])

    def test_file_fact_uses_the_actual_filename_as_relevance_target(self):
        policy = build_internal_grounding_policy("读取公司数据业务系统说明.txt的内容")

        self.assertTrue(policy["required"])
        self.assertEqual(policy["target_terms"], ["公司数据业务系统说明.txt"])


class PermissionAnswerTruthfulnessTests(SimpleTestCase):
    def test_read_permission_and_tool_names_are_not_completion_claims(self):
        for content in (
            "可读（read）：knowledge/help.txt；没有写入或删除权限。",
            "可用工具包括 workspace.read，用于读取已授权文件。",
            "I can read authorized files.",
            "Please read the file before answering.",
        ):
            with self.subTest(content=content):
                state = {"tool_results": []}
                self.assertEqual(_enforce_runtime_truthfulness(state, content), content)
                self.assertNotIn("events", state)

    def test_completed_read_claim_still_requires_successful_receipt(self):
        for content in (
            "我已经读取文件。",
            "I read the file.",
            "We have already read the file.",
            "The file has been read.",
            "Successfully read the file.",
            "可读（read）：knowledge/help.txt。我已经读取文件。",
        ):
            with self.subTest(content=content):
                state = {"run_id": "read-claim", "sequence": 0, "events": [], "tool_results": []}
                self.assertEqual(
                    _enforce_runtime_truthfulness(state, content),
                    UNVERIFIED_TOOL_CLAIM_MESSAGE,
                )


class Phase2ToolValidationTests(SimpleTestCase):
    def test_default_registry_contains_only_platform_tools(self):
        registry = default_tool_registry()

        self.assertEqual(
            sorted(registry.definitions),
            [
                "company.project_tables.list",
                "company.table.clear_cells",
                "company.table.read",
                "company.table.write",
                "python.execute",
                "workspace.checkout",
                "workspace.delete",
                "workspace.list",
                "workspace.locate",
                "workspace.move",
                "workspace.publish",
                "workspace.read",
            ],
        )
        self.assertEqual(
            [item["name"] for item in registry.snapshot_manifest()],
            [
                "company.project_tables.list",
                "company.table.clear_cells",
                "company.table.read",
                "company.table.write",
                "python.execute",
                "workspace.checkout",
                "workspace.delete",
                "workspace.list",
                "workspace.locate",
                "workspace.move",
                "workspace.publish",
                "workspace.read",
            ],
        )

    def test_pdf_read_guidance_keeps_document_extraction_out_of_the_sandbox(self):
        registry = default_tool_registry()

        self.assertIn("always use workspace.read", registry.get("workspace.read").description)
        self.assertIn("Do not use workspace.checkout merely to read", registry.get("workspace.checkout").description)
        self.assertIn("Never use python.execute", registry.get("python.execute").description)

    def test_json_schema_rejects_missing_unknown_and_wrong_type(self):
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        self.assertEqual(validate_arguments(schema, {"text": "ok"}), {"text": "ok"})
        with self.assertRaises(ToolValidationError):
            validate_arguments(schema, {})
        with self.assertRaises(ToolValidationError):
            validate_arguments(schema, {"text": "ok", "extra": True})
        with self.assertRaises(ToolValidationError):
            validate_arguments(schema, {"text": 123})

    def test_nested_tool_schema_rejects_invalid_publish_items(self):
        schema = default_tool_registry().get("workspace.publish").args_schema

        canonical = {
            "files": [{
                "sandbox_path": "../output/a.txt",
                "logical_path": "generated/a.txt",
                "mode": "create",
            }],
        }
        self.assertEqual(validate_arguments(schema, canonical), canonical)

        with self.assertRaises(ToolValidationError):
            validate_arguments(schema, {"files": []})
        with self.assertRaises(ToolValidationError):
            validate_arguments(schema, {"files": [{"sandbox_path": "a.txt"}]})
        with self.assertRaises(ToolValidationError):
            validate_arguments(schema, {
                "files": [{
                    "sandbox_path": "a.txt",
                    "logical_path": "generated/a.txt",
                    "mode": "overwrite",
                }],
            })

    def test_delete_schema_requires_optimistic_lock_versions(self):
        schema = default_tool_registry().get("workspace.delete").args_schema

        with self.assertRaises(ToolValidationError):
            validate_arguments(schema, {"logical_paths": ["generated/a.txt"]})

        self.assertEqual(
            validate_arguments(schema, {
                "logical_paths": ["generated/a.txt"],
                "if_match_versions": {"generated/a.txt": 3},
            }),
            {
                "logical_paths": ["generated/a.txt"],
                "if_match_versions": {"generated/a.txt": 3},
            },
        )

    def test_tool_router_requires_registration_permission_and_valid_arguments(self):
        router = _echo_router()
        context = _tool_context(permissions=frozenset({"tool.runtime.echo"}))
        definition, request = router.route(name="runtime.echo", arguments={"text": "hello"}, context=context)

        self.assertEqual(definition.executor, "backend")
        self.assertEqual(request.arguments, {"text": "hello"})
        with self.assertRaises(PermissionError):
            router.route(name="python.execute", arguments={"code": "print(1)"}, context=context)
        with self.assertRaises(PermissionError):
            router.route(name="missing.tool", arguments={}, context=context)


class ResponseContinuationTests(SimpleTestCase):
    def _graph(self, gateway, recorder):
        return build_phase2_graph(
            gateway,
            context_resolver=lambda _request: {
                "model_messages": [
                    {"role": "system", "content": "Use the authorized echo tool."},
                    {"role": "user", "content": "请执行 echo 并返回结果"},
                ],
            },
            tool_router=_echo_router(),
            backend_executor=EchoBackendExecutor(),
            tool_recorder=recorder,
        )

    @staticmethod
    def _echo_call():
        return ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
            "id": "echo-after-correction", "name": "runtime.echo", "arguments": {"text": "actual result"},
        })

    def test_missing_receipt_continues_to_authorized_tool_without_streaming_false_claim(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "已经"}),
             ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "执行 echo，结果是虚构值。"})],
            [self._echo_call()],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "已执行 echo，结果是 actual result。"})],
        ])
        recorder = InMemoryToolCallRecorder()
        final = asyncio.run(self._graph(gateway, recorder).ainvoke(_initial_state(
            permissions=frozenset({"tool.runtime.echo"}),
        )))

        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(len(gateway.requests), 3)
        self.assertEqual(len(recorder.records), 1)
        self.assertEqual(recorder.records[0].status, "completed")
        self.assertEqual(gateway.requests[1].messages[-1], gateway.requests[0].messages[-1])
        self.assertEqual([m["role"] for m in gateway.requests[1].messages], ["system", "user"])
        self.assertEqual(gateway.requests[2].messages[-1]["content"], "actual result")
        deltas = "".join(e.data.get("delta", "") for e in final["events"] if e.event_type == "message.delta")
        self.assertEqual(deltas, "已执行 echo，结果是 actual result。")
        self.assertEqual(final["output_content"], deltas)
        self.assertEqual(sum(e.event_type == "response.retry_required" for e in final["events"]), 1)

    def test_continuation_preserves_tool_denial_and_returns_specific_blocker(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "已执行 echo。"})],
            [self._echo_call()],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "当前没有 echo 工具权限，无法执行该操作。"})],
        ])
        recorder = InMemoryToolCallRecorder()
        final = asyncio.run(self._graph(gateway, recorder).ainvoke(_initial_state()))

        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(len(gateway.requests), 3)
        self.assertEqual(recorder.records, [])
        self.assertFalse(final["tool_results"][0]["success"])
        self.assertEqual(final["tool_results"][0]["error_code"], str(ErrorCode.TOOL_DENIED))
        self.assertEqual(final["output_content"], "当前没有 echo 工具权限，无法执行该操作。")

    def test_repeated_false_claims_exhaust_correction_budget_without_leaking_drafts(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "已执行 echo。"})]
            for _ in range(4)
        ])
        recorder = InMemoryToolCallRecorder()
        final = asyncio.run(self._graph(gateway, recorder).ainvoke(_initial_state()))

        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(len(gateway.requests), 3)
        self.assertEqual(final["error"].stage, "verify_response")
        self.assertFalse(any(e.event_type == "message.delta" for e in final["events"]))
        self.assertFalse(any(e.event_type == "message.completed" for e in final["events"]))
        self.assertEqual(recorder.records, [])

    def test_memory_success_claim_is_corrected_to_actual_async_status(self):
        answer = "该偏好可被异步提取，目前尚未确认保存完成。"
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "已写入长期记忆。"})],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": answer})],
        ])
        final = asyncio.run(self._graph(gateway, InMemoryToolCallRecorder()).ainvoke(_initial_state()))

        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(len(gateway.requests), 2)
        self.assertEqual(final["output_content"], answer)
        self.assertEqual(final["tool_results"], [])
        self.assertEqual(
            "".join(e.data.get("delta", "") for e in final["events"] if e.event_type == "message.delta"),
            answer,
        )


class Phase2RuntimeGraphTests(SimpleTestCase):
    def test_grounding_retry_keeps_system_instruction_before_conversation(self):
        for initial_system in ([], [{"role": "system", "content": "Trusted company policy"}]):
            with self.subTest(initial_system=initial_system):
                conversation = [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "您好"},
                    {"role": "user", "content": "介绍单位员工林学承"},
                ]
                gateway = ScriptedGateway([
                    [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "unsupported fact"})],
                    [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "still unsupported"})],
                ])
                graph = build_phase2_graph(
                    gateway,
                    context_resolver=lambda _request: {
                        "config": {"internal_grounding": {"required": True}},
                        "model_messages": initial_system + conversation,
                    },
                )
                final_state = asyncio.run(graph.ainvoke(_initial_state(
                    permissions=frozenset({"tool.workspace.read"}),
                )))

                self.assertEqual(len(gateway.requests), 2)
                retry_messages = gateway.requests[1].messages
                self.assertEqual(retry_messages[0]["role"], "system")
                self.assertIn(GROUNDING_RETRY_INSTRUCTION, retry_messages[0]["content"])
                if initial_system:
                    self.assertIn(initial_system[0]["content"], retry_messages[0]["content"])
                self.assertEqual(retry_messages[1:], conversation)
                self.assertEqual(gateway.requests[0].messages, initial_system + conversation)
                self.assertEqual(final_state["status"], "COMPLETED")
                self.assertEqual(final_state["output_content"], GROUNDING_REFUSAL)

    def test_internal_fact_without_read_evidence_returns_platform_refusal(self):
        gateway = ScriptedGateway([[ModelEvent(
            str(ModelEventType.TEXT_DELTA),
            {"delta": "刘祉轩是高级研发工程师。"},
        )]])

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                context_resolver=lambda _request: {
                    "config": {
                        "internal_grounding": {
                            "required": True,
                            "target_terms": ["林学承"],
                            "attachment_sources": [],
                        },
                    },
                    "model_messages": [{"role": "user", "content": "介绍单位员工林学承"}],
                },
            )
            return await graph.ainvoke(_initial_state())

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertTrue(final_state["grounding_blocked"])
        self.assertEqual(final_state["output_content"], GROUNDING_REFUSAL)
        deltas = "".join(
            event.data.get("delta", "")
            for event in final_state["events"]
            if event.event_type == str(RunEventType.MESSAGE_DELTA)
        )
        self.assertEqual(deltas, GROUNDING_REFUSAL)
        self.assertNotIn("刘祉轩", deltas)

    def test_internal_fact_forces_read_retry_then_streams_grounded_answer(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "刘祉轩是高级研发工程师。"})],
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "read-1",
                "name": "workspace.read",
                "arguments": {"logical_path": "knowledge/人员信息/林学承.pdf"},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {
                "delta": "林学承的资料显示其专业为网络空间安全。来源：knowledge/人员信息/林学承.pdf",
            })],
        ])

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                context_resolver=lambda _request: {
                    "config": {
                        "internal_grounding": {
                            "required": True,
                            "target_terms": ["林学承"],
                            "attachment_sources": [],
                        },
                    },
                    "model_messages": [{"role": "user", "content": "介绍单位员工林学承"}],
                },
                backend_executor=GroundedReadBackendExecutor(
                    logical_path="knowledge/人员信息/林学承.pdf",
                    content="林学承，网络空间安全专业。",
                ),
            )
            return await graph.ainvoke(_initial_state(
                permissions=frozenset({"tool.workspace.read"}),
            ))

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertFalse(final_state["grounding_blocked"])
        self.assertEqual(final_state["grounding_retry_count"], 1)
        self.assertEqual(
            final_state["grounding_evidence"][0]["logical_path"],
            "knowledge/人员信息/林学承.pdf",
        )
        self.assertNotIn("刘祉轩", final_state["output_content"])
        self.assertIn("林学承", final_state["output_content"])
        self.assertEqual(len(gateway.requests), 3)
        self.assertTrue(any(
            message.get("role") == "system" and "workspace.read" in str(message.get("content"))
            for message in gateway.requests[1].messages
        ))

    def test_irrelevant_workspace_read_does_not_unlock_internal_fact_answer(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "read-wrong",
                "name": "workspace.read",
                "arguments": {"logical_path": "knowledge/人员信息/刘祉轩.pdf"},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "刘祉轩的虚构资料。"})],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "继续编造林学承资料。"})],
        ])

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                context_resolver=lambda _request: {
                    "config": {
                        "internal_grounding": {
                            "required": True,
                            "target_terms": ["林学承"],
                            "attachment_sources": [],
                        },
                    },
                    "model_messages": [{"role": "user", "content": "介绍单位员工林学承"}],
                },
                backend_executor=GroundedReadBackendExecutor(
                    logical_path="knowledge/人员信息/刘祉轩.pdf",
                    content="刘祉轩，其他员工。",
                ),
            )
            return await graph.ainvoke(_initial_state(
                permissions=frozenset({"tool.workspace.read"}),
            ))

        final_state = asyncio.run(invoke())

        self.assertTrue(final_state["grounding_blocked"])
        self.assertEqual(final_state["grounding_evidence"], [])
        self.assertEqual(final_state["output_content"], GROUNDING_REFUSAL)
        self.assertNotIn("刘祉轩", final_state["output_content"])

    def test_phase2_graph_uses_memory_gateway_through_recall_node(self):
        gateway = ScriptedGateway([[ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "done"})]])
        memory_gateway = RecordingMemoryGateway()

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                context_resolver=lambda _request: {
                    "config": {"memory_policy": {"session": {"enabled": True}}},
                    "model_messages": [
                        {"role": "user", "content": "请记住，以后称呼我为老板"}
                    ],
                },
                memory_gateway=memory_gateway,
            )
            return await graph.ainvoke(_initial_state())

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual(
            final_state["memory_items"][0]["content"],
            "remembered 请记住，以后称呼我为老板",
        )
        self.assertEqual(memory_gateway.requests[0].scopes, ("agent", "session"))
        self.assertEqual(memory_gateway.requests[0].query, "请记住，以后称呼我为老板")
        self.assertEqual(len(memory_gateway.commit_requests), 1)
        self.assertEqual(
            [message["role"] for message in memory_gateway.commit_requests[0].messages],
            ["user", "assistant"],
        )
        self.assertEqual(
            memory_gateway.commit_requests[0].messages[0]["content"],
            "请记住，以后称呼我为老板",
        )
        self.assertEqual(memory_gateway.commit_requests[0].messages[1]["content"], "done")
        self.assertEqual(
            memory_gateway.commit_requests[0].memory_types,
            ("preferences", "profile"),
        )

    def test_trivial_greeting_does_not_enqueue_memory(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "你好，我可以帮你处理问题。"})]
        ])
        memory_gateway = RecordingMemoryGateway()

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                context_resolver=lambda _request: {
                    "config": {"memory_policy": {"session": {"enabled": True}}},
                    "model_messages": [{"role": "user", "content": "你好"}],
                },
                memory_gateway=memory_gateway,
            )
            return await graph.ainvoke(_initial_state())

        final_state = asyncio.run(invoke())

        self.assertEqual(memory_gateway.commit_requests, [])
        self.assertFalse(final_state["enqueue_memory_commit"])
        skipped = [
            event for event in final_state["events"]
            if event.event_type == "memory.commit_skipped"
        ]
        self.assertEqual(skipped[-1].data["reason"], "trivial_turn")
    def test_backend_tool_loop_returns_to_model_and_completes(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "runtime.echo",
                "arguments": {"text": "tool says hi"},
            })],
            [
                ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "final"}),
                ModelEvent(str(ModelEventType.USAGE), {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}),
                ModelEvent(str(ModelEventType.FINISH), {"reason": "stop"}),
            ],
        ])
        recorder = InMemoryToolCallRecorder()

        async def collect():
            facade = Phase2AgentRuntimeFacade(
                gateway,
                permissions=frozenset({"tool.runtime.echo"}),
                tool_recorder=recorder,
                tool_router=_echo_router(),
                backend_executor=EchoBackendExecutor(),
            )
            return [event async for event in facade.stream(_run_request())]

        events = asyncio.run(collect())

        self.assertEqual(
            [event.event_type for event in events],
            [
                str(RunEventType.RUN_STARTED),
                str(RunEventType.PROGRESS),
                str(RunEventType.PROGRESS),
                str(RunEventType.TOOL_STARTED),
                str(RunEventType.TOOL_COMPLETED),
                str(RunEventType.PROGRESS),
                str(RunEventType.MESSAGE_DELTA),
                str(RunEventType.MESSAGE_COMPLETED),
                str(RunEventType.RUN_COMPLETED),
            ],
        )
        self.assertEqual(events[-2].data["content"], "final")
        self.assertEqual(len(recorder.records), 1)
        self.assertEqual(recorder.records[0].status, "completed")
        self.assertEqual(gateway.requests[1].messages[-1]["role"], "tool")
        self.assertEqual(gateway.requests[1].messages[-1]["content"], "tool says hi")

    def test_eight_tool_rounds_complete_without_hitting_langgraph_default_limit(self):
        batches = [
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": f"call-{index}",
                "name": "runtime.echo",
                "arguments": {"text": f"round-{index}"},
            })]
            for index in range(8)
        ]
        batches.append([
            ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "final"}),
            ModelEvent(str(ModelEventType.FINISH), {"reason": "stop"}),
        ])
        gateway = ScriptedGateway(batches)
        recorder = InMemoryToolCallRecorder()

        async def collect():
            facade = Phase2AgentRuntimeFacade(
                gateway,
                max_tool_rounds=8,
                permissions=frozenset({"tool.runtime.echo"}),
                tool_recorder=recorder,
                tool_router=_echo_router(),
                backend_executor=EchoBackendExecutor(),
            )
            return [event async for event in facade.stream(_run_request())]

        events = asyncio.run(collect())

        self.assertEqual(events[-1].event_type, str(RunEventType.RUN_COMPLETED))
        self.assertEqual(len(recorder.records), 8)
        self.assertTrue(all(record.status == "completed" for record in recorder.records))

    def test_duplicate_tool_call_is_not_executed_twice(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "runtime.echo",
                "arguments": {"text": "one"},
            })],
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "runtime.echo",
                "arguments": {"text": "one"},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "done"})],
        ])
        recorder = InMemoryToolCallRecorder()

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                tool_recorder=recorder,
                tool_router=_echo_router(),
                backend_executor=EchoBackendExecutor(),
            )
            state = _initial_state(permissions=frozenset({"tool.runtime.echo"}), max_tool_rounds=8)
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual(len(recorder.records), 1)
        self.assertEqual(final_state["tool_round"], 1)

    def test_max_tool_rounds_fails_run(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "runtime.echo",
                "arguments": {"text": "one"},
            })],
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-2",
                "name": "runtime.echo",
                "arguments": {"text": "two"},
            })],
        ])

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                tool_router=_echo_router(),
                backend_executor=EchoBackendExecutor(),
            )
            state = _initial_state(permissions=frozenset({"tool.runtime.echo"}), max_tool_rounds=1)
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "FAILED")
        self.assertEqual(final_state["error"].code, str(ErrorCode.TOOL_DENIED))

    def test_tool_validation_failure_fails_without_execution(self):
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "runtime.echo",
                "arguments": {"unexpected": "bad"},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "recovered"})],
        ])
        recorder = InMemoryToolCallRecorder()

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                tool_recorder=recorder,
                tool_router=_echo_router(),
                backend_executor=EchoBackendExecutor(),
            )
            state = _initial_state(permissions=frozenset({"tool.runtime.echo"}))
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertFalse(final_state["tool_results"][0]["success"])
        self.assertEqual(final_state["tool_results"][0]["error_code"], str(ErrorCode.TOOL_INVALID_ARGUMENTS))
        self.assertEqual(recorder.records, [])

    def test_multiple_tool_calls_execute_before_model_continues(self):
        gateway = ScriptedGateway([
            [
                ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                    "id": "call-1",
                    "name": "runtime.echo",
                    "arguments": {"text": "one"},
                }),
                ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                    "id": "call-2",
                    "name": "runtime.echo",
                    "arguments": {"text": "two"},
                }),
            ],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "done"})],
        ])
        recorder = InMemoryToolCallRecorder()

        async def invoke():
            graph = build_phase2_graph(
                gateway,
                tool_recorder=recorder,
                tool_router=_echo_router(),
                backend_executor=EchoBackendExecutor(),
            )
            state = _initial_state(permissions=frozenset({"tool.runtime.echo"}))
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual([item["content"] for item in final_state["tool_results"]], ["one", "two"])
        self.assertEqual(len(recorder.records), 2)
        second_request_messages = gateway.requests[1].messages
        self.assertEqual(second_request_messages[-3]["role"], "assistant")
        self.assertEqual(len(second_request_messages[-3]["tool_calls"]), 2)
        self.assertEqual([item["role"] for item in second_request_messages[-2:]], ["tool", "tool"])

    def test_sandbox_tool_executor_reuses_one_sandbox_per_run(self):
        provider = FakeSandboxProvider()
        provider.enqueue_execution(ExecutionResult(0, "first", "", (), 5, False))
        provider.enqueue_execution(ExecutionResult(0, "second", "", (), 5, False))
        executor = SandboxToolExecutor(provider)
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "python.execute",
                "arguments": {"code": "print('first')"},
            })],
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-2",
                "name": "python.execute",
                "arguments": {"code": "print('second')"},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "done"})],
        ])

        async def invoke():
            graph = build_phase2_graph(gateway, sandbox_executor=executor)
            state = _initial_state(permissions=frozenset({"tool.code.execute"}), max_tool_rounds=3)
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual(len(provider.active_sandbox_ids), 1)
        self.assertEqual([item["content"] for item in final_state["tool_results"]], ["first", "second"])

    def test_sandbox_tool_executor_uses_run_work_relative_cwd(self):
        provider = RecordingFakeSandboxProvider()
        provider.enqueue_execution(ExecutionResult(0, "ok", "", (), 5, False))
        executor = SandboxToolExecutor(provider)

        async def invoke():
            return await executor.execute(
                ToolDefinition(
                    name="python.execute",
                    executor="sandbox",
                    required_permission="tool.code.execute",
                    timeout_seconds=3,
                    args_schema={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
                ),
                ToolRequest(name="python.execute", arguments={"code": "print('ok')"}, timeout_seconds=3),
                _tool_context(permissions=frozenset({"tool.code.execute"})),
            )

        result = asyncio.run(invoke())

        self.assertTrue(result.success)
        self.assertEqual(provider.execution_requests[0].cwd, ".")

    def test_sandbox_create_failure_maps_to_tool_failure(self):
        provider = FakeSandboxProvider(create_error=SandboxCreateError("template is unavailable"))
        executor = SandboxToolExecutor(provider)
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "python.execute",
                "arguments": {"code": "print('ok')"},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "sandbox unavailable"})],
        ])
        recorder = InMemoryToolCallRecorder()

        async def invoke():
            graph = build_phase2_graph(gateway, sandbox_executor=executor, tool_recorder=recorder)
            state = _initial_state(permissions=frozenset({"tool.code.execute"}))
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual(final_state["tool_results"][0]["error_code"], str(ErrorCode.SANDBOX_CREATE_FAILED))
        self.assertEqual(recorder.records[0].status, "failed")
        self.assertEqual(recorder.records[0].error_code, str(ErrorCode.SANDBOX_CREATE_FAILED))
        self.assertIn(str(RunEventType.TOOL_FAILED), [event.event_type for event in final_state["events"]])
    def test_phase2_facade_terminates_sandbox_after_stream_completion(self):
        provider = FakeSandboxProvider()
        provider.enqueue_execution(ExecutionResult(0, "ok", "", (), 5, False))
        executor = SandboxToolExecutor(provider)
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "python.execute",
                "arguments": {"code": "print('ok')"},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "done"})],
        ])

        async def collect():
            facade = Phase2AgentRuntimeFacade(
                gateway,
                permissions=frozenset({"tool.code.execute"}),
                sandbox_executor=executor,
            )
            return [event async for event in facade.stream(_run_request())]

        events = asyncio.run(collect())

        self.assertEqual(events[-1].event_type, str(RunEventType.RUN_COMPLETED))
        self.assertEqual(len(provider.active_sandbox_ids), 0)
        self.assertEqual(len(provider.terminated_sandbox_ids), 1)

    def test_tool_timeout_emits_failed_event_and_failed_run(self):
        router = ToolRouter(default_tool_registry([
            ToolDefinition(
                name="slow.echo",
                executor="backend",
                required_permission="tool.slow",
                timeout_seconds=0.01,
                args_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ]))
        gateway = ScriptedGateway([
            [ModelEvent(str(ModelEventType.TOOL_CALL_COMPLETED), {
                "id": "call-1",
                "name": "slow.echo",
                "arguments": {},
            })],
            [ModelEvent(str(ModelEventType.TEXT_DELTA), {"delta": "tool timed out"})],
        ])

        async def invoke():
            graph = build_phase2_graph(gateway, tool_router=router, backend_executor=SlowBackendExecutor())
            state = _initial_state(permissions=frozenset({"tool.slow"}))
            return await graph.ainvoke(state)

        final_state = asyncio.run(invoke())

        self.assertEqual(final_state["status"], "COMPLETED")
        self.assertEqual(final_state["tool_results"][0]["error_code"], str(ErrorCode.SANDBOX_TIMEOUT))
        self.assertIn(str(RunEventType.TOOL_FAILED), [event.event_type for event in final_state["events"]])


class MemoryQualityGateTests(SimpleTestCase):
    def test_preference_is_limited_to_profile_and_preferences(self):
        assessment = assess_memory_candidate(
            "以后请称呼我为老板",
            "好的，以后我会称呼您为老板。",
        )

        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.memory_types, frozenset({"profile", "preferences"}))

    def test_tool_smoke_test_is_rejected_even_with_successful_output(self):
        assessment = assess_memory_candidate(
            "测试工具调用，执行 python 并原样返回 stdout",
            "user",
            has_successful_tool=True,
        )

        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "tool_smoke_test")

    def test_successful_meaningful_tool_task_becomes_case(self):
        assessment = assess_memory_candidate(
            "帮我查询客户订单并整理退款情况",
            "已完成",
            has_successful_tool=True,
        )

        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.memory_types, frozenset({"cases"}))

    def test_only_meaningful_successful_tool_results_cross_threshold(self):
        self.assertFalse(has_meaningful_successful_tool_result([
            {"success": True, "content": "user", "artifact_refs": []},
            {"success": False, "content": "a long failed result", "artifact_refs": []},
        ]))
        self.assertTrue(has_meaningful_successful_tool_result([
            {"success": True, "content": "查询到订单已退款并完成客户通知", "artifact_refs": []},
        ]))


class RecordingMemoryGateway:
    def __init__(self):
        self.requests = []
        self.commit_requests = []

    async def recall(self, request):
        self.requests.append(request)
        return [
            MemoryItem(
                memory_id="memory-1",
                scope="agent",
                content=f"remembered {request.query}",
                score=0.9,
                source_ref="viking://memory-1",
            )
        ]

    async def commit(self, request):
        self.commit_requests.append(request)


class ScriptedGateway:
    def __init__(self, batches):
        self.batches = list(batches)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        batch = self.batches.pop(0) if self.batches else []
        for event in batch:
            await asyncio.sleep(0)
            yield event


class RecordingFakeSandboxProvider(FakeSandboxProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.execution_requests = []

    async def execute(self, sandbox_id, request):
        self.execution_requests.append(request)
        return await super().execute(sandbox_id, request)

class SlowBackendExecutor(BackendToolExecutor):
    async def execute(self, definition, request, context):
        await asyncio.sleep(1)
        return await super().execute(definition, request, context)


def _run_request(**overrides) -> RunRequest:
    values = {
        "request_id": str(uuid4()),
        "company_id": "company-a",
        "agent_id": "agent-a1",
        "user_id": "user-a",
        "session_id": "session-a",
        "source": "agent_test",
        "user_message_id": "hello",
        "locale": "zh-CN",
        "trace_id": str(uuid4()),
    }
    values.update(overrides)
    return RunRequest(**values)


def _initial_state(*, permissions=frozenset(), max_tool_rounds=8) -> AgentState:
    return {
        "request": _run_request(),
        "run_id": str(uuid4()),
        "status": "PENDING",
        "sequence": 0,
        "events": [],
        "output_content": "",
        "usage": {},
        "cancel_requested": False,
        "max_execution_seconds": 30,
        "tool_calls": [],
        "pending_tool_call": None,
        "tool_results": [],
        "completed_tool_call_keys": set(),
        "tool_round": 0,
        "max_tool_rounds": max_tool_rounds,
        "permissions": permissions,
        "enqueue_memory_commit": False,
        "grounding_evidence": [],
        "grounding_retry_count": 0,
        "grounding_retry_pending": False,
        "grounding_blocked": False,
    }


def _tool_context(*, permissions: frozenset[str]):
    from apps.agent_runtime.contracts import ToolContext

    return ToolContext(
        run_id="run-1",
        tool_call_id="tool-1",
        company_id="company-a",
        agent_id="agent-a",
        user_id="user-a",
        permissions=permissions,
    )
