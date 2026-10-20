"""One-off, explicit AGSX/COS cloud validation for Phase 0."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from io import BytesIO
from typing import Any

TIMEOUT_SECONDS = 300
EXPECTED_CPU_CORES = 2
EXPECTED_MEMORY_MIB = 2048
TTL_TOLERANCE_SECONDS = 15
NETWORK_PROBE_TIMEOUT_SECONDS = 3
EXECUTION_TIMEOUT_SECONDS = 1
PREFIX_ROOT = "touming-test/"


class ValidationFailure(RuntimeError):
    """Raised when an explicit Phase 0 cloud assertion is not satisfied."""


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValidationFailure(f"Missing required environment variable: {name}")
    return value


def safe_error(error: Exception) -> str:
    message = str(error)
    for name in ("E2B_API_KEY", "AGSX_COS_SECRET_ID", "AGSX_COS_SECRET_KEY"):
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return f"{type(error).__name__}: {message[:240]}"


def test_cos_prefix(report: dict[str, Any], *, allow_bucket_root_list: bool) -> None:
    from qcloud_cos import CosConfig, CosS3Client

    bucket = required("AGSX_TEST_COS_BUCKET")
    region = required("AGSX_TEST_COS_REGION")
    prefix = required("AGSX_TEST_COS_PREFIX").lstrip("/")
    if not prefix.startswith(PREFIX_ROOT):
        raise ValidationFailure(f"AGSX_TEST_COS_PREFIX must begin with {PREFIX_ROOT}")
    if not prefix.endswith("/"):
        prefix += "/"

    client = CosS3Client(CosConfig(
        Region=region,
        SecretId=required("AGSX_COS_SECRET_ID"),
        SecretKey=required("AGSX_COS_SECRET_KEY"),
        Scheme="https",
    ))
    key = f"{prefix}phase0-{uuid.uuid4().hex}/input.txt"
    payload = f"touming phase0 {uuid.uuid4()}\n".encode()
    payload_hash = hashlib.sha256(payload).hexdigest()

    try:
        client.put_object(Bucket=bucket, Key=key, Body=BytesIO(payload))
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head.get("Content-Length", -1)) != len(payload):
            raise ValidationFailure("COS object size differs from upload")

        response = client.get_object(Bucket=bucket, Key=key)
        downloaded = response["Body"].get_raw_stream().read()
        if hashlib.sha256(downloaded).hexdigest() != payload_hash:
            raise ValidationFailure("COS downloaded payload differs from upload")

        client.list_objects(Bucket=bucket, Prefix=prefix)
        try:
            client.list_objects(Bucket=bucket, Prefix="")
        except Exception:
            report["cos_outside_prefix_blocked"] = True
        else:
            if not allow_bucket_root_list:
                raise ValidationFailure("COS credential can list bucket root")
            report["test_bucket_root_list_allowed"] = True
        report["cos_prefix_round_trip"] = True
    finally:
        try:
            client.delete_object(Bucket=bucket, Key=key)
            report["cos_test_object_deleted"] = True
        except Exception:
            report["cos_test_object_deleted"] = False
            raise


def test_sandbox_specification(sandbox: Any, report: dict[str, Any]) -> None:
    """Validate the effective control-plane limits, not just requested values."""
    info = sandbox.get_info()
    observed_ttl_seconds = (info.end_at - info.started_at).total_seconds()

    # State and resource values are safe operational evidence; no sandbox ID is reported.
    report["sandbox_control_plane_state"] = str(info.state)
    report["sandbox_cpu_cores"] = info.cpu_count
    report["sandbox_memory_mib"] = info.memory_mb
    report["sandbox_observed_ttl_seconds"] = round(observed_ttl_seconds, 3)
    report["sandbox_public_egress_disabled"] = info.allow_internet_access is False

    if info.cpu_count != EXPECTED_CPU_CORES:
        raise ValidationFailure(
            f"Sandbox CPU count is {info.cpu_count}, expected {EXPECTED_CPU_CORES}"
        )
    if info.memory_mb != EXPECTED_MEMORY_MIB:
        raise ValidationFailure(
            f"Sandbox memory is {info.memory_mb} MiB, expected {EXPECTED_MEMORY_MIB}"
        )
    if abs(observed_ttl_seconds - TIMEOUT_SECONDS) > TTL_TOLERANCE_SECONDS:
        raise ValidationFailure("Sandbox control-plane TTL differs from the requested 300 seconds")
    if info.allow_internet_access is not False:
        raise ValidationFailure("Sandbox control-plane network policy does not deny public egress")

    report["sandbox_ttl_verified"] = True
    report["sandbox_specification_verified"] = True


def test_isolated_network(sandbox: Any, report: dict[str, Any]) -> None:
    """Prove that a harmless outbound TCP probe is rejected inside the sandbox."""
    probe = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('1.1.1.1', 443), timeout={NETWORK_PROBE_TIMEOUT_SECONDS})\n"
        "except OSError:\n"
        "    print('egress-blocked')\n"
        "else:\n"
        "    raise RuntimeError('public egress unexpectedly available')\n"
    )
    result = sandbox.run_code(probe, timeout=10, request_timeout=15)
    if getattr(result, "error", None):
        raise ValidationFailure("Sandbox network probe returned an execution error")
    if "egress-blocked" not in "".join(getattr(result.logs, "stdout", ())):
        raise ValidationFailure("Sandbox public egress probe was not blocked")
    report["sandbox_isolated_network_verified"] = True


def test_execution_timeout(sandbox: Any, report: dict[str, Any]) -> None:
    """Record the SDK behavior when code exceeds an explicit execution timeout."""
    try:
        result = sandbox.run_code(
            "import time\ntime.sleep(2)",
            timeout=EXECUTION_TIMEOUT_SECONDS,
            request_timeout=10,
        )
    except Exception as error:
        observed_timeout = "timeout" in str(error).lower() or "timed out" in str(error).lower()
    else:
        observed_timeout = bool(
            getattr(result, "error", None)
            and "timeout" in str(result.error).lower()
        )

    if not observed_timeout:
        raise ValidationFailure("Sandbox execution timeout was not observed")
    report["sandbox_execution_timeout_verified"] = True


def test_sandbox(
    report: dict[str, Any],
    *,
    verify_specification: bool,
    verify_isolated_network: bool,
    verify_execution_timeout: bool,
) -> None:
    from e2b_code_interpreter import Sandbox

    template = (
        os.getenv("AGSX_TEMPLATE", "").strip()
        or os.getenv("AGS_TEMPLATE", "").strip()
        or required("AGSX_TOOL_NAME")
    )
    workdir = f"/tmp/touming-phase0-{uuid.uuid4().hex}"
    input_path = f"{workdir}/input.txt"
    output_path = f"{workdir}/output.txt"
    source = f"touming phase0 input {uuid.uuid4()}\n"
    expected = hashlib.sha256(source.encode()).hexdigest()
    sandbox = None

    try:
        # Never inherit backend networking: P0 requires effective public-egress denial.
        sandbox = Sandbox.create(
            template=template,
            timeout=TIMEOUT_SECONDS,
            allow_internet_access=False,
        )
        report["sandbox_created"] = True
        report["sandbox_timeout_seconds"] = TIMEOUT_SECONDS

        if verify_specification:
            test_sandbox_specification(sandbox, report)

        command = sandbox.commands.run("python -V")
        if "Python " not in str(getattr(command, "stdout", "") or ""):
            raise ValidationFailure("Sandbox command execution did not return Python version")
        report["sandbox_python_command"] = True

        sandbox.files.write(input_path, source)
        report["sandbox_file_uploaded"] = True
        code = (
            "from hashlib import sha256\n"
            "from pathlib import Path\n"
            f"value = Path({input_path!r}).read_text()\n"
            f"Path({output_path!r}).parent.mkdir(parents=True, exist_ok=True)\n"
            f"Path({output_path!r}).write_text('sha256=' + sha256(value.encode()).hexdigest() + '\\n')\n"
        )
        result = sandbox.run_code(code)
        if getattr(result, "error", None):
            raise ValidationFailure("Sandbox Python transformation returned an error")
        report["sandbox_python_execution"] = True

        if sandbox.files.read(output_path) != f"sha256={expected}\n":
            raise ValidationFailure("Sandbox downloaded file differs from expected output")
        report["sandbox_file_downloaded"] = True

        if verify_isolated_network:
            test_isolated_network(sandbox, report)
        if verify_execution_timeout:
            test_execution_timeout(sandbox, report)
    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
                report["sandbox_terminated"] = True
            except Exception:
                report["sandbox_terminated"] = False
                raise


def run(
    *,
    allow_test_bucket_root_list: bool = False,
    verify_specification: bool = False,
    verify_isolated_network: bool = False,
    verify_execution_timeout: bool = False,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "cloud_validation": "phase0",
        "started_at_epoch": int(time.time()),
        "sandbox_created": False,
        "sandbox_terminated": False,
        "sandbox_specification_verified": False,
        "sandbox_isolated_network_verified": False,
        "sandbox_execution_timeout_verified": False,
        "cos_prefix_round_trip": False,
        "cos_outside_prefix_blocked": False,
        "test_bucket_root_list_allowed": False,
    }
    try:
        domain = required("E2B_DOMAIN")
        if "://" in domain or "/" in domain:
            raise ValidationFailure("E2B_DOMAIN must contain only a domain name")
        required("E2B_API_KEY")
        test_cos_prefix(report, allow_bucket_root_list=allow_test_bucket_root_list)
        test_sandbox(
            report,
            verify_specification=verify_specification,
            verify_isolated_network=verify_isolated_network,
            verify_execution_timeout=verify_execution_timeout,
        )
        report["status"] = "passed"
        return 0, report
    except Exception as error:
        report["status"] = "failed"
        report["error"] = safe_error(error)
        return 1, report
    finally:
        report["finished_at_epoch"] = int(time.time())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-cloud", action="store_true")
    parser.add_argument(
        "--allow-test-bucket-root-list",
        action="store_true",
        help="Permit root listing only for a disposable, isolated test bucket.",
    )
    parser.add_argument(
        "--verify-specification",
        action="store_true",
        help="Require 2C2G, 300-second TTL, and denied public-egress control-plane evidence.",
    )
    parser.add_argument(
        "--verify-isolated-network",
        action="store_true",
        help="Run a harmless outbound TCP probe that must be rejected by the sandbox.",
    )
    parser.add_argument(
        "--verify-execution-timeout",
        action="store_true",
        help="Require a short Python execution to exceed its explicit timeout.",
    )
    args = parser.parse_args(argv)
    if not args.confirm_cloud:
        parser.error("--confirm-cloud is required to create cloud resources")
    code, report = run(
        allow_test_bucket_root_list=args.allow_test_bucket_root_list,
        verify_specification=args.verify_specification,
        verify_isolated_network=args.verify_isolated_network,
        verify_execution_timeout=args.verify_execution_timeout,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())

