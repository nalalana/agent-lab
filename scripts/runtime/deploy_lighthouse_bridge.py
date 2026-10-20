#!/usr/bin/env python3
"""Recovery-only deployer for the retired TSDS Lighthouse bridge.

Phase 4-C removed this script from every active deployment path. It remains for
one release cycle so an approved rollback to the previous backend release can
restore the preserved runtime package. Do not invoke it during a normal deploy.

The script uploads only repository runtime files. It reads the SSH private key
from a local path and never copies that key to the repository or remote host.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REMOTE_DIR = "/opt/tsds/bridge"
DEFAULT_ENV_FILE = "/etc/tsds/shared-host-bridge.env"
DEFAULT_SERVICE_NAME = "tsds-shared-host-bridge.service"
RUNTIME_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _printable_command(command: list[str]) -> str:
    masked: list[str] = []
    hide_next = False
    for part in command:
        if hide_next:
            masked.append("[ssh-key-path]")
            hide_next = False
            continue
        masked.append(part)
        if part in {
            "-i",
            "--private-key",
            "--api-token",
            "--openviking-api-key",
            "--openviking-embedding-api-key",
            "--openviking-rerank-api-key",
        }:
            hide_next = True
    return " ".join(shlex.quote(part) for part in masked)


def _is_transient_ssh_failure(command: list[str], result: subprocess.CompletedProcess[str]) -> bool:
    if not command or command[0] not in {"ssh", "scp"}:
        return False
    output = (result.stdout or "").lower()
    markers = (
        "kex_exchange_identification",
        "banner exchange",
        "connection closed",
        "connection reset",
        "connection timed out",
        "connection refused",
        "broken pipe",
    )
    return result.returncode == 255 and any(marker in output for marker in markers)


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
    retries: int = 1,
    retry_delay: int = 5,
) -> subprocess.CompletedProcess[str]:
    attempts = max(1, retries)
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, attempts + 1):
        print(f"[deploy] {_printable_command(command)}")
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0 or attempt == attempts or not _is_transient_ssh_failure(command, result):
            return result
        print(f"[deploy] transient SSH failure, retrying in {retry_delay}s ({attempt}/{attempts})")
        time.sleep(retry_delay)
    assert result is not None
    return result

def _require_ok(result: subprocess.CompletedProcess[str], step: str) -> None:
    if result.returncode == 0:
        if (result.stdout or "").strip():
            print((result.stdout or "").strip())
        return
    output = (result.stdout or "").strip()
    raise RuntimeError(f"{step} failed with exit code {result.returncode}: {output}")


def _ssh_base(args: argparse.Namespace) -> list[str]:
    options = [
        "-i",
        str(args.private_key),
        "-p",
        str(args.port),
        "-o",
        f"ConnectTimeout={args.connect_timeout}",
    ]
    if not args.strict_host_key:
        options.extend(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"])
    return ["ssh", *options, f"{args.user}@{args.host}"]


def _scp_base(args: argparse.Namespace) -> list[str]:
    options = [
        "-i",
        str(args.private_key),
        "-P",
        str(args.port),
        "-o",
        f"ConnectTimeout={args.connect_timeout}",
    ]
    if not args.strict_host_key:
        options.extend(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"])
    return ["scp", *options]


def _ssh(args: argparse.Namespace, remote_command: str, *, input_text: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return _run(
        [*_ssh_base(args), remote_command],
        input_text=input_text,
        timeout=timeout or args.timeout,
        retries=args.ssh_retries,
        retry_delay=args.ssh_retry_delay,
    )


def _source_files() -> list[tuple[Path, str]]:
    # Memory Gateway modules must be deployed beside the standalone bridge so
    # both direct-script and repository imports remain available.
    return [
        (REPO_ROOT / "scripts" / "runtime" / "shared_host_bridge.py", "shared_host_bridge.py"),
        (REPO_ROOT / "scripts" / "runtime" / "openclaw_tools_2026_4_8.py", "openclaw_tools_2026_4_8.py"),
        (REPO_ROOT / "scripts" / "runtime" / "openviking_client.py", "openviking_client.py"),
        (REPO_ROOT / "scripts" / "runtime" / "memory_gateway.py", "memory_gateway.py"),
        (REPO_ROOT / "scripts" / "runtime" / "memory_inventory.py", "memory_inventory.py"),
        (REPO_ROOT / "scripts" / "runtime" / "openviking_memory_plugin.py", "openviking_memory_plugin.py"),
        (REPO_ROOT / "backend" / "apps" / "runtime" / "security.py", "runtime_security.py"),
        (REPO_ROOT / "backend" / "apps" / "runtime" / "policy.py", "runtime_policy.py"),
        (REPO_ROOT / "backend" / "apps" / "company" / "openclaw_model_catalog.py", "openclaw_model_catalog.py"),
    ]


def _validate_sources(files: Iterable[tuple[Path, str]]) -> None:
    missing = [str(path) for path, _remote_name in files if not path.exists()]
    if missing:
        raise RuntimeError(f"missing deploy source files: {', '.join(missing)}")


def _env_payload(args: argparse.Namespace) -> str:
    token = args.api_token or ""
    lines = [
        f"TSDS_RUNTIME_ROOT={args.runtime_root}",
        f"TSDS_RUNTIME_API_TOKEN={token}",
        f"TSDS_RUNTIME_BRIDGE_HOST={args.listen_host}",
        f"TSDS_RUNTIME_BRIDGE_PORT={args.bridge_port}",
        f"TSDS_OPENCLAW_IMAGE={args.openclaw_image}",
        f"TSDS_WORKSPACE_AGENT_IMAGE={args.workspace_agent_image}",
        f"TSDS_DEBIAN_MIRROR={args.debian_mirror}",
        f"TSDS_DEBIAN_SECURITY_MIRROR={args.debian_security_mirror}",
        f"TSDS_NPM_REGISTRY={args.npm_registry}",
        # OpenViking credentials are written only to the protected remote env file.
        f"OPENVIKING_BASE_URL={args.openviking_base_url}",
        f"OPENVIKING_API_KEY={args.openviking_api_key}",
        f"OPENVIKING_TIMEOUT={args.openviking_timeout}",
        f"TSDS_OPENVIKING_ENABLED={args.openviking_enabled}",
        f"TSDS_OPENVIKING_IMAGE={args.openviking_image}",
        f"TSDS_OPENVIKING_EMBEDDING_API_BASE={args.openviking_embedding_api_base}",
        f"TSDS_OPENVIKING_EMBEDDING_API_KEY={args.openviking_embedding_api_key}",
        f"TSDS_OPENVIKING_EMBEDDING_MODEL={args.openviking_embedding_model}",
        f"TSDS_OPENVIKING_EMBEDDING_DIMENSION={args.openviking_embedding_dimension}",
        f"TSDS_OPENVIKING_VLM_API_BASE={args.openviking_vlm_api_base}",
        f"TSDS_OPENVIKING_VLM_MODEL={args.openviking_vlm_model}",
        f"TSDS_OPENVIKING_QUERY_PLANNER_API_BASE={args.openviking_query_planner_api_base}",
        f"TSDS_OPENVIKING_QUERY_PLANNER_MODEL={args.openviking_query_planner_model}",
        f"TSDS_OPENVIKING_RERANK_API_BASE={args.openviking_rerank_api_base}",
        f"TSDS_OPENVIKING_RERANK_API_KEY={args.openviking_rerank_api_key}",
        f"TSDS_OPENVIKING_RERANK_MODEL={args.openviking_rerank_model}",
        f"TSDS_OPENVIKING_STARTUP_TIMEOUT={args.openviking_startup_timeout}",
    ]
    return "\n".join(lines) + "\n"


def _service_payload(args: argparse.Namespace) -> str:
    return f"""[Unit]
Description=TSDS shared-host OpenClaw bridge
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={args.env_file}
WorkingDirectory={args.remote_dir}
ExecStart=/usr/bin/python3 {args.remote_dir}/shared_host_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _load_company_payload(path_value: str) -> dict[str, object] | None:
    if not path_value:
        return None

    path = Path(path_value).expanduser()
    if not path.is_file():
        raise RuntimeError(f"company deploy payload not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid company deploy payload: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("company deploy payload must be a JSON object")

    company_id = str(payload.get("company_id") or "").strip()
    if not company_id:
        raise RuntimeError("company deploy payload requires company_id")
    namespace = str(payload.get("runtime_namespace") or f"tsds-{company_id[:12]}").strip()
    if not RUNTIME_NAMESPACE_PATTERN.fullmatch(namespace):
        raise RuntimeError("company deploy payload has an invalid runtime_namespace")
    config_payload = payload.get("config_payload", "")
    if isinstance(config_payload, dict):
        config_payload = json.dumps(config_payload, ensure_ascii=False, separators=(",", ":"))
    if not isinstance(config_payload, str) or not config_payload.strip():
        raise RuntimeError("company deploy payload requires a non-empty config_payload")
    try:
        parsed_config = json.loads(config_payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("company deploy config_payload must be valid JSON") from exc
    if not isinstance(parsed_config, dict):
        raise RuntimeError("company deploy config_payload must encode a JSON object")
    provider_env = payload.get("provider_env", {})
    if not isinstance(provider_env, dict):
        raise RuntimeError("company deploy provider_env must be a JSON object")

    payload["company_id"] = company_id
    payload["runtime_namespace"] = namespace
    payload["config_payload"] = config_payload
    payload["provider_env"] = provider_env
    return payload


def _company_deploy_program(args: argparse.Namespace, payload: dict[str, object]) -> str:
    """Build a stdin-only remote program so credentials never enter argv/logs."""

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f'''import json
import time
import urllib.error
import urllib.request

deployment = json.loads({payload_json!r})
env_file = {args.env_file!r}
bridge_port = {args.bridge_port!r}
health_timeout = {args.company_health_timeout!r}

environment = {{}}
with open(env_file, "r", encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.rstrip("\\n")
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        environment[key] = value

headers = {{"Content-Type": "application/json"}}
token = environment.get("TSDS_RUNTIME_API_TOKEN", "")
if token:
    headers["Authorization"] = "Bearer " + token

namespace = deployment["runtime_namespace"]
base_url = f"http://127.0.0.1:{{bridge_port}}/runtime/{{namespace}}"

def request(method, suffix, body=None, timeout=60):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url + "/" + suffix, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"{{suffix}} returned HTTP {{exc.code}}: {{detail}}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{{suffix}} returned a non-object response")
    return result

gateway_port = int(deployment.get("gateway_port") or 18789)
provision = request("POST", "provision", {{
    "company_id": deployment["company_id"],
    "runtime_namespace": namespace,
    "gateway_port": gateway_port,
    "config_path": deployment.get("config_path", ""),
    "workspace_prefix": deployment.get("workspace_prefix", ""),
    "provider_env": deployment.get("provider_env", {{}}),
}}, timeout=60)
bootstrap = request("POST", "bootstrap", {{
    "company_id": deployment["company_id"],
    "gateway_port": gateway_port,
    "current_publish_version": deployment.get("current_publish_version", ""),
    "config_payload": deployment["config_payload"],
    "provider_env": deployment.get("provider_env", {{}}),
}}, timeout={args.company_bootstrap_timeout!r})

deadline = time.monotonic() + health_timeout
health = {{}}
while time.monotonic() < deadline:
    health = request("GET", "health", timeout=30)
    if health.get("status") == "healthy":
        break
    time.sleep(3)
else:
    raise RuntimeError("company runtime did not become healthy: " + json.dumps(health, ensure_ascii=False))

print(json.dumps({{
    "company_id": deployment["company_id"],
    "runtime_namespace": namespace,
    "gateway_endpoint": provision.get("gateway_endpoint", ""),
    "container_name": bootstrap.get("container_name", ""),
    "workspace_agent_container_name": bootstrap.get("workspace_agent_container_name", ""),
    "health": health.get("status", "unknown"),
}}, ensure_ascii=False))
'''


def _deploy_company(args: argparse.Namespace, payload: dict[str, object]) -> None:
    result = _ssh(
        args,
        "sudo /usr/bin/python3 -",
        input_text=_company_deploy_program(args, payload),
        timeout=args.company_bootstrap_timeout + args.company_health_timeout + 60,
    )
    _require_ok(result, "provision and bootstrap company runtime")


def deploy(args: argparse.Namespace) -> dict[str, object]:
    private_key = Path(args.private_key).expanduser()
    if not private_key.exists():
        raise RuntimeError(f"SSH private key not found: {private_key}")
    args.private_key = private_key

    files = _source_files()
    _validate_sources(files)
    company_payload = _load_company_payload(args.company_payload_file)

    remote_tmp = f"/tmp/tsds-bridge-deploy-{os.getpid()}"
    _require_ok(_ssh(args, f"mkdir -p {shlex.quote(remote_tmp)}", timeout=args.connect_timeout + 20), "create remote temp dir")

    try:
        for local_path, remote_name in files:
            result = _run(
                [
                    *_scp_base(args),
                    str(local_path),
                    f"{args.user}@{args.host}:{remote_tmp}/{remote_name}",
                ],
                timeout=args.timeout,
                retries=args.ssh_retries,
                retry_delay=args.ssh_retry_delay,
            )
            _require_ok(result, f"upload {remote_name}")

        if args.install_docker:
            docker_install = (
                "set -eu; "
                "if ! command -v docker >/dev/null 2>&1; then "
                "sudo apt-get update; "
                "sudo apt-get install -y docker.io; "
                "fi; "
                "if ! docker compose version >/dev/null 2>&1; then "
                "sudo apt-get update; "
                "if apt-cache show docker-compose-plugin >/dev/null 2>&1; then "
                "sudo apt-get install -y docker-compose-plugin; "
                "elif apt-cache show docker-compose-v2 >/dev/null 2>&1; then "
                "sudo apt-get install -y docker-compose-v2; "
                "else "
                "echo 'docker compose plugin package not found' >&2; "
                "exit 1; "
                "fi; "
                "fi; "
                "sudo systemctl enable --now docker >/dev/null 2>&1 || true; "
                "docker compose version"
            )
            _require_ok(_ssh(args, docker_install, timeout=args.timeout), "install docker")
        install_command = " && ".join(
            [
                f"sudo mkdir -p {shlex.quote(args.remote_dir)} /etc/tsds",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/shared_host_bridge.py {shlex.quote(args.remote_dir)}/shared_host_bridge.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/openclaw_tools_2026_4_8.py {shlex.quote(args.remote_dir)}/openclaw_tools_2026_4_8.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/openviking_client.py {shlex.quote(args.remote_dir)}/openviking_client.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/memory_gateway.py {shlex.quote(args.remote_dir)}/memory_gateway.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/memory_inventory.py {shlex.quote(args.remote_dir)}/memory_inventory.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/openviking_memory_plugin.py {shlex.quote(args.remote_dir)}/openviking_memory_plugin.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/runtime_security.py {shlex.quote(args.remote_dir)}/runtime_security.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/runtime_policy.py {shlex.quote(args.remote_dir)}/runtime_policy.py",
                f"sudo install -m 0644 {shlex.quote(remote_tmp)}/openclaw_model_catalog.py {shlex.quote(args.remote_dir)}/openclaw_model_catalog.py",
            ]
        )
        _require_ok(_ssh(args, install_command, timeout=args.timeout), "install bridge files")

        _require_ok(
            _ssh(args, f"sudo tee {shlex.quote(args.env_file)} >/dev/null && sudo chmod 600 {shlex.quote(args.env_file)}", input_text=_env_payload(args)),
            "write bridge env",
        )
        _require_ok(
            _ssh(args, f"sudo tee /etc/systemd/system/{shlex.quote(args.service_name)} >/dev/null", input_text=_service_payload(args)),
            "write systemd unit",
        )
        _require_ok(
            _ssh(args, f"sudo systemctl daemon-reload && sudo systemctl enable --now {shlex.quote(args.service_name)} && sudo systemctl restart {shlex.quote(args.service_name)}", timeout=args.timeout),
            "restart bridge service",
        )
        _require_ok(
            _ssh(args, f"systemctl is-active {shlex.quote(args.service_name)} && ss -ltn | grep ':{args.bridge_port} '", timeout=60),
            "verify bridge service",
        )
        if company_payload is not None:
            _deploy_company(args, company_payload)
        return {
            "host": args.host,
            "user": args.user,
            "port": args.port,
            "remote_dir": args.remote_dir,
            "env_file": args.env_file,
            "service_name": args.service_name,
            "bridge_port": args.bridge_port,
            "api_base": f"http://{args.host}:{args.bridge_port}",
            "uploaded_files": [remote_name for _local_path, remote_name in files],
            "company_runtime": (
                {
                    "company_id": company_payload["company_id"],
                    "runtime_namespace": company_payload["runtime_namespace"],
                    "status": "healthy",
                }
                if company_payload is not None
                else None
            ),
        }
    finally:
        _ssh(args, f"rm -rf {shlex.quote(remote_tmp)}", timeout=60)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy TSDS shared-host bridge to a Lighthouse host")
    parser.add_argument("--host", default=os.getenv("TENCENT_LIGHTHOUSE_PUBLIC_IP", ""), help="Lighthouse public IP")
    parser.add_argument("--user", default=os.getenv("TENCENT_LIGHTHOUSE_SSH_USER", "ubuntu"), help="SSH user")
    parser.add_argument("--port", type=int, default=int(os.getenv("TENCENT_LIGHTHOUSE_SSH_PORT", "22")), help="SSH port")
    parser.add_argument("--private-key", default=os.getenv("TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH", ""), help="SSH private key path")
    parser.add_argument("--api-token", default=os.getenv("RUNTIME_DOCKER_SHARED_HOST_API_TOKEN", ""), help="Bridge bearer token")
    parser.add_argument("--bridge-port", type=int, default=int(os.getenv("TSDS_RUNTIME_BRIDGE_PORT", "19080")), help="Bridge listen port")
    parser.add_argument("--listen-host", default=os.getenv("TSDS_RUNTIME_BRIDGE_HOST", "0.0.0.0"), help="Bridge listen host")
    parser.add_argument("--runtime-root", default=os.getenv("TSDS_RUNTIME_ROOT", "/data/tsds/tenants"), help="Tenant runtime root")
    parser.add_argument("--openclaw-image", default=os.getenv("TSDS_OPENCLAW_IMAGE", "node:22-bookworm-slim"), help="Base image used to build the OpenClaw gateway")
    parser.add_argument("--workspace-agent-image", default=os.getenv("TSDS_WORKSPACE_AGENT_IMAGE", os.getenv("TSDS_OPENCLAW_IMAGE", "node:22-bookworm-slim")), help="Image used by the workspace agent service")
    parser.add_argument("--debian-mirror", default=os.getenv("TSDS_DEBIAN_MIRROR", ""), help="Optional Debian package mirror used inside the gateway image build")
    parser.add_argument("--debian-security-mirror", default=os.getenv("TSDS_DEBIAN_SECURITY_MIRROR", ""), help="Optional Debian security package mirror used inside the gateway image build")
    parser.add_argument("--npm-registry", default=os.getenv("TSDS_NPM_REGISTRY", ""), help="Optional npm registry used inside the gateway image build")
    parser.add_argument("--openviking-base-url", default=os.getenv("OPENVIKING_BASE_URL", ""), help="OpenViking server URL reachable from the bridge host")
    parser.add_argument("--openviking-api-key", default=os.getenv("OPENVIKING_API_KEY", ""), help="Trusted OpenViking gateway key")
    parser.add_argument("--openviking-timeout", type=int, default=int(os.getenv("OPENVIKING_TIMEOUT", "30")), help="OpenViking request timeout")
    parser.add_argument("--openviking-enabled", default=os.getenv("TSDS_OPENVIKING_ENABLED", "0"), help="Enable one isolated OpenViking service per company runtime")
    parser.add_argument("--openviking-image", default=os.getenv("TSDS_OPENVIKING_IMAGE", "ghcr.io/volcengine/openviking:v0.4.12"), help="OpenViking container image")
    parser.add_argument("--openviking-embedding-api-base", default=os.getenv("TSDS_OPENVIKING_EMBEDDING_API_BASE", "http://112.95.75.67:9014/v1"), help="Embedding API base used by tenant OpenViking")
    parser.add_argument("--openviking-embedding-api-key", default=os.getenv("TSDS_OPENVIKING_EMBEDDING_API_KEY", ""), help="Embedding API key used by tenant OpenViking")
    parser.add_argument("--openviking-embedding-model", default=os.getenv("TSDS_OPENVIKING_EMBEDDING_MODEL", "qwen3-embedding-8b"), help="Embedding model used by tenant OpenViking")
    parser.add_argument("--openviking-embedding-dimension", type=int, default=int(os.getenv("TSDS_OPENVIKING_EMBEDDING_DIMENSION", "4096")), help="Embedding vector dimension")
    parser.add_argument("--openviking-vlm-api-base", default=os.getenv("TSDS_OPENVIKING_VLM_API_BASE", "https://api.minimaxi.com/v1"), help="VLM API base used by tenant OpenViking")
    parser.add_argument("--openviking-vlm-model", default=os.getenv("TSDS_OPENVIKING_VLM_MODEL", "MiniMax-M2.7"), help="VLM model used by tenant OpenViking")
    parser.add_argument("--openviking-query-planner-api-base", default=os.getenv("TSDS_OPENVIKING_QUERY_PLANNER_API_BASE", "http://112.95.75.67:9016"), help="Query planner API base")
    parser.add_argument("--openviking-query-planner-model", default=os.getenv("TSDS_OPENVIKING_QUERY_PLANNER_MODEL", "ollama/guoxuter/ov_intent_analysis_sft:v7_q8"), help="Query planner model")
    parser.add_argument("--openviking-rerank-api-base", default=os.getenv("TSDS_OPENVIKING_RERANK_API_BASE", "http://112.95.75.67:9017/rerank"), help="Rerank API base used by tenant OpenViking")
    parser.add_argument("--openviking-rerank-api-key", default=os.getenv("TSDS_OPENVIKING_RERANK_API_KEY", ""), help="Rerank API key used by tenant OpenViking")
    parser.add_argument("--openviking-rerank-model", default=os.getenv("TSDS_OPENVIKING_RERANK_MODEL", "qwen3-reranker-4b"), help="Rerank model used by tenant OpenViking")
    parser.add_argument("--openviking-startup-timeout", type=int, default=int(os.getenv("TSDS_OPENVIKING_STARTUP_TIMEOUT", "180")), help="Seconds to wait for tenant OpenViking health")
    parser.add_argument("--company-payload-file", default=os.getenv("TSDS_COMPANY_DEPLOY_PAYLOAD_FILE", ""), help="Optional JSON payload; when set, provision and bootstrap the company after bridge deployment")
    parser.add_argument("--company-bootstrap-timeout", type=int, default=int(os.getenv("TSDS_COMPANY_BOOTSTRAP_TIMEOUT", "1800")), help="Maximum seconds for the company OpenClaw bootstrap request")
    parser.add_argument("--company-health-timeout", type=int, default=int(os.getenv("TSDS_COMPANY_HEALTH_TIMEOUT", "180")), help="Maximum seconds to wait for gateway and workspace-agent health")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR, help="Remote bridge directory")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="Remote env file")
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME, help="systemd service name")
    parser.add_argument("--connect-timeout", type=int, default=int(os.getenv("TENCENT_LIGHTHOUSE_SSH_CONNECT_TIMEOUT", "20")), help="SSH connect timeout")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TENCENT_LIGHTHOUSE_BOOTSTRAP_TIMEOUT", "600")), help="Deploy step timeout")
    parser.add_argument("--ssh-retries", type=int, default=int(os.getenv("TENCENT_LIGHTHOUSE_SSH_RETRIES", "3")), help="SSH/SCP retries for transient connection failures")
    parser.add_argument("--ssh-retry-delay", type=int, default=int(os.getenv("TENCENT_LIGHTHOUSE_SSH_RETRY_DELAY", "5")), help="Seconds between transient SSH/SCP retries")
    parser.add_argument("--strict-host-key", action="store_true", default=os.getenv("TENCENT_LIGHTHOUSE_SSH_STRICT_HOST_KEY", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--install-docker", action="store_true", help="Install docker.io when Docker is missing")
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host or TENCENT_LIGHTHOUSE_PUBLIC_IP is required")
    if not args.private_key:
        parser.error("--private-key or TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH is required")
    return args


def main(argv: list[str]) -> int:
    try:
        result = deploy(parse_args(argv))
    except Exception as exc:
        print(f"[deploy] failed: {exc}", file=sys.stderr)
        return 1
    print("[deploy] success")
    for key, value in result.items():
        if key == "api_token":
            continue
        print(f"[deploy] {key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
