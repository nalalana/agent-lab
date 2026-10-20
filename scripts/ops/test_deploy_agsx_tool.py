from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("deploy_agsx_tool.py")
SPEC = importlib.util.spec_from_file_location("deploy_agsx_tool", MODULE_PATH)
assert SPEC and SPEC.loader
deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy
SPEC.loader.exec_module(deploy)


class DeployAgsxToolTests(unittest.TestCase):
    def test_default_config_is_outside_the_repository(self) -> None:
        self.assertEqual(deploy.DEFAULT_CONFIG, Path("/etc/touming/agsx/deploy.env"))

    def test_tracked_example_declares_every_required_key(self) -> None:
        values = deploy.parse_key_value_file(
            deploy.REPO_ROOT / "config/agsx/deploy.env.example"
        )
        required = (
            set(deploy.REQUIRED_CONFIG_KEYS)
            | set(deploy.CLOUD_SECRET_NAMES)
            | set(deploy.REGISTRY_SECRET_NAMES)
            | set(deploy.ARTIFACT_CONFIG_KEYS)
        )
        self.assertEqual(sorted(required - set(values)), [])

    def valid_config(self) -> dict[str, str]:
        return {
            "AGSX_REGION": "ap-guangzhou",
            "AGSX_CLI_DOMAIN": "tencentags.com",
            "AGSX_TOOL_NAME": "touming-code-v1-20260819",
            "AGSX_TOOL_DESCRIPTION": "Production sandbox",
            "AGSX_CLIENT_TOKEN": "phase4c-20260819-deploy-001",
            "AGSX_ROLE_ARN": "qcs::cam::uin/100000000001:roleName/agsx-runtime",
            "AGSX_IMAGE_REGISTRY_TYPE": "personal",
            "AGSX_IMAGE_REGISTRY_HOST": "ccr.ccs.tencentyun.com",
            "AGSX_IMAGE_REPOSITORY": "touming/touming-code",
            "AGSX_IMAGE_TAG": "20260819-abcdef1",
            "AGSX_REGISTRY_USERNAME": "100000000001",
            "AGSX_REGISTRY_PASSWORD": "registry-secret-for-test",
            "AGSX_BASE_IMAGE": "registry/base@sha256:" + "a" * 64,
            "AGSX_DOCKERFILE": "config/agsx/Dockerfile",
            "AGSX_BUILD_CONTEXT": "config/agsx",
            "AGSX_DEFAULT_TIMEOUT": "5m",
            "AGSX_CPU": "2",
            "AGSX_MEMORY": "2Gi",
            "AGSX_NETWORK_MODE": "SANDBOX",
            "AGSX_PERSISTENT": "false",
            "AGSX_ENVIRONMENT": "production",
            "AGSX_OWNER": "agent-platform",
            "AGSX_TOOL_POLL_SECONDS": "1",
            "AGSX_TOOL_POLL_ATTEMPTS": "2",
            "AGSX_RELEASE_ROOT": "/var/lib/touming/agsx/releases",
            "AGSX_RUNTIME_ENV_PATH": "/etc/touming/agsx/runtime.env",
            "TENCENTCLOUD_SECRET_ID": "AKID-test-only",
            "TENCENTCLOUD_SECRET_KEY": "tencent-secret-for-test",
            "AGSX_ENABLED": "true",
            "E2B_DOMAIN": "agsx.example.internal",
            "E2B_API_KEY": "e2b-test-only",
            "AGSX_TEMPLATE": "touming-code-v1-20260819",
            "AGSX_DEFAULT_CPU_CORES": "2",
            "AGSX_DEFAULT_MEMORY_MIB": "2048",
            "AGSX_DEFAULT_TTL_SECONDS": "300",
            "AGSX_NETWORK_POLICY": "isolated",
            "AGSX_REQUEST_TIMEOUT_SECONDS": "60",
            "AGSX_SANDBOX_CLEANUP_GRACE_SECONDS": "120",
            "AGSX_REAPER_BATCH_SIZE": "100",
            "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES": "25",
            "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR": "true",
            "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G": "10",
            "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS": "1000000",
            "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS": "10000000",
            "AGSX_COST_CURRENCY": "CNY",
            "AGSX_ENFORCE_EFFECTIVE_RESOURCES": "true",
            "AGSX_ALERT_FAILURE_WINDOW_MINUTES": "15",
            "AGSX_ALERT_FAILURE_RATE": "0.2",
            "AGSX_ALERT_FAILURE_MIN_CREATES": "5",
            "AGENT_RUNTIME_ARTIFACT_COS_BUCKET": "touming-artifacts-100000000001",
            "AGENT_RUNTIME_ARTIFACT_COS_REGION": "ap-guangzhou",
            "AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID": "AKID-artifact-test-only",
            "AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY": "artifact-secret-for-test",
            "AGENT_RUNTIME_ARTIFACT_COS_PREFIX": "agent-runtime/v1",
        }

    @staticmethod
    def write_config(path: Path, values: dict[str, str], mode: int = 0o600) -> None:
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
        path.chmod(mode)

    def test_parser_does_not_execute_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            config = Path(directory) / "deploy.env"
            config.write_text(f"AGSX_OWNER=$(touch {marker})\n", encoding="utf-8")
            values = deploy.parse_key_value_file(config)
            self.assertEqual(values["AGSX_OWNER"], f"$(touch {marker})")
            self.assertFalse(marker.exists())

    def test_load_configuration_uses_only_protected_deploy_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "deploy.env"
            self.write_config(config_path, self.valid_config())
            with patch.dict(os.environ, {"AGSX_CPU": "99"}, clear=False):
                loaded = deploy.load_configuration(config_path)
            self.assertEqual(loaded["AGSX_CPU"], "2")
            self.assertEqual(loaded["TENCENTCLOUD_SECRET_ID"], "AKID-test-only")

    def test_load_configuration_rejects_phase0_smoke_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "deploy.env"
            values = self.valid_config()
            values["AGSX_TEST_COS_BUCKET"] = "phase0-smoke-only"
            self.write_config(config_path, values)

            with self.assertRaisesRegex(deploy.DeploymentError, "unknown AGSX configuration"):
                deploy.load_configuration(config_path)

    def test_world_readable_deploy_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "deploy.env"
            self.write_config(config_path, self.valid_config(), mode=0o644)
            with self.assertRaises(deploy.DeploymentError):
                deploy.load_configuration(config_path)

    def test_symlink_deploy_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "real-deploy.env"
            link = Path(directory) / "deploy.env"
            self.write_config(target, self.valid_config())
            link.symlink_to(target)
            with self.assertRaises(deploy.DeploymentError):
                deploy.load_configuration(link)

    def test_secrets_are_loaded_from_deploy_config(self) -> None:
        secrets = deploy.load_secrets(self.valid_config())
        self.assertEqual(secrets["TENCENTCLOUD_SECRET_KEY"].value, "tencent-secret-for-test")
        self.assertEqual(secrets["AGSX_REGISTRY_PASSWORD"].source, "protected deploy.env")

    def test_rendered_request_contains_no_secret_or_placeholder(self) -> None:
        request = deploy.render_request(self.valid_config(), deploy.DEFAULT_TEMPLATE)
        serialized = json.dumps(request)
        self.assertNotIn("tencent-secret-for-test", serialized)
        self.assertNotIn("registry-secret-for-test", serialized)
        self.assertNotIn("<", serialized)
        self.assertEqual(request["NetworkConfiguration"]["NetworkMode"], "SANDBOX")
        self.assertEqual(request["Description"], "Production sandbox")
        self.assertEqual(request["CustomConfiguration"]["Command"], ["/init"])
        self.assertEqual(request["CustomConfiguration"]["Args"], ["sleep", "infinity"])
        self.assertEqual(request["CustomConfiguration"]["Resources"], {"CPU": "2", "Memory": "2Gi"})
        self.assertFalse(request["Persistent"])

        image = request["CustomConfiguration"]["Image"]
        self.assertEqual(image, deploy.image_reference(self.valid_config()))
        self.assertNotIn("@sha256:", image)

        http_get = request["CustomConfiguration"]["Probe"]["HttpGet"]
        self.assertEqual(http_get["Path"], "/health")
        self.assertEqual(http_get["Port"], 49983)
        self.assertEqual(http_get["Scheme"], "HTTP")


    def test_runtime_environment_contains_only_backend_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.valid_config()
            runtime_path = Path(directory) / "runtime.env"
            config["AGSX_RUNTIME_ENV_PATH"] = str(runtime_path)
            result = deploy.write_runtime_environment(config)
            payload = result.read_text(encoding="utf-8")
            self.assertIn('AGSX_ENABLED="true"', payload)
            self.assertIn('E2B_API_KEY="e2b-test-only"', payload)
            self.assertNotIn("TENCENTCLOUD_SECRET_ID", payload)
            self.assertNotIn("AGSX_REGISTRY_PASSWORD", payload)
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)

    def test_dry_run_entrypoint_writes_only_secret_free_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "deploy.env"
            output_path = root / "release"
            self.write_config(config_path, self.valid_config())
            result = deploy.deploy(
                SimpleNamespace(
                    config=config_path,
                    template=deploy.DEFAULT_TEMPLATE,
                    output_dir=output_path,
                    dry_run=True,
                    check_config=False,
                    confirm_cloud=False,
                )
            )
            payload = result.read_text(encoding="utf-8")
            self.assertNotIn("tencent-secret-for-test", payload)
            self.assertNotIn("registry-secret-for-test", payload)
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)

    def test_configuration_rejects_mutable_image_inputs(self) -> None:
        config = self.valid_config()
        config["AGSX_IMAGE_TAG"] = "latest"
        with self.assertRaises(deploy.DeploymentError):
            deploy.validate_configuration(config)
        config = self.valid_config()
        config["AGSX_BASE_IMAGE"] = "registry/base:latest"
        with self.assertRaises(deploy.DeploymentError):
            deploy.validate_configuration(config)

    def test_configuration_rejects_phase4c_invariant_changes(self) -> None:
        for key, value in (
            ("AGSX_CPU", "1"),
            ("AGSX_MEMORY", "1Gi"),
            ("AGSX_NETWORK_MODE", "PUBLIC"),
            ("AGSX_PERSISTENT", "true"),
            ("AGSX_DEFAULT_TIMEOUT", "10m"),
            ("AGSX_DEFAULT_MEMORY_MIB", "1024"),
            ("AGSX_NETWORK_POLICY", "public"),
        ):
            with self.subTest(key=key):
                config = self.valid_config()
                config[key] = value
                with self.assertRaises(deploy.DeploymentError):
                    deploy.validate_configuration(config)

    def test_configuration_rejects_unsafe_full_rollout_settings(self) -> None:
        for key, value in (
            ("AGSX_ENABLED", "false"),
            ("AGSX_ENFORCE_EFFECTIVE_RESOURCES", "false"),
            ("AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR", "false"),
            ("AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G", "0"),
            ("AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS", "0"),
        ):
            with self.subTest(key=key):
                config = self.valid_config()
                config[key] = value
                with self.assertRaises(deploy.DeploymentError):
                    deploy.validate_configuration(config)
        config = self.valid_config()
        config["AGSX_TEMPLATE"] = "different-tool"
        with self.assertRaises(deploy.DeploymentError):
            deploy.validate_configuration(config)

    def test_redaction_and_environment_sanitization(self) -> None:
        self.assertEqual(
            deploy._redact("failed for top-secret", ["top-secret"]),
            "failed for <redacted>",
        )
        with patch.dict(
            os.environ,
            {
                "TENCENTCLOUD_SECRET_KEY": "cloud-secret",
                "AGSX_REGISTRY_PASSWORD": "registry-secret",
                "E2B_API_KEY": "runtime-secret",
            },
            clear=False,
        ):
            environment = deploy.sanitized_environment()
        self.assertNotIn("TENCENTCLOUD_SECRET_KEY", environment)
        self.assertNotIn("AGSX_REGISTRY_PASSWORD", environment)
        self.assertNotIn("E2B_API_KEY", environment)

    def test_agr_status_and_doctor_require_healthy_credentials_and_connectivity(self) -> None:
        deploy.validate_agr_status(
            {
                "Data": {
                    "Auth": {
                        "SecretId": {"Present": True},
                        "SecretKey": {"Present": True},
                    }
                }
            }
        )
        with self.assertRaises(deploy.DeploymentError):
            deploy.validate_agr_status(
                {
                    "Data": {
                        "Auth": {
                            "SecretId": {"Present": True},
                            "SecretKey": {"Present": False},
                        }
                    }
                }
            )
        checks = [
            {"Name": "SecretId", "Status": "ok"},
            {"Name": "SecretKey", "Status": "ok"},
            {"Name": "Connectivity", "Status": "warning"},
        ]
        with self.assertRaises(deploy.DeploymentError):
            deploy.validate_agr_doctor({"Data": {"Checks": checks}})
        checks[2]["Status"] = "ok"
        deploy.validate_agr_doctor({"Data": {"Checks": checks}})

    def test_matching_active_tool_is_safe_to_reuse(self) -> None:
        config = self.valid_config()
        request = deploy.render_request(config, deploy.DEFAULT_TEMPLATE)
        digest = "b" * 64
        request["CustomConfiguration"]["Image"] += f"@sha256:{digest}"
        tool = {
            "ToolId": "tool-123",
            "ToolName": config["AGSX_TOOL_NAME"],
            "Status": "ACTIVE",
            "NetworkMode": "SANDBOX",
            "Image": deploy.image_reference(config) + f"@sha256:{digest}",
            "CPU": "2",
            "Memory": "2Gi",
        }
        tag_request = deploy.render_request(config, deploy.DEFAULT_TEMPLATE)
        self.assertEqual(deploy.verify_reused_tool(tool, tag_request), "tool-123")
        self.assertEqual(deploy.verify_reused_tool(tool, request), "tool-123")

        tag_only_tool = dict(tool)
        tag_only_tool["Image"] = deploy.image_reference(config)
        self.assertEqual(
            deploy.verify_reused_tool(tag_only_tool, tag_request), "tool-123"
        )

        # agr responses have their own top-level command Status. Tool lifecycle
        # state must come from Data.Status instead of the "succeeded" envelope.
        tag_only_tool["ImageDigest"] = f"sha256:{digest}"
        enveloped_tool = {"Status": "succeeded", "Data": dict(tag_only_tool)}
        self.assertEqual(deploy.tool_status(enveloped_tool), "ACTIVE")
        self.assertEqual(
            deploy.verify_reused_tool(enveloped_tool, tag_request), "tool-123"
        )
        self.assertEqual(
            deploy.tool_image_digest(enveloped_tool),
            deploy.image_reference(config) + f"@sha256:{digest}",
        )
        tool["Image"] = deploy.image_reference(config) + "@sha256:" + "c" * 64
        with self.assertRaises(deploy.DeploymentError):
            deploy.verify_reused_tool(tool, request)
        tool["Image"] = deploy.image_reference(config) + f"@sha256:{digest}"
        tool["Memory"] = "1Gi"
        with self.assertRaises(deploy.DeploymentError):
            deploy.verify_reused_tool(tool, request)


if __name__ == "__main__":
    unittest.main()
