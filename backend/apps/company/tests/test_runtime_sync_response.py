import json

from django.test import SimpleTestCase

from apps.company.services import CompanyService


class RuntimeSyncResponseSummaryTests(SimpleTestCase):
    def test_summary_omits_full_runtime_config_payloads(self):
        summary = CompanyService._summarize_runtime_sync_payload(
            {
                "version": {
                    "id": 1,
                    "version": "v-test",
                    "status": "published",
                    "config_payload": '{"secret":"platform-config"}',
                },
                "bootstrap": {
                    "status": "success",
                    "message": "bootstrap wrote /data/tsds/tenants/demo/config/openclaw.json via http://127.0.0.1:25312",
                    "stdout": "raw bootstrap output",
                    "task": {
                        "id": 2,
                        "status": "done",
                        "stderr": "raw stderr",
                    },
                },
                "readback": {
                    "published_version": "v-test",
                    "current_runtime_version": "v-test",
                    "config_payload": '{"secret":"runtime-config"}',
                    "config_path": "/private/config.json",
                },
                "diff": {
                    "is_in_sync": True,
                    "mismatched_sections": [],
                    "platform_config": {"secret": "platform"},
                    "runtime_config": {"secret": "runtime"},
                },
                "instance": {
                    "id": 3,
                    "config_status": "in_sync",
                    "private_ip": "10.0.0.1",
                    "config_path": "/private/config.json",
                },
            }
        )

        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertIn("v-test", serialized)
        self.assertNotIn("config_payload", serialized)
        self.assertNotIn("platform_config", serialized)
        self.assertNotIn("runtime_config", serialized)
        self.assertNotIn("raw bootstrap output", serialized)
        self.assertNotIn("raw stderr", serialized)
        self.assertNotIn("10.0.0.1", serialized)
        self.assertNotIn("/private/config.json", serialized)
        self.assertNotIn("/data/tsds", serialized)
        self.assertNotIn("127.0.0.1", serialized)
        self.assertIn("[internal-path]", serialized)
        self.assertIn("[internal-endpoint]", serialized)

    def test_error_summary_for_diff_omits_full_configs(self):
        message = CompanyService._summarize_runtime_sync_error(
            {
                "is_in_sync": False,
                "mismatched_sections": ["agent_permission_bindings"],
                "platform_config": {"secret": "platform"},
                "runtime_config": {"secret": "runtime"},
            }
        )

        self.assertIn("agent_permission_bindings", message)
        self.assertNotIn("platform_config", message)
        self.assertNotIn("runtime_config", message)
        self.assertNotIn("secret", message)

    def test_error_summary_redacts_internal_paths_from_messages(self):
        message = CompanyService._summarize_runtime_sync_error(
            {
                "message": "failed reading /srv/openclaw-home/.openclaw/openclaw.json from C:\\tmp\\openclaw.json",
            }
        )

        self.assertIn("[internal-path]", message)
        self.assertNotIn("/srv/openclaw-home", message)
        self.assertNotIn("C:\\tmp", message)
