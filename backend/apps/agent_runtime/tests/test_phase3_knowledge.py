"""Phase 3-B knowledge indexing, migration, and management API tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.agent_runtime.knowledge.identity import (
    company_knowledge_namespace,
    knowledge_idempotency_key,
    knowledge_target_uri,
)
from apps.agent_runtime.knowledge.services import KnowledgeIndexService
from apps.agent_runtime.knowledge.retriever import KnowledgeRetriever
from apps.agent_runtime.knowledge.tasks import index_knowledge_file
from apps.agent_runtime.memory.client import OpenVikingClient
from apps.agent_runtime.memory.namespaces import MemoryNamespace
from apps.agent_runtime.models import AgentRun, MemorySyncJob
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.agent_runtime.views import knowledge_file_list, run_list
from apps.company.models import TransparentCompanyAgentTestMessage
from apps.runtime.models import WorkspaceFile
from common.utils.Login_utils import sign_token


class FakeResponse:
    status_code = 200

    def json(self):
        return {"status": "ok", "result": {"status": "success", "root_uri": "viking://resources/x"}}


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse()


class KnowledgeIdentityTests(TestCase):
    def test_company_and_content_version_are_part_of_trusted_uri(self):
        company = create_memory_fixture("phase3-identity")["company_a"]
        workspace_file = SimpleNamespace(
            pk=7,
            company_id=company.id,
            company=company,
            logical_path="knowledge/reports/Q1 final.pdf",
            checksum="sha256-a",
            storage_key="runtime/company-a/knowledge/reports/Q1.pdf",
        )

        namespace = company_knowledge_namespace(company)
        uri = knowledge_target_uri(workspace_file, namespace)

        self.assertTrue(uri.startswith(namespace.root_uri))
        self.assertTrue(uri.endswith("Q1-final.pdf"))
        self.assertIn(f":{company.id}:7:", knowledge_idempotency_key(workspace_file))

    def test_openviking_resource_request_uses_backend_identity(self):
        namespace = MemoryNamespace("company_knowledge", "tm-a", "company-a", "viking://resources/company-a/")
        session = FakeSession()
        client = OpenVikingClient("http://openviking:1933", "secret", session=session)

        result = client.add_resource(
            namespace,
            source_url="https://cos.example/signed",
            target_uri="viking://resources/company-a/one.pdf",
        )

        self.assertEqual(result["root_uri"], "viking://resources/x")
        _method, url, kwargs = session.calls[0]
        self.assertEqual(url, "http://openviking:1933/api/v1/resources")
        self.assertEqual(kwargs["headers"]["X-OpenViking-Account"], "tm-a")
        self.assertEqual(kwargs["json"]["path"], "https://cos.example/signed")
        self.assertTrue(kwargs["json"]["wait"])


class KnowledgeIndexDatabaseTests(TestCase):
    def setUp(self):
        self.fixture = create_memory_fixture("phase3-knowledge")
        self.file = self._file(self.fixture["company_a"], "knowledge/guide.md")

    def test_enqueue_is_idempotent_for_same_file_version(self):
        dispatched = []
        service = KnowledgeIndexService(dispatcher=dispatched.append)

        with self.captureOnCommitCallbacks(execute=True):
            first = service.enqueue(self.file)
        MemorySyncJob.objects.filter(pk=first.pk).update(status=MemorySyncJob.STATUS_COMPLETED)
        with self.captureOnCommitCallbacks(execute=True):
            second = service.enqueue(self.file)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(dispatched, [str(first.pk)])
        self.assertEqual(MemorySyncJob.objects.count(), 1)

    def test_index_task_updates_job_and_file_atomically_visible_states(self):
        job = KnowledgeIndexService(dispatcher=lambda _job_id: None).enqueue(self.file)
        result = {"resource_uri": "viking://resources/company/guide.md", "openviking_task_id": "ov-1"}

        with patch("apps.agent_runtime.knowledge.tasks.KnowledgeIndexer") as indexer_class:
            indexer_class.return_value.index.return_value = result
            payload = index_knowledge_file.run(str(job.pk))

        job.refresh_from_db()
        self.file.refresh_from_db()
        self.assertEqual(payload["status"], MemorySyncJob.STATUS_COMPLETED)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(self.file.resource_sync_status, WorkspaceFile.RESOURCE_SYNC_COMPLETED)
        self.assertEqual(self.file.resource_uri, result["resource_uri"])

    def test_retriever_searches_only_agent_allowed_files(self):
        allowed = self.file
        allowed.resource_sync_status = WorkspaceFile.RESOURCE_SYNC_COMPLETED
        allowed.resource_uri = "viking://resources/company/allowed"
        allowed.save(update_fields=["resource_sync_status", "resource_uri"])
        denied = self._file(self.fixture["company_a"], "knowledge/private/secret.md")
        denied.resource_sync_status = WorkspaceFile.RESOURCE_SYNC_COMPLETED
        denied.resource_uri = "viking://resources/company/denied"
        denied.save(update_fields=["resource_sync_status", "resource_uri"])
        permission = self.fixture["agent_a1"].permissionBindings.get()
        permission.knowledgeRead = ["knowledge"]
        permission.knowledgeDeny = ["knowledge/private"]
        permission.save(update_fields=["knowledgeRead", "knowledgeDeny"])
        client = SimpleNamespace(search_resources=lambda _namespace, **kwargs: [{
            "uri": f'{kwargs["target_uri"]}/chunk.md', "content": "allowed", "score": 0.9
        }])

        items = KnowledgeRetriever(client=client).recall(
            company_id=self.fixture["company_a"].pk,
            agent_id=self.fixture["agent_a1"].pk,
            query="guide",
        )

        self.assertEqual([item.logical_path for item in items], [allowed.logical_path])

    @staticmethod
    def _file(company, logical_path):
        return WorkspaceFile.objects.create(
            company=company,
            logical_path=logical_path,
            file_name=logical_path.rsplit("/", 1)[-1],
            storage_backend=WorkspaceFile.STORAGE_TENCENT_COS,
            storage_key=f"runtime/{company.cid}/{logical_path}",
            checksum="sha256-version-1",
            sync_status=WorkspaceFile.SYNC_IN_SYNC,
            resource_sync_status=WorkspaceFile.RESOURCE_SYNC_PENDING,
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )


class RuntimeManagementApiTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.fixture = create_memory_fixture("phase3-api")
        self.file_a = KnowledgeIndexDatabaseTests._file(
            self.fixture["company_a"], "knowledge/a.md"
        )
        KnowledgeIndexDatabaseTests._file(self.fixture["company_b"], "knowledge/b.md")

    def test_knowledge_list_is_company_scoped(self):
        request = self._request("/knowledge/files", self.fixture["company_a"].cid)
        with self._admin(self.fixture["company_a"]):
            response = knowledge_file_list(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["data"]["total"], 1)
        self.assertEqual(payload["data"]["items"][0]["id"], self.file_a.pk)

    def test_non_admin_cannot_list_runs(self):
        request = self._request("/runs", self.fixture["company_a"].cid)
        with patch("apps.agent_runtime.views.get_user_from_request", return_value=self.fixture["owner"]), patch(
            "apps.agent_runtime.views.CompanyService._get_company_for_admin",
            return_value=(False, "权限不足"),
        ):
            response = run_list(request)

        self.assertEqual(response.status_code, 403)

    def test_run_list_does_not_cross_company_boundary(self):
        self._run(self.fixture["company_a"], self.fixture["agent_a1"], self.fixture["session_a1"], "a")
        self._run(self.fixture["company_b"], self.fixture["agent_b1"], self.fixture["session_b1"], "b")
        request = self._request("/runs", self.fixture["company_a"].cid)
        with self._admin(self.fixture["company_a"]):
            response = run_list(request)

        payload = json.loads(response.content)
        self.assertEqual(payload["data"]["total"], 1)
        self.assertEqual(payload["data"]["items"][0]["request_id"], "request-a")

    def _admin(self, company):
        user_patch = patch("apps.agent_runtime.views.get_user_from_request", return_value=self.fixture["owner"])
        company_patch = patch(
            "apps.agent_runtime.views.CompanyService._get_company_for_admin",
            return_value=(True, company),
        )

        class CombinedPatch:
            def __enter__(self):
                user_patch.start()
                company_patch.start()

            def __exit__(self, exc_type, exc_value, traceback):
                company_patch.stop()
                user_patch.stop()

        return CombinedPatch()

    def _request(self, path, cid):
        return self.factory.get(
            path,
            {"cid": cid},
            HTTP_ACCESS_TOKEN=sign_token({"user_id": self.fixture["owner"].pk}),
        )

    def _run(self, company, agent, session, suffix):
        message = TransparentCompanyAgentTestMessage.objects.create(
            session=session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content="hello",
            created_at="2026-08-13",
            updated_at="2026-08-13",
        )
        return AgentRun.objects.create(
            request_id=f"request-{suffix}",
            company=company,
            agent=agent,
            user=self.fixture["owner"],
            session=session,
            source=AgentRun.SOURCE_AGENT_TEST,
            engine=AgentRun.ENGINE_UNIFIED,
            input_message=message,
        )
