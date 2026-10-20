from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from apps.runtime.serializers import (
    RuntimeWorkspaceFileRenameSerializer,
    RuntimeWorkspaceUploadFileSerializer,
    RuntimeWorkspaceUploadTicketSerializer,
)


class RuntimeWorkspaceUploadSecurityTests(SimpleTestCase):
    def _file_payload(self, logical_path: str):
        return {
            "cid": "company-1",
            "logical_path": logical_path,
            "file": SimpleUploadedFile("readme.md", b"# hello", content_type="text/markdown"),
        }

    def test_upload_file_accepts_knowledge_logical_path(self):
        serializer = RuntimeWorkspaceUploadFileSerializer(
            data=self._file_payload("knowledge/readme.md")
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["logical_path"], "knowledge/readme.md")

    def test_upload_file_rejects_path_traversal(self):
        serializer = RuntimeWorkspaceUploadFileSerializer(
            data=self._file_payload("../config/openclaw.json")
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("logical_path", serializer.errors)

    def test_upload_file_rejects_absolute_runtime_path(self):
        serializer = RuntimeWorkspaceUploadFileSerializer(
            data=self._file_payload("/data/tsds/tenants/demo/workspace/knowledge/readme.md")
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("logical_path", serializer.errors)

    def test_upload_ticket_rejects_nested_file_name(self):
        serializer = RuntimeWorkspaceUploadTicketSerializer(
            data={
                "cid": "company-1",
                "logical_path": "knowledge/readme.md",
                "file_name": "../readme.md",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("file_name", serializer.errors)

    def test_rename_rejects_runtime_internal_path(self):
        serializer = RuntimeWorkspaceFileRenameSerializer(
            data={
                "cid": "company-1",
                "file_id": 1,
                "new_logical_path": "config/openclaw.json",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("new_logical_path", serializer.errors)
