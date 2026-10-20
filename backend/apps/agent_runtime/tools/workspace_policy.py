"""One permission evaluator shared by tool availability, execution and answer facts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from apps.runtime.policy import normalize_workspace_reference


@dataclass(frozen=True)
class WorkspacePolicy:
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    delete_scopes: tuple[str, ...]
    denied_scopes: tuple[str, ...]
    read_denied_scopes: tuple[str, ...]
    write_denied_scopes: tuple[str, ...]
    delete_denied_scopes: tuple[str, ...]
    project_table_read: bool = False
    project_table_write: bool = False

    @classmethod
    def from_config(cls, tool_policy: dict[str, Any] | None) -> "WorkspacePolicy":
        knowledge = dict((tool_policy or {}).get("knowledge") or {})
        read_values = knowledge.get("read")
        if read_values is None:
            read_values = knowledge.get("allow") or []
        read_denied_scopes = _normalize_scopes(knowledge.get("read_deny") or [])
        return cls(
            read_scopes=_normalize_scopes(read_values),
            # Wildcard mutation grants are intentionally ignored. Administrators
            # must name an exact writable/deletable subtree.
            write_scopes=tuple(scope for scope in _normalize_scopes(knowledge.get("write") or []) if scope != "*"),
            delete_scopes=tuple(scope for scope in _normalize_scopes(knowledge.get("delete") or []) if scope != "*"),
            denied_scopes=_normalize_scopes(knowledge.get("deny") or []),
            read_denied_scopes=read_denied_scopes,
            # Match the shared runtime policy: an explicit read denial also
            # prevents writes, while a write denial does not prevent reads.
            write_denied_scopes=read_denied_scopes + _normalize_scopes(knowledge.get("write_deny") or []),
            delete_denied_scopes=_normalize_scopes(knowledge.get("delete_deny") or []),
            project_table_read=bool(
                (tool_policy or {}).get("project_tables", {}).get("projects_read")
                or (tool_policy or {}).get("project_tables", {}).get("tables_read")
            ),
            project_table_write=bool((tool_policy or {}).get("project_tables", {}).get("tables_write")),
        )

    def allows_read_file(self, path: str) -> bool:
        return self._allows_file(path, self.read_scopes, self.read_denied_scopes)

    def allows_write_file(self, path: str) -> bool:
        return self._allows_file(path, self.write_scopes, self.write_denied_scopes)

    def allows_create_file(self, path: str) -> bool:
        """Allow creation only when the target parent is covered by a writable folder scope."""
        normalized = normalize_workspace_reference(path)
        if not normalized or normalized == "*" or self._is_denied(normalized, self.write_denied_scopes):
            return False
        parent = normalize_workspace_reference(str(PurePosixPath(normalized).parent))
        if not parent or parent == "." or self._is_denied(parent, self.write_denied_scopes):
            return False
        return any(_scope_contains(scope, parent) for scope in self.write_scopes)

    def allows_delete_file(self, path: str) -> bool:
        return self._allows_file(path, self.delete_scopes, self.delete_denied_scopes)

    def allows_move_source(self, path: str) -> bool:
        # Moving copies source bytes to another path. Do not let it disclose
        # explicitly unreadable data through a readable destination.
        return self.allows_delete_file(path) and not self._is_denied(path, self.read_denied_scopes)

    def allows_read_directory(self, path: str) -> bool:
        normalized = normalize_workspace_reference(path)
        if normalized == "*":
            normalized = ""
        if self._is_denied(normalized, self.read_denied_scopes):
            return False
        return any(
            not normalized
            or _scope_contains(scope, normalized)
            or _scope_contains(normalized, scope)
            for scope in self.read_scopes
        )

    def _allows_file(self, path: str, scopes: tuple[str, ...], action_denied_scopes: tuple[str, ...]) -> bool:
        normalized = normalize_workspace_reference(path)
        if not normalized or normalized == "*" or self._is_denied(normalized, action_denied_scopes):
            return False
        return any(_scope_contains(scope, normalized) for scope in scopes)

    def _is_denied(self, path: str, action_denied_scopes: tuple[str, ...]) -> bool:
        return any(_scope_contains(scope, path) for scope in self.denied_scopes + action_denied_scopes)


    def tool_permissions(self) -> frozenset[str]:
        """Coarse tool availability; every invocation still checks its exact paths."""
        permissions = set()
        readable = any(not self._is_denied(scope, self.read_denied_scopes) for scope in self.read_scopes)
        writable = any(not self._is_denied(scope, self.write_denied_scopes) for scope in self.write_scopes)
        deletable = any(not self._is_denied(scope, self.delete_denied_scopes) for scope in self.delete_scopes)
        if readable:
            permissions.update({"tool.workspace.list", "tool.workspace.locate", "tool.workspace.read", "tool.workspace.checkout"})
        if writable:
            permissions.add("tool.workspace.write")
        if deletable:
            permissions.add("tool.workspace.delete")
        if writable and any(self.allows_move_source(scope) for scope in self.delete_scopes):
            permissions.add("tool.workspace.move")
        if self.project_table_read:
            permissions.add("tool.project_table.read")
        if self.project_table_write:
            permissions.add("tool.project_table.write")
        return frozenset(permissions)

    def facts(self, readable_paths=()) -> dict[str, Any]:
        """Describe authorization, not file contents or the existence of denied paths."""
        return {
            "rules": {
                "read": list(self.read_scopes), "write": list(self.write_scopes),
                "delete": list(self.delete_scopes), "deny": list(self.denied_scopes),
                "read_deny": list(self.read_denied_scopes),
                "write_deny": list(dict.fromkeys(self.write_denied_scopes)),
                "delete_deny": list(self.delete_denied_scopes),
            },
            "readable_files": [
                {"logical_path": path, "read": True, "write": self.allows_write_file(path), "delete": self.allows_delete_file(path)}
                for path in sorted(set(readable_paths)) if self.allows_read_file(path)
            ],
            "semantics": "Deny overrides grants. read_deny also blocks writing; write_deny does not block reading. Folder grants have file exceptions. Rules alone do not prove existence or completed operations.",
        }


def _normalize_scopes(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    normalized = [normalize_workspace_reference(value) for value in values]
    return tuple(dict.fromkeys(value for value in normalized if value))


def _scope_contains(scope: str, path: str) -> bool:
    normalized_scope = normalize_workspace_reference(scope)
    normalized_path = normalize_workspace_reference(path)
    return bool(
        normalized_scope == "*"
        or normalized_path == normalized_scope
        or normalized_path.startswith(f"{normalized_scope}/")
    )
