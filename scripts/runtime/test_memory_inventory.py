import unittest

try:
    from memory_gateway import AgentMemorySpace
    from memory_inventory import BUILTIN_MEMORY_TYPES, build_memory_inventory
    from openviking_client import OpenVikingClientError
except ImportError:
    from scripts.runtime.memory_gateway import AgentMemorySpace
    from scripts.runtime.memory_inventory import BUILTIN_MEMORY_TYPES, build_memory_inventory
    from scripts.runtime.openviking_client import OpenVikingClientError


class FakeOpenVikingClient:
    def __init__(self, listings):
        self.listings = listings

    def list_directory(self, identity, uri):
        return {"items": self.listings.get(uri, [])}


class MissingRootOpenVikingClient:
    def list_directory(self, identity, uri):
        raise OpenVikingClientError(
            "OpenViking request failed with HTTP 404: Directory not found",
            http_status=404,
            response_text='{"code":"NOT_FOUND","message":"Directory not found"}',
        )


class MemoryInventoryTests(unittest.TestCase):
    def test_unmaterialized_agent_root_is_a_valid_empty_inventory(self):
        root = "viking://user/new-agent/memories/"
        inventory = build_memory_inventory(
            MissingRootOpenVikingClient(),
            [
                AgentMemorySpace(
                    agent_id="new-agent",
                    viking_space_id="space-new",
                    account_id="company-1",
                    user_id="new-agent",
                    root_uri=root,
                    status="active",
                )
            ],
        )

        self.assertEqual(inventory["list"], [])
        self.assertEqual(inventory["type_counts"], {})
        self.assertEqual(inventory["warnings"], [])

    def test_builtin_types_match_openviking_0_4_12_templates(self):
        self.assertEqual(
            [item["key"] for item in BUILTIN_MEMORY_TYPES],
            [
                "profile",
                "preferences",
                "entities",
                "events",
                "identity",
                "soul",
                "cases",
                "trajectories",
                "experiences",
                "tools",
                "skills",
            ],
        )
        self.assertEqual(
            [item["short_label"] for item in BUILTIN_MEMORY_TYPES],
            [
                "用户画像",
                "用户偏好",
                "实体知识",
                "事件记录",
                "Agent 身份",
                "核心设定",
                "任务案例",
                "执行轨迹",
                "执行经验",
                "工具经验",
                "技能经验",
            ],
        )

    def test_discovers_builtin_and_custom_types_inside_owned_space(self):
        root = "viking://user/support/memories/"
        client = FakeOpenVikingClient(
            {
                root: [
                    {
                        "uri": f"{root}profile.md",
                        "isDir": False,
                        "modTime": "2026-07-30T00:00:00.000Z",
                    },
                    {"uri": f"{root}supplier-risk", "isDir": True},
                    {"uri": "viking://user/other/memories/private.md", "type": "file"},
                ],
                f"{root}supplier-risk/": [
                    {
                        "uri": f"{root}supplier-risk/primary.md",
                        "isDir": False,
                        "abstract": "risk",
                    },
                ],
            }
        )
        inventory = build_memory_inventory(
            client,
            [
                AgentMemorySpace(
                    agent_id="support",
                    viking_space_id="space-1",
                    account_id="company-1",
                    user_id="support",
                    root_uri=root,
                    status="active",
                )
            ],
        )

        self.assertEqual(len(inventory["list"]), 2)
        self.assertEqual(inventory["type_counts"], {"profile": 1, "supplier-risk": 1})
        custom = next(item for item in inventory["memory_types"] if item["key"] == "supplier-risk")
        self.assertFalse(custom["builtin"])
        self.assertTrue(all(item["owner_agent_id"] == "support" for item in inventory["list"]))
        self.assertEqual(inventory["list"][0]["updated_at"], "2026-07-30T00:00:00.000Z")
        self.assertEqual(inventory["openviking_version"], "0.4.12")

    def test_maps_root_files_and_execution_directories_to_current_types(self):
        root = "viking://user/support/memories/"
        client = FakeOpenVikingClient(
            {
                root: [
                    {"uri": f"{root}identity.md", "isDir": False},
                    {"uri": f"{root}soul.md", "isDir": False},
                    {"uri": f"{root}trajectories", "isDir": True},
                    {"uri": f"{root}experiences", "isDir": True},
                ],
                f"{root}trajectories/": [
                    {"uri": f"{root}trajectories/task.md", "isDir": False},
                ],
                f"{root}experiences/": [
                    {"uri": f"{root}experiences/lesson.md", "isDir": False},
                ],
            }
        )

        inventory = build_memory_inventory(
            client,
            [
                AgentMemorySpace(
                    agent_id="support",
                    viking_space_id="space-1",
                    account_id="company-1",
                    user_id="support",
                    root_uri=root,
                    status="active",
                )
            ],
        )

        self.assertEqual(
            inventory["type_counts"],
            {"identity": 1, "soul": 1, "trajectories": 1, "experiences": 1},
        )
        self.assertNotIn("patterns", [item["key"] for item in inventory["memory_types"]])


if __name__ == "__main__":
    unittest.main()
