import json
import posixpath
import re
from typing import Any


RESTRICTED_RUNTIME_ACCESS_MESSAGE = "无法访问系统内部目录或配置文件"
DEFAULT_USER_VISIBLE_ERROR = "操作未完成，请稍后重试"

_SENSITIVE_KEY_NAMES = {
    "agentmeta",
    "debug",
    "debuginfo",
    "injectedworkspacefiles",
    "lastcallusage",
    "meta",
    "payloads",
    "rawpayload",
    "rawresponse",
    "result",
    "runid",
    "sandbox",
    "schema",
    "sessionid",
    "stack",
    "stderr",
    "stdout",
    "stopreason",
    "systempromptreport",
    "tools",
    "trace",
    "usage",
    "workspacedir",
}

_PATH_ROOTS = ("data", "opt", "home", "root", "tmp", "var", "usr", "etc", "Users")
_UNIX_PATH_RE = re.compile(
    r"(?P<path>/(?:" + "|".join(_PATH_ROOTS) + r")(?:/[^\s'\"`<>:|]+)+)"
)
_WINDOWS_PATH_RE = re.compile(
    r"(?P<path>[A-Za-z]:\\(?:[^\s'\"`<>|]+\\)*[^\s'\"`<>|]+)"
)
_SHELL_NOISE_RE = re.compile(r"\b(?:sh|bash|zsh|cmd|powershell)(?::|\s+\d+:)", re.IGNORECASE)
_SHELL_NOISE_LINE_RE = re.compile(r"^\s*(?:sh|bash|zsh|cmd|powershell)(?::|\s+\d+:)", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ABSOLUTE_PATH_HINT_RE = re.compile(
    r"(^|[\s'\"`(])(?:"
    r"/(?:" + "|".join(_PATH_ROOTS) + r")(?=/|$)"
    r"|[A-Za-z]:\\"
    r")"
)
_JSON_DECODER = json.JSONDecoder()

_RUNTIME_DIRECTORY_MARKERS = (
    "/data/tsds",
    "/opt/tsds",
    ".openclaw-runtime",
    "home/.openclaw",
    "/.openclaw/",
    "compose.yml",
    "dockerfile.gateway",
    "memory.sqlite",
    "workspace-main-agent",
    "systempromptreport",
    "workspacedir",
    "injectedworkspacefiles",
)
_RUNTIME_DEBUG_MARKERS = (
    "runid",
    "sessionid",
    "systempromptreport",
    "workspacedir",
    "injectedworkspacefiles",
    "agentmeta",
    "payloads",
    "sandbox",
    "toolcalls",
    "schema",
)
_SYSTEM_DIRECTORY_PATTERNS = (
    re.compile(r"(^|[\s/])config(?:/|\b)", re.IGNORECASE),
    re.compile(r"(^|[\s/])runtime(?:/|\b)", re.IGNORECASE),
    re.compile(r"(^|[\s/])home(?:/|\b)", re.IGNORECASE),
    re.compile(r"(^|[\s/])memory(?:/|\b)", re.IGNORECASE),
    re.compile(r"(^|[\s/])logs(?:/|\b)", re.IGNORECASE),
    re.compile(r"(^|[\s/])dockerfile(?:\.gateway)?(?:\b|/)", re.IGNORECASE),
    re.compile(r"(^|[\s/])compose\.yml(?:\b|/)", re.IGNORECASE),
    re.compile(r"(^|[\s/])\.openclaw(?:-runtime)?(?:/|\b)", re.IGNORECASE),
)
_RESTRICTED_ACCESS_VERBS = (
    "scan",
    "list",
    "show",
    "browse",
    "enumerate",
    "read",
    "open",
    "cat",
    "find",
    "extract",
    "unzip",
    "view",
    "查看",
    "列出",
    "扫描",
    "枚举",
    "读取",
    "打开",
    "搜索",
    "解压",
    "浏览",
)
_LOGICAL_PATH_BLOCKLIST = {
    ".openclaw",
    ".openclaw-runtime",
    ".runtime_state.json",
    "compose.yml",
    "config",
    "dockerfile",
    "dockerfile.gateway",
    "home",
    "logs",
    "memory",
    "openclaw.json",
    "runtime",
}


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _redact_absolute_paths(text: str) -> str:
    if not text:
        return text
    text = _UNIX_PATH_RE.sub("[internal-path]", text)
    text = _WINDOWS_PATH_RE.sub("[internal-path]", text)
    return text


def _contains_runtime_debug_markers(text: str) -> bool:
    normalized = _normalized_key(text)
    return sum(marker in normalized for marker in _RUNTIME_DEBUG_MARKERS) >= 2


def _contains_system_directory_probe(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    if any(marker in lowered for marker in _RUNTIME_DIRECTORY_MARKERS):
        return True
    if _contains_runtime_debug_markers(lowered):
        return True
    if _ABSOLUTE_PATH_HINT_RE.search(text):
        return True
    if any(verb in lowered for verb in _RESTRICTED_ACCESS_VERBS):
        return any(pattern.search(lowered) for pattern in _SYSTEM_DIRECTORY_PATTERNS)
    return False


def _strip_shell_noise_lines(text: str) -> str:
    if not text:
        return ""
    lines = []
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SHELL_NOISE_LINE_RE.match(line):
            continue
        lines.append(raw_line)
    return "\n".join(lines).strip()


def _extract_embedded_json(value: str) -> tuple[Any | None, str]:
    text = str(value or "").strip()
    if not text or text[0] not in "{[":
        return None, text
    try:
        parsed, end = _JSON_DECODER.raw_decode(text)
    except ValueError:
        return None, text
    remainder = text[end:].strip()
    return parsed, remainder


def extract_embedded_runtime_json(value: Any) -> tuple[Any | None, str]:
    return _extract_embedded_json(str(value or ""))


def _extract_runtime_payload_texts(value: Any) -> list[str]:
    texts: list[str] = []

    def append_text(item: Any) -> None:
        if isinstance(item, str):
            text = item.strip()
            if text:
                texts.append(text)
        elif isinstance(item, list):
            for entry in item:
                if isinstance(entry, dict) and entry.get("type") == "text":
                    append_text(entry.get("text"))

    if isinstance(value, dict):
        payloads = value.get("payloads")
        if isinstance(payloads, list):
            for item in payloads:
                if isinstance(item, dict):
                    append_text(item.get("text"))
        result = value.get("result")
        if isinstance(result, (dict, list)):
            texts.extend(_extract_runtime_payload_texts(result))
        for key in ("assistant_content", "reply", "output"):
            append_text(value.get(key))
        choices = value.get("choices")
        if isinstance(choices, list):
            for item in choices:
                if not isinstance(item, dict):
                    continue
                message = item.get("message")
                if isinstance(message, dict):
                    append_text(message.get("content"))
    elif isinstance(value, list):
        for item in value:
            texts.extend(_extract_runtime_payload_texts(item))

    deduped: list[str] = []
    seen: set[str] = set()
    for item in texts:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _sanitize_plain_assistant_text(
    value: Any,
    *,
    restricted_message: str,
) -> str:
    original = str(value or "").strip()
    if not original:
        return ""
    without_shell_noise = _strip_shell_noise_lines(original)
    if not without_shell_noise:
        return ""
    sanitized = sanitize_user_visible_text(without_shell_noise)
    if not sanitized:
        return ""
    if _contains_runtime_debug_markers(original):
        return restricted_message
    return sanitized


def sanitize_user_visible_text(value: Any) -> str:
    if value is None:
        return ""
    return _redact_absolute_paths(str(value)).strip()


def sanitize_assistant_content_for_user(
    value: Any,
    restricted_message: str = RESTRICTED_RUNTIME_ACCESS_MESSAGE,
) -> str:
    original = str(value or "").strip()
    if not original:
        return ""
    parsed_payload, trailing = _extract_embedded_json(original)
    if parsed_payload is not None:
        payload_texts = _extract_runtime_payload_texts(parsed_payload)
        safe_parts: list[str] = []
        for item in payload_texts:
            safe_text = _sanitize_plain_assistant_text(
                item,
                restricted_message=restricted_message,
            )
            if not safe_text:
                continue
            if safe_text == restricted_message:
                return restricted_message
            safe_parts.append(safe_text)
        if safe_parts:
            return "\n\n".join(safe_parts).strip()
        trailing_text = _sanitize_plain_assistant_text(
            trailing,
            restricted_message=restricted_message,
        )
        if trailing_text:
            return trailing_text
        if _contains_runtime_debug_markers(original):
            return DEFAULT_USER_VISIBLE_ERROR
        return ""
    return _sanitize_plain_assistant_text(
        original,
        restricted_message=restricted_message,
    )


def sanitize_runtime_debug_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _normalized_key(key) in _SENSITIVE_KEY_NAMES:
                continue
            sanitized[key] = sanitize_runtime_debug_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_runtime_debug_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_runtime_debug_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_user_visible_text(value)
    return value


def _flatten_error_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return " ".join(
            part
            for part in (_flatten_error_text(item) for item in value.values())
            if part
        ).strip()
    if isinstance(value, (list, tuple)):
        return " ".join(
            part
            for part in (_flatten_error_text(item) for item in value)
            if part
        ).strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value).strip()


def sanitize_user_visible_error(
    value: Any,
    default_message: str = DEFAULT_USER_VISIBLE_ERROR,
) -> str:
    original = _flatten_error_text(value)
    text = sanitize_user_visible_text(original)
    lowered = text.lower()
    if not lowered:
        return default_message
    if _SHELL_NOISE_RE.search(lowered):
        return default_message
    if any(keyword in lowered for keyword in ("permission denied", "access denied")):
        return "无法访问该文件"
    if any(keyword in lowered for keyword in ("file not found", "no such file", "not a file")):
        return "无法访问该文件"
    if any(keyword in lowered for keyword in ("directory", "workspace-tree", "list dir", "read dir")):
        return "目录读取失败"
    if any(keyword in lowered for keyword in ("timed out", "timeout", "deadline exceeded")):
        return default_message
    if any(keyword in lowered for keyword in ("invalid", "unsupported", "bad request", "required", "validation")):
        return "请求未通过校验"
    if "[internal-path]" in text:
        return default_message
    return default_message


def sanitize_tool_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        name = str(item.get("name") or item.get("tool_name") or "").strip()
        if name:
            entry["name"] = name
        status = str(item.get("status") or "").strip()
        if not status and item.get("success") is not None:
            status = "completed" if item.get("success") is True else "failed"
        if status:
            entry["status"] = status
        tool_call_id = str(item.get("tool_call_id") or "").strip()
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        model_call_id = str(item.get("model_call_id") or "").strip()
        if model_call_id:
            entry["model_call_id"] = model_call_id
        if entry:
            sanitized.append(entry)
    return sanitized


def sanitize_agent_test_runtime_payload(
    payload: Any,
    *,
    include_tool_calls: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    token_count = 0
    usage = payload.get("usage")
    token_candidates = [
        payload.get("token_count"),
        payload.get("total_tokens"),
        payload.get("totalTokens"),
    ]
    if isinstance(usage, dict):
        token_candidates.extend([
            usage.get("total_tokens"),
            usage.get("totalTokens"),
            usage.get("token_count"),
            usage.get("tokenCount"),
        ])
    for candidate in token_candidates:
        try:
            token_count = max(int(float(candidate or 0)), 0)
        except (TypeError, ValueError):
            token_count = 0
        if token_count > 0:
            break
    sanitized = {
        "assistant_content": sanitize_assistant_content_for_user(payload.get("assistant_content") or ""),
        "token_count": token_count,
    }
    if include_tool_calls:
        sanitized["tool_calls"] = sanitize_tool_calls(payload.get("tool_calls"))
    return sanitized


def is_restricted_runtime_request(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return _contains_system_directory_probe(text)


def validate_workspace_logical_path(value: Any, field_name: str = "path") -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError(f"{field_name} is required")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        raise ValueError(f"{field_name} is not allowed")
    normalized = posixpath.normpath(raw)
    if normalized in ("", ".", "/"):
        raise ValueError(f"{field_name} is required")
    if normalized.startswith("../") or normalized == "..":
        raise ValueError(f"{field_name} is not allowed")
    segments = [segment for segment in normalized.split("/") if segment and segment != "."]
    if not segments:
        raise ValueError(f"{field_name} is required")
    if segments[0].lower() in _LOGICAL_PATH_BLOCKLIST:
        raise ValueError(f"{field_name} is not allowed")
    return "/".join(segments)


def validate_runtime_identifier(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if not _IDENTIFIER_RE.match(normalized):
        raise ValueError(f"{field_name} is invalid")
    return normalized


def build_openclaw_agent_exec_command(
    *,
    container_name: str,
    tenant_home: str,
    agent_id: Any,
    session_key: Any,
    content: Any,
) -> list[str]:
    return [
        "docker",
        "exec",
        "-e",
        f"HOME={tenant_home}",
        container_name,
        "openclaw",
        "agent",
        "--agent",
        validate_runtime_identifier(agent_id, "agent_id"),
        "--session-id",
        validate_runtime_identifier(session_key, "session_key"),
        "--message",
        str(content or ""),
        "--json",
    ]


def build_openclaw_agent_stream_command(
    *,
    container_name: str,
    tenant_home: str,
    agent_id: Any,
    session_key: Any,
    content: Any,
) -> list[str]:
    return [
        "docker",
        "exec",
        "-e",
        f"HOME={tenant_home}",
        container_name,
        "openclaw",
        "agent",
        "--agent",
        validate_runtime_identifier(agent_id, "agent_id"),
        "--session-id",
        validate_runtime_identifier(session_key, "session_key"),
        "--message",
        str(content or ""),
    ]


def build_openclaw_models_list_command(*, container_name: str, tenant_home: str) -> list[str]:
    return [
        "docker",
        "exec",
        "-e",
        f"HOME={tenant_home}",
        container_name,
        "openclaw",
        "models",
        "list",
        "--all",
        "--json",
    ]
