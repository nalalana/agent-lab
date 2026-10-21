"""Company-scoped workspace tools for the unified LangGraph runtime.

The trusted Django backend authorizes logical paths, reads bounded UTF-8 text
and PDF text directly, and brokers files into or out of a Run-scoped AGSX
sandbox only when transformation or code execution is required. The sandbox
never receives COS credentials.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import time
import unicodedata
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from pypdf import PdfReader
from common.immudbIO.errors import UnconfirmedWriteError

from apps.agent_runtime.contracts import ToolContext, ToolRequest, ToolResult
from apps.agent_runtime.enums import ErrorCode
from apps.agent_runtime.tools.definitions import ToolDefinition
from apps.agent_runtime.tools.project_tables import ProjectTablePermissionDenied, execute_project_table_tool
from apps.agent_runtime.tools.workspace_policy import WorkspacePolicy
from apps.agent_runtime.tools.executors import (
    BackendToolExecutor,
    SandboxInputFile,
    SandboxToolExecutor,
    ToolExecutionError,
)
from apps.runtime.models import WorkspaceFile
from apps.runtime.policy import normalize_workspace_reference
from apps.runtime.security import validate_workspace_logical_path
from common.utils.Tengcentcos_utils import (
    delete_object,
    get_object_bytes,
    put_object_with_key,
)


logger = logging.getLogger("agent_runtime.workspace")

WORKSPACE_READ_TOOLS = frozenset({
    "workspace.list",
    "workspace.locate",
    "workspace.read",
})
PROJECT_TABLE_TOOLS = frozenset({
    "company.project_tables.list",
    "company.table.read",
    "company.table.write",
    "company.table.clear_cells",
})

WORKSPACE_TOOLS = WORKSPACE_READ_TOOLS | frozenset({
    "workspace.checkout",
    "workspace.publish",
    "workspace.move",
    "workspace.delete",
})


class WorkspacePermissionDenied(PermissionError):
    pass




class DjangoWorkspaceBackendToolExecutor(BackendToolExecutor):
    """Authorize durable Workspace mutations and broker sandbox file transfer."""

    def __init__(
        self,
        *,
        company_id: Any,
        tool_policy: dict[str, Any] | None,
        sandbox_executor: SandboxToolExecutor | None = None,
    ) -> None:
        self.company_id = company_id
        self.policy = WorkspacePolicy.from_config(tool_policy)
        self.sandbox_executor = sandbox_executor

    async def execute(
        self,
        definition: ToolDefinition,
        request: ToolRequest,
        context: ToolContext,
    ) -> ToolResult:
        if request.name in PROJECT_TABLE_TOOLS:
            try:
                payload = await sync_to_async(
                    execute_project_table_tool,
                    thread_sensitive=True,
                )(request, context)
            except UnconfirmedWriteError as exc:
                return _failure(str(exc), exc.code)
            except ToolExecutionError:
                raise
            except ProjectTablePermissionDenied as exc:
                return _failure(str(exc), ErrorCode.TOOL_DENIED)
            except (ValueError, FileNotFoundError) as exc:
                return _failure(str(exc), ErrorCode.TOOL_INVALID_ARGUMENTS)
            except Exception:
                logger.exception("project/table tool failed tool=%s", request.name)
                return _failure("项目表格工具暂时不可用", ErrorCode.INTERNAL_ERROR)
            return _success(payload)
        if request.name not in WORKSPACE_TOOLS:
            return await super().execute(definition, request, context)
        try:
            if request.name in WORKSPACE_READ_TOOLS:
                payload = await sync_to_async(self._execute_read_sync, thread_sensitive=True)(request)
            elif request.name == "workspace.checkout":
                payload = await self._checkout(definition, request, context)
            elif request.name == "workspace.publish":
                payload = await self._publish(request, context)
            elif request.name == "workspace.move":
                payload = await sync_to_async(self._move, thread_sensitive=True)(request.arguments, context)
            else:
                payload = await sync_to_async(self._delete, thread_sensitive=True)(request.arguments, context)
        except WorkspacePermissionDenied as exc:
            return _failure(str(exc), ErrorCode.TOOL_DENIED)
        except (ValueError, FileNotFoundError) as exc:
            return _failure(str(exc), ErrorCode.TOOL_INVALID_ARGUMENTS)
        except ToolExecutionError:
            raise
        except Exception:
            logger.exception("workspace tool failed tool=%s", request.name)
            return _failure("workspace 工具暂时不可用", ErrorCode.INTERNAL_ERROR)
        return _success(payload)

    def _execute_read_sync(self, request: ToolRequest) -> dict[str, Any]:
        if not self.policy.read_scopes:
            raise WorkspacePermissionDenied("当前 Agent 没有可读取的 knowledge scope")
        if request.name == "workspace.list":
            return self._list(request.arguments)
        if request.name == "workspace.locate":
            return self._locate(request.arguments)
        routed = self._route_directory_read(request.arguments)
        if routed is not None:
            return routed
        return self._read(request.arguments)

    def _route_directory_read(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Route folder-shaped workspace.read calls to list or locate.

        Models sometimes pass an authorized folder scope to workspace.read even
        though that tool accepts a concrete file. Treat an exact readable file
        as a file first; otherwise route known/readable directory targets
        without attempting file content retrieval.
        """
        requested_path = _validated_optional_path(arguments.get("logical_path"))
        if not requested_path or requested_path == "*":
            return None

        readable_files = self._readable_files()
        if requested_path in readable_files:
            return None

        prefix = f"{requested_path}/"
        is_directory = (
            requested_path in self.policy.read_scopes
            or any(path.startswith(prefix) for path in readable_files)
        )
        if not is_directory:
            return None

        if str(arguments.get("file_name") or "").strip():
            return self._locate(arguments)
        return self._list({"logical_path": requested_path})

    def _all_files(self) -> dict[str, WorkspaceFile]:
        latest_by_path: dict[str, WorkspaceFile] = {}
        queryset = WorkspaceFile.objects.filter(company_id=self.company_id).order_by("logical_path", "-id")
        for item in queryset:
            path = normalize_workspace_reference(item.logical_path)
            if path and path not in latest_by_path:
                latest_by_path[path] = item
        return latest_by_path

    def permission_facts(self) -> dict[str, Any]:
        return self.policy.facts(self._readable_files())

    def _readable_files(self) -> dict[str, WorkspaceFile]:
        return {
            path: item
            for path, item in self._all_files().items()
            if self.policy.allows_read_file(path)
        }

    def _list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        directory = _validated_optional_path(arguments.get("logical_path"))
        if directory == "*":
            directory = ""
        if not self.policy.allows_read_directory(directory):
            raise WorkspacePermissionDenied("当前 Agent 无权访问该 logical path")

        entries: dict[str, dict[str, Any]] = {}
        prefix = f"{directory}/" if directory else ""
        for path, item in self._readable_files().items():
            if prefix and not path.startswith(prefix):
                continue
            relative = path[len(prefix):] if prefix else path
            if not relative:
                continue
            child, separator, _remainder = relative.partition("/")
            child_path = f"{directory}/{child}" if directory else child
            if separator:
                entries.setdefault(child_path, {
                    "name": child,
                    "logical_path": child_path,
                    "kind": "directory",
                })
            else:
                entries[child_path] = _file_payload(item, path)
        return {
            "tool_name": "workspace.list",
            "logical_path": directory,
            "items": sorted(entries.values(), key=lambda value: (value["kind"] != "directory", value["logical_path"])),
        }

    def _locate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested_path = _validated_optional_path(arguments.get("logical_path"))
        requested_name = str(arguments.get("file_name") or "").strip().lower()
        matches = []
        for path, item in self._readable_files().items():
            if requested_path and requested_path != "*":
                if path != requested_path and not path.startswith(f"{requested_path}/"):
                    continue
            if requested_name and requested_name not in str(item.file_name or "").lower():
                continue
            matches.append(_file_payload(item, path))
        return {
            "tool_name": "workspace.locate",
            "logical_path": "" if requested_path == "*" else requested_path,
            "items": sorted(matches, key=lambda value: value["logical_path"]),
        }

    def _read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested_path = _validated_optional_path(arguments.get("logical_path"))
        requested_name = str(arguments.get("file_name") or "").strip().lower()
        if not requested_path and not requested_name:
            raise ValueError("读取文件时必须提供 logical_path 或 file_name")

        candidates = []
        for path, item in self._readable_files().items():
            if requested_path:
                path_matches = path == requested_path
                directory_matches = bool(
                    requested_name and path.startswith(f"{requested_path}/")
                )
                if not path_matches and not directory_matches:
                    continue
            if requested_name and str(item.file_name or "").strip().lower() != requested_name:
                continue
            candidates.append((path, item))
        if not candidates:
            raise FileNotFoundError("未找到匹配的 workspace 文件")
        if len(candidates) > 1:
            raise ValueError("匹配到多个文件，请使用 logical_path 指定具体文件")

        path, item = candidates[0]
        is_pdf = _is_pdf_file(item, path)
        maximum = (
            _max_workspace_pdf_bytes()
            if is_pdf
            else max(1024, int(getattr(settings, "AGENT_RUNTIME_WORKSPACE_READ_MAX_BYTES", 1024 * 1024)))
        )
        if int(item.size or 0) > maximum:
            kind = "PDF" if is_pdf else "文件"
            raise ValueError(f"{kind}过大，最大允许读取 {maximum} 字节")
        try:
            raw = get_object_bytes(_cos_storage_key(item, path), max_bytes=maximum)
        except Exception as exc:
            raise ValueError("workspace 文件内容无效") from exc
        if len(raw) > maximum:
            kind = "PDF" if is_pdf else "文件"
            raise ValueError(f"{kind}过大，最大允许读取 {maximum} 字节")
        if is_pdf:
            extraction = _extract_pdf_text(raw, path)
            return {
                "tool_name": "workspace.read",
                "logical_path": path,
                "file": _file_payload(item, path),
                **extraction,
            }
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("当前工具只支持 UTF-8 文本文件；请使用 workspace.checkout") from exc
        return {
            "tool_name": "workspace.read",
            "logical_path": path,
            "file": _file_payload(item, path),
            "content": content,
            "encoding": "utf-8",
        }

    async def _checkout(
        self,
        definition: ToolDefinition,
        request: ToolRequest,
        context: ToolContext,
    ) -> dict[str, Any]:
        if self.sandbox_executor is None:
            raise ToolExecutionError(
                code=str(ErrorCode.SANDBOX_CREATE_FAILED),
                message="沙箱执行器不可用",
                retryable=True,
            )
        prepared = await sync_to_async(self._prepare_checkout, thread_sensitive=True)(request.arguments)
        inputs = tuple(
            SandboxInputFile(name=item["name"], content=item["content"])
            for item in prepared
        )
        sandbox_paths = await self.sandbox_executor.stage_files(definition, context, inputs)
        files = []
        for metadata, sandbox_path in zip(prepared, sandbox_paths):
            public_metadata = {key: value for key, value in metadata.items() if key != "content"}
            files.append({**public_metadata, "sandbox_path": sandbox_path})
        return {"tool_name": "workspace.checkout", "files": files}

    def _prepare_checkout(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        paths = _validated_path_list(arguments.get("logical_paths"), max_count=_max_artifact_count())
        files = self._all_files()
        max_file = _max_artifact_bytes()
        max_total = _max_total_artifact_bytes()
        total = 0
        prepared = []
        for path in paths:
            if not self.policy.allows_read_file(path):
                raise WorkspacePermissionDenied(f"当前 Agent 无权读取: {path}")
            item = files.get(path)
            if item is None:
                raise FileNotFoundError(f"workspace 文件不存在: {path}")
            if int(item.size or 0) > max_file:
                raise ValueError(f"文件超过沙箱单文件限制: {path}")
            content = get_object_bytes(_cos_storage_key(item, path), max_bytes=max_file)
            total += len(content)
            if total > max_total:
                raise ValueError("文件合计大小超过沙箱输入限制")
            checksum = hashlib.sha256(content).hexdigest()
            if item.checksum and str(item.checksum).lower() != checksum:
                raise ValueError(f"workspace 文件完整性校验失败: {path}")
            prepared.append({
                "logical_path": path,
                "name": str(item.file_name or PurePosixPath(path).name),
                "size": len(content),
                "sha256": checksum,
                "version": int(item.version or 1),
                "content": content,
            })
        return prepared

    async def _publish(self, request: ToolRequest, context: ToolContext) -> dict[str, Any]:
        if self.sandbox_executor is None:
            raise ToolExecutionError(
                code=str(ErrorCode.SANDBOX_CREATE_FAILED),
                message="沙箱执行器不可用",
                retryable=True,
            )
        specifications = _validated_publish_files(request.arguments.get("files"))
        output_files = await self.sandbox_executor.collect_output_files(context)
        output_by_name = {}
        for output in output_files:
            if output.name in output_by_name:
                raise ValueError(f"沙箱输出文件名重复: {output.name}")
            output_by_name[output.name] = output
        selected = []
        for specification in specifications:
            output = output_by_name.get(specification["sandbox_path"])
            if output is None:
                raise FileNotFoundError(f"沙箱输出文件不存在: {specification['sandbox_path']}")
            selected.append((specification, output))
        files = await sync_to_async(self._persist_outputs, thread_sensitive=True)(selected, context)
        return {"tool_name": "workspace.publish", "files": files}

    @transaction.atomic
    def _persist_outputs(self, selected, context: ToolContext) -> list[dict[str, Any]]:
        max_file = _max_artifact_bytes()
        max_total = _max_total_artifact_bytes()
        total = 0
        destinations = set()
        validated = []
        for specification, output in selected:
            path = _validated_path(specification["logical_path"])
            mode = specification["mode"]
            if path in destinations:
                raise ValueError(f"重复的目标 logical path: {path}")
            destinations.add(path)
            if mode == "create":
                if not self.policy.allows_create_file(path):
                    raise WorkspacePermissionDenied(f"当前 Agent 无权在目标文件夹创建文件: {path}")
            elif not self.policy.allows_write_file(path):
                raise WorkspacePermissionDenied(f"当前 Agent 无权写入: {path}")
            if len(output.content) > max_file:
                raise ValueError(f"沙箱输出超过单文件限制: {output.name}")
            total += len(output.content)
            if total > max_total:
                raise ValueError("沙箱输出合计大小超过限制")
            validated.append((specification, output, path))

        persisted = []
        for specification, output, path in validated:
            existing = (
                WorkspaceFile.objects.select_for_update()
                .filter(company_id=self.company_id, logical_path=path)
                .order_by("-id")
                .first()
            )
            mode = specification["mode"]
            if mode == "create" and existing is not None:
                raise ValueError(f"目标文件已存在，请使用 replace: {path}")
            if mode == "replace" and existing is None:
                raise FileNotFoundError(f"待覆盖文件不存在: {path}")
            if mode == "replace":
                expected = specification.get("if_match_version")
                if expected is None:
                    raise ValueError(f"覆盖文件必须提供 if_match_version: {path}")
                if int(existing.version or 1) != int(expected):
                    raise ValueError(f"文件版本已变化，请重新读取后再覆盖: {path}")

            file_name = PurePosixPath(path).name
            storage_key = _versioned_storage_key(self.company_id, path)
            mime_type = output.content_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            put_object_with_key(output.content, storage_key, mime_type)
            checksum = hashlib.sha256(output.content).hexdigest()
            now = str(time.time())[:20]
            is_knowledge = path == "knowledge" or path.startswith("knowledge/")
            values = {
                "instance": None,
                "file_name": file_name,
                "storage_backend": WorkspaceFile.STORAGE_TENCENT_COS,
                "storage_key": storage_key,
                "checksum": checksum,
                "mime_type": mime_type,
                "size": len(output.content),
                "sync_status": WorkspaceFile.SYNC_IN_SYNC,
                "resource_sync_status": (
                    WorkspaceFile.RESOURCE_SYNC_PENDING
                    if is_knowledge
                    else WorkspaceFile.RESOURCE_SYNC_NOT_APPLICABLE
                ),
                "resource_last_error": "",
                "created_by": str(context.user_id or context.agent_id)[:128],
                "updated_at": now,
            }
            if existing is None:
                item = WorkspaceFile.objects.create(
                    company_id=self.company_id,
                    logical_path=path,
                    version=1,
                    created_at=now,
                    **values,
                )
            else:
                item = existing
                for key, value in values.items():
                    setattr(item, key, value)
                item.version = int(item.version or 1) + 1
                item.save(update_fields=[*values.keys(), "version"])
            persisted.append(_file_payload(item, path))
        return persisted

    @transaction.atomic
    def _move(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        source = _validated_path(arguments.get("source_path"))
        destination = _validated_path(arguments.get("destination_path"))
        if source == destination:
            raise ValueError("源路径与目标路径不能相同")
        if not self.policy.allows_move_source(source):
            raise WorkspacePermissionDenied(f"当前 Agent 无权移动源文件: {source}")
        if not self.policy.allows_write_file(destination):
            raise WorkspacePermissionDenied(f"当前 Agent 无权写入目标路径: {destination}")
        item = (
            WorkspaceFile.objects.select_for_update()
            .filter(company_id=self.company_id, logical_path=source)
            .order_by("-id")
            .first()
        )
        if item is None:
            raise FileNotFoundError(f"源文件不存在: {source}")
        if WorkspaceFile.objects.filter(company_id=self.company_id, logical_path=destination).exists():
            raise ValueError(f"目标文件已存在: {destination}")
        expected = arguments.get("if_match_version")
        if expected is None or int(item.version or 1) != int(expected):
            raise ValueError("源文件版本已变化，请重新读取后再移动")
        source_key = _cos_storage_key(item, source)
        content = get_object_bytes(source_key, max_bytes=_max_artifact_bytes())
        destination_key = _versioned_storage_key(self.company_id, destination)
        put_object_with_key(content, destination_key, item.mime_type or "application/octet-stream")
        delete_object(source_key)
        item.logical_path = destination
        item.file_name = PurePosixPath(destination).name
        item.storage_key = destination_key
        item.version = int(item.version or 1) + 1
        item.updated_at = str(time.time())[:20]
        item.created_by = str(context.user_id or context.agent_id)[:128]
        item.save(update_fields=[
            "logical_path", "file_name", "storage_key", "version", "updated_at", "created_by",
        ])
        return {
            "tool_name": "workspace.move",
            "source_path": source,
            "file": _file_payload(item, destination),
        }

    @transaction.atomic
    def _delete(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        paths = _validated_path_list(arguments.get("logical_paths"), max_count=1)
        versions = arguments.get("if_match_versions")
        if not isinstance(versions, dict):
            raise ValueError("删除文件必须提供 if_match_versions")
        path = paths[0]
        if not self.policy.allows_delete_file(path):
            raise WorkspacePermissionDenied(f"当前 Agent 无权删除: {path}")
        item = (
            WorkspaceFile.objects.select_for_update()
            .filter(company_id=self.company_id, logical_path=path)
            .order_by("-id")
            .first()
        )
        if item is None:
            raise FileNotFoundError(f"待删除文件不存在: {path}")
        expected = versions.get(path)
        if expected is None or isinstance(expected, bool) or not isinstance(expected, int):
            raise ValueError(f"删除文件必须提供整数版本: {path}")
        if int(item.version or 1) != expected:
            raise ValueError(f"文件版本已变化，请重新读取后再删除: {path}")
        deleted = _file_payload(item, path)
        delete_object(_cos_storage_key(item, path))
        item.delete()
        return {
            "tool_name": "workspace.delete",
            "deleted": [deleted],
            "actor": str(context.agent_id),
        }


def _success(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(
        success=True,
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        artifact_refs=(),
        error_code=None,
        retryable=False,
    )


def _failure(message: str, code: ErrorCode) -> ToolResult:
    return ToolResult(
        success=False,
        content=json.dumps({
            "success": False,
            "error": str(message),
            "instruction": "不要重复调用该工具，请根据错误直接向用户说明。",
        }, ensure_ascii=False),
        artifact_refs=(),
        error_code=str(code),
        retryable=False,
    )


def _validated_optional_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw == "*":
        return "*"
    return _validated_path(raw)


def _validated_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "*":
        raise ValueError("必须提供精确的 logical path")
    return normalize_workspace_reference(validate_workspace_logical_path(raw, "logical_path"))


def _validated_path_list(value: Any, *, max_count: int) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("logical_paths 必须是非空数组")
    if len(value) > max(max_count, 1):
        raise ValueError("logical_paths 数量超过限制")
    paths = [_validated_path(item) for item in value]
    if len(set(paths)) != len(paths):
        raise ValueError("logical_paths 不能包含重复路径")
    return paths


def _validated_publish_files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("files 必须是非空数组")
    if len(value) > _max_artifact_count():
        raise ValueError("files 数量超过限制")
    result = []
    seen_names = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("files 元素必须是对象")
        supplied_path = str(raw.get("sandbox_path") or "").strip()
        parsed = PurePosixPath(supplied_path)
        if not supplied_path or "\x00" in supplied_path or parsed.is_absolute():
            raise ValueError("sandbox_path 必须使用相对于沙箱 work 目录的 ../output/<文件名>")
        if len(parsed.parts) == 1:
            # Backward compatibility for calls generated before the canonical
            # work-relative path was introduced. New tool manifests only teach
            # ../output/<filename>, so models do not need to guess or retry.
            sandbox_path = parsed.name
        elif len(parsed.parts) == 3 and parsed.parts[:2] == ("..", "output"):
            sandbox_path = parsed.name
        else:
            raise ValueError("sandbox_path 必须使用相对于沙箱 work 目录的 ../output/<文件名>")
        if sandbox_path in {"", ".", ".."} or len(sandbox_path) > 128:
            raise ValueError("sandbox_path 必须指向 output 目录中的普通文件")
        if sandbox_path in seen_names:
            raise ValueError(f"重复的 sandbox_path: {sandbox_path}")
        seen_names.add(sandbox_path)
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in {"create", "replace"}:
            raise ValueError("mode 必须是 create 或 replace")
        result.append({
            "sandbox_path": sandbox_path,
            "logical_path": _validated_path(raw.get("logical_path")),
            "mode": mode,
            "if_match_version": raw.get("if_match_version"),
        })
    return result


def _is_pdf_file(item: WorkspaceFile, path: str) -> bool:
    mime_type = str(item.mime_type or "").split(";", 1)[0].strip().lower()
    return mime_type == "application/pdf" or PurePosixPath(path).suffix.lower() == ".pdf"


def _extract_pdf_text(content: bytes, path: str) -> dict[str, Any]:
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:
        raise ValueError(f"PDF 文件无法解析: {path}") from exc
    if reader.is_encrypted:
        raise ValueError(f"PDF 文件已加密，无法读取: {path}")

    page_count = len(reader.pages)
    maximum_pages = _max_workspace_pdf_pages()
    if page_count > maximum_pages:
        raise ValueError(f"PDF 页数超过限制，最多允许 {maximum_pages} 页")

    maximum_chars = _max_workspace_pdf_text_chars()
    chunks: list[str] = []
    used = 0
    truncated = False
    has_extractable_text = False
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise ValueError(f"PDF 第 {page_number} 页文本提取失败: {path}") from exc
        if page_text.strip():
            has_extractable_text = True
        block = f"--- Page {page_number} ---\n{page_text}"
        separator = "\n\n" if chunks else ""
        available = maximum_chars - used - len(separator)
        if available <= 0:
            truncated = True
            break
        if len(block) > available:
            block = block[:available]
            truncated = True
        chunks.append(f"{separator}{block}")
        used += len(separator) + len(block)
        if truncated:
            break

    extracted = "".join(chunks)
    if not has_extractable_text:
        extracted = "[PDF 未包含可提取文本，可能是扫描件或纯图片 PDF]"
    return {
        "content": extracted,
        "encoding": "pdf-text",
        "page_count": page_count,
        "text_truncated": truncated,
        "has_extractable_text": has_extractable_text,
    }


def _max_workspace_pdf_bytes() -> int:
    return max(1024, int(getattr(settings, "AGENT_RUNTIME_WORKSPACE_PDF_MAX_BYTES", 10 * 1024 * 1024)))


def _max_workspace_pdf_pages() -> int:
    return max(1, int(getattr(settings, "AGENT_RUNTIME_WORKSPACE_PDF_MAX_PAGES", 100)))


def _max_workspace_pdf_text_chars() -> int:
    return max(1024, int(getattr(settings, "AGENT_RUNTIME_WORKSPACE_PDF_MAX_TEXT_CHARS", 120_000)))


def _max_artifact_bytes() -> int:
    return max(1, int(getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_BYTES", 50 * 1024 * 1024)))


def _max_total_artifact_bytes() -> int:
    return max(1, int(getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_TOTAL_ARTIFACT_BYTES", 200 * 1024 * 1024)))


def _max_artifact_count() -> int:
    return max(1, int(getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_COUNT", 50)))


def _versioned_storage_key(company_id: Any, path: str) -> str:
    company_hash = hashlib.sha256(str(company_id).encode("utf-8")).hexdigest()[:24]
    return f"agent-runtime/workspace/{company_hash}/{uuid4().hex}/{PurePosixPath(path).name}"


def _cos_storage_key(item: WorkspaceFile, path: str) -> str:
    """Return an exact COS key only for durable Workspace records.

    Runtime-cache records must never be interpreted as COS object names.  This
    keeps checkout/move/delete from crossing storage backends if legacy or
    partially synchronized metadata is encountered.
    """

    if item.storage_backend != WorkspaceFile.STORAGE_TENCENT_COS:
        raise ValueError(f"workspace 文件不是可持久化的 COS 对象: {path}")
    storage_key = str(item.storage_key or "").strip()
    if not storage_key:
        raise ValueError(f"workspace 文件缺少 COS 对象键: {path}")
    return storage_key


def _file_payload(item: WorkspaceFile, path: str) -> dict[str, Any]:
    return {
        "id": item.pk,
        "name": str(item.file_name or path.rsplit("/", 1)[-1]),
        "logical_path": path,
        "kind": "file",
        "size": int(item.size or 0),
        "mime_type": str(item.mime_type or ""),
        "sha256": str(item.checksum or ""),
        "version": int(item.version or 1),
        "updated_at": str(item.updated_at or ""),
    }
