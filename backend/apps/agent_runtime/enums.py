"""Stable runtime enums shared by the unified agent runtime contracts."""

from enum import Enum


class StrValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class RunEventType(StrValueEnum):
    RUN_STARTED = "run.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_COMPLETED = "sandbox.completed"
    WARNING = "warning"
    PROGRESS = "run.progress"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class ModelEventType(StrValueEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    USAGE = "usage"
    FINISH = "finish"
    ERROR = "error"
    CALL_METRICS = "model_call_metrics"


class ErrorCode(StrValueEnum):
    AUTH_DENIED = "AUTH_DENIED"
    RISK_REJECTED = "RISK_REJECTED"
    AGENT_DISABLED = "AGENT_DISABLED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MEMORY_UNAVAILABLE = "MEMORY_UNAVAILABLE"
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_INVALID_ARGUMENTS = "TOOL_INVALID_ARGUMENTS"
    OPERATION_OUTCOME_UNKNOWN = "OPERATION_OUTCOME_UNKNOWN"
    SANDBOX_CREATE_FAILED = "SANDBOX_CREATE_FAILED"
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    RUN_CANCELLED = "RUN_CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
