"""Tool registry and policy checks for model-requested tool calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from apps.agent_runtime.contracts import ToolContext, ToolRequest

from .definitions import ToolDefinition, ToolDeniedError, validate_arguments


@dataclass
class ToolRegistry:
    definitions: dict[str, ToolDefinition] = field(default_factory=dict)

    def register(self, definition: ToolDefinition) -> None:
        self.definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self.definitions.get(str(name or "").strip())

    def snapshot_manifest(self) -> list[dict]:
        return [
            self.definitions[name].to_snapshot_dict()
            for name in sorted(self.definitions)
        ]


class ToolRouter:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def route(self, *, name: str, arguments: dict, context: ToolContext) -> tuple[ToolDefinition, ToolRequest]:
        definition = self.registry.get(name)
        if definition is None:
            raise ToolDeniedError("工具未注册")
        if definition.required_permission and definition.required_permission not in context.permissions:
            raise ToolDeniedError("工具权限不足")
        validated = validate_arguments(definition.args_schema, arguments)
        return definition, ToolRequest(
            name=definition.name,
            arguments=validated,
            timeout_seconds=definition.timeout_seconds,
        )


def default_tool_registry(extra_definitions: Iterable[ToolDefinition] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="python.execute",
        executor="sandbox",
        required_permission="tool.code.execute",
        timeout_seconds=60,
        description=(
            "Execute Python code in an isolated sandbox whose current directory is the Run "
            "work directory. Any file that will be published MUST be written with the "
            "canonical relative path ../output/<filename>. Do not use an absolute path or "
            "write publishable files to the current directory. Never use python.execute "
            "merely to read or extract text from workspace documents or PDFs; call "
            "workspace.read directly."
        ),
        network_policy="isolated",
        args_schema={
            "type": "object",
            "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 20000}},
            "required": ["code"],
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="company.project_tables.list",
        effect_class="read_only",
        executor="backend",
        required_permission="tool.project_table.read",
        timeout_seconds=15,
        description=(
            "列出当前 Agent 和操作者均可读取的公司项目及表格；每张表会说明 Agent 实际只读或读写/清空单元格能力，"
            "scope 表示私有、公开可读或公开读写。先调用此工具发现 table tid。"
        ),
        args_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ))
    registry.register(ToolDefinition(
        name="company.table.read",
        effect_class="read_only",
        executor="backend",
        required_permission="tool.project_table.read",
        timeout_seconds=30,
        description="按 tid 分页读取一个已授权公司项目表格的数据。",
        args_schema={
            "type": "object",
            "properties": {
                "tid": {"type": "string", "minLength": 1, "maxLength": 128},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["tid"],
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="company.table.write",
        effect_class="mutates",
        executor="backend",
        required_permission="tool.project_table.write",
        timeout_seconds=30,
        description=(
            "向一个已授权公司项目表格写入单元格。每个 cells 项包含从 0 开始的行号 r、列号 c 和 value；"
            "仅能写入 Agent 与当前操作者均有写入权限的表格。"
        ),
        args_schema={
            "type": "object",
            "properties": {
                "tid": {"type": "string", "minLength": 1, "maxLength": 128},
                "cells": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "r": {"type": "integer"},
                            "c": {"type": "integer"},
                            "value": {},
                        },
                        "required": ["r", "c", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tid", "cells"],
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="company.table.clear_cells",
        effect_class="mutates",
        executor="backend",
        required_permission="tool.project_table.write",
        timeout_seconds=30,
        description=(
            "清空一个已授权公司项目表格中指定单元格的内容，保留行列和表结构。"
            "仅在用户明确要求删除/清空单元格内容时使用；改表结构、删除行或列请拒绝，并提示至【管理项目表格】页面手动修改。"
        ),
        args_schema={
            "type": "object",
            "properties": {
                "tid": {"type": "string", "minLength": 1, "maxLength": 128},
                "cells": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {"r": {"type": "integer"}, "c": {"type": "integer"}},
                        "required": ["r", "c"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tid", "cells"],
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="workspace.list",
        executor="backend",
        required_permission="tool.workspace.list",
        timeout_seconds=10,
        description="List files and directories in the current company's authorized workspace.",
        args_schema={
            "type": "object",
            "properties": {"logical_path": {"type": "string", "maxLength": 512}},
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="workspace.locate",
        executor="backend",
        required_permission="tool.workspace.locate",
        timeout_seconds=10,
        description="Locate authorized workspace files by file name or logical path.",
        args_schema={
            "type": "object",
            "properties": {
                "logical_path": {"type": "string", "maxLength": 512},
                "file_name": {"type": "string", "maxLength": 255},
            },
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="workspace.read",
        executor="backend",
        required_permission="tool.workspace.read",
        timeout_seconds=20,
        description=(
            "Read one authorized UTF-8 text file or PDF directly in the trusted backend. "
            "logical_path must identify one concrete file. To inspect a folder, call "
            "workspace.list; to search within a folder, call workspace.locate. Never pass "
            "a folder path to workspace.read. "
            "For PDF reading or text extraction, always use workspace.read; never use "
            "workspace.checkout followed by python.execute."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "logical_path": {
                    "type": "string",
                    "maxLength": 512,
                    "description": "Exact file path only; use workspace.list or workspace.locate for folders.",
                },
                "file_name": {"type": "string", "maxLength": 255},
            },
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="workspace.checkout",
        executor="backend",
        required_permission="tool.workspace.checkout",
        timeout_seconds=60,
        description=(
            "Copy authorized company workspace files into the current isolated sandbox. "
            "Use this only when a file must be modified, transformed, or processed by "
            "user-requested code. Do not use workspace.checkout merely to read text or a "
            "PDF; workspace.read handles those directly in the backend."
        ),
        network_policy="isolated",
        args_schema={
            "type": "object",
            "properties": {
                "logical_paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string", "maxLength": 512},
                },
            },
            "required": ["logical_paths"],
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="workspace.publish",
        executor="backend",
        required_permission="tool.workspace.write",
        timeout_seconds=60,
        description=(
            "Persist regular files from the isolated sandbox output directory to an "
            "authorized company workspace path. A writable folder scope atomically allows "
            "both mode=create for new files in that folder and mode=replace for existing "
            "files; it does not allow paths outside the folder. Use exactly the same "
            "work-relative path for writing and publishing: python.execute writes "
            "../output/<filename>, and sandbox_path MUST also be ../output/<filename>. "
            "Never use an absolute sandbox path."
        ),
        network_policy="isolated",
        args_schema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "sandbox_path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 138,
                                "description": (
                                    "Canonical path relative to the sandbox work directory: "
                                    "../output/<filename>"
                                ),
                            },
                            "logical_path": {"type": "string", "minLength": 1, "maxLength": 512},
                            "mode": {"type": "string", "enum": ["create", "replace"]},
                            "if_match_version": {"type": "integer"},
                        },
                        "required": ["sandbox_path", "logical_path", "mode"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["files"],
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="workspace.move",
        executor="backend",
        required_permission="tool.workspace.move",
        timeout_seconds=60,
        description="Move one authorized company workspace file to an authorized destination path.",
        args_schema={
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "minLength": 1, "maxLength": 512},
                "destination_path": {"type": "string", "minLength": 1, "maxLength": 512},
                "if_match_version": {"type": "integer"},
            },
            "required": ["source_path", "destination_path", "if_match_version"],
            "additionalProperties": False,
        },
    ))
    registry.register(ToolDefinition(
        name="workspace.delete",
        executor="backend",
        required_permission="tool.workspace.delete",
        timeout_seconds=30,
        description=(
            "Delete exact authorized company workspace files. Directory and wildcard deletion "
            "are intentionally unsupported."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "logical_paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": {"type": "string", "maxLength": 512},
                },
                "if_match_versions": {"type": "object"},
            },
            "required": ["logical_paths", "if_match_versions"],
            "additionalProperties": False,
        },
    ))
    for definition in list(extra_definitions or []):
        registry.register(definition)
    return registry
