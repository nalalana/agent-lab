from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.agent_runtime.config import ConfigResolver
from apps.agent_runtime.contracts import ToolContext, ToolRequest
from apps.agent_runtime.runtime import (
    PUBLIC_CLIENT_FORBIDDEN_TOOL_PERMISSIONS,
    UnifiedRuntimeService,
)
from apps.agent_runtime.tools.executors import SandboxOutputFile
from apps.agent_runtime.tools.registry import default_tool_registry
from apps.agent_runtime.tools.workspace import DjangoWorkspaceBackendToolExecutor
from apps.agent_runtime.tools.workspace_policy import WorkspacePolicy
from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentPermissionBinding,
)
from apps.runtime.models import WorkspaceFile
from apps.user.models import User


class RecordingSandboxExecutor:
    def __init__(self, *, outputs=()):
        self.outputs = tuple(outputs)
        self.staged = ()

    async def stage_files(self, definition, context, files):
        self.staged = tuple(files)
        return tuple(f"/tmp/touming-runs/{context.run_id}/input/{index:03d}-{item.name}" for index, item in enumerate(files))

    async def collect_output_files(self, context):
        return self.outputs


@override_settings(
    AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_BYTES=4 * 1024 * 1024,
    AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES=8 * 1024 * 1024,
    AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT=5,
)
class WorkspaceSandboxToolTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create(
            uid="workspace-sandbox-user",
            username="workspace-sandbox-user",
            created_at="2026-08-26",
            updated_at="2026-08-26",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="workspace-sandbox-company",
            company_name="Workspace Sandbox Company",
            created_at="2026-08-26",
            updated_at="2026-08-26",
        )
        self.registry = default_tool_registry()
        self.context = ToolContext(
            run_id=str(uuid4()),
            tool_call_id=str(uuid4()),
            company_id=self.company.pk,
            agent_id="agent-1",
            user_id=self.user.pk,
            permissions=frozenset({
                "tool.workspace.list",
                "tool.workspace.locate",
                "tool.workspace.read",
                "tool.workspace.checkout",
                "tool.workspace.write",
                "tool.workspace.delete",
                "tool.workspace.move",
            }),
        )

    def _file(self, path, *, payload=b"source", version=1, mime_type="application/octet-stream"):
        return WorkspaceFile.objects.create(
            company=self.company,
            logical_path=path,
            file_name=path.rsplit("/", 1)[-1],
            storage_backend=WorkspaceFile.STORAGE_TENCENT_COS,
            storage_key=f"objects/{path}",
            checksum=hashlib.sha256(payload).hexdigest(),
            mime_type=mime_type,
            size=len(payload),
            version=version,
            sync_status=WorkspaceFile.SYNC_IN_SYNC,
            created_by=str(self.user.pk),
            created_at="2026-08-26",
            updated_at="2026-08-26",
        )

    def _execute(self, executor, name, arguments):
        definition = self.registry.get(name)
        request = ToolRequest(name=name, arguments=arguments, timeout_seconds=definition.timeout_seconds)
        return asyncio.run(executor.execute(definition, request, self.context))

    def test_read_extracts_pdf_text_directly_without_using_the_sandbox(self):
        payload = b"%PDF-test"
        self._file(
            "knowledge/people/resume.pdf",
            payload=payload,
            mime_type="application/pdf",
        )
        sandbox = RecordingSandboxExecutor()
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge/people"]}},
            sandbox_executor=sandbox,
        )

        class Page:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        class Reader:
            is_encrypted = False
            pages = (Page("姓名：刘祉轩"), Page("经历：后端工程师"))

        with (
            patch("apps.agent_runtime.tools.workspace.get_object_bytes", return_value=payload) as get_bytes,
            patch("apps.agent_runtime.tools.workspace.PdfReader", return_value=Reader()),
        ):
            result = self._execute(executor, "workspace.read", {
                "logical_path": "knowledge/people/resume.pdf",
            })

        self.assertTrue(result.success)
        content = json.loads(result.content)
        self.assertEqual(content["encoding"], "pdf-text")
        self.assertEqual(content["page_count"], 2)
        self.assertTrue(content["has_extractable_text"])
        self.assertFalse(content["text_truncated"])
        self.assertIn("姓名：刘祉轩", content["content"])
        self.assertIn("经历：后端工程师", content["content"])
        get_bytes.assert_called_once()
        self.assertEqual(sandbox.staged, ())

    def test_read_folder_routes_to_workspace_list_without_reading_file_content(self):
        self._file("knowledge/people/resume.pdf", payload=b"%PDF-test", mime_type="application/pdf")
        self._file("knowledge/people/notes.txt", payload=b"notes", mime_type="text/plain")
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge/people"]}},
            sandbox_executor=RecordingSandboxExecutor(),
        )

        with patch("apps.agent_runtime.tools.workspace.get_object_bytes") as get_bytes:
            result = self._execute(executor, "workspace.read", {
                "logical_path": "knowledge/people",
            })

        self.assertTrue(result.success)
        content = json.loads(result.content)
        self.assertEqual(content["tool_name"], "workspace.list")
        self.assertEqual(
            [item["logical_path"] for item in content["items"]],
            ["knowledge/people/notes.txt", "knowledge/people/resume.pdf"],
        )
        get_bytes.assert_not_called()

    def test_read_folder_with_file_name_routes_to_workspace_locate(self):
        self._file("knowledge/people/resume.pdf", payload=b"%PDF-test", mime_type="application/pdf")
        self._file("knowledge/people/notes.txt", payload=b"notes", mime_type="text/plain")
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge/people"]}},
            sandbox_executor=RecordingSandboxExecutor(),
        )

        with patch("apps.agent_runtime.tools.workspace.get_object_bytes") as get_bytes:
            result = self._execute(executor, "workspace.read", {
                "logical_path": "knowledge/people",
                "file_name": "resume.pdf",
            })

        self.assertTrue(result.success)
        content = json.loads(result.content)
        self.assertEqual(content["tool_name"], "workspace.locate")
        self.assertEqual(
            [item["logical_path"] for item in content["items"]],
            ["knowledge/people/resume.pdf"],
        )
        get_bytes.assert_not_called()

    @override_settings(AGENT_RUNTIME_WORKSPACE_PDF_MAX_PAGES=1)
    def test_read_rejects_pdf_over_page_limit_without_using_the_sandbox(self):
        payload = b"%PDF-test"
        self._file(
            "knowledge/people/long.pdf",
            payload=payload,
            mime_type="application/pdf",
        )
        sandbox = RecordingSandboxExecutor()
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge/people"]}},
            sandbox_executor=sandbox,
        )
        reader = type("Reader", (), {
            "is_encrypted": False,
            "pages": (object(), object()),
        })()

        with (
            patch("apps.agent_runtime.tools.workspace.get_object_bytes", return_value=payload),
            patch("apps.agent_runtime.tools.workspace.PdfReader", return_value=reader),
        ):
            result = self._execute(executor, "workspace.read", {
                "logical_path": "knowledge/people/long.pdf",
            })

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_INVALID_ARGUMENTS")
        self.assertEqual(sandbox.staged, ())

    def test_checkout_transfers_large_binary_without_returning_bytes_to_model(self):
        payload = b"x" * (2 * 1024 * 1024)
        self._file("knowledge/data.bin", payload=payload)
        sandbox = RecordingSandboxExecutor()
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge"]}},
            sandbox_executor=sandbox,
        )

        with patch("apps.agent_runtime.tools.workspace.get_object_bytes", return_value=payload):
            result = self._execute(executor, "workspace.checkout", {"logical_paths": ["knowledge/data.bin"]})

        self.assertTrue(result.success)
        content = json.loads(result.content)
        self.assertEqual(content["files"][0]["size"], len(payload))
        self.assertIn("/input/000-data.bin", content["files"][0]["sandbox_path"])
        self.assertNotIn(payload[:32].decode(), result.content)
        self.assertEqual(sandbox.staged[0].content, payload)

    def test_checkout_never_resolves_a_file_from_another_company(self):
        other_user = User.objects.create(
            uid="workspace-sandbox-other-user",
            username="workspace-sandbox-other-user",
            created_at="2026-08-26",
            updated_at="2026-08-26",
        )
        other_company = Company.objects.create(
            account=other_user,
            cid="workspace-sandbox-other-company",
            company_name="Other Workspace Sandbox Company",
            created_at="2026-08-26",
            updated_at="2026-08-26",
        )
        WorkspaceFile.objects.create(
            company=other_company,
            logical_path="knowledge/private.bin",
            file_name="private.bin",
            storage_backend=WorkspaceFile.STORAGE_TENCENT_COS,
            storage_key="objects/other-company/private.bin",
            checksum=hashlib.sha256(b"private").hexdigest(),
            mime_type="application/octet-stream",
            size=7,
            version=1,
            sync_status=WorkspaceFile.SYNC_IN_SYNC,
            created_at="2026-08-26",
            updated_at="2026-08-26",
        )
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge"]}},
            sandbox_executor=RecordingSandboxExecutor(),
        )

        with patch("apps.agent_runtime.tools.workspace.get_object_bytes") as get_bytes:
            result = self._execute(executor, "workspace.checkout", {
                "logical_paths": ["knowledge/private.bin"],
            })

        self.assertFalse(result.success)
        get_bytes.assert_not_called()

    def test_checkout_rejects_non_durable_runtime_cache_records(self):
        item = self._file("knowledge/cache.bin")
        item.storage_backend = WorkspaceFile.STORAGE_RUNTIME_CACHE
        item.save(update_fields=["storage_backend"])
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge"]}},
            sandbox_executor=RecordingSandboxExecutor(),
        )

        with patch("apps.agent_runtime.tools.workspace.get_object_bytes") as get_bytes:
            result = self._execute(executor, "workspace.checkout", {
                "logical_paths": ["knowledge/cache.bin"],
            })

        self.assertFalse(result.success)
        get_bytes.assert_not_called()

    def test_write_scope_does_not_grant_read(self):
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["generated"]}},
            sandbox_executor=RecordingSandboxExecutor(),
        )

        result = self._execute(executor, "workspace.list", {})

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_DENIED")

    def test_read_locates_then_reads_exact_file_within_file_scope(self):
        payload = "公司数据业务系统说明".encode("utf-8")
        path = "knowledge/业务说明/公司数据业务系统说明.txt"
        self._file(path, payload=payload)
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": [path]}},
            sandbox_executor=RecordingSandboxExecutor(),
        )

        with patch(
            "apps.agent_runtime.tools.workspace.get_object_bytes",
            return_value=payload,
        ) as get_bytes:
            located = self._execute(executor, "workspace.read", {
                "logical_path": "knowledge/业务说明",
                "file_name": "公司数据业务系统说明.txt",
            })
            self.assertTrue(located.success)
            location = json.loads(located.content)
            self.assertEqual(location["tool_name"], "workspace.locate")
            self.assertEqual(location["logical_path"], "knowledge/业务说明")
            self.assertEqual([item["logical_path"] for item in location["items"]], [path])
            get_bytes.assert_not_called()

            result = self._execute(executor, "workspace.read", {
                "logical_path": location["items"][0]["logical_path"],
            })

        self.assertTrue(result.success)
        content = json.loads(result.content)
        self.assertEqual(content["tool_name"], "workspace.read")
        self.assertEqual(content["logical_path"], path)
        self.assertEqual(content["content"], payload.decode("utf-8"))
        get_bytes.assert_called_once()
        self.assertEqual(get_bytes.call_args.args[0], f"objects/{path}")

    def test_publish_creates_versioned_workspace_file_from_sandbox_output(self):
        sandbox = RecordingSandboxExecutor(outputs=(
            SandboxOutputFile(
                name="report.csv",
                content=b"a,b\n1,2\n",
                content_type="text/csv",
                artifact_ref="artifact://test",
            ),
        ))
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["generated/reports"]}},
            sandbox_executor=sandbox,
        )

        with patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object:
            result = self._execute(executor, "workspace.publish", {
                "files": [{
                    "sandbox_path": "../output/report.csv",
                    "logical_path": "generated/reports/report.csv",
                    "mode": "create",
                }],
            })

        self.assertTrue(result.success)
        item = WorkspaceFile.objects.get(company=self.company, logical_path="generated/reports/report.csv")
        self.assertEqual(item.version, 1)
        self.assertEqual(item.checksum, hashlib.sha256(b"a,b\n1,2\n").hexdigest())
        put_object.assert_called_once()

    def test_publish_matches_unicode_sandbox_output_name(self):
        name = "公司数据业务系统说明.txt"
        sandbox = RecordingSandboxExecutor(outputs=(
            SandboxOutputFile(name, b"updated", "text/plain", "artifact://test"),
        ))
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["knowledge/业务说明"]}},
            sandbox_executor=sandbox,
        )

        with patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object:
            result = self._execute(executor, "workspace.publish", {
                "files": [{
                    "sandbox_path": name,
                    "logical_path": f"knowledge/业务说明/{name}",
                    "mode": "create",
                }],
            })

        self.assertTrue(result.success)
        self.assertTrue(WorkspaceFile.objects.filter(
            company=self.company,
            logical_path=f"knowledge/业务说明/{name}",
        ).exists())
        put_object.assert_called_once()

    def test_folder_write_scope_atomically_allows_create_and_replace(self):
        self._file("knowledge/reports/existing.txt", payload=b"old", version=3)
        sandbox = RecordingSandboxExecutor(outputs=(
            SandboxOutputFile("existing.txt", b"updated", "text/plain", "artifact://existing"),
            SandboxOutputFile("new.txt", b"created", "text/plain", "artifact://new"),
        ))
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["knowledge/reports"]}},
            sandbox_executor=sandbox,
        )

        with patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object:
            result = self._execute(executor, "workspace.publish", {
                "files": [
                    {
                        "sandbox_path": "../output/existing.txt",
                        "logical_path": "knowledge/reports/existing.txt",
                        "mode": "replace",
                        "if_match_version": 3,
                    },
                    {
                        "sandbox_path": "../output/new.txt",
                        "logical_path": "knowledge/reports/new.txt",
                        "mode": "create",
                    },
                ],
            })

        self.assertTrue(result.success)
        existing = WorkspaceFile.objects.get(
            company=self.company,
            logical_path="knowledge/reports/existing.txt",
        )
        created = WorkspaceFile.objects.get(
            company=self.company,
            logical_path="knowledge/reports/new.txt",
        )
        self.assertEqual(existing.version, 4)
        self.assertEqual(existing.checksum, hashlib.sha256(b"updated").hexdigest())
        self.assertEqual(created.version, 1)
        self.assertEqual(created.checksum, hashlib.sha256(b"created").hexdigest())
        self.assertEqual(put_object.call_count, 2)

    def test_publish_rejects_absolute_or_non_output_sandbox_paths(self):
        sandbox = RecordingSandboxExecutor(outputs=(
            SandboxOutputFile("report.txt", b"content", "text/plain", "artifact://test"),
        ))
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["generated"]}},
            sandbox_executor=sandbox,
        )

        for sandbox_path in ("/tmp/report.txt", "output/report.txt", "../work/report.txt"):
            with self.subTest(sandbox_path=sandbox_path):
                with patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object:
                    result = self._execute(executor, "workspace.publish", {
                        "files": [{
                            "sandbox_path": sandbox_path,
                            "logical_path": "generated/report.txt",
                            "mode": "create",
                        }],
                    })

                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "TOOL_INVALID_ARGUMENTS")
                put_object.assert_not_called()

    def test_file_write_scope_does_not_allow_creating_a_sibling_file(self):
        self._file("knowledge/reports/existing.txt", payload=b"old", version=1)
        sandbox = RecordingSandboxExecutor(outputs=(
            SandboxOutputFile("new.txt", b"created", "text/plain", "artifact://new"),
        ))
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["knowledge/reports/existing.txt"]}},
            sandbox_executor=sandbox,
        )

        with patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object:
            result = self._execute(executor, "workspace.publish", {
                "files": [{
                    "sandbox_path": "new.txt",
                    "logical_path": "knowledge/reports/new.txt",
                    "mode": "create",
                }],
            })

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_DENIED")
        self.assertFalse(WorkspaceFile.objects.filter(
            company=self.company,
            logical_path="knowledge/reports/new.txt",
        ).exists())
        put_object.assert_not_called()

    def test_replace_rejects_stale_version_before_writing_cos(self):
        self._file("generated/report.txt", payload=b"old", version=4)
        sandbox = RecordingSandboxExecutor(outputs=(
            SandboxOutputFile("report.txt", b"new", "text/plain", "artifact://test"),
        ))
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["generated"]}},
            sandbox_executor=sandbox,
        )

        with patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object:
            result = self._execute(executor, "workspace.publish", {
                "files": [{
                    "sandbox_path": "report.txt",
                    "logical_path": "generated/report.txt",
                    "mode": "replace",
                    "if_match_version": 3,
                }],
            })

        self.assertFalse(result.success)
        put_object.assert_not_called()

    def test_delete_requires_explicit_delete_scope_and_current_version(self):
        self._file("generated/tmp.txt", version=2)
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"write": ["generated"], "delete": []}},
            sandbox_executor=RecordingSandboxExecutor(),
        )

        with patch("apps.agent_runtime.tools.workspace.delete_object") as delete_object_mock:
            result = self._execute(executor, "workspace.delete", {
                "logical_paths": ["generated/tmp.txt"],
                "if_match_versions": {"generated/tmp.txt": 2},
            })

        self.assertFalse(result.success)
        self.assertTrue(WorkspaceFile.objects.filter(company=self.company, logical_path="generated/tmp.txt").exists())
        delete_object_mock.assert_not_called()

    def test_read_denial_filters_list_locate_and_read_without_fetching_denied_content(self):
        self._file("knowledge/allowed.txt")
        self._file("knowledge/restricted/private.txt")
        self._file("knowledge/restricted-extra/public.txt")
        for deny_field in ("read_deny", "deny"):
            executor = DjangoWorkspaceBackendToolExecutor(
                company_id=self.company.pk,
                tool_policy={"knowledge": {"read": ["knowledge"], deny_field: ["knowledge/restricted"]}},
            )
            with self.subTest(deny_field=deny_field), patch("apps.agent_runtime.tools.workspace.get_object_bytes", return_value=b"source") as get_object:
                listed = self._execute(executor, "workspace.list", {"logical_path": "knowledge"})
                located = self._execute(executor, "workspace.locate", {"logical_path": "knowledge"})
                directory = self._execute(executor, "workspace.list", {"logical_path": "knowledge/restricted"})
                read = self._execute(executor, "workspace.read", {"logical_path": "knowledge/restricted/private.txt"})
                by_name = self._execute(executor, "workspace.read", {"file_name": "private.txt"})

                self.assertTrue(listed.success)
                self.assertEqual(
                    {item["logical_path"] for item in json.loads(listed.content)["items"]},
                    {"knowledge/allowed.txt", "knowledge/restricted-extra"},
                )
                self.assertTrue(located.success)
                self.assertEqual(
                    {item["logical_path"] for item in json.loads(located.content)["items"]},
                    {"knowledge/allowed.txt", "knowledge/restricted-extra/public.txt"},
                )
                self.assertFalse(directory.success)
                self.assertEqual(directory.error_code, "TOOL_DENIED")
                self.assertFalse(read.success)
                self.assertFalse(by_name.success)
                get_object.assert_not_called()

    def test_read_denial_prevents_checkout_before_fetch_or_sandbox_transfer(self):
        path = "knowledge/restricted/private.txt"
        self._file(path)
        sandbox = RecordingSandboxExecutor()
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {"read": ["knowledge"], "read_deny": ["knowledge/restricted"]}},
            sandbox_executor=sandbox,
        )
        with patch("apps.agent_runtime.tools.workspace.get_object_bytes", return_value=b"source") as get_object:
            result = self._execute(executor, "workspace.checkout", {"logical_paths": [path]})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_DENIED")
        self.assertEqual(sandbox.staged, ())
        get_object.assert_not_called()

    def test_publish_denials_reject_entire_batch_before_create_or_replace(self):
        existing = self._file("knowledge/restricted/existing.txt", version=3)
        sandbox = RecordingSandboxExecutor(outputs=(
            SandboxOutputFile("safe.txt", b"safe", "text/plain", "artifact://safe"),
            SandboxOutputFile("changed.txt", b"changed", "text/plain", "artifact://changed"),
        ))
        for mode in ("create", "replace"):
            target = existing.logical_path if mode == "replace" else "knowledge/restricted/new.txt"
            for deny_field in ("write_deny", "read_deny", "deny"):
                for deny_scope in ("knowledge/restricted", target):
                    with self.subTest(mode=mode, deny_field=deny_field, deny_scope=deny_scope):
                        executor = DjangoWorkspaceBackendToolExecutor(
                            company_id=self.company.pk,
                            tool_policy={"knowledge": {"write": ["knowledge"], deny_field: [deny_scope]}},
                            sandbox_executor=sandbox,
                        )
                        with patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object:
                            result = self._execute(executor, "workspace.publish", {"files": [
                                {"sandbox_path": "../output/safe.txt", "logical_path": "knowledge/safe.txt", "mode": "create"},
                                {"sandbox_path": "../output/changed.txt", "logical_path": target, "mode": mode, "if_match_version": 3},
                            ]})
                        self.assertFalse(result.success)
                        self.assertEqual(result.error_code, "TOOL_DENIED")
                        put_object.assert_not_called()
                        self.assertEqual(WorkspaceFile.objects.filter(company=self.company).count(), 1)
                        existing.refresh_from_db()
                        self.assertEqual(existing.version, 3)

    def test_delete_denial_preserves_file_and_storage_object(self):
        item = self._file("knowledge/restricted/private.txt", version=2)
        for deny_field in ("delete_deny", "deny"):
            for deny_scope in ("knowledge/restricted", item.logical_path):
                with self.subTest(deny_field=deny_field, deny_scope=deny_scope):
                    executor = DjangoWorkspaceBackendToolExecutor(
                        company_id=self.company.pk,
                        tool_policy={"knowledge": {"delete": ["knowledge"], deny_field: [deny_scope]}},
                    )
                    with patch("apps.agent_runtime.tools.workspace.delete_object") as delete_object_mock:
                        result = self._execute(executor, "workspace.delete", {
                            "logical_paths": [item.logical_path],
                            "if_match_versions": {item.logical_path: 2},
                        })
                    self.assertFalse(result.success)
                    self.assertEqual(result.error_code, "TOOL_DENIED")
                    self.assertTrue(WorkspaceFile.objects.filter(pk=item.pk).exists())
                    delete_object_mock.assert_not_called()

    def test_move_denials_protect_source_and_destination_before_storage_access(self):
        item = self._file("knowledge/source/item.txt", version=2)
        destination = "knowledge/destination/item.txt"
        cases = (
            ("delete_deny", "knowledge/source"),
            ("read_deny", "knowledge/source"),
            ("write_deny", "knowledge/destination"),
            ("read_deny", "knowledge/destination"),
            ("deny", "knowledge/source"),
            ("deny", "knowledge/destination"),
        )
        for deny_field, deny_scope in cases:
            with self.subTest(deny_field=deny_field, deny_scope=deny_scope):
                executor = DjangoWorkspaceBackendToolExecutor(
                    company_id=self.company.pk,
                    tool_policy={"knowledge": {
                        "read": ["knowledge"], "write": ["knowledge"], "delete": ["knowledge"],
                        deny_field: [deny_scope],
                    }},
                )
                with (
                    patch("apps.agent_runtime.tools.workspace.get_object_bytes") as get_object,
                    patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object,
                    patch("apps.agent_runtime.tools.workspace.delete_object") as delete_object_mock,
                ):
                    result = self._execute(executor, "workspace.move", {
                        "source_path": item.logical_path, "destination_path": destination, "if_match_version": 2,
                    })
                self.assertFalse(result.success)
                self.assertEqual(result.error_code, "TOOL_DENIED")
                get_object.assert_not_called()
                put_object.assert_not_called()
                delete_object_mock.assert_not_called()
                item.refresh_from_db()
                self.assertEqual(item.logical_path, "knowledge/source/item.txt")
                self.assertEqual(item.version, 2)

    def test_move_preserves_independent_source_write_and_destination_delete_denials(self):
        item = self._file("knowledge/source/item.txt", version=2)
        destination = "knowledge/destination/item.txt"
        executor = DjangoWorkspaceBackendToolExecutor(
            company_id=self.company.pk,
            tool_policy={"knowledge": {
                "write": ["knowledge/destination"], "delete": ["knowledge/source"],
                "write_deny": ["knowledge/source"], "delete_deny": ["knowledge/destination"],
            }},
        )
        with (
            patch("apps.agent_runtime.tools.workspace.get_object_bytes", return_value=b"source"),
            patch("apps.agent_runtime.tools.workspace.put_object_with_key") as put_object,
            patch("apps.agent_runtime.tools.workspace.delete_object") as delete_object_mock,
        ):
            result = self._execute(executor, "workspace.move", {
                "source_path": item.logical_path, "destination_path": destination, "if_match_version": 2,
            })
        self.assertTrue(result.success)
        item.refresh_from_db()
        self.assertEqual(item.logical_path, destination)
        self.assertEqual(item.version, 3)
        put_object.assert_called_once()
        delete_object_mock.assert_called_once()

    def test_saved_action_denials_reach_the_workspace_executor(self):
        company = TransparentCompany.objects.create(company=self.company)
        agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=company, agentId="deny-agent", name="Deny Agent", enabled=True, status="active",
        )
        TransparentCompanyAgentPermissionBinding.objects.create(
            transparentCompany=company, agent=agent, enabled=True, status="active",
            knowledgeRead=["knowledge"], knowledgeWrite=["knowledge"], knowledgeDelete=["knowledge"],
            knowledgeReadDeny=["knowledge/private"], knowledgeWriteDeny=["knowledge/readonly"],
            knowledgeDeleteDeny=["knowledge/retained"],
        )
        policy = ConfigResolver()._resolve_tool_policy(company, agent)
        executor = DjangoWorkspaceBackendToolExecutor(company_id=self.company.pk, tool_policy=policy)
        self.assertFalse(executor.policy.allows_read_file("knowledge/private/a.txt"))
        self.assertFalse(executor.policy.allows_write_file("knowledge/private/a.txt"))
        self.assertTrue(executor.policy.allows_read_file("knowledge/readonly/a.txt"))
        self.assertFalse(executor.policy.allows_write_file("knowledge/readonly/a.txt"))
        self.assertFalse(executor.policy.allows_delete_file("knowledge/retained/a.txt"))
        self.assertTrue(executor.policy.allows_write_file("knowledge/retained/a.txt"))


class WorkspaceActionDenyPolicyTests(SimpleTestCase):
    def test_public_entry_ceiling_removes_mutation_without_changing_agent_test_policy(self):
        runtime = UnifiedRuntimeService()
        configured = {"knowledge": {
            "read": ["knowledge"],
            "write": ["knowledge"],
            "delete": ["knowledge"],
        }}

        public_policy = runtime._effective_tool_policy(configured, source="public_client")
        public_permissions = runtime._tool_permissions(public_policy, source="public_client")
        agent_test_permissions = runtime._tool_permissions(configured, source="agent_test")

        self.assertEqual(configured["knowledge"]["write"], ["knowledge"])
        self.assertEqual(configured["knowledge"]["delete"], ["knowledge"])
        self.assertEqual(public_policy["knowledge"]["write"], [])
        self.assertEqual(public_policy["knowledge"]["delete"], [])
        self.assertEqual(
            public_permissions,
            frozenset({"tool.workspace.list", "tool.workspace.locate", "tool.workspace.read"}),
        )
        self.assertTrue(PUBLIC_CLIENT_FORBIDDEN_TOOL_PERMISSIONS <= agent_test_permissions)

    def test_action_denials_override_parent_grants_without_disabling_other_actions(self):
        for field, expected in (
            ("read_deny", (False, False, False, True)),
            ("write_deny", (True, False, False, True)),
            ("delete_deny", (True, True, True, False)),
            ("deny", (False, False, False, False)),
        ):
            for scope in ("knowledge/private", "knowledge/private/a.txt", "*"):
                with self.subTest(field=field, scope=scope):
                    policy = WorkspacePolicy.from_config({"knowledge": {
                        "read": ["knowledge"], "write": ["knowledge"], "delete": ["knowledge"], field: [scope],
                    }})
                    self.assertEqual((
                        policy.allows_read_file("knowledge/private/a.txt"),
                        policy.allows_write_file("knowledge/private/a.txt"),
                        policy.allows_create_file("knowledge/private/a.txt"),
                        policy.allows_delete_file("knowledge/private/a.txt"),
                    ), expected)
                    if scope != "*":
                        self.assertTrue(policy.allows_read_directory("knowledge"))
                        self.assertTrue(policy.allows_write_file("knowledge/private-extra/a.txt"))
                    else:
                        self.assertEqual(policy.allows_read_directory(""), expected[0])

    def test_runtime_hides_fully_denied_tools_but_keeps_partially_allowed_tools(self):
        runtime = UnifiedRuntimeService()
        scopes = {"read": ["knowledge"], "write": ["knowledge"], "delete": ["knowledge"]}
        all_permissions = runtime._tool_permissions({"knowledge": scopes})
        read_permissions = {"tool.workspace.list", "tool.workspace.locate", "tool.workspace.read", "tool.workspace.checkout"}
        for field, removed in (
            ("read_deny", read_permissions | {"tool.workspace.write", "tool.workspace.move"}),
            ("write_deny", {"tool.workspace.write", "tool.workspace.move"}),
            ("delete_deny", {"tool.workspace.delete", "tool.workspace.move"}),
            ("deny", set(all_permissions) - {"tool.code.execute"}),
        ):
            for denied in ("knowledge", "*"):
                with self.subTest(field=field, denied=denied):
                    self.assertEqual(
                        runtime._tool_permissions({"knowledge": {**scopes, field: [denied]}}),
                        all_permissions - removed,
                    )
            with self.subTest(field=field, denied="partial"):
                self.assertEqual(
                    runtime._tool_permissions({"knowledge": {**scopes, field: ["knowledge/private"]}}),
                    all_permissions,
                )

    def test_runtime_does_not_offer_move_for_explicitly_unreadable_sources(self):
        permissions = UnifiedRuntimeService()._tool_permissions({"knowledge": {
            "write": ["knowledge/destination"],
            "delete": ["knowledge/source"],
            "read_deny": ["knowledge/source"],
        }})
        self.assertIn("tool.workspace.write", permissions)
        self.assertIn("tool.workspace.delete", permissions)
        self.assertNotIn("tool.workspace.move", permissions)
