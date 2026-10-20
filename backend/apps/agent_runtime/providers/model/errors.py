"""Safe error normalization for model providers.

Provider exceptions often contain request URLs or response bodies.  This module
deliberately maps them to the small, public error vocabulary frozen in Phase 0;
raw exceptions may be logged only by the caller's restricted logger.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from apps.agent_runtime.enums import ErrorCode


@dataclass(frozen=True)
class NormalizedModelError:
    code: str
    message: str
    retryable: bool


class ModelConfigurationError(ValueError):
    """Raised when a company has no usable model or credential configuration."""

    def __init__(self, message: str = "模型配置不可用") -> None:
        super().__init__(message)
        self.code = str(ErrorCode.MODEL_AUTH_FAILED)
        self.safe_message = message
        self.retryable = False


def normalize_model_error(error: BaseException) -> NormalizedModelError:
    """Return a stable, secret-free error for an arbitrary SDK exception."""

    status_code = _status_code(error)
    class_name = error.__class__.__name__.lower()

    if isinstance(error, (asyncio.TimeoutError, TimeoutError)) or "timeout" in class_name:
        return NormalizedModelError(
            str(ErrorCode.MODEL_TIMEOUT),
            "模型响应超时，请稍后重试",
            True,
        )
    if status_code in {401, 403} or any(
        marker in class_name for marker in ("authentication", "permissiondenied")
    ):
        return NormalizedModelError(
            str(ErrorCode.MODEL_AUTH_FAILED),
            "模型配置不可用",
            False,
        )
    if status_code == 429 or "ratelimit" in class_name:
        return NormalizedModelError(
            str(ErrorCode.MODEL_RATE_LIMITED),
            "模型服务繁忙，请稍后重试",
            True,
        )
    if status_code is not None and status_code >= 500:
        return NormalizedModelError(
            str(ErrorCode.INTERNAL_ERROR),
            "模型服务暂不可用，请稍后重试",
            True,
        )
    if "connection" in class_name:
        return NormalizedModelError(
            str(ErrorCode.INTERNAL_ERROR),
            "模型服务暂不可用，请稍后重试",
            True,
        )
    return NormalizedModelError(
        str(ErrorCode.INTERNAL_ERROR),
        "模型调用失败，请稍后重试",
        False,
    )


def _status_code(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    if value is None:
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
