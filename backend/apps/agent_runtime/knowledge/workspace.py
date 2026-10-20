"""Company-scoped knowledge workspace backed by COS and LangGraph services."""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
from pathlib import PurePosixPath

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.agent_runtime.knowledge.services import KnowledgeIndexService
from apps.agent_runtime.models import KnowledgeFolder
from apps.runtime.models import WorkspaceFile
from common.utils.Tengcentcos_utils import _get_cos_client_and_bucket, normalize_cos_key, put_object_with_key

KNOWLEDGE_ROOT = "knowledge"


def normalize_path(value: str, *, allow_root: bool = True) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        normalized = KNOWLEDGE_ROOT
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("知识库路径不合法")
    if normalized != KNOWLEDGE_ROOT and not normalized.startswith(f"{KNOWLEDGE_ROOT}/"):
        raise ValueError("知识库路径必须位于 knowledge 目录")
    if not allow_root and normalized == KNOWLEDGE_ROOT:
        raise ValueError("不能修改知识库根目录")
    if len(normalized) > 512:
        raise ValueError("知识库路径过长")
    return normalized


def list_tree(company, logical_path: str = KNOWLEDGE_ROOT, recursive: bool = True) -> dict:
    base = normalize_path(logical_path)
    nodes: dict[str, dict] = {}

    def ensure(path: str, kind: str = "directory", **extra):
        current = nodes.setdefault(
            path,
            {
                "name": PurePosixPath(path).name,
                "logical_path": path,
                "kind": kind,
                "runtime_visible": True,
                "children": [],
            },
        )
        if kind == "file":
            current["kind"] = "file"
        current.update({key: value for key, value in extra.items() if value not in (None, "")})
        return current

    folders = KnowledgeFolder.objects.filter(company=company).order_by("logical_path")
    files = WorkspaceFile.objects.filter(company=company).filter(
        models.Q(logical_path=KNOWLEDGE_ROOT)
        | models.Q(logical_path__startswith=f"{KNOWLEDGE_ROOT}/")
    ).order_by("logical_path", "id")
    paths = list(folders.values_list("logical_path", flat=True)) + list(
        files.values_list("logical_path", flat=True)
    )
    for path in paths:
        if path != base and not path.startswith(f"{base}/"):
            continue
        parts = path.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor == base or ancestor.startswith(f"{base}/"):
                ensure(ancestor)
    for folder in folders:
        path = normalize_path(folder.logical_path)
        if path == base or path.startswith(f"{base}/"):
            ensure(path, updated_at=folder.updated_at.isoformat())
    for item in files:
        path = normalize_path(item.logical_path)
        if path == base or path.startswith(f"{base}/"):
            ensure(
                path,
                "file",
                size=item.size,
                updated_at=item.updated_at,
                sync_status=item.resource_sync_status,
            )

    for path, node in nodes.items():
        parent = posixpath.dirname(path)
        if parent in nodes:
            nodes[parent]["children"].append(node)
    sort_key = lambda item: (
        0 if item["kind"] == "directory" else 1,
        item["name"].lower(),
    )
    for node in nodes.values():
        node["children"].sort(key=sort_key)
        if not recursive or not node.get("children"):
            node.pop("children", None)
    items = [
        node for path, node in nodes.items() if posixpath.dirname(path) == base
    ]
    items.sort(key=sort_key)
    return {
        "cid": company.cid,
        "logical_path": base,
        "recursive": bool(recursive),
        "runtime_visible": True,
        "runtime_source": "langgraph_native",
        "items": items,
    }


def create_folder(company, logical_path: str, created_by: str = "") -> dict:
    path = normalize_path(logical_path, allow_root=False)
    folder, created = KnowledgeFolder.objects.get_or_create(
        company=company,
        logical_path=path,
        defaults={"created_by": str(created_by or "")[:128]},
    )
    return {"logical_path": folder.logical_path, "created": created}


def open_file(company, logical_path: str):
    """Open one company-owned knowledge file from COS for preview or download."""
    path = normalize_path(logical_path, allow_root=False)
    workspace_file = (
        WorkspaceFile.objects.filter(company=company, logical_path=path)
        .order_by("-id")
        .first()
    )
    if workspace_file is None:
        raise FileNotFoundError("知识文件不存在")
    if (
        workspace_file.storage_backend != WorkspaceFile.STORAGE_TENCENT_COS
        or not workspace_file.storage_key
    ):
        raise ValueError("知识文件暂不支持预览或下载")

    client, bucket_name = _get_cos_client_and_bucket()
    response = client.get_object(
        Bucket=bucket_name,
        Key=normalize_cos_key(workspace_file.storage_key),
    )
    return workspace_file, response["Body"]


@transaction.atomic
def upload_file(company, uploaded_file, logical_path: str, created_by: str = "") -> dict:
    path = normalize_path(logical_path, allow_root=False)
    file_name = PurePosixPath(path).name
    max_size = int(getattr(settings, "KNOWLEDGE_UPLOAD_MAX_BYTES", 50 * 1024 * 1024))
    if uploaded_file.size > max_size:
        raise ValueError("文件不能超过 50MB")
    content = uploaded_file.read()
    checksum = hashlib.sha256(content).hexdigest()
    storage_key = f"agent-runtime/{company.cid}/{path}"
    mime_type = (
        uploaded_file.content_type
        or mimetypes.guess_type(file_name)[0]
        or "application/octet-stream"
    )
    put_object_with_key(content, storage_key, mime_type)
    now = str(timezone.now().timestamp())[:20]
    workspace_file = (
        WorkspaceFile.objects.filter(company=company, logical_path=path)
        .order_by("-id")
        .first()
    )
    values = {
        "instance": None,
        "file_name": file_name,
        "storage_backend": WorkspaceFile.STORAGE_TENCENT_COS,
        "storage_key": storage_key,
        "checksum": checksum,
        "mime_type": mime_type,
        "size": len(content),
        "sync_status": WorkspaceFile.SYNC_IN_SYNC,
        "resource_sync_status": WorkspaceFile.RESOURCE_SYNC_PENDING,
        "resource_last_error": "",
        "created_by": str(created_by or "")[:128],
        "updated_at": now,
    }
    if workspace_file is None:
        workspace_file = WorkspaceFile.objects.create(
            company=company, logical_path=path, created_at=now, **values
        )
    else:
        for key, value in values.items():
            setattr(workspace_file, key, value)
        workspace_file.version = int(workspace_file.version or 1) + 1
        workspace_file.save(update_fields=[*values, "version"])
    parent = posixpath.dirname(path)
    if parent != KNOWLEDGE_ROOT:
        KnowledgeFolder.objects.get_or_create(
            company=company,
            logical_path=parent,
            defaults={"created_by": str(created_by or "")[:128]},
        )
    job = KnowledgeIndexService().enqueue(workspace_file, force=True)
    return {
        "file": {
            "id": workspace_file.pk,
            "logical_path": path,
            "file_name": file_name,
            "size": len(content),
            "index_status": workspace_file.resource_sync_status,
        },
        "job_id": str(job.pk),
    }


@transaction.atomic
def delete_path(company, logical_path: str) -> dict:
    path = normalize_path(logical_path, allow_root=False)
    scope = models.Q(logical_path=path) | models.Q(logical_path__startswith=f"{path}/")
    files = list(WorkspaceFile.objects.filter(company=company).filter(scope))
    for item in files:
        if item.storage_key:
            client, bucket_name = _get_cos_client_and_bucket()
            client.delete_object(Bucket=bucket_name, Key=normalize_cos_key(item.storage_key))
    file_ids = [item.pk for item in files]
    if file_ids:
        WorkspaceFile.objects.filter(pk__in=file_ids).delete()
    deleted_folders, _ = KnowledgeFolder.objects.filter(company=company).filter(scope).delete()
    return {
        "logical_path": path,
        "deleted_files": len(file_ids),
        "deleted_folders": deleted_folders,
    }
