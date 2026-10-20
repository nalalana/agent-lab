import json
import time
from unittest.mock import patch

from django.test import TestCase

from apps.agent_runtime.models import KnowledgeFolder
from apps.company.models import Company
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.runtime.models import OpenClawInstance, WorkspaceFile
from apps.user.models import User
from common.utils.Login_utils import sign_token


class CompanyPermissionWorkspaceTreeApiTests(TestCase):
    maxDiff = None

    def setUp(self):
        now = str(time.time())[:20]
        self.admin_user = User.objects.create(
            uid="user-admin",
            username="admin",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.viewer_user = User.objects.create(
            uid="user-viewer",
            username="viewer",
            created_at=now,
            updated_at=now,
            role="user",
        )
        self.company = Company.objects.create(
            account=self.admin_user,
            cid="company-permission-tree",
            company_name="Permission Tree Inc",
            status=Company.COMPANY_STATUS_TRANSPARENT,
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            created_at=now,
            updated_at=now,
        )
        self.instance = OpenClawInstance.objects.create(
            company=self.company,
            provider=OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST,
            runtime_namespace="tsds-company-permission-tree",
            status=OpenClawInstance.STATUS_RUNNING,
            openclaw_status=OpenClawInstance.OPENCLAW_STATUS_HEALTHY,
            config_status=OpenClawInstance.CONFIG_STATUS_IN_SYNC,
            created_at=now,
            updated_at=now,
        )
        self.workspace_zip = WorkspaceFile.objects.create(
            company=self.company,
            instance=self.instance,
            logical_path="knowledge/1775028071209-workspace.zip",
            file_name="1775028071209-workspace.zip",
            storage_backend=WorkspaceFile.STORAGE_TENCENT_COS,
            storage_key="knowledge/1775028071209-workspace.zip",
            sync_status=WorkspaceFile.SYNC_IN_SYNC,
            created_by=self.admin_user.uid,
            created_at=now,
            updated_at=now,
        )
        self.report_pdf = WorkspaceFile.objects.create(
            company=self.company,
            instance=self.instance,
            logical_path="knowledge/reports/q1.pdf",
            file_name="q1.pdf",
            storage_backend=WorkspaceFile.STORAGE_TENCENT_COS,
            storage_key="knowledge/reports/q1.pdf",
            sync_status=WorkspaceFile.SYNC_PENDING,
            created_by=self.admin_user.uid,
            created_at=now,
            updated_at=now,
        )

        principal = get_or_create_principal(Principal.TYPE_USER, self.admin_user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.admin_user)

    def _token(self, user: User) -> str:
        return sign_token({"user_id": user.id})

    def _get(self, user: User, query: str):
        return self.client.get(
            f"/api/company/permission-config/workspace-tree?cid={self.company.cid}{query}",
            HTTP_ACCESS_TOKEN=self._token(user),
        )

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.list_workspace_tree")
    def test_admin_can_access_workspace_tree_and_see_known_logical_path(self, mock_list_workspace_tree):
        mock_list_workspace_tree.return_value = (
            True,
            {
                "status": "healthy",
                "source": "shared_host_api",
                "stderr": "permission denied: /data/tsds/tenants/demo/workspace",
                "items": [
                    {
                        "logical_path": "knowledge/1775028071209-workspace.zip",
                        "path": "/data/tsds/tenants/demo/workspace/knowledge/1775028071209-workspace.zip",
                        "name": "1775028071209-workspace.zip",
                        "kind": "file",
                    },
                ],
            },
        )

        response = self._get(self.admin_user, "&path=knowledge")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result"], 1)
        self.assertEqual(payload["data"]["logical_path"], "knowledge")
        self.assertIn(
            {
                "name": "1775028071209-workspace.zip",
                "logical_path": "knowledge/1775028071209-workspace.zip",
                "kind": "file",
                "runtime_visible": True,
                "sync_status": WorkspaceFile.SYNC_IN_SYNC,
            },
            payload["data"]["items"],
        )
        self.assertTrue(payload["data"]["runtime_visible"])
        self.assertEqual(payload["data"]["runtime_source"], "shared_host_api")

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.list_workspace_tree")
    def test_non_admin_is_denied(self, mock_list_workspace_tree):
        response = self._get(self.viewer_user, "")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["result"], 0)
        mock_list_workspace_tree.assert_not_called()

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.list_workspace_tree")
    def test_response_never_leaks_host_paths_or_stderr(self, mock_list_workspace_tree):
        mock_list_workspace_tree.return_value = (
            True,
            {
                "status": "healthy",
                "stderr": "sh: 1: /srv/openclaw-home/demo: Permission denied",
                "items": [
                    {
                        "logical_path": "knowledge/1775028071209-workspace.zip",
                        "path": "/data/tsds/tenants/demo/workspace/knowledge/1775028071209-workspace.zip",
                        "host_path": "/srv/openclaw-home/demo/knowledge/1775028071209-workspace.zip",
                        "kind": "file",
                    }
                ],
            },
        )

        response = self._get(self.admin_user, "&path=knowledge")

        self.assertEqual(response.status_code, 200)
        response_text = json.dumps(response.json(), ensure_ascii=False)
        self.assertNotIn("/data/", response_text)
        self.assertNotIn("/srv/openclaw-home/", response_text)
        self.assertNotIn("stderr", response_text)

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.list_workspace_tree")
    def test_recursive_false_returns_only_immediate_children(self, mock_list_workspace_tree):
        mock_list_workspace_tree.return_value = (
            True,
            {
                "status": "healthy",
                "items": [
                    {"logical_path": "knowledge/reports", "kind": "directory"},
                    {"logical_path": "knowledge/1775028071209-workspace.zip", "kind": "file"},
                    {"logical_path": "knowledge/reports/q1.pdf", "kind": "file"},
                ],
            },
        )

        response = self._get(self.admin_user, "&path=knowledge&recursive=false")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["data"]["items"],
            [
                {
                    "name": "reports",
                    "logical_path": "knowledge/reports",
                    "kind": "directory",
                    "runtime_visible": True,
                },
                {
                    "name": "1775028071209-workspace.zip",
                    "logical_path": "knowledge/1775028071209-workspace.zip",
                    "kind": "file",
                    "runtime_visible": True,
                    "sync_status": WorkspaceFile.SYNC_IN_SYNC,
                },
            ],
        )
        self.assertNotIn("children", payload["data"]["items"][0])

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.list_workspace_tree")
    def test_recursive_true_returns_nested_children(self, mock_list_workspace_tree):
        mock_list_workspace_tree.return_value = (
            True,
            {
                "status": "healthy",
                "items": [
                    {"logical_path": "knowledge/reports", "kind": "directory"},
                    {"logical_path": "knowledge/1775028071209-workspace.zip", "kind": "file"},
                    {"logical_path": "knowledge/reports/q1.pdf", "kind": "file"},
                ],
            },
        )

        response = self._get(self.admin_user, "&path=knowledge&recursive=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["data"]["items"],
            [
                {
                    "name": "reports",
                    "logical_path": "knowledge/reports",
                    "kind": "directory",
                    "runtime_visible": True,
                    "children": [
                        {
                            "name": "q1.pdf",
                            "logical_path": "knowledge/reports/q1.pdf",
                            "kind": "file",
                            "runtime_visible": True,
                            "sync_status": WorkspaceFile.SYNC_PENDING,
                        }
                    ],
                },
                {
                    "name": "1775028071209-workspace.zip",
                    "logical_path": "knowledge/1775028071209-workspace.zip",
                    "kind": "file",
                    "runtime_visible": True,
                    "sync_status": WorkspaceFile.SYNC_IN_SYNC,
                },
            ],
        )

    def test_recursive_tree_includes_persisted_empty_knowledge_folder(self):
        KnowledgeFolder.objects.create(
            company=self.company,
            logical_path="knowledge/empty-folder",
            created_by=self.admin_user.uid,
        )

        response = self._get(self.admin_user, "&path=knowledge&recursive=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        empty_folder = next(
            item
            for item in payload["data"]["items"]
            if item["logical_path"] == "knowledge/empty-folder"
        )
        self.assertEqual(empty_folder["name"], "empty-folder")
        self.assertEqual(empty_folder["kind"], "directory")
        self.assertTrue(empty_folder["runtime_visible"])
        self.assertNotIn("children", empty_folder)

    @patch("apps.runtime.services.DockerSharedHostProviderAdapter.list_workspace_tree")
    def test_recursive_tree_preserves_unzipped_workspace_descendants(self, mock_list_workspace_tree):
        unzipped_file = (
            "knowledge/1775028071209-workspace/workspace/public/company/company_profile.md"
        )
        mock_list_workspace_tree.return_value = (
            True,
            {
                "status": "healthy",
                "items": [
                    {
                        "logical_path": "knowledge/1775028071209-workspace",
                        "kind": "directory",
                    },
                    {
                        "logical_path": "knowledge/1775028071209-workspace/workspace",
                        "kind": "directory",
                    },
                    {
                        "logical_path": "knowledge/1775028071209-workspace/workspace/public",
                        "kind": "directory",
                    },
                    {
                        "logical_path": "knowledge/1775028071209-workspace/workspace/public/company",
                        "kind": "directory",
                    },
                    {
                        "logical_path": unzipped_file,
                        "kind": "file",
                    },
                    {
                        "path": "/data/tsds/tenants/demo/workspace/knowledge/host-path-fallback.md",
                        "kind": "file",
                    },
                    {
                        "logical_path": "knowledge/../escaped.md",
                        "kind": "file",
                    },
                ],
            },
        )

        response = self._get(self.admin_user, "&path=knowledge&recursive=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        def flatten(items):
            paths = []
            for item in items:
                paths.append(item["logical_path"])
                paths.extend(flatten(item.get("children") or []))
            return paths

        flattened_paths = flatten(payload["data"]["items"])
        self.assertIn(unzipped_file, flattened_paths)
        self.assertIn("knowledge/host-path-fallback.md", flattened_paths)
        self.assertNotIn("knowledge/escaped.md", flattened_paths)

        top_item = payload["data"]["items"][0]
        self.assertEqual(top_item["logical_path"], "knowledge/1775028071209-workspace")
        self.assertEqual(top_item["children"][0]["logical_path"], "knowledge/1775028071209-workspace/workspace")
