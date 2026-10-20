"""Tool definitions and validation for the Phase 2 runtime loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from apps.agent_runtime.enums import ErrorCode

ToolExecutorKind = Literal["backend", "sandbox", "browser"]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    executor: ToolExecutorKind
    required_permission: str
    timeout_seconds: int
    description: str = ""
    network_policy: str = "isolated"
    args_schema: dict[str, Any] = field(default_factory=dict)

    def to_snapshot_dict(self) -> dict[str, Any]:
        """Return the stable, secret-free contract persisted with each Run."""
        return {
            "name": self.name,
            "executor": self.executor,
            "requiredPermission": self.required_permission,
            "timeoutSeconds": self.timeout_seconds,
            "description": self.description,
            "networkPolicy": self.network_policy,
            "argsSchema": self.args_schema,
        }


class ToolValidationError(ValueError):
    code = str(ErrorCode.TOOL_INVALID_ARGUMENTS)

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class ToolDeniedError(PermissionError):
    code = str(ErrorCode.TOOL_DENIED)

    def __init__(self, message: str = "工具权限不足") -> None:
        self.safe_message = message
        super().__init__(message)


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ToolValidationError("工具参数必须是对象")
    schema = dict(schema or {})
    if schema.get("type") not in (None, "object"):
        raise ToolValidationError("工具参数 Schema 仅支持 object")
    properties = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    additional_allowed = bool(schema.get("additionalProperties", True))

    for key in required:
        if key not in arguments:
            raise ToolValidationError(f"工具参数缺少字段: {key}")
    if not additional_allowed:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolValidationError(f"工具参数包含未知字段: {unknown[0]}")

    validated = dict(arguments)
    for key, rule in properties.items():
        if key not in validated:
            continue
        _validate_value(key, validated[key], dict(rule or {}))
    return validated


def _validate_value(key: str, value: Any, rule: dict[str, Any]) -> None:
    expected = rule.get("type")
    if expected == "string" and not isinstance(value, str):
        raise ToolValidationError(f"工具参数字段类型错误: {key}")
    if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ToolValidationError(f"工具参数字段类型错误: {key}")
    if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ToolValidationError(f"工具参数字段类型错误: {key}")
    if expected == "boolean" and not isinstance(value, bool):
        raise ToolValidationError(f"工具参数字段类型错误: {key}")
    if expected == "array" and not isinstance(value, list):
        raise ToolValidationError(f"工具参数字段类型错误: {key}")
    if expected == "object" and not isinstance(value, dict):
        raise ToolValidationError(f"工具参数字段类型错误: {key}")
    if "enum" in rule and value not in set(rule.get("enum") or []):
        raise ToolValidationError(f"工具参数字段枚举无效: {key}")
    if isinstance(value, str):
        min_length = rule.get("minLength")
        max_length = rule.get("maxLength")
        if min_length is not None and len(value) < int(min_length):
            raise ToolValidationError(f"工具参数字段过短: {key}")
        if max_length is not None and len(value) > int(max_length):
            raise ToolValidationError(f"工具参数字段过长: {key}")
    if isinstance(value, list):
        min_items = rule.get("minItems")
        max_items = rule.get("maxItems")
        if min_items is not None and len(value) < int(min_items):
            raise ToolValidationError(f"工具参数数组元素过少: {key}")
        if max_items is not None and len(value) > int(max_items):
            raise ToolValidationError(f"工具参数数组元素过多: {key}")
        item_rule = rule.get("items")
        if isinstance(item_rule, dict):
            for index, item in enumerate(value):
                _validate_value(f"{key}[{index}]", item, item_rule)
    if isinstance(value, dict) and isinstance(rule.get("properties"), dict):
        validate_arguments(rule, value)
