#!/usr/bin/env python3
"""Safely stop legacy Bridge and tenant Compose projects without deleting data."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONFIRMATION = "STOP_OPENCLAW_PHASE4C"
COMPOSE_NAMES = {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}
SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
REQUIRED_PREFLIGHT_CHECK_IDS = frozenset(
    {
        "migrations",
        "legacy_runtime_read_only",
        "unified_runtime_policies",
        "active_agent_runs",
        "active_agsx_sandboxes",
        "agsx_cleanup_candidates",
        "agsx_production_configuration",
        "legacy_provision_tasks",
        "legacy_archive_integrity",
    }
)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError(f"{label} must be a schemaVersion 1 JSON object")
    return payload


def _parse_timestamp(value: object, label: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid generatedAt value") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"{label} generatedAt must include a timezone")
    return timestamp.astimezone(timezone.utc)


def _validate_freshness(payload: dict[str, Any], label: str, max_age_minutes: int) -> None:
    generated_at = _parse_timestamp(payload.get("generatedAt"), label)
    age = datetime.now(timezone.utc) - generated_at
    if age < timedelta(minutes=-5) or age > timedelta(minutes=max_age_minutes):
        raise ValueError(f"{label} is stale or generated in the future")


def _validate_runtime_root(value: object) -> Path:
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("inventory runtimeRoot must be an absolute non-symlink path")
    resolved = raw.resolve(strict=False)
    if resolved.name != "tenants" or len(resolved.parts) < 4:
        raise ValueError("inventory runtimeRoot must be a dedicated .../tenants directory")
    return resolved


def _compose_paths(inventory: dict[str, Any], runtime_root: Path) -> list[Path]:
    result = []
    for tenant in inventory.get("tenants") or []:
        if not isinstance(tenant, dict) or not tenant.get("composeFile"):
            continue
        compose_path = Path(str(tenant["composeFile"]))
        if not compose_path.is_absolute() or compose_path.is_symlink():
            raise ValueError("inventory contains a non-absolute or symlink Compose path")
        resolved = compose_path.resolve(strict=False)
        try:
            resolved.relative_to(runtime_root)
        except ValueError as exc:
            raise ValueError("inventory contains a Compose path outside runtimeRoot") from exc
        if resolved.name not in COMPOSE_NAMES or not resolved.is_file():
            raise ValueError(f"inventory Compose file is unavailable: {resolved}")
        result.append(resolved)
    return sorted(set(result))


def build_stop_plan(
    *,
    inventory: dict[str, Any],
    preflight: dict[str, Any],
    stop_timeout_seconds: int,
) -> dict[str, Any]:
    if inventory.get("hostname") != socket.gethostname():
        raise ValueError("inventory was generated on a different host")
    summary = preflight.get("summary") or {}
    if not summary.get("ready") or int(summary.get("blockers") or 0) != 0:
        raise ValueError("Phase4-C preflight report is not ready")
    checks = {
        str(item.get("id")): str(item.get("status"))
        for item in preflight.get("checks") or []
        if isinstance(item, dict)
    }
    missing_or_failed = sorted(
        check_id
        for check_id in REQUIRED_PREFLIGHT_CHECK_IDS
        if checks.get(check_id) != "pass"
    )
    if missing_or_failed:
        raise ValueError(
            "Phase4-C preflight is missing required passing checks: "
            + ", ".join(missing_or_failed)
        )
    runtime_root = _validate_runtime_root(inventory.get("runtimeRoot"))
    bridge = inventory.get("bridge") or {}
    service = str(bridge.get("service") or "")
    if not SERVICE_PATTERN.fullmatch(service):
        raise ValueError("inventory contains an invalid systemd service name")
    compose_paths = _compose_paths(inventory, runtime_root)
    stop_commands = [["systemctl", "disable", "--now", service]]
    stop_commands.extend(
        [
            "docker",
            "compose",
            "-f",
            str(compose_path),
            "stop",
            "--timeout",
            str(max(stop_timeout_seconds, 1)),
        ]
        for compose_path in compose_paths
    )
    recovery_commands = [
        ["docker", "compose", "-f", str(compose_path), "up", "-d"]
        for compose_path in compose_paths
    ]
    recovery_commands.append(["systemctl", "enable", "--now", service])
    return {
        "schemaVersion": 1,
        "runtimeRoot": str(runtime_root),
        "service": service,
        "tenantComposeCount": len(compose_paths),
        "stopCommands": stop_commands,
        "recoveryCommands": recovery_commands,
        "deletesData": False,
    }


def _execute(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"command could not be executed: {command[0]}") from exc
    record = {
        "command": command,
        "returnCode": result.returncode,
        "output": (result.stdout or "")[-4000:],
    }
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: {' '.join(command)}"
        )
    return record


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--preflight-report", required=True)
    parser.add_argument("--max-report-age-minutes", type=int, default=60)
    parser.add_argument("--stop-timeout-seconds", type=int, default=60)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--output")
    args = parser.parse_args()

    try:
        inventory = _load_json(Path(args.inventory), "inventory")
        preflight = _load_json(Path(args.preflight_report), "preflight report")
        max_age = max(args.max_report_age_minutes, 1)
        _validate_freshness(inventory, "inventory", max_age)
        _validate_freshness(preflight, "preflight report", max_age)
        plan = build_stop_plan(
            inventory=inventory,
            preflight=preflight,
            stop_timeout_seconds=args.stop_timeout_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")

    executions = []
    if args.apply:
        for command in plan["stopCommands"]:
            executions.append(_execute(command))
    result = {
        **plan,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "executions": executions,
    }
    if args.output:
        _write_json_atomic(Path(args.output), result)
        print(f"Phase4-C stop result written to {Path(args.output).expanduser().resolve()}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
