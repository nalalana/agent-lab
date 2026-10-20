#!/usr/bin/env python3
"""Build a secret-free inventory before stopping legacy OpenClaw infrastructure."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_ROOT = os.getenv("TSDS_RUNTIME_ROOT", "/opt/tsds/tenants")
DEFAULT_SERVICE = "tsds-shared-host-bridge.service"
NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
COMPOSE_NAMES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")


def validate_runtime_root(value: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError("runtime root must be absolute")
    if raw.is_symlink():
        raise ValueError("runtime root cannot be a symlink")
    resolved = raw.resolve(strict=False)
    if resolved.name != "tenants" or len(resolved.parts) < 4:
        raise ValueError("runtime root must be a dedicated .../tenants directory")
    if resolved in {Path("/"), Path("/opt"), Path("/data"), Path("/opt/tsds")}:
        raise ValueError("runtime root is too broad")
    return resolved


def _run(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, (result.stdout or "").strip()


def _systemd_state(service_name: str) -> dict[str, str]:
    active_code, active = _run(["systemctl", "is-active", service_name])
    enabled_code, enabled = _run(["systemctl", "is-enabled", service_name])
    return {
        "service": service_name,
        "active": active if active_code == 0 else active or "unknown",
        "enabled": enabled if enabled_code == 0 else enabled or "unknown",
    }


def _tenant_inventory(runtime_root: Path) -> list[dict[str, Any]]:
    if not runtime_root.is_dir():
        return []
    tenants = []
    for candidate in sorted(runtime_root.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if not NAMESPACE_PATTERN.fullmatch(candidate.name):
            continue
        compose_path = next(
            (candidate / name for name in COMPOSE_NAMES if (candidate / name).is_file()),
            None,
        )
        tenants.append(
            {
                "namespace": candidate.name,
                "composeFile": str(compose_path) if compose_path else None,
                "hasOpenClawHome": (candidate / "home" / ".openclaw").is_dir(),
                "hasOpenVikingData": (candidate / "openviking").is_dir(),
            }
        )
    return tenants


def _docker_projects() -> list[str]:
    code, output = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project",
            "--format",
            '{{.Label "com.docker.compose.project"}}',
        ]
    )
    if code != 0:
        return []
    return sorted({line.strip() for line in output.splitlines() if line.strip()})


def _project_images(projects: list[str]) -> list[dict[str, str]]:
    images: dict[str, str] = {}
    for project in projects:
        code, output = _run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Image}}",
            ]
        )
        if code != 0:
            continue
        for reference in {line.strip() for line in output.splitlines() if line.strip()}:
            inspect_code, image_id = _run(
                ["docker", "image", "inspect", reference, "--format", "{{.Id}}"]
            )
            images[reference] = image_id if inspect_code == 0 else "unknown"
    return [
        {"reference": reference, "imageId": image_id}
        for reference, image_id in sorted(images.items())
    ]


def build_inventory(
    *,
    runtime_root: Path,
    service_name: str,
    bridge_port: int,
) -> dict[str, Any]:
    runtime_root = validate_runtime_root(str(runtime_root))
    tenants = _tenant_inventory(runtime_root)
    projects = _docker_projects()
    _ss_code, listeners = _run(["ss", "-H", "-ltn"])
    process_code, process_output = _run(["pgrep", "-fc", "shared_host_bridge.py"])
    git_code, git_commit = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "runtimeRoot": str(runtime_root),
        "runtimeRootExists": runtime_root.is_dir(),
        "bridge": {
            **_systemd_state(service_name),
            "port": bridge_port,
            "portListening": any(
                any(token.endswith(f":{bridge_port}") for token in line.split())
                for line in listeners.splitlines()
                if line.strip()
            ),
            "processCount": int(process_output) if process_code == 0 and process_output.isdigit() else 0,
        },
        "tenants": tenants,
        "docker": {
            "composeProjects": projects,
            "images": _project_images(projects),
        },
        "release": {
            "repositoryCommit": git_commit if git_code == 0 else "unknown",
            "preserveForOneRelease": True,
        },
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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
    parser.add_argument("--runtime-root", default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE)
    parser.add_argument("--bridge-port", type=int, default=19080)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        report = build_inventory(
            runtime_root=Path(args.runtime_root),
            service_name=args.service_name,
            bridge_port=max(args.bridge_port, 1),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.output:
        write_json_atomic(Path(args.output), report)
        print(f"Phase4-C inventory written to {Path(args.output).expanduser().resolve()}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
