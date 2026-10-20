"""OpenClaw 2026.3.11 command builders for the shared-host bridge.

Contract:
- This module is intentionally version-specific. OpenClaw CLI flags have changed
  between releases, so bridge code must call these builders instead of spelling
  request-time CLI arguments inline.
- Agent chat, model catalog, skill catalog, and cron CLI command construction
  belongs here. When upgrading OpenClaw, add a new module such as
  ``openclaw_tools_2026_4_0.py`` and switch the bridge import after smoke tests.
- The bridge may still own bootstrap wiring such as installing OpenClaw in the
  gateway image and starting ``openclaw gateway`` because that is container
  lifecycle configuration, not request-time command construction.

The module only builds commands/arguments. It does not execute subprocesses,
read tenant files, authorize requests, or sanitize output; those responsibilities
stay in ``shared_host_bridge.py``.
"""

from __future__ import annotations

from typing import Any

OPENCLAW_VERSION = "2026.3.11"

_IDENTIFIER_ALLOWED_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")


def validate_openclaw_identifier(value: Any, field_name: str = "id") -> str:
    """Validate identifiers accepted by the 2026.3.11 CLI arguments we use."""

    normalized = str(value or "").strip()
    if not normalized or any(char not in _IDENTIFIER_ALLOWED_CHARS for char in normalized):
        raise ValueError(f"{field_name} is invalid")
    return normalized


def build_openclaw_command(
    *,
    container_name: str,
    tenant_home: str,
    args: list[str],
    json_output: bool = False,
) -> list[str]:
    """Build a docker-exec OpenClaw command for the tenant gateway container."""

    command = [
        "docker",
        "exec",
        "-e",
        f"HOME={tenant_home}",
        container_name,
        "openclaw",
        *args,
    ]
    if json_output:
        command.append("--json")
    return command


def build_openclaw_agent_exec_command(
    *,
    container_name: str,
    tenant_home: str,
    agent_id: Any,
    session_key: Any,
    content: Any,
) -> list[str]:
    """OpenClaw 2026.3.11 non-streaming agent turn command."""

    return build_openclaw_command(
        container_name=container_name,
        tenant_home=tenant_home,
        args=[
            "agent",
            "--agent",
            validate_openclaw_identifier(agent_id, "agent_id"),
            "--session-id",
            validate_openclaw_identifier(session_key, "session_key"),
            "--message",
            str(content or ""),
        ],
        json_output=True,
    )


def build_openclaw_agent_stream_command(
    *,
    container_name: str,
    tenant_home: str,
    agent_id: Any,
    session_key: Any,
    content: Any,
) -> list[str]:
    """OpenClaw 2026.3.11 streaming agent turn command."""

    return build_openclaw_command(
        container_name=container_name,
        tenant_home=tenant_home,
        args=[
            "agent",
            "--agent",
            validate_openclaw_identifier(agent_id, "agent_id"),
            "--session-id",
            validate_openclaw_identifier(session_key, "session_key"),
            "--message",
            str(content or ""),
        ],
    )


def build_openclaw_models_list_command(*, container_name: str, tenant_home: str) -> list[str]:
    """OpenClaw 2026.3.11 model catalog command."""

    return build_openclaw_command(
        container_name=container_name,
        tenant_home=tenant_home,
        args=["models", "list", "--all"],
        json_output=True,
    )


def build_openclaw_skills_list_command(*, container_name: str, tenant_home: str) -> list[str]:
    """OpenClaw 2026.3.11 native skill catalog command."""

    return build_openclaw_command(
        container_name=container_name,
        tenant_home=tenant_home,
        args=["skills", "list"],
        json_output=True,
    )


def build_openclaw_cron_command(
    *,
    container_name: str,
    tenant_home: str,
    args: list[str],
    json_output: bool = True,
) -> list[str]:
    """Build an OpenClaw cron command.

    In 2026.3.11, ``cron add/list/rm`` support ``--json`` while ``cron run`` and
    ``cron runs`` do not. Callers must pass ``json_output=False`` for those two
    commands and parse the embedded JSON from stdout.
    """

    return build_openclaw_command(
        container_name=container_name,
        tenant_home=tenant_home,
        args=["cron", *args],
        json_output=json_output,
    )


def build_openclaw_cron_list_args() -> list[str]:
    """List all cron jobs, including disabled jobs shown in the platform UI."""

    return ["list", "--all"]


def build_openclaw_cron_runs_args(job_id: Any, limit: int) -> list[str]:
    """Read run history. 2026.3.11 requires ``--id`` rather than a positional id."""

    return ["runs", "--id", validate_openclaw_identifier(job_id), "--limit", str(limit)]


def build_openclaw_cron_add_args(payload: dict[str, Any]) -> list[str]:
    """Build args for creating a cron job in OpenClaw 2026.3.11.

    Schedule modes supported by this version are ``--every``, ``--cron``, and
    ``--at``. Custom platform sessions are routed through ``--session-key``;
    ``--session`` is kept for native ``main``/``current``/``isolated`` targets.
    ``--no-deliver`` is deliberate: Touming reads execution results from cron
    run history instead of requiring an external chat delivery channel.
    """

    name = str(payload.get("name") or "").strip()
    message = str(payload.get("message") or "").strip()
    schedule_type = str(payload.get("schedule_type") or "every").strip().lower()
    schedule_value = str(payload.get("schedule_value") or "").strip()
    if not name:
        raise ValueError("name is required")
    if not message:
        raise ValueError("message is required")
    if schedule_type not in {"at", "every", "cron"}:
        raise ValueError("schedule_type is invalid")
    if not schedule_value:
        raise ValueError("schedule_value is required")

    args = ["add", "--name", name, f"--{schedule_type}", schedule_value]
    agent_id = str(payload.get("agent_id") or "").strip()
    session = str(payload.get("session") or "").strip() or "isolated"
    custom_session_key = ""
    if session.startswith("session:"):
        custom_session = validate_openclaw_identifier(session.removeprefix("session:"), "session")
        custom_session_key = f"agent:{agent_id or 'main'}:{custom_session}"
        session = ""
    if agent_id:
        args.extend(["--agent", validate_openclaw_identifier(agent_id, "agent_id")])
    if custom_session_key:
        args.extend(["--session-key", validate_openclaw_identifier(custom_session_key, "session_key")])
    elif session:
        args.extend(["--session", validate_openclaw_identifier(session, "session")])
    if session in {"main", "current"}:
        args.extend(["--system-event", message, "--wake", "now"])
    else:
        args.extend(["--message", message])
    args.append("--no-deliver")
    if payload.get("enabled") is False:
        args.append("--disabled")
    return args


def build_openclaw_cron_run_args(job_id: Any) -> list[str]:
    """Run a cron job now and wait long enough for the result to reach history."""

    return ["run", validate_openclaw_identifier(job_id), "--expect-final", "--timeout", "90000"]


def build_openclaw_cron_remove_args(job_id: Any) -> list[str]:
    """Remove a cron job. This subcommand supports JSON output in 2026.3.11."""

    return ["rm", validate_openclaw_identifier(job_id)]
