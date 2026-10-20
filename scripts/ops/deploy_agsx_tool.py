#!/usr/bin/env python3
"""Build, push and provision the Phase4-C production AGSX Tool safely.

The protected ``/etc/touming/agsx/deploy.env`` file is the single source of
truth for deployment credentials, AGSX provisioning values and AGSX runtime
settings. The file is parsed as data: no shell, ``source`` or ``eval`` is used.
Secrets are never placed in subprocess arguments or generated JSON files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path("/etc/touming/agsx/deploy.env")
DEFAULT_TEMPLATE = REPO_ROOT / "config/agsx/tool-2c2g.example.json"
DEFAULT_RELEASE_ROOT = Path("/var/lib/touming/agsx/releases")
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
ROLE_ARN_PATTERN = re.compile(r"^qcs::cam::uin/[^:]+:roleName/[^/]+$")
DIGEST_IMAGE_PATTERN = re.compile(r"@sha256:[0-9a-fA-F]{64}$")
IMAGE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")

DEPLOYMENT_CONFIG_KEYS = {
    "AGSX_REGION",
    "AGSX_CLI_DOMAIN",
    "AGSX_TOOL_NAME",
    "AGSX_TOOL_DESCRIPTION",
    "AGSX_CLIENT_TOKEN",
    "AGSX_ROLE_ARN",
    "AGSX_IMAGE_REGISTRY_TYPE",
    "AGSX_IMAGE_REGISTRY_HOST",
    "AGSX_IMAGE_REPOSITORY",
    "AGSX_REGISTRY_USERNAME",
    "AGSX_REGISTRY_PASSWORD",
    "AGSX_IMAGE_TAG",
    "AGSX_BASE_IMAGE",
    "AGSX_DOCKERFILE",
    "AGSX_BUILD_CONTEXT",
    "AGSX_DEFAULT_TIMEOUT",
    "AGSX_CPU",
    "AGSX_MEMORY",
    "AGSX_NETWORK_MODE",
    "AGSX_PERSISTENT",
    "AGSX_ENVIRONMENT",
    "AGSX_OWNER",
    "AGSX_TOOL_POLL_SECONDS",
    "AGSX_TOOL_POLL_ATTEMPTS",
    "AGSX_RELEASE_ROOT",
    "AGSX_RUNTIME_ENV_PATH",
}
RUNTIME_CONFIG_KEYS = {
    "AGSX_ENABLED",
    "E2B_DOMAIN",
    "E2B_API_KEY",
    "AGSX_TEMPLATE",
    "AGSX_DEFAULT_CPU_CORES",
    "AGSX_DEFAULT_MEMORY_MIB",
    "AGSX_DEFAULT_TTL_SECONDS",
    "AGSX_NETWORK_POLICY",
    "AGSX_REQUEST_TIMEOUT_SECONDS",
    "AGSX_SANDBOX_CLEANUP_GRACE_SECONDS",
    "AGSX_REAPER_BATCH_SIZE",
    "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES",
    "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR",
    "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G",
    "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS",
    "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS",
    "AGSX_COST_CURRENCY",
    "AGSX_ENFORCE_EFFECTIVE_RESOURCES",
    "AGSX_ALERT_FAILURE_WINDOW_MINUTES",
    "AGSX_ALERT_FAILURE_RATE",
    "AGSX_ALERT_FAILURE_MIN_CREATES",
}
CONFIG_KEYS = DEPLOYMENT_CONFIG_KEYS | RUNTIME_CONFIG_KEYS

OPTIONAL_CONFIG_KEYS = {
    "AGSX_TOOL_DESCRIPTION",
    "AGSX_CLI_DOMAIN",
    "AGSX_ENVIRONMENT",
    "AGSX_OWNER",
    "AGSX_TOOL_POLL_SECONDS",
    "AGSX_TOOL_POLL_ATTEMPTS",
    "AGSX_RELEASE_ROOT",
    "AGSX_RUNTIME_ENV_PATH",
}
REQUIRED_CONFIG_KEYS = CONFIG_KEYS - OPTIONAL_CONFIG_KEYS

CLOUD_SECRET_NAMES = ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY")
OPTIONAL_CLOUD_SECRET_NAMES = ("TENCENTCLOUD_TOKEN",)
REGISTRY_SECRET_NAMES = ("AGSX_REGISTRY_PASSWORD",)
RUNTIME_SECRET_NAMES = (
    "E2B_API_KEY",
    "AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID",
    "AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY",
    "AGENT_RUNTIME_ARTIFACT_COS_TOKEN",
    "AGSX_COS_SECRET_ID",
    "AGSX_COS_SECRET_KEY",
    "AGSX_COS_TOKEN",
)
ALL_SECRET_NAMES = (
    CLOUD_SECRET_NAMES
    + OPTIONAL_CLOUD_SECRET_NAMES
    + REGISTRY_SECRET_NAMES
    + RUNTIME_SECRET_NAMES
)
ARTIFACT_CONFIG_KEYS = (
    "AGENT_RUNTIME_ARTIFACT_COS_BUCKET",
    "AGENT_RUNTIME_ARTIFACT_COS_REGION",
    "AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID",
    "AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY",
    "AGENT_RUNTIME_ARTIFACT_COS_PREFIX",
)
BACKEND_RUNTIME_CONFIG_KEYS = tuple(
    sorted(
        RUNTIME_CONFIG_KEYS
        | set(ARTIFACT_CONFIG_KEYS)
        | {"AGENT_RUNTIME_ARTIFACT_COS_TOKEN"}
    )
)


class DeploymentError(RuntimeError):
    """A safe, user-actionable deployment failure."""


@dataclass(frozen=True)
class SecretValue:
    value: str
    source: str


def _validate_protected_file(path: Path, description: str) -> None:
    if not path.is_absolute():
        raise DeploymentError(f"{description} path must be absolute: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise DeploymentError(f"{description} does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeploymentError(f"{description} must be a regular, non-symlink file: {path}")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise DeploymentError(
            f"{description} must be owned by root or the deployment user: {path}"
        )
    # 0640 is allowed for a dedicated operations group. Group write/execute and
    # every permission for other users are rejected.
    if metadata.st_mode & 0o027:
        actual = stat.S_IMODE(metadata.st_mode)
        raise DeploymentError(
            f"{description} permissions must be 0600/0400 or group-read-only; "
            f"found {actual:04o}: {path}"
        )


def parse_key_value_file(path: Path, *, protected: bool = False) -> dict[str, str]:
    """Parse a non-executable KEY=VALUE file without evaluating shell syntax."""
    path = path.expanduser()
    if protected:
        _validate_protected_file(path, "AGSX deploy configuration")
    elif not path.is_file():
        raise DeploymentError(f"configuration file does not exist: {path}")

    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not KEY_PATTERN.fullmatch(key):
            raise DeploymentError(f"invalid KEY=VALUE entry at {path}:{number}")
        if key in values:
            raise DeploymentError(f"duplicate key {key} at {path}:{number}")
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if "\x00" in value or "\r" in value or "\n" in value:
            raise DeploymentError(f"invalid control character at {path}:{number}")
        values[key] = value
    return values


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or "<" in lowered or "replace_me" in lowered or "change-me" in lowered


def load_configuration(path: Path) -> dict[str, str]:
    # Do not resolve first: lstat in the protected-file check must see and
    # reject a symlink rather than silently following it.
    config = parse_key_value_file(path.expanduser(), protected=True)
    unknown = sorted(key for key in config if key.startswith("AGSX_") and key not in CONFIG_KEYS)
    if unknown:
        raise DeploymentError(f"unknown AGSX configuration keys: {', '.join(unknown)}")

    config.setdefault("AGSX_CLI_DOMAIN", "tencentags.com")
    config.setdefault("AGSX_TOOL_DESCRIPTION", "Touming production isolated code sandbox")
    config.setdefault("AGSX_ENVIRONMENT", "production")
    config.setdefault("AGSX_OWNER", "agent-platform")
    config.setdefault("AGSX_TOOL_POLL_SECONDS", "5")
    config.setdefault("AGSX_TOOL_POLL_ATTEMPTS", "60")
    config.setdefault("AGSX_RELEASE_ROOT", str(DEFAULT_RELEASE_ROOT))
    config.setdefault("AGSX_RUNTIME_ENV_PATH", "/etc/touming/agsx/runtime.env")

    required = set(REQUIRED_CONFIG_KEYS) | set(CLOUD_SECRET_NAMES) | set(REGISTRY_SECRET_NAMES)
    required.update(ARTIFACT_CONFIG_KEYS)
    missing = sorted(key for key in required if _is_placeholder(config.get(key, "")))
    if missing:
        raise DeploymentError(f"missing or placeholder production configuration: {', '.join(missing)}")
    validate_configuration(config)
    return config


def _positive_integer(config: Mapping[str, str], key: str) -> int:
    try:
        value = int(config[key])
    except (KeyError, ValueError) as exc:
        raise DeploymentError(f"{key} must be a positive integer") from exc
    if value <= 0:
        raise DeploymentError(f"{key} must be a positive integer")
    return value


def _true(config: Mapping[str, str], key: str) -> bool:
    value = config.get(key, "").strip().lower()
    if value not in {"true", "false"}:
        raise DeploymentError(f"{key} must be true or false")
    return value == "true"


def validate_configuration(config: Mapping[str, str]) -> None:
    tool_name = config["AGSX_TOOL_NAME"]
    if not TOOL_NAME_PATTERN.fullmatch(tool_name):
        raise DeploymentError("AGSX_TOOL_NAME must be 1-50 letters, digits, underscores or hyphens")
    token = config["AGSX_CLIENT_TOKEN"]
    if _is_placeholder(token) or len(token) > 64:
        raise DeploymentError("AGSX_CLIENT_TOKEN must be a real value no longer than 64 characters")
    role = config["AGSX_ROLE_ARN"]
    if not ROLE_ARN_PATTERN.fullmatch(role) or _is_placeholder(role):
        raise DeploymentError("AGSX_ROLE_ARN must be a real qcs::cam role ARN")
    registry_type = config["AGSX_IMAGE_REGISTRY_TYPE"]
    if registry_type not in {"personal", "enterprise"}:
        raise DeploymentError(
            "this custom-image workflow requires a personal or enterprise registry"
        )
    for key in (
        "AGSX_REGION",
        "AGSX_CLI_DOMAIN",
        "AGSX_IMAGE_REGISTRY_HOST",
        "AGSX_IMAGE_REPOSITORY",
        "AGSX_REGISTRY_USERNAME",
    ):
        value = config[key]
        if _is_placeholder(value) or any(char.isspace() for char in value):
            raise DeploymentError(f"{key} must be a real value without whitespace")
    if "://" in config["AGSX_CLI_DOMAIN"] or "/" in config["AGSX_CLI_DOMAIN"]:
        raise DeploymentError("AGSX_CLI_DOMAIN must contain only a domain name")

    tag = config["AGSX_IMAGE_TAG"]
    if tag.lower() == "latest" or _is_placeholder(tag) or not IMAGE_TAG_PATTERN.fullmatch(tag):
        raise DeploymentError("AGSX_IMAGE_TAG must be an immutable, non-latest Docker tag")
    base_image = config["AGSX_BASE_IMAGE"]
    if not DIGEST_IMAGE_PATTERN.search(base_image) or _is_placeholder(base_image):
        raise DeploymentError("AGSX_BASE_IMAGE must be pinned with a real @sha256 digest")

    phase4c_expected = {
        "AGSX_DEFAULT_TIMEOUT": "5m",
        "AGSX_CPU": "2",
        "AGSX_MEMORY": "2Gi",
        "AGSX_NETWORK_MODE": "SANDBOX",
        "AGSX_PERSISTENT": "false",
        "AGSX_DEFAULT_CPU_CORES": "2",
        "AGSX_DEFAULT_MEMORY_MIB": "2048",
        "AGSX_DEFAULT_TTL_SECONDS": "300",
        "AGSX_NETWORK_POLICY": "isolated",
    }
    for key, expected in phase4c_expected.items():
        if config[key] != expected:
            raise DeploymentError(f"Phase4-C requires {key}={expected}")

    if not _true(config, "AGSX_ENABLED"):
        raise DeploymentError("production one-click deployment requires AGSX_ENABLED=true")
    if not _true(config, "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR"):
        raise DeploymentError("production quota control requires fail-closed behavior")
    if not _true(config, "AGSX_ENFORCE_EFFECTIVE_RESOURCES"):
        raise DeploymentError("full production rollout requires AGSX_ENFORCE_EFFECTIVE_RESOURCES=true")

    domain = config["E2B_DOMAIN"]
    if _is_placeholder(domain) or "://" in domain or "/" in domain or any(c.isspace() for c in domain):
        raise DeploymentError("E2B_DOMAIN must contain only the AGSX endpoint domain")
    if config["AGSX_TEMPLATE"] != tool_name:
        raise DeploymentError("AGSX_TEMPLATE must exactly match AGSX_TOOL_NAME")

    for key in (
        "AGSX_TOOL_POLL_SECONDS",
        "AGSX_TOOL_POLL_ATTEMPTS",
        "AGSX_REQUEST_TIMEOUT_SECONDS",
        "AGSX_SANDBOX_CLEANUP_GRACE_SECONDS",
        "AGSX_REAPER_BATCH_SIZE",
        "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES",
        "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G",
        "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS",
        "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS",
        "AGSX_ALERT_FAILURE_WINDOW_MINUTES",
        "AGSX_ALERT_FAILURE_MIN_CREATES",
    ):
        _positive_integer(config, key)
    try:
        failure_rate = float(config["AGSX_ALERT_FAILURE_RATE"])
    except ValueError as exc:
        raise DeploymentError("AGSX_ALERT_FAILURE_RATE must be a number in (0, 1]") from exc
    if not 0 < failure_rate <= 1:
        raise DeploymentError("AGSX_ALERT_FAILURE_RATE must be a number in (0, 1]")
    if not CURRENCY_PATTERN.fullmatch(config["AGSX_COST_CURRENCY"]):
        raise DeploymentError("AGSX_COST_CURRENCY must be a three-letter uppercase code")

    release_root = Path(config["AGSX_RELEASE_ROOT"]).expanduser()
    if not release_root.is_absolute():
        raise DeploymentError("AGSX_RELEASE_ROOT must be an absolute path")
    resolved_root = release_root.resolve()
    repository = REPO_ROOT.resolve()
    if resolved_root == repository or repository in resolved_root.parents:
        raise DeploymentError("AGSX_RELEASE_ROOT must be outside the Git repository")

    runtime_env_path = Path(config["AGSX_RUNTIME_ENV_PATH"]).expanduser()
    if not runtime_env_path.is_absolute():
        raise DeploymentError("AGSX_RUNTIME_ENV_PATH must be an absolute path")
    resolved_runtime_path = runtime_env_path.resolve()
    if resolved_runtime_path == repository or repository in resolved_runtime_path.parents:
        raise DeploymentError("AGSX_RUNTIME_ENV_PATH must be outside the Git repository")


def load_secrets(config: Mapping[str, str]) -> dict[str, SecretValue]:
    """Read deployment secrets only from the protected deploy.env file."""
    secrets: dict[str, SecretValue] = {}
    for name in CLOUD_SECRET_NAMES + REGISTRY_SECRET_NAMES:
        value = config.get(name, "")
        if _is_placeholder(value):
            raise DeploymentError(f"missing secret {name} in deploy.env")
        secrets[name] = SecretValue(value=value, source="protected deploy.env")
    for name in OPTIONAL_CLOUD_SECRET_NAMES:
        value = config.get(name, "")
        if value:
            if _is_placeholder(value):
                raise DeploymentError(f"{name} still contains a placeholder")
            secrets[name] = SecretValue(value=value, source="protected deploy.env")
    return secrets


def resolve_repo_path(raw_path: str, description: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    resolved = path.resolve()
    if not resolved.exists():
        raise DeploymentError(f"{description} does not exist: {resolved}")
    return resolved


def prepare_output_directory(raw_path: Path) -> Path:
    if not raw_path.is_absolute():
        raise DeploymentError("release output directory must be an absolute path")
    output = raw_path.resolve()
    repository = REPO_ROOT.resolve()
    if output == repository or repository in output.parents:
        raise DeploymentError("release output directory must not be inside the Git repository")
    output.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(output, 0o750)
    return output


def release_output_directory(config: Mapping[str, str], override: Optional[Path]) -> Path:
    if override is not None:
        return prepare_output_directory(override.expanduser())
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = Path(config["AGSX_RELEASE_ROOT"]) / f"{config['AGSX_TOOL_NAME']}-{timestamp}"
    return prepare_output_directory(path)


def render_request(config: Mapping[str, str], template_path: Path) -> dict[str, Any]:
    try:
        request = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"cannot read AGSX request template: {template_path}") from exc
    try:
        request["ToolName"] = config["AGSX_TOOL_NAME"]
        request["Description"] = config["AGSX_TOOL_DESCRIPTION"]
        request["DefaultTimeout"] = config["AGSX_DEFAULT_TIMEOUT"]
        request["ClientToken"] = config["AGSX_CLIENT_TOKEN"]
        request["NetworkConfiguration"]["NetworkMode"] = config["AGSX_NETWORK_MODE"]
        request["RoleArn"] = config["AGSX_ROLE_ARN"]
        custom = request["CustomConfiguration"]
        custom["Image"] = image_reference(config)
        custom["ImageRegistryType"] = config["AGSX_IMAGE_REGISTRY_TYPE"]
        custom["Resources"] = {"CPU": config["AGSX_CPU"], "Memory": config["AGSX_MEMORY"]}
    except (KeyError, TypeError) as exc:
        raise DeploymentError("AGSX request template does not have the required structure") from exc
    request["Persistent"] = config["AGSX_PERSISTENT"].lower() == "true"
    request["Tags"] = [
        {"Key": "service", "Value": "touming"},
        {"Key": "environment", "Value": config["AGSX_ENVIRONMENT"]},
        {"Key": "owner", "Value": config["AGSX_OWNER"]},
        {"Key": "schemaVersion", "Value": "1"},
    ]
    return request


def image_reference(config: Mapping[str, str]) -> str:
    return (
        f"{config['AGSX_IMAGE_REGISTRY_HOST'].rstrip('/')}/"
        f"{config['AGSX_IMAGE_REPOSITORY'].strip('/')}:{config['AGSX_IMAGE_TAG']}"
    )


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_runtime_environment(config: Mapping[str, str]) -> Path:
    """Atomically render the runtime-only subset consumed by Backend/Celery."""
    path = Path(config["AGSX_RUNTIME_ENV_PATH"]).expanduser()
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise DeploymentError(f"runtime environment parent must be a real directory: {parent}")
    if path.exists() or path.is_symlink():
        _validate_protected_file(path, "generated AGSX runtime environment")

    payload = "".join(
        f"{key}={json.dumps(config.get(key, ''), ensure_ascii=False)}\n"
        for key in BACKEND_RUNTIME_CONFIG_KEYS
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def _redact(text: str, secrets: Iterable[str]) -> str:
    result = text
    for value in secrets:
        if value:
            result = result.replace(value, "<redacted>")
    return result


def run_command(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    label: str,
    secret_values: Iterable[str] = (),
    stdin: Optional[str] = None,
) -> str:
    print(f"[AGSX] {label}", flush=True)
    process = subprocess.run(
        list(arguments),
        env=dict(environment),
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode:
        details = (process.stderr or process.stdout or "no command output")[-6000:]
        raise DeploymentError(f"{label} failed: {_redact(details, secret_values).strip()}")
    return process.stdout


def sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in ALL_SECRET_NAMES:
        environment.pop(name, None)
        environment.pop(f"{name}_FILE", None)
    return environment


def cloud_environment(secrets: Mapping[str, SecretValue]) -> dict[str, str]:
    environment = sanitized_environment()
    for name in CLOUD_SECRET_NAMES + OPTIONAL_CLOUD_SECRET_NAMES:
        if name in secrets:
            environment[name] = secrets[name].value
    return environment


def agr_arguments(config: Mapping[str, str], *command: str) -> list[str]:
    return [
        "agr",
        *command,
        "--region",
        config["AGSX_REGION"],
        "--domain",
        config["AGSX_CLI_DOMAIN"],
        "--non-interactive",
        "-o",
        "json",
    ]


def parse_json_output(raw: str, label: str) -> Any:
    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise DeploymentError(f"{label} did not return valid JSON")


def _data_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("Data"), dict):
        raise DeploymentError(f"{label} response is missing its Data object")
    return value["Data"]


def validate_agr_status(value: Any) -> None:
    data = _data_mapping(value, "agr status")
    auth = data.get("Auth")
    if not isinstance(auth, dict):
        raise DeploymentError("agr status response is missing Data.Auth")
    missing = []
    for key in ("SecretId", "SecretKey"):
        entry = auth.get(key)
        if not isinstance(entry, dict) or entry.get("Present") is not True:
            missing.append(key)
    if missing:
        raise DeploymentError(
            "agr did not detect required environment credentials: " + ", ".join(missing)
        )


def validate_agr_doctor(value: Any) -> None:
    data = _data_mapping(value, "agr doctor")
    checks = data.get("Checks")
    if not isinstance(checks, list):
        raise DeploymentError("agr doctor response is missing Data.Checks")
    statuses = {
        str(item.get("Name")): str(item.get("Status", "")).lower()
        for item in checks
        if isinstance(item, dict)
    }
    failed = [name for name in ("SecretId", "SecretKey", "Connectivity") if statuses.get(name) != "ok"]
    if failed:
        raise DeploymentError("agr doctor checks are not healthy: " + ", ".join(failed))


def find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_key(child, key)
            if found is not None:
                return found
    return None


def find_tool(value: Any, tool_name: str) -> Optional[Mapping[str, Any]]:
    if isinstance(value, dict):
        if value.get("ToolName") == tool_name:
            return value
        for child in value.values():
            found = find_tool(child, tool_name)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_tool(child, tool_name)
            if found:
                return found
    return None


def tool_data(value: Any) -> Mapping[str, Any]:
    """Return a Tool object without confusing the agr response envelope."""
    if not isinstance(value, dict):
        raise DeploymentError("agr Tool response is not a JSON object")
    data = value.get("Data")
    if isinstance(data, dict):
        return data
    return value


def tool_status(value: Any) -> str:
    return str(tool_data(value).get("Status") or "")


def tool_status_reason(value: Any) -> str:
    return str(tool_data(value).get("StatusReason") or "").strip()


def tool_image_digest(value: Any) -> Optional[str]:
    data = tool_data(value)
    image = str(find_key(data, "Image") or "")
    if "@sha256:" in image:
        return image
    digest = str(find_key(data, "ImageDigest") or "")
    if image and digest.startswith("sha256:"):
        return f"{image}@{digest}"
    return None


def check_prerequisites() -> None:
    missing = [program for program in ("docker", "agr", "git") if shutil.which(program) is None]
    if missing:
        raise DeploymentError(f"missing required executables: {', '.join(missing)}")


def validate_built_image(output: str) -> None:
    fields = output.strip().split(maxsplit=2)
    if len(fields) != 3 or fields[0] != "amd64" or fields[1] != "linux":
        raise DeploymentError(f"built image must be linux/amd64; inspect returned: {output.strip()}")
    try:
        entrypoint = json.loads(fields[2])
    except json.JSONDecodeError as exc:
        raise DeploymentError("cannot parse built image entrypoint") from exc
    if not isinstance(entrypoint, list) or "/init" not in entrypoint:
        raise DeploymentError(f"built image must preserve the /init entrypoint; found {entrypoint!r}")


def _image_repository_and_digest(reference: str) -> tuple[str, Optional[str]]:
    base, separator, digest = reference.partition("@sha256:")
    last_slash = base.rfind("/")
    last_colon = base.rfind(":")
    repository = base[:last_colon] if last_colon > last_slash else base
    return repository, f"sha256:{digest}" if separator else None


def verify_reused_tool(tool: Mapping[str, Any], request: Mapping[str, Any]) -> str:
    tool = tool_data(tool)
    tool_id = find_key(tool, "ToolId")
    status_value = tool_status(tool)
    if not tool_id:
        raise DeploymentError("existing Tool has no ToolId")
    if not status_value.upper().endswith("ACTIVE"):
        raise DeploymentError(f"existing Tool is not ACTIVE: {status_value or 'unknown'}")
    comparisons = {
        "NetworkMode": request["NetworkConfiguration"]["NetworkMode"],
        "Image": request["CustomConfiguration"]["Image"],
        "CPU": request["CustomConfiguration"]["Resources"]["CPU"],
        "Memory": request["CustomConfiguration"]["Resources"]["Memory"],
    }
    for key, expected in comparisons.items():
        actual = find_key(tool, key)
        if actual is None:
            continue
        if key == "Image":
            if str(actual) == str(expected):
                continue
            actual_base = str(actual).partition("@sha256:")[0]
            expected_base = str(expected).partition("@sha256:")[0]
            actual_repository, actual_digest = _image_repository_and_digest(str(actual))
            expected_repository, expected_digest = _image_repository_and_digest(str(expected))
            if (
                actual_digest is not None
                and expected_digest is not None
                and actual_repository == expected_repository
                and actual_digest == expected_digest
            ):
                continue
            if expected_digest is None and actual_digest is not None and actual_base == expected_base:
                # The pre-build request identifies the already immutable release
                # tag; AGSX commonly returns that tag plus its resolved digest.
                continue
        elif str(actual) == str(expected):
            continue
        raise DeploymentError(f"existing Tool {key}={actual!r}, expected {expected!r}")
    return str(tool_id)


def git_commit(environment: Mapping[str, str]) -> str:
    return run_command(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        environment=environment,
        label="recording source commit",
    ).strip()


def git_commit_time(environment: Mapping[str, str], commit: str) -> str:
    # Use source metadata rather than wall-clock time so retrying the exact same
    # release does not change the image digest merely because it was rebuilt.
    return run_command(
        ["git", "-C", str(REPO_ROOT), "show", "-s", "--format=%cI", commit],
        environment=environment,
        label="recording source commit time",
    ).strip()


def deploy(args: argparse.Namespace) -> Path:
    config = load_configuration(args.config)
    template_path = resolve_repo_path(str(args.template), "request template")
    dockerfile = resolve_repo_path(config["AGSX_DOCKERFILE"], "Dockerfile")
    build_context = resolve_repo_path(config["AGSX_BUILD_CONTEXT"], "Docker build context")
    output_directory = release_output_directory(config, args.output_dir)
    request = render_request(config, template_path)
    request_path = output_directory / "tool-request.json"
    atomic_write_json(request_path, request)
    print(f"[AGSX] validated request written to {request_path}", flush=True)

    if args.dry_run:
        print("[AGSX] dry-run complete; deploy.env is valid and no cloud call was made")
        return request_path

    secrets = load_secrets(config)
    if args.check_config:
        for name in CLOUD_SECRET_NAMES + OPTIONAL_CLOUD_SECRET_NAMES + REGISTRY_SECRET_NAMES:
            if name in secrets:
                print(f"[AGSX] {name}: available from {secrets[name].source}")
        print("[AGSX] configuration check complete; secret values were not displayed")
        return request_path

    if not args.confirm_cloud:
        raise DeploymentError("refusing production mutation without --confirm-cloud")
    check_prerequisites()
    clean_environment = sanitized_environment()
    cloud_env = cloud_environment(secrets)
    cloud_secret_values = [
        secret.value for name, secret in secrets.items() if name.startswith("TENCENTCLOUD_")
    ]
    source_commit = git_commit(clean_environment)
    image_created = git_commit_time(clean_environment, source_commit)

    raw_version = run_command(
        agr_arguments(config, "version"),
        environment=cloud_env,
        label="recording agr CLI version",
        secret_values=cloud_secret_values,
    )
    agr_version_response = parse_json_output(raw_version, "agr version")
    raw_status = run_command(
        agr_arguments(config, "status"),
        environment=cloud_env,
        label="validating Tencent Cloud identity",
        secret_values=cloud_secret_values,
    )
    validate_agr_status(parse_json_output(raw_status, "agr status"))
    raw_doctor = run_command(
        agr_arguments(config, "doctor"),
        environment=cloud_env,
        label="running agr diagnostics",
        secret_values=cloud_secret_values,
    )
    validate_agr_doctor(parse_json_output(raw_doctor, "agr doctor"))
    raw_tools = run_command(
        agr_arguments(config, "tool", "list", "--limit", "100"),
        environment=cloud_env,
        label="checking existing AGSX Tools",
        secret_values=cloud_secret_values,
    )
    existing = find_tool(parse_json_output(raw_tools, "agr tool list"), config["AGSX_TOOL_NAME"])

    image = image_reference(config)
    reused = existing is not None
    if existing:
        # A matching ACTIVE Tool means this immutable release is already fully
        # deployed. Reuse it before any registry/build mutation.
        tool_id = verify_reused_tool(existing, request)
        raw_tool = run_command(
            agr_arguments(config, "tool", "get", tool_id),
            environment=cloud_env,
            label=f"reading existing Tool {tool_id}",
            secret_values=cloud_secret_values,
        )
        tool_response = parse_json_output(raw_tool, "agr tool get")
        verify_reused_tool(tool_response, request)
        image_digest = tool_image_digest(tool_response)
    else:
        with tempfile.TemporaryDirectory(prefix="touming-agsx-docker-") as docker_config:
            docker_env = dict(clean_environment)
            docker_env["DOCKER_CONFIG"] = docker_config
            registry_password = secrets["AGSX_REGISTRY_PASSWORD"].value
            run_command(
                [
                    "docker",
                    "login",
                    config["AGSX_IMAGE_REGISTRY_HOST"],
                    "--username",
                    config["AGSX_REGISTRY_USERNAME"],
                    "--password-stdin",
                ],
                environment=docker_env,
                label="logging in to the image registry with an ephemeral Docker config",
                secret_values=(registry_password,),
                stdin=registry_password + "\n",
            )
            run_command(
                [
                    "docker",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--build-arg",
                    f"AGSX_BASE_IMAGE={config['AGSX_BASE_IMAGE']}",
                    "--build-arg",
                    f"IMAGE_CREATED={image_created}",
                    "--build-arg",
                    f"IMAGE_REVISION={source_commit}",
                    "--file",
                    str(dockerfile),
                    "--tag",
                    image,
                    str(build_context),
                ],
                environment=docker_env,
                label=f"building immutable image {image}",
            )
            inspection = run_command(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Architecture}} {{.Os}} {{json .Config.Entrypoint}}",
                    image,
                ],
                environment=docker_env,
                label="validating image architecture and entrypoint",
            )
            validate_built_image(inspection)
            push_output = run_command(
                ["docker", "push", image],
                environment=docker_env,
                label=f"pushing immutable image {image}",
            )

        digest_match = re.search(r"digest:\s*(sha256:[0-9a-fA-F]{64})", push_output)
        if not digest_match:
            raise DeploymentError("docker push output did not contain the immutable image digest")

        # Keep the registry digest for deployment audit records. Tencent Cloud AGSX
        # requires CustomConfiguration.Image to use a tagged image reference and
        # rejects references containing an @sha256 digest.
        image_digest = f"{image}@{digest_match.group(1)}"
        request["CustomConfiguration"]["Image"] = image
        atomic_write_json(request_path, request)
        print(
            f"[AGSX] finalized request with pushed image tag at {request_path}",
            flush=True,
        )

        raw_create = run_command(
            agr_arguments(config, "tool", "create", "--request", f"@{request_path}"),
            environment=cloud_env,
            label="creating the AGSX Tool",
            secret_values=cloud_secret_values,
        )
        create_response = parse_json_output(raw_create, "agr tool create")
        atomic_write_json(output_directory / "tool-create-response.json", create_response)
        tool_id = find_key(create_response, "ToolId")
        if not tool_id:
            raise DeploymentError("agr tool create response did not contain ToolId")
        tool_response = None
        for attempt in range(1, int(config["AGSX_TOOL_POLL_ATTEMPTS"]) + 1):
            raw_tool = run_command(
                agr_arguments(config, "tool", "get", str(tool_id)),
                environment=cloud_env,
                label=f"polling Tool {tool_id} ({attempt}/{config['AGSX_TOOL_POLL_ATTEMPTS']})",
                secret_values=cloud_secret_values,
            )
            tool_response = parse_json_output(raw_tool, "agr tool get")
            atomic_write_json(output_directory / "tool-final-response.json", tool_response)
            status_value = tool_status(tool_response).upper()
            status_reason = tool_status_reason(tool_response)
            status_message = f"[AGSX] Tool {tool_id} status={status_value or 'UNKNOWN'}"
            if status_reason:
                status_message += f" reason={status_reason}"
            print(status_message, flush=True)
            if status_value.endswith("ACTIVE"):
                break
            if any(marker in status_value for marker in ("FAIL", "ERROR", "DELETE")):
                reason_suffix = f": {status_reason}" if status_reason else ""
                raise DeploymentError(
                    f"Tool entered terminal status {status_value}{reason_suffix}"
                )
            time.sleep(int(config["AGSX_TOOL_POLL_SECONDS"]))
        else:
            last_status = tool_status(tool_response) if tool_response is not None else "unknown"
            last_reason = tool_status_reason(tool_response) if tool_response is not None else ""
            reason_suffix = f"; reason={last_reason}" if last_reason else ""
            raise DeploymentError(
                "Tool did not become ACTIVE before the polling limit; "
                f"last status={last_status or 'unknown'}{reason_suffix}"
            )
        assert tool_response is not None

    atomic_write_json(output_directory / "tool-final-response.json", tool_response)
    runtime_environment_path = write_runtime_environment(config)
    print(
        f"[AGSX] runtime-only environment written to {runtime_environment_path}",
        flush=True,
    )
    request_hash = hashlib.sha256(request_path.read_bytes()).hexdigest()
    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "agrCli": {
            "version": find_key(agr_version_response, "Version"),
            "commit": find_key(agr_version_response, "Commit"),
        },
        "sourceCommit": source_commit,
        "toolId": str(tool_id),
        "toolName": config["AGSX_TOOL_NAME"],
        "toolStatus": tool_status(tool_response) or "ACTIVE",
        "reusedExistingTool": reused,
        "region": config["AGSX_REGION"],
        "image": image,
        "imageDigest": image_digest,
        "networkMode": config["AGSX_NETWORK_MODE"],
        "resources": {"cpu": config["AGSX_CPU"], "memory": config["AGSX_MEMORY"]},
        "persistent": False,
        "runtimeEnvironment": str(runtime_environment_path),
        "requestSha256": request_hash,
    }
    result_path = output_directory / "deployment-result.json"
    atomic_write_json(result_path, result)
    print(f"[AGSX] deployment complete; secret-free result written to {result_path}")
    return result_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"protected AGSX deploy.env (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="optional absolute release directory; otherwise AGSX_RELEASE_ROOT is used",
    )
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="AGSX request JSON template")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="validate/render only; make no cloud call")
    modes.add_argument(
        "--check-config",
        action="store_true",
        help="validate deploy.env and confirm required secret fields without showing values",
    )
    parser.add_argument("--confirm-cloud", action="store_true", help="required acknowledgment for cloud mutation")
    return parser


def main() -> int:
    try:
        deploy(build_parser().parse_args())
    except DeploymentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
