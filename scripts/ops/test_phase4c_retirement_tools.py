from __future__ import annotations

import shutil
import socket
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import phase4c_openclaw_inventory as inventory_tool
import phase4c_stop_openclaw as stop_tool


class Phase4CRetirementToolTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(Path.cwd() / ".tmp_phase4c_tests", ignore_errors=True)

    @staticmethod
    @contextmanager
    def _temporary_directory():
        base = Path.cwd() / ".tmp_phase4c_tests" / uuid.uuid4().hex
        base.mkdir(parents=True, exist_ok=True)
        try:
            yield str(base)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def _runtime_root(self, temporary_directory: str) -> Path:
        root = Path(temporary_directory) / "opt" / "tsds" / "tenants"
        root.mkdir(parents=True)
        return root

    @staticmethod
    def _ready_preflight() -> dict:
        return {
            "schemaVersion": 1,
            "summary": {"ready": True, "blockers": 0},
            "checks": [
                {"id": check_id, "status": "pass"}
                for check_id in sorted(stop_tool.REQUIRED_PREFLIGHT_CHECK_IDS)
            ],
        }

    def test_inventory_is_secret_free_and_records_recoverable_assets(self):
        with self._temporary_directory() as temporary_directory:
            root = self._runtime_root(temporary_directory)
            tenant = root / "tenant-a"
            tenant.mkdir()
            (tenant / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            secret_dir = tenant / "secrets"
            secret_dir.mkdir()
            (secret_dir / "token").write_text("must-not-leak", encoding="utf-8")

            with patch.object(
                inventory_tool,
                "_systemd_state",
                return_value={
                    "service": inventory_tool.DEFAULT_SERVICE,
                    "active": "active",
                    "enabled": "enabled",
                },
            ), patch.object(
                inventory_tool,
                "_docker_projects",
                return_value=["tenant-a"],
            ), patch.object(
                inventory_tool,
                "_project_images",
                return_value=[{"reference": "openclaw:v1", "imageId": "sha256:test"}],
            ), patch.object(
                inventory_tool,
                "_run",
                side_effect=[(0, "LISTEN 0 128 0.0.0.0:19080 0.0.0.0:*"), (0, "1"), (0, "abc123")],
            ):
                report = inventory_tool.build_inventory(
                    runtime_root=root,
                    service_name=inventory_tool.DEFAULT_SERVICE,
                    bridge_port=19080,
                )

            self.assertEqual(report["tenants"][0]["namespace"], "tenant-a")
            self.assertTrue(report["bridge"]["portListening"])
            self.assertTrue(report["release"]["preserveForOneRelease"])
            self.assertNotIn("must-not-leak", str(report))

    def test_stop_plan_uses_stop_only_and_provides_reverse_order_recovery(self):
        with self._temporary_directory() as temporary_directory:
            root = self._runtime_root(temporary_directory)
            tenant = root / "tenant-a"
            tenant.mkdir()
            compose_path = tenant / "compose.yml"
            compose_path.write_text("services: {}\n", encoding="utf-8")
            inventory = {
                "schemaVersion": 1,
                "hostname": socket.gethostname(),
                "runtimeRoot": str(root),
                "bridge": {"service": inventory_tool.DEFAULT_SERVICE},
                "tenants": [{"namespace": "tenant-a", "composeFile": str(compose_path)}],
            }
            preflight = self._ready_preflight()

            plan = stop_tool.build_stop_plan(
                inventory=inventory,
                preflight=preflight,
                stop_timeout_seconds=30,
            )

            self.assertEqual(
                plan["stopCommands"][0],
                ["systemctl", "disable", "--now", inventory_tool.DEFAULT_SERVICE],
            )
            self.assertIn("stop", plan["stopCommands"][1])
            self.assertNotIn("down", str(plan["stopCommands"]))
            self.assertFalse(plan["deletesData"])
            self.assertEqual(plan["recoveryCommands"][-1][1:3], ["enable", "--now"])

    def test_stop_plan_rejects_compose_path_outside_inventory_root(self):
        with self._temporary_directory() as temporary_directory:
            root = self._runtime_root(temporary_directory)
            outside = Path(temporary_directory) / "compose.yml"
            outside.write_text("services: {}\n", encoding="utf-8")
            inventory = {
                "schemaVersion": 1,
                "hostname": socket.gethostname(),
                "runtimeRoot": str(root),
                "bridge": {"service": inventory_tool.DEFAULT_SERVICE},
                "tenants": [{"namespace": "bad", "composeFile": str(outside)}],
            }
            preflight = self._ready_preflight()

            with self.assertRaisesRegex(ValueError, "outside runtimeRoot"):
                stop_tool.build_stop_plan(
                    inventory=inventory,
                    preflight=preflight,
                    stop_timeout_seconds=30,
                )

    def test_stop_plan_rejects_old_preflight_without_phase4b_evidence(self):
        with self._temporary_directory() as temporary_directory:
            root = self._runtime_root(temporary_directory)
            inventory = {
                "schemaVersion": 1,
                "hostname": socket.gethostname(),
                "runtimeRoot": str(root),
                "bridge": {"service": inventory_tool.DEFAULT_SERVICE},
                "tenants": [],
            }
            preflight = self._ready_preflight()
            preflight["checks"] = [
                item
                for item in preflight["checks"]
                if item["id"] != "legacy_archive_integrity"
            ]

            with self.assertRaisesRegex(ValueError, "legacy_archive_integrity"):
                stop_tool.build_stop_plan(
                    inventory=inventory,
                    preflight=preflight,
                    stop_timeout_seconds=30,
                )


if __name__ == "__main__":
    unittest.main()
