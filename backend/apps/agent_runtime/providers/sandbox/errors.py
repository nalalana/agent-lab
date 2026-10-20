"""Safe, stable mappings for sandbox-provider failures in Phase 0."""

from __future__ import annotations

from apps.agent_runtime.contracts import ErrorInfo
from apps.agent_runtime.enums import ErrorCode


class SandboxProviderError(RuntimeError):
    """Base exception for a sandbox-provider operation failure."""

    error_code = str(ErrorCode.INTERNAL_ERROR)


class SandboxCreateError(SandboxProviderError):
    """Raised when a sandbox cannot be created with the requested specification."""

    error_code = str(ErrorCode.SANDBOX_CREATE_FAILED)


class SandboxTimeoutError(SandboxProviderError):
    """Raised when a command or sandbox lifetime exceeds its configured limit."""

    error_code = str(ErrorCode.SANDBOX_TIMEOUT)


class SandboxCancelledError(SandboxProviderError):
    """Raised when a caller cancels work before a terminal provider response."""

    error_code = str(ErrorCode.RUN_CANCELLED)


def map_sandbox_error(error: BaseException, *, stage: str, trace_id: str) -> ErrorInfo:
    """Convert an internal provider error into the frozen safe public contract.

    Provider messages can contain endpoint, template, or credential details.  The
    returned message is deliberately static; raw exceptions remain restricted to
    the server-side logger used by a later runtime integration stage.
    """
    description = str(error).lower()

    if isinstance(error, SandboxTimeoutError) or "timeout" in description or "timed out" in description:
        return ErrorInfo(
            code=str(ErrorCode.SANDBOX_TIMEOUT),
            message="Sandbox execution timed out",
            retryable=True,
            stage=stage,
            trace_id=trace_id,
        )

    if isinstance(error, SandboxCancelledError) or "cancel" in description or "killed" in description:
        return ErrorInfo(
            code=str(ErrorCode.RUN_CANCELLED),
            message="Sandbox execution was cancelled",
            retryable=False,
            stage=stage,
            trace_id=trace_id,
        )

    if stage == "sandbox.create":
        # Authentication and template configuration failures need operator action.
        non_retryable = any(token in description for token in ("api key", "auth", "forbidden", "template"))
        return ErrorInfo(
            code=str(ErrorCode.SANDBOX_CREATE_FAILED),
            message="Sandbox could not be created",
            retryable=not non_retryable,
            stage=stage,
            trace_id=trace_id,
        )

    return ErrorInfo(
        code=str(ErrorCode.INTERNAL_ERROR),
        message="Sandbox operation failed",
        retryable=True,
        stage=stage,
        trace_id=trace_id,
    )
