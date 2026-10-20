"""Focused functional tests for Memory Gateway ownership enforcement."""

from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from memory_gateway import (  # noqa: E402
    AgentMemorySpace,
    MemoryGateway,
    MemoryGatewayDenied,
    resolve_runtime_actor,
)
from openviking_client import OpenVikingHTTPClient, OpenVikingIdentity  # noqa: E402


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpenVikingClient:
    def __init__(self):
        self.calls = []

    def find_memories(self, identity, *, root_uri, query, limit):
        self.calls.append(("find", identity, root_uri, query, limit))
        return {
            "memories": [
                {"uri": f"{root_uri}allowed.md", "content": "allowed"},
                {"uri": "viking://user/other/memories/hidden.md", "content": "hidden"},
            ]
        }

    def find_resources(self, identity, *, root_uri, query, limit):
        self.calls.append(("find_resource", identity, root_uri, query, limit))
        return {
            "resources": [
                {"uri": f"{root_uri}/allowed", "content": "allowed resource"},
                {"uri": "viking://resources/knowledge/hidden.md", "content": "hidden"},
            ]
        }

    def list_directory(self, identity, uri):
        self.calls.append(("list", identity, uri))
        return []

    def ensure_memory_root(self, identity, root_uri):
        self.calls.append(("mkdir", identity, root_uri))
        return {"uri": root_uri}

    def read(self, identity, uri):
        self.calls.append(("read", identity, uri))
        return {"uri": uri, "content": "memory"}

    def write(self, identity, *, uri, content, mode):
        self.calls.append(("write", identity, uri, content, mode))
        return {"uri": uri, "mode": mode}

    def delete(self, identity, uri):
        self.calls.append(("delete", identity, uri))
        return {"uri": uri}


class MemoryGatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeOpenVikingClient()
        self.gateway = MemoryGateway(self.client)
        self.space = AgentMemorySpace(
            agent_id="customer-agent",
            viking_space_id="account:customer",
            account_id="account",
            user_id="customer",
            root_uri="viking://user/customer/memories/",
            status="active",
        )

    def test_runtime_identity_comes_from_session_key(self):
        actor = resolve_runtime_actor(
            {
                "agent_id": "customer-agent",
                "session_key": "agent:customer-agent:main",
            }
        )
        self.assertEqual(actor, "customer-agent")

    def test_spoofed_agent_id_is_denied(self):
        with self.assertRaises(MemoryGatewayDenied):
            resolve_runtime_actor(
                {
                    "agent_id": "ceo-agent",
                    "session_key": "agent:customer-agent:main",
                }
            )

    def test_search_filters_cross_space_results(self):
        result = self.gateway.execute(
            action="read",
            arguments={"query": "customer preference", "limit": 8},
            space=self.space,
        )
        self.assertEqual(
            result["data"]["memories"],
            [
                {
                    "uri": "viking://user/customer/memories/allowed.md",
                    "content": "allowed",
                }
            ],
        )

    def test_write_update_delete_remain_in_owned_root(self):
        created = self.gateway.execute(
            action="write",
            arguments={"path": "facts/order-policy.md", "content": "Use supplier A."},
            space=self.space,
        )
        uri = created["data"]["uri"]
        self.assertTrue(uri.startswith(self.space.root_uri))

        self.gateway.execute(
            action="update",
            arguments={"uri": uri, "content": "Use supplier B."},
            space=self.space,
        )
        self.gateway.execute(
            action="delete",
            arguments={"uri": uri},
            space=self.space,
        )
        self.assertEqual(
            [call[0] for call in self.client.calls],
            ["mkdir", "write", "write", "delete"],
        )

    def test_cross_agent_uri_is_denied(self):
        with self.assertRaisesRegex(MemoryGatewayDenied, "Cross-Agent"):
            self.gateway.execute(
                action="read",
                arguments={"uri": "viking://user/other/memories/private.md"},
                space=self.space,
            )

    def test_archived_space_is_denied(self):
        archived = AgentMemorySpace(
            agent_id=self.space.agent_id,
            viking_space_id=self.space.viking_space_id,
            account_id=self.space.account_id,
            user_id=self.space.user_id,
            root_uri=self.space.root_uri,
            status="archived",
        )
        with self.assertRaisesRegex(MemoryGatewayDenied, "not active"):
            self.gateway.execute(
                action="read",
                arguments={"query": "anything"},
                space=archived,
            )

    def test_authorized_cross_agent_query_searches_fixed_memory_and_resource_roots(self):
        result = self.gateway.query_agent(
            query="August watermelon prices",
            target_space=self.space,
            resource_identity=OpenVikingIdentity("company-account", "resource-owner"),
            resource_roots=[
                "viking://resources/knowledge/procurement.md",
                "viking://resources/knowledge/procurement.md/details",
            ],
            sources=["memory", "resource"],
            limit=6,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["agent_id"], "customer-agent")
        self.assertEqual(
            result["data"]["memory"]["memories"],
            [{"uri": f"{self.space.root_uri}allowed.md", "content": "allowed"}],
        )
        self.assertEqual(len(result["data"]["resource"]), 1)
        self.assertEqual(
            result["data"]["resource"][0]["result"]["resources"],
            [
                {
                    "uri": "viking://resources/knowledge/procurement.md/allowed",
                    "content": "allowed resource",
                }
            ],
        )

    def test_cross_agent_query_hydrates_authorized_resource_abstract(self):
        class AbstractOnlyClient(FakeOpenVikingClient):
            def find_resources(self, identity, *, root_uri, query, limit):
                return {
                    "resources": [
                        {"uri": f"{root_uri}/source.md", "abstract": "summary"},
                        {"uri": "viking://resources/knowledge/hidden.md", "abstract": "hidden"},
                    ]
                }

            def read(self, identity, uri):
                self.calls.append(("read", identity, uri))
                return "authoritative source text"

        gateway = MemoryGateway(AbstractOnlyClient())
        result = gateway.query_agent(
            query="price",
            target_space=self.space,
            resource_identity=OpenVikingIdentity("company-account", "resource-owner"),
            resource_roots=["viking://resources/knowledge/procurement.md"],
            sources=["resource"],
        )

        hits = result["data"]["resource"][0]["result"]["resources"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["content"], "authoritative source text")
        self.assertFalse(hits[0]["content_truncated"])

    def test_zero_semantic_hits_fall_back_to_exact_authorized_resource(self):
        class ZeroHitClient(FakeOpenVikingClient):
            def find_resources(self, identity, *, root_uri, query, limit):
                return {"resources": [], "total": 0}

            def list_directory(self, identity, uri):
                return [
                    {
                        "uri": f"{uri}/customer-service.md",
                        "size": 100,
                        "isDir": False,
                    },
                    {
                        "uri": "viking://resources/knowledge/other/secret.md",
                        "size": 100,
                        "isDir": False,
                    },
                ]

            def read(self, identity, uri):
                return "无籽西瓜 10.20 元/公斤"

        gateway = MemoryGateway(ZeroHitClient())
        result = gateway.query_agent(
            query="无籽西瓜零售价",
            target_space=self.space,
            resource_identity=OpenVikingIdentity("company-account", "resource-owner"),
            resource_roots=["viking://resources/knowledge/customer-service.md"],
            sources=["resource"],
        )

        resource_result = result["data"]["resource"][0]["result"]
        self.assertEqual(resource_result["authorized_fallback_count"], 1)
        self.assertEqual(len(resource_result["resources"]), 1)
        self.assertEqual(
            resource_result["resources"][0]["content"],
            "无籽西瓜 10.20 元/公斤",
        )
        self.assertEqual(
            resource_result["resources"][0]["retrieval_mode"],
            "authorized_resource_fallback",
        )

    def test_broad_knowledge_scope_does_not_use_direct_read_fallback(self):
        class BroadScopeClient(FakeOpenVikingClient):
            def find_resources(self, identity, *, root_uri, query, limit):
                return {"resources": [], "total": 0}

            def list_directory(self, identity, uri):
                raise AssertionError("broad knowledge scope must not be listed")

        gateway = MemoryGateway(BroadScopeClient())
        result = gateway.query_agent(
            query="price",
            target_space=self.space,
            resource_identity=OpenVikingIdentity("company-account", "resource-owner"),
            resource_roots=["viking://resources/knowledge"],
            sources=["resource"],
        )

        self.assertEqual(result["data"]["resource"][0]["result"]["total"], 0)

    def test_cross_agent_query_rejects_model_supplied_uri_as_a_source(self):
        with self.assertRaisesRegex(MemoryGatewayDenied, "At least one"):
            self.gateway.query_agent(
                query="secret",
                target_space=self.space,
                resource_identity=OpenVikingIdentity("company-account", "resource-owner"),
                resource_roots=["viking://resources/knowledge/procurement.md"],
                sources=["viking://resources/knowledge/other.md"],
            )


class OpenVikingHTTPClientTests(unittest.TestCase):
    def test_trusted_identity_headers_are_injected(self):
        captured = {}

        def opener(request, timeout):
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return FakeHTTPResponse({"status": "ok", "result": {"items": []}})

        client = OpenVikingHTTPClient(
            "http://openviking:1933",
            api_key="trusted-key",
            timeout=17,
            opener=opener,
        )
        result = client.find_memories(
            OpenVikingIdentity("company-account", "customer-agent"),
            root_uri="viking://user/customer-agent/memories/",
            query="preference",
            limit=5,
        )

        self.assertEqual(result, {"items": []})
        self.assertEqual(captured["timeout"], 17)
        self.assertEqual(captured["headers"]["X-api-key"], "trusted-key")
        self.assertEqual(captured["headers"]["X-openviking-account"], "company-account")
        self.assertEqual(captured["headers"]["X-openviking-user"], "customer-agent")

    def test_resource_upload_and_async_ingestion_contract(self):
        captured = []

        def opener(request, timeout):
            captured.append(
                {
                    "url": request.full_url,
                    "headers": dict(request.header_items()),
                    "body": request.data,
                    "timeout": timeout,
                }
            )
            if request.full_url.endswith("/api/v1/resources/temp_upload"):
                return FakeHTTPResponse({"status": "ok", "result": {"temp_file_id": "tmp-1"}})
            return FakeHTTPResponse({"status": "accepted", "result": {"task_id": "ov-task-1"}})

        client = OpenVikingHTTPClient("http://openviking:1933", opener=opener)
        identity = OpenVikingIdentity("company-account", "bootstrap")
        with tempfile.NamedTemporaryFile(suffix=".md") as source_file:
            source_file.write(b"# Knowledge\nAuthoritative source")
            source_file.flush()
            upload = client.temp_upload(identity, source_file.name, file_name="policy.md")
        result = client.add_resource(
            identity,
            temp_file_id=upload["temp_file_id"],
            target_uri="viking://resources/knowledge/policy.md",
            source_name="policy.md",
            wait=False,
        )

        self.assertEqual(result["task_id"], "ov-task-1")
        self.assertIn("multipart/form-data", captured[0]["headers"]["Content-type"])
        ingest_payload = json.loads(captured[1]["body"].decode("utf-8"))
        self.assertEqual(ingest_payload["to"], "viking://resources/knowledge/policy.md")
        self.assertFalse(ingest_payload["wait"])
        self.assertEqual(ingest_payload["processing_mode"], "semantic_and_vectors")
        self.assertIn("searchable summaries", ingest_payload["instruction"])

    def test_automatic_memory_recall_and_commit_contract(self):
        captured = []

        def opener(request, timeout):
            captured.append((request.get_method(), request.full_url, request.data))
            return FakeHTTPResponse({"status": "ok", "result": {"ok": True}})

        client = OpenVikingHTTPClient("http://openviking:1933", opener=opener)
        identity = OpenVikingIdentity("company-account", "agent-1")
        client.recall(identity, query="customer preferences")
        client.ensure_session(identity, "session-1")
        client.add_session_messages(
            identity,
            "session-1",
            [{"role": "user", "peer_id": "agent-1", "content": "remember this"}],
        )
        client.commit_session(identity, "session-1", keep_recent_count=10)

        self.assertEqual(captured[0][1], "http://openviking:1933/api/v1/search/recall")
        self.assertIn("auto_create=true", captured[1][1])
        self.assertTrue(captured[2][1].endswith("/api/v1/sessions/session-1/messages/batch"))
        self.assertTrue(captured[3][1].endswith("/api/v1/sessions/session-1/commit"))

    def test_resource_find_is_scoped_by_server_selected_target_uri(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse({"status": "ok", "result": {"resources": []}})

        client = OpenVikingHTTPClient("http://openviking:1933", opener=opener)
        client.find_resources(
            OpenVikingIdentity("company-account", "resource-owner"),
            root_uri="viking://resources/knowledge/procurement.md",
            query="watermelon price",
            limit=7,
        )

        self.assertTrue(captured["url"].endswith("/api/v1/search/find"))
        self.assertEqual(
            captured["payload"],
            {
                "query": "watermelon price",
                "target_uri": "viking://resources/knowledge/procurement.md",
                "context_type": ["resource"],
                "limit": 7,
            },
        )


if __name__ == "__main__":
    unittest.main()
