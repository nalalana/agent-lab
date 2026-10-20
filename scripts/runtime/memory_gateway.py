"""Identity-bound Memory Gateway for per-Agent OpenViking spaces."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

try:
    from openviking_client import (
        OpenVikingClientError,
        OpenVikingHTTPClient,
        OpenVikingIdentity,
    )
except ImportError:  # pragma: no cover - repository package import
    from scripts.runtime.openviking_client import (
        OpenVikingClientError,
        OpenVikingHTTPClient,
        OpenVikingIdentity,
    )


MEMORY_ACTIONS = frozenset({"read", "write", "update", "delete"})
CROSS_AGENT_SOURCES = frozenset({"memory", "resource"})
CROSS_AGENT_HYDRATE_ITEM_LIMIT = 3
CROSS_AGENT_CONTENT_CHAR_LIMIT = 12_000
SESSION_AGENT_PATTERN = re.compile(r"^agent:([^:]+):")
SAFE_MEMORY_PATH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,240}\.md$")


class MemoryGatewayDenied(ValueError):
    """Raised when identity, ownership, or operation validation fails."""


@dataclass(frozen=True)
class AgentMemorySpace:
    """OpenViking routing metadata published by the control plane."""

    agent_id: str
    viking_space_id: str
    account_id: str
    user_id: str
    root_uri: str
    status: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AgentMemorySpace":
        return cls(
            agent_id=str(payload.get("agent_id") or "").strip(),
            viking_space_id=str(payload.get("viking_space_id") or "").strip(),
            account_id=str(payload.get("account_id") or "").strip(),
            user_id=str(payload.get("user_id") or "").strip(),
            root_uri=str(payload.get("root_uri") or "").strip().rstrip("/") + "/",
            status=str(payload.get("status") or "").strip().lower(),
        )

    def validate(self) -> None:
        if not all(
            (
                self.agent_id,
                self.viking_space_id,
                self.account_id,
                self.user_id,
                self.root_uri,
            )
        ):
            raise MemoryGatewayDenied("Agent memory space is incomplete")
        if self.status != "active":
            raise MemoryGatewayDenied("Agent memory space is not active")


def resolve_runtime_actor(payload: dict[str, Any]) -> str:
    """Derive the actor from runtime session context, never from a target field."""

    session_key = str(payload.get("session_key") or "").strip()
    match = SESSION_AGENT_PATTERN.match(session_key)
    if match is None:
        raise MemoryGatewayDenied("Runtime session does not contain an Agent identity")
    actor_agent_id = match.group(1).strip()
    claimed_agent_id = str(payload.get("agent_id") or "").strip()
    if claimed_agent_id and claimed_agent_id != actor_agent_id:
        raise MemoryGatewayDenied("Runtime Agent identity mismatch")
    return actor_agent_id


class MemoryGateway:
    """Execute CRUD only inside the authenticated Agent's own memory root."""

    def __init__(self, client: OpenVikingHTTPClient):
        self.client = client

    def execute(
        self,
        *,
        action: str,
        arguments: dict[str, Any],
        space: AgentMemorySpace,
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action.startswith("memory."):
            normalized_action = normalized_action.split(".", 1)[1]
        if normalized_action not in MEMORY_ACTIONS:
            raise MemoryGatewayDenied("Unsupported memory action")

        space.validate()
        identity = OpenVikingIdentity(space.account_id, space.user_id)
        if normalized_action == "read":
            result = self._read(identity, space, arguments)
        elif normalized_action == "write":
            result = self._write(identity, space, arguments)
        elif normalized_action == "update":
            result = self._update(identity, space, arguments)
        else:
            result = self._delete(identity, space, arguments)
        return {
            "status": "completed",
            "action": normalized_action,
            "agent_id": space.agent_id,
            "viking_space_id": space.viking_space_id,
            "data": result,
        }

    def query_agent(
        self,
        *,
        query: str,
        target_space: AgentMemorySpace,
        resource_identity: OpenVikingIdentity,
        resource_roots: list[str],
        sources: list[str] | tuple[str, ...] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Query server-resolved target scopes for an already-authorized CEO.

        Authorization and Agent lookup intentionally stay in the Bridge where
        the authenticated runtime session and signed company config are
        available. This data-plane helper accepts no caller-supplied URI.
        """

        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise MemoryGatewayDenied("Cross-Agent query is required")
        if len(normalized_query) > 4000:
            raise MemoryGatewayDenied("Cross-Agent query is too long")
        normalized_limit = max(1, min(int(limit or 8), 20))
        normalized_sources = tuple(
            dict.fromkeys(
                str(item or "").strip().lower()
                for item in (sources or ("memory", "resource"))
                if str(item or "").strip().lower() in CROSS_AGENT_SOURCES
            )
        )
        if not normalized_sources:
            raise MemoryGatewayDenied("At least one of memory or resource is required")

        target_space.validate()
        data: dict[str, Any] = {}
        searched_scopes: list[dict[str, str]] = []
        if "memory" in normalized_sources:
            target_identity = OpenVikingIdentity(
                target_space.account_id,
                target_space.user_id,
            )
            memory_result = self.client.find_memories(
                target_identity,
                root_uri=target_space.root_uri,
                query=normalized_query,
                limit=normalized_limit,
            )
            filtered_memory = self._filter_results_by_roots(
                memory_result,
                (target_space.root_uri,),
            )
            data["memory"] = self._hydrate_search_results(
                target_identity,
                filtered_memory,
                (target_space.root_uri,),
            )
            searched_scopes.append({"source": "memory", "uri": target_space.root_uri})

        if "resource" in normalized_sources:
            normalized_roots = self._minimal_resource_roots(resource_roots)
            resource_results: list[dict[str, Any]] = []
            for root_uri in normalized_roots:
                raw_result = self.client.find_resources(
                    resource_identity,
                    root_uri=root_uri,
                    query=normalized_query,
                    limit=normalized_limit,
                )
                filtered_resource = self._filter_results_by_roots(
                    raw_result,
                    (root_uri,),
                )
                hydrated_resource = self._hydrate_search_results(
                    resource_identity,
                    filtered_resource,
                    (root_uri,),
                )
                resource_results.append({
                    "root_uri": root_uri,
                    "result": self._append_direct_resource_content(
                        resource_identity,
                        hydrated_resource,
                        root_uri,
                    ),
                })
                searched_scopes.append({"source": "resource", "uri": root_uri})
            data["resource"] = resource_results

        return {
            "status": "completed",
            "action": "agent.query",
            "agent_id": target_space.agent_id,
            "viking_space_id": target_space.viking_space_id,
            "sources": list(normalized_sources),
            "searched_scopes": searched_scopes,
            "data": data,
        }

    def _read(
        self,
        identity: OpenVikingIdentity,
        space: AgentMemorySpace,
        arguments: dict[str, Any],
    ) -> Any:
        uri = str(arguments.get("uri") or "").strip()
        if uri:
            return self.client.read(identity, self._owned_uri(space, uri))
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise MemoryGatewayDenied("Memory read requires query or uri")
        if len(query) > 4000:
            raise MemoryGatewayDenied("Memory query is too long")
        limit = max(1, min(int(arguments.get("limit") or 8), 20))
        result = self.client.find_memories(
            identity,
            root_uri=space.root_uri,
            query=query,
            limit=limit,
        )
        return self._filter_owned_results(space, result)

    def _write(
        self,
        identity: OpenVikingIdentity,
        space: AgentMemorySpace,
        arguments: dict[str, Any],
    ) -> Any:
        # Bootstrap normally creates this directory. Keep the write path
        # self-healing for older tenants and Agents published concurrently.
        self.client.ensure_memory_root(identity, space.root_uri)
        content = self._validated_content(arguments)
        relative_path = str(arguments.get("path") or "").strip()
        if not relative_path:
            relative_path = f"notes/memory-{uuid.uuid4().hex}.md"
        uri = self._new_owned_uri(space, relative_path)
        return self.client.write(identity, uri=uri, content=content, mode="create")

    def _update(
        self,
        identity: OpenVikingIdentity,
        space: AgentMemorySpace,
        arguments: dict[str, Any],
    ) -> Any:
        uri = self._owned_uri(space, str(arguments.get("uri") or "").strip())
        content = self._validated_content(arguments)
        return self.client.write(identity, uri=uri, content=content, mode="replace")

    def _delete(
        self,
        identity: OpenVikingIdentity,
        space: AgentMemorySpace,
        arguments: dict[str, Any],
    ) -> Any:
        uri = self._owned_uri(space, str(arguments.get("uri") or "").strip())
        return self.client.delete(identity, uri)

    @staticmethod
    def _validated_content(arguments: dict[str, Any]) -> str:
        content = str(arguments.get("content") or "")
        if not content.strip():
            raise MemoryGatewayDenied("Memory content is required")
        if len(content.encode("utf-8")) > 100_000:
            raise MemoryGatewayDenied("Memory content exceeds 100000 bytes")
        return content

    @staticmethod
    def _new_owned_uri(space: AgentMemorySpace, relative_path: str) -> str:
        normalized = str(relative_path or "").strip().lstrip("/")
        if (
            not SAFE_MEMORY_PATH_PATTERN.fullmatch(normalized)
            or ".." in normalized.split("/")
            or "//" in normalized
        ):
            raise MemoryGatewayDenied("Invalid memory path")
        return f"{space.root_uri}{normalized}"

    @staticmethod
    def _owned_uri(space: AgentMemorySpace, uri: str) -> str:
        candidate = str(uri or "").strip()
        if not candidate or candidate == space.root_uri.rstrip("/"):
            raise MemoryGatewayDenied("A memory item URI is required")
        if not candidate.startswith(space.root_uri):
            raise MemoryGatewayDenied("Cross-Agent memory access is denied")
        return candidate

    @classmethod
    def _filter_owned_results(cls, space: AgentMemorySpace, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        filtered = dict(result)
        for key in ("memories", "resources", "items"):
            values = filtered.get(key)
            if not isinstance(values, list):
                continue
            filtered[key] = [
                item
                for item in values
                if not isinstance(item, dict)
                or not item.get("uri")
                or str(item.get("uri")).startswith(space.root_uri)
            ]
        return filtered

    @staticmethod
    def _minimal_resource_roots(resource_roots: list[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw_root in resource_roots:
            root = str(raw_root or "").strip().rstrip("/")
            if not root.startswith("viking://resources/knowledge"):
                continue
            if root not in normalized:
                normalized.append(root)
        return tuple(
            root
            for root in normalized
            if not any(
                root != parent and root.startswith(f"{parent}/")
                for parent in normalized
            )
        )

    @classmethod
    def _filter_results_by_roots(cls, result: Any, roots: tuple[str, ...]) -> Any:
        """Defensively remove top-level search hits outside selected scopes."""

        if not isinstance(result, dict):
            return result
        filtered = dict(result)
        for key in ("memories", "resources", "items", "results"):
            values = filtered.get(key)
            if not isinstance(values, list):
                continue
            filtered[key] = [
                item
                for item in values
                if not isinstance(item, dict)
                or not item.get("uri")
                or any(str(item.get("uri")).startswith(root) for root in roots)
            ]
        return filtered

    def _hydrate_search_results(
        self,
        identity: OpenVikingIdentity,
        result: Any,
        roots: tuple[str, ...],
    ) -> Any:
        """Attach bounded source text so factual answers do not rely on abstracts."""

        if not isinstance(result, dict):
            return result
        hydrated = dict(result)
        candidates: list[tuple[str, int, dict[str, Any]]] = []
        for key in ("resources", "memories", "results", "items"):
            values = hydrated.get(key)
            if not isinstance(values, list):
                continue
            copied_values = [dict(item) if isinstance(item, dict) else item for item in values]
            hydrated[key] = copied_values
            for index, item in enumerate(copied_values):
                if not isinstance(item, dict) or str(item.get("content") or "").strip():
                    continue
                uri = str(item.get("uri") or "").strip()
                if uri and any(uri.startswith(root) for root in roots):
                    candidates.append((key, index, item))

        # Prefer original parsed documents over generated overview files.
        candidates.sort(key=lambda candidate: candidate[2].get("uri", "").endswith("/.overview.md"))
        for _key, _index, item in candidates[:CROSS_AGENT_HYDRATE_ITEM_LIMIT]:
            try:
                source = self.client.read(identity, str(item.get("uri") or ""))
            except OpenVikingClientError:
                continue
            if isinstance(source, str):
                content = source
            elif isinstance(source, dict):
                content = str(source.get("content") or source.get("text") or "")
            else:
                content = ""
            if not content:
                continue
            item["content"] = content[:CROSS_AGENT_CONTENT_CHAR_LIMIT]
            item["content_truncated"] = len(content) > CROSS_AGENT_CONTENT_CHAR_LIMIT
        return hydrated

    def _append_direct_resource_content(
        self,
        identity: OpenVikingIdentity,
        result: Any,
        root_uri: str,
    ) -> Any:
        """Read direct documents from one exact authorized resource root.

        Semantic search can return zero hits for an exact term present in a
        small document. This root was selected by the Bridge from the target
        Agent's knowledgeRead policy, so reading a bounded number of its direct
        files is a safe deterministic fallback. Broad knowledge roots do not
        use this fallback.
        """

        if not isinstance(result, dict):
            return result
        normalized_root = str(root_uri or "").strip().rstrip("/")
        if normalized_root == "viking://resources/knowledge":
            return result
        try:
            listing = self.client.list_directory(identity, normalized_root)
        except OpenVikingClientError:
            return result
        if isinstance(listing, list):
            entries = listing
        elif isinstance(listing, dict):
            entries = next(
                (
                    listing.get(key)
                    for key in ("items", "resources", "entries")
                    if isinstance(listing.get(key), list)
                ),
                [],
            )
        else:
            entries = []

        resources = result.get("resources")
        if not isinstance(resources, list):
            resources = []
        resources = [dict(item) if isinstance(item, dict) else item for item in resources]
        existing_uris = {
            str(item.get("uri") or "").strip()
            for item in resources
            if isinstance(item, dict)
        }
        appended = 0
        for entry in entries:
            if appended >= CROSS_AGENT_HYDRATE_ITEM_LIMIT:
                break
            if not isinstance(entry, dict):
                continue
            uri = str(entry.get("uri") or "").strip()
            is_directory = bool(entry.get("isDir") or entry.get("is_dir"))
            if (
                not uri.startswith(f"{normalized_root}/")
                or is_directory
                or uri.endswith("/.overview.md")
                or uri in existing_uris
            ):
                continue
            try:
                source = self.client.read(identity, uri)
            except OpenVikingClientError:
                continue
            if isinstance(source, str):
                content = source
            elif isinstance(source, dict):
                content = str(source.get("content") or source.get("text") or "")
            else:
                content = ""
            if not content:
                continue
            resources.append({
                **entry,
                "uri": uri,
                "content": content[:CROSS_AGENT_CONTENT_CHAR_LIMIT],
                "content_truncated": len(content) > CROSS_AGENT_CONTENT_CHAR_LIMIT,
                "retrieval_mode": "authorized_resource_fallback",
            })
            existing_uris.add(uri)
            appended += 1

        if appended:
            result = dict(result)
            result["resources"] = resources
            result["authorized_fallback_count"] = appended
        return result


def denied_response(exc: Exception, *, action: str = "") -> dict[str, Any]:
    """Return a stable response without leaking credentials or internal URLs."""

    if isinstance(exc, OpenVikingClientError):
        message = str(exc)
    elif isinstance(exc, MemoryGatewayDenied):
        message = str(exc)
    else:
        message = "Memory Gateway request failed"
    return {
        "status": "denied",
        "action": str(action or "").strip().lower(),
        "message": message[:500],
    }
