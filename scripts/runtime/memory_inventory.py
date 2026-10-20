"""Build a read-only console inventory from Agent-owned OpenViking spaces."""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable

try:
    from memory_gateway import AgentMemorySpace
    from openviking_client import (
        OpenVikingClientError,
        OpenVikingHTTPClient,
        OpenVikingIdentity,
    )
except ImportError:  # pragma: no cover - repository package import
    from scripts.runtime.memory_gateway import AgentMemorySpace
    from scripts.runtime.openviking_client import (
        OpenVikingClientError,
        OpenVikingHTTPClient,
        OpenVikingIdentity,
    )


OPENVIKING_VERSION = "0.4.12"

# Keep this catalog aligned with the built-in memory templates shipped by the
# pinned OpenViking image. Custom registry templates are appended dynamically
# by _type_catalog when entries using them are discovered.
BUILTIN_MEMORY_TYPES: tuple[dict[str, Any], ...] = (
    {"key": "profile", "label": "用户画像 Profile", "short_label": "用户画像", "owner_scope": "user", "builtin": True, "sort_order": 10},
    {"key": "preferences", "label": "用户偏好 Preferences", "short_label": "用户偏好", "owner_scope": "user", "builtin": True, "sort_order": 20},
    {"key": "entities", "label": "实体知识 Entities", "short_label": "实体知识", "owner_scope": "user", "builtin": True, "sort_order": 30},
    {"key": "events", "label": "事件记录 Events", "short_label": "事件记录", "owner_scope": "user", "builtin": True, "sort_order": 40},
    {"key": "identity", "label": "Agent 身份 Identity", "short_label": "Agent 身份", "owner_scope": "agent", "builtin": True, "sort_order": 50},
    {"key": "soul", "label": "核心设定 Soul", "short_label": "核心设定", "owner_scope": "agent", "builtin": True, "sort_order": 60},
    {"key": "cases", "label": "任务案例 Cases", "short_label": "任务案例", "owner_scope": "agent", "builtin": True, "sort_order": 70},
    {"key": "trajectories", "label": "执行轨迹 Trajectories", "short_label": "执行轨迹", "owner_scope": "agent", "builtin": True, "sort_order": 80},
    {"key": "experiences", "label": "执行经验 Experiences", "short_label": "执行经验", "owner_scope": "agent", "builtin": True, "sort_order": 90},
    {"key": "tools", "label": "工具经验 Tools", "short_label": "工具经验", "owner_scope": "agent", "builtin": True, "sort_order": 100},
    {"key": "skills", "label": "技能经验 Skills", "short_label": "技能经验", "owner_scope": "agent", "builtin": True, "sort_order": 110},
)


def _list_items(payload: Any) -> list[dict[str, Any]]:
    """Accept current and legacy OpenViking list response shapes."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "entries", "children", "files", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _item_uri(item: dict[str, Any], parent_uri: str) -> str:
    explicit_uri = str(item.get("uri") or item.get("path") or "").strip()
    if explicit_uri.startswith("viking://"):
        return explicit_uri
    name = str(item.get("name") or item.get("filename") or "").strip().strip("/")
    return f"{parent_uri.rstrip('/')}/{name}" if name else ""


def _is_directory(item: dict[str, Any], uri: str) -> bool:
    flag = item.get("is_dir")
    if not isinstance(flag, bool):
        flag = item.get("isDir")
    if isinstance(flag, bool):
        return flag
    kind = str(item.get("kind") or item.get("type") or "").strip().lower()
    if kind in {"directory", "dir", "folder"}:
        return True
    if kind in {"file", "memory"}:
        return False
    return uri.endswith("/")


def _memory_type(root_uri: str, item_uri: str) -> str:
    relative = item_uri.removeprefix(root_uri.rstrip("/") + "/").strip("/")
    first_segment = relative.split("/", 1)[0] if relative else ""
    if "/" not in relative and first_segment.endswith(".md"):
        first_segment = PurePosixPath(first_segment).stem
    return first_segment or "uncategorized"


def _updated_at(item: dict[str, Any]) -> str:
    value = (
        item.get("updated_at")
        or item.get("modified_at")
        or item.get("modTime")
        or item.get("mtime")
        or ""
    )
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value or "")


def _is_missing_directory(exc: OpenVikingClientError) -> bool:
    """Treat an unmaterialized Agent memory directory as an empty space."""

    if exc.http_status == 404:
        return True
    error_text = f"{exc} {exc.response_text}".lower()
    return "http 404" in error_text and (
        "directory not found" in error_text or '"type":"directory"' in error_text
    )


def _memory_entry(space: AgentMemorySpace, item: dict[str, Any], uri: str) -> dict[str, Any]:
    name = str(item.get("name") or item.get("filename") or PurePosixPath(uri).name).strip()
    title = str(item.get("title") or item.get("display_name") or PurePosixPath(name).stem or uri)
    summary = str(item.get("summary") or item.get("abstract") or item.get("overview") or "").strip()
    digest = hashlib.sha256(f"{space.viking_space_id}:{uri}".encode("utf-8")).hexdigest()[:24]
    return {
        "id": f"ov-{digest}",
        "uri": uri,
        "title": title,
        "memory_type": _memory_type(space.root_uri, uri),
        "owner_agent_id": space.agent_id,
        "version": item.get("version"),
        "provenance": str(item.get("provenance") or "OpenViking filesystem"),
        "source_session": str(item.get("source_session") or ""),
        "extraction_status": str(item.get("extraction_status") or "archived"),
        "last_hit": str(item.get("last_hit") or ""),
        "updated_at": _updated_at(item),
        "isolation_boundary": space.root_uri,
        "cross_boundary": False,
        "summary": summary,
    }


def _walk_space(
    client: OpenVikingHTTPClient,
    space: AgentMemorySpace,
    *,
    max_items: int,
    max_depth: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Traverse only the configured space root and cap work for console safety."""

    space.validate()
    identity = OpenVikingIdentity(space.account_id, space.user_id)
    queue: deque[tuple[str, int]] = deque([(space.root_uri, 0)])
    visited: set[str] = set()
    entries: list[dict[str, Any]] = []
    truncated = False
    while queue and len(entries) < max_items:
        directory_uri, depth = queue.popleft()
        if directory_uri in visited:
            continue
        visited.add(directory_uri)
        try:
            directory_payload = client.list_directory(identity, directory_uri)
        except OpenVikingClientError as exc:
            if _is_missing_directory(exc):
                # OpenViking materializes user memory directories lazily. A new
                # Agent with no captured memories is therefore a valid empty
                # inventory, not a console warning.
                continue
            raise
        for item in _list_items(directory_payload):
            uri = _item_uri(item, directory_uri)
            if not uri or not uri.startswith(space.root_uri):
                continue
            if _is_directory(item, uri):
                if depth < max_depth:
                    queue.append((uri.rstrip("/") + "/", depth + 1))
                continue
            entries.append(_memory_entry(space, item, uri))
            if len(entries) >= max_items:
                truncated = bool(queue)
                break
    return entries, truncated or bool(queue)


def _type_catalog(type_keys: Iterable[str]) -> list[dict[str, Any]]:
    catalog = {item["key"]: dict(item) for item in BUILTIN_MEMORY_TYPES}
    for key in type_keys:
        if key not in catalog:
            catalog[key] = {
                "key": key,
                "label": key,
                "owner_scope": "custom",
                "builtin": False,
                "sort_order": 1000,
            }
    return sorted(catalog.values(), key=lambda item: (item["sort_order"], item["key"]))


def build_memory_inventory(
    client: OpenVikingHTTPClient,
    spaces: Iterable[AgentMemorySpace],
    *,
    max_items_per_space: int = 500,
    max_depth: int = 8,
) -> dict[str, Any]:
    """Aggregate dynamic memory types without turning business tags into scopes."""

    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for space in spaces:
        try:
            space_entries, truncated = _walk_space(
                client,
                space,
                max_items=max_items_per_space,
                max_depth=max_depth,
            )
            entries.extend(space_entries)
            if truncated:
                warnings.append(f"Agent {space.agent_id} 的记忆列表已达到控制台展示上限")
        except Exception as exc:
            warnings.append(f"Agent {space.agent_id} 的记忆空间读取失败: {exc}")
    counts = Counter(entry["memory_type"] for entry in entries)
    return {
        "list": entries,
        "memory_types": _type_catalog(counts.keys()),
        "type_counts": dict(counts),
        "warnings": warnings,
        "source": "openviking_memory_v2",
        "openviking_version": OPENVIKING_VERSION,
    }
