from collections.abc import Iterable as IterableABC
import posixpath
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

WORKSPACE_TOOL_LIST = "workspace.list"
WORKSPACE_TOOL_LOCATE = "workspace.locate"
WORKSPACE_TOOL_READ = "workspace.read"
WORKSPACE_TOOL_UNZIP = "workspace.unzip"
WORKSPACE_TOOL_WRITE = "workspace.write"
WORKSPACE_TOOL_RENAME = "workspace.rename"
WORKSPACE_TOOL_DELETE = "workspace.delete"
REGISTERED_WORKSPACE_TOOLS = frozenset({
    WORKSPACE_TOOL_LIST,
    WORKSPACE_TOOL_LOCATE,
    WORKSPACE_TOOL_READ,
    WORKSPACE_TOOL_UNZIP,
    WORKSPACE_TOOL_WRITE,
    WORKSPACE_TOOL_RENAME,
    WORKSPACE_TOOL_DELETE,
})


def normalize_tool_ids(values: Any) -> list[str]:
    # Return registered tool IDs only, preserving caller order.
    return list(dict.fromkeys(
        item
        for item in (str(value or "").strip() for value in _iter_string_values(values))
        if item in REGISTERED_WORKSPACE_TOOLS
    ))


MEMORY_ACTION_READ = "read"
MEMORY_ACTION_WRITE = "write"
MEMORY_ACTION_UPDATE = "update"
MEMORY_ACTION_DELETE = "delete"
MEMORY_ACTIONS = (
    MEMORY_ACTION_READ,
    MEMORY_ACTION_WRITE,
    MEMORY_ACTION_UPDATE,
    MEMORY_ACTION_DELETE,
)


def _iter_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item or "") for item in value]
    if isinstance(value, IterableABC) and not isinstance(value, dict):
        return [str(item or "") for item in value]
    return []


def normalize_workspace_reference(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw in {"*", ".", "/"}:
        return "*"
    absolute_like = raw.startswith("/") or (len(raw) >= 3 and raw[1] == ":" and raw[2] == "/")
    if absolute_like:
        if "/workspace/" not in raw:
            return ""
        raw = raw.split("/workspace/", 1)[1]
    elif raw.startswith("workspace/"):
        raw = raw[len("workspace/"):]
    elif raw == "workspace":
        return "*"
    raw = raw.lstrip("/")
    if not raw:
        return "*"
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", "/"}:
        return "*"
    if normalized.startswith("../") or normalized == "..":
        return ""
    return normalized.strip("/")


def normalize_pattern_list(values: Any) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in _iter_string_values(values):
        candidate = str(item or "").strip()
        if not candidate:
            continue
        normalized.append(candidate)
    return tuple(dict.fromkeys(normalized))


def normalize_scope_list(values: Any) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in _iter_string_values(values):
        candidate = normalize_workspace_reference(item)
        if not candidate:
            continue
        normalized.append(candidate)
    return tuple(dict.fromkeys(normalized))


def normalize_knowledge_read_scopes(knowledge: dict[str, Any]) -> tuple[str, ...]:
    """Normalize read aliases without inheriting write access."""
    scopes: list[str] = []
    for key in (
        "read",
        "readable_scopes",
        "readableScopes",
        "knowledgeRead",
    ):
        scopes.extend(normalize_scope_list(knowledge.get(key)))
    return tuple(dict.fromkeys(scopes))


def normalize_knowledge_write_scopes(knowledge: dict[str, Any]) -> tuple[str, ...]:
    scopes: list[str] = []
    for key in ("write", "writable_scopes", "writableScopes", "knowledgeWrite"):
        scopes.extend(normalize_scope_list(knowledge.get(key)))
    return tuple(dict.fromkeys(scope for scope in scopes if scope != "*"))


def normalize_knowledge_delete_scopes(knowledge: dict[str, Any]) -> tuple[str, ...]:
    scopes: list[str] = []
    for key in ("delete", "deletable_scopes", "deletableScopes", "knowledgeDelete"):
        scopes.extend(normalize_scope_list(knowledge.get(key)))
    return tuple(dict.fromkeys(scope for scope in scopes if scope != "*"))


def normalize_knowledge_action_deny_scopes(
    knowledge: dict[str, Any],
    action: str,
) -> tuple[str, ...]:
    action_name = str(action or "").strip().lower()
    if action_name not in {"read", "write", "delete"}:
        return ()
    camel_action = action_name.title()
    scopes: list[str] = []
    for key in (
        f"{action_name}_deny",
        f"{action_name}Deny",
        f"knowledge{camel_action}Deny",
    ):
        scopes.extend(normalize_scope_list(knowledge.get(key)))
    return tuple(dict.fromkeys(scopes))


def normalize_optional_boolean(value: Any) -> bool | None:
    """Normalize API/config booleans while preserving an unspecified value."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in {"true", "1", "yes", "on"}:
            return True
        if candidate in {"false", "0", "no", "off"}:
            return False
    return None


def normalize_memory_permissions(memory: dict[str, Any]) -> dict[str, bool | None]:
    """Return a complete tri-state map for the four memory actions."""

    return {
        action: normalize_optional_boolean(memory.get(action))
        for action in MEMORY_ACTIONS
    }


def _pattern_matches(pattern: str, value: str) -> bool:
    pattern_value = str(pattern or "").strip()
    candidate = str(value or "").strip()
    if not pattern_value or not candidate:
        return False
    if pattern_value == "*":
        return True
    if pattern_value.endswith("*"):
        return candidate.startswith(pattern_value[:-1])
    return candidate == pattern_value


def _scope_matches(scope: str, logical_path: str) -> bool:
    scope_value = normalize_workspace_reference(scope)
    path_value = normalize_workspace_reference(logical_path)
    if not scope_value or not path_value:
        return False
    if scope_value == "*":
        return True
    return path_value == scope_value or path_value.startswith(f"{scope_value}/")


def _directory_intersects_scope(directory_path: str, scope: str) -> bool:
    directory_value = normalize_workspace_reference(directory_path)
    scope_value = normalize_workspace_reference(scope)
    if not scope_value:
        return False
    if directory_value in {"", "*"}:
        return True
    if scope_value == "*":
        return True
    return (
        directory_value == scope_value
        or directory_value.startswith(f"{scope_value}/")
        or scope_value.startswith(f"{directory_value}/")
    )


def _intersect_scope_lists(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    if not left:
        return tuple(dict.fromkeys(str(item) for item in right if item))
    if not right:
        return tuple(dict.fromkeys(str(item) for item in left if item))
    result: list[str] = []
    for left_scope in left:
        normalized_left = normalize_workspace_reference(left_scope)
        if not normalized_left:
            continue
        for right_scope in right:
            normalized_right = normalize_workspace_reference(right_scope)
            if not normalized_right:
                continue
            if normalized_left == "*" and normalized_right:
                result.append(normalized_right)
                continue
            if normalized_right == "*" and normalized_left:
                result.append(normalized_left)
                continue
            if _scope_matches(normalized_left, normalized_right):
                result.append(normalized_right)
                continue
            if _scope_matches(normalized_right, normalized_left):
                result.append(normalized_left)
    return tuple(dict.fromkeys(result))


def _extract_policy_block(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if "configJson" in payload and isinstance(payload.get("configJson"), dict):
        payload = payload.get("configJson") or {}
    permissions = payload.get("permissions")
    if isinstance(permissions, dict):
        return permissions
    return payload


def extract_global_permission_payload(payload: Any, agent_id: str = "") -> dict[str, Any]:
    block = _extract_policy_block(payload)
    if not isinstance(block, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("skills", "knowledge", "memory"):
        if isinstance(block.get(key), dict):
            result[key] = block.get(key)
    agent_key = str(agent_id or "").strip()
    if not agent_key:
        return result
    for override_key in ("agents", "agent_overrides", "agent_permission_bindings"):
        overrides = block.get(override_key)
        if not isinstance(overrides, dict):
            continue
        agent_payload = overrides.get(agent_key)
        if isinstance(agent_payload, dict):
            for key in ("skills", "knowledge", "memory"):
                if isinstance(agent_payload.get(key), dict):
                    result[key] = agent_payload.get(key)
            break
    return result


def extract_permission_layer(payload: Any) -> dict[str, dict[str, Any]]:
    block = _extract_policy_block(payload)
    skills = block.get("skills") if isinstance(block.get("skills"), dict) else {}
    knowledge = block.get("knowledge") if isinstance(block.get("knowledge"), dict) else {}
    memory = block.get("memory") if isinstance(block.get("memory"), dict) else {}
    return {
        "skills": {
            "include": normalize_pattern_list(skills.get("include")),
            "exclude": normalize_pattern_list(skills.get("exclude")),
        },
        "knowledge": {
            "read": normalize_knowledge_read_scopes(knowledge),
            "write": normalize_knowledge_write_scopes(knowledge),
            "delete": normalize_knowledge_delete_scopes(knowledge),
            "deny": normalize_scope_list(knowledge.get("deny")),
            "read_deny": normalize_knowledge_action_deny_scopes(knowledge, "read"),
            "write_deny": normalize_knowledge_action_deny_scopes(knowledge, "write"),
            "delete_deny": normalize_knowledge_action_deny_scopes(knowledge, "delete"),
        },
        "memory": normalize_memory_permissions(memory),
    }


@dataclass(frozen=True)
class EffectiveRuntimePolicy:
    mode: str
    skill_include_groups: tuple[tuple[str, ...], ...]
    skill_excludes: tuple[str, ...]
    knowledge_reads: tuple[str, ...]
    knowledge_writes: tuple[str, ...]
    knowledge_deletes: tuple[str, ...]
    knowledge_denies: tuple[str, ...]
    knowledge_read_denies: tuple[str, ...]
    knowledge_write_denies: tuple[str, ...]
    knowledge_delete_denies: tuple[str, ...]
    memory_read: bool
    memory_write: bool
    memory_update: bool
    memory_delete: bool
    default_allow_skills: bool
    default_allow_memory: bool

    def denies_path(self, logical_path: str) -> bool:
        candidate = normalize_workspace_reference(logical_path)
        if candidate in {"", "*"}:
            return False
        return any(_scope_matches(scope, candidate) for scope in self.knowledge_denies)

    def _denies_action_path(
        self,
        logical_path: str,
        scopes: tuple[str, ...],
    ) -> bool:
        candidate = normalize_workspace_reference(logical_path)
        if candidate in {"", "*"}:
            return False
        return self.denies_path(candidate) or any(
            _scope_matches(scope, candidate)
            for scope in scopes
        )

    def denies_read_path(self, logical_path: str) -> bool:
        return self._denies_action_path(logical_path, self.knowledge_read_denies)

    def denies_write_path(self, logical_path: str) -> bool:
        # A path that cannot be read must never remain writable.
        return self.denies_read_path(logical_path) or self._denies_action_path(
            logical_path,
            self.knowledge_write_denies,
        )

    def denies_delete_path(self, logical_path: str) -> bool:
        return self._denies_action_path(logical_path, self.knowledge_delete_denies)

    def allows_skill(self, skill_id: str) -> bool:
        candidate = str(skill_id or "").strip()
        if not candidate:
            return False
        if any(_pattern_matches(pattern, candidate) for pattern in self.skill_excludes):
            return False
        if not self.skill_include_groups:
            return bool(self.default_allow_skills)
        return all(
            any(_pattern_matches(pattern, candidate) for pattern in group)
            for group in self.skill_include_groups
        )

    def allows_tool(self, tool_name: str) -> bool:
        return str(tool_name or "").strip() in {
            WORKSPACE_TOOL_LIST,
            WORKSPACE_TOOL_LOCATE,
            WORKSPACE_TOOL_READ,
            WORKSPACE_TOOL_UNZIP,
            WORKSPACE_TOOL_WRITE,
            WORKSPACE_TOOL_RENAME,
            WORKSPACE_TOOL_DELETE,
        }

    def allows_path(self, logical_path: str) -> bool:
        candidate = normalize_workspace_reference(logical_path)
        if candidate in {"", "*"}:
            return bool(self.knowledge_reads)
        if self.denies_read_path(candidate):
            return False
        if not self.knowledge_reads:
            return False
        return any(_scope_matches(scope, candidate) for scope in self.knowledge_reads)

    def allows_write_path(self, logical_path: str) -> bool:
        return self._allows_action_path(
            logical_path,
            self.knowledge_writes,
            self.knowledge_write_denies,
            deny_read=True,
        )

    def allows_delete_path(self, logical_path: str) -> bool:
        return self._allows_action_path(
            logical_path,
            self.knowledge_deletes,
            self.knowledge_delete_denies,
        )

    def _allows_action_path(
        self,
        logical_path: str,
        scopes: tuple[str, ...],
        deny_scopes: tuple[str, ...],
        *,
        deny_read: bool = False,
    ) -> bool:
        candidate = normalize_workspace_reference(logical_path)
        if candidate in {"", "*"}:
            return False
        if deny_read and self.denies_read_path(candidate):
            return False
        if self._denies_action_path(candidate, deny_scopes):
            return False
        return any(_scope_matches(scope, candidate) for scope in scopes)

    def allows_directory(self, logical_path: str = "") -> bool:
        directory_value = normalize_workspace_reference(logical_path)
        if directory_value in {"", "*"}:
            return bool(self.knowledge_reads)
        if self.denies_read_path(directory_value):
            return False
        if self.allows_path(directory_value):
            return True
        if not self.knowledge_reads:
            return False
        return any(
            normalize_workspace_reference(scope).startswith(f"{directory_value}/")
            for scope in self.knowledge_reads
        )

    def allows_memory(self, action: str) -> bool:
        candidate = str(action or "").strip().lower()
        if candidate.startswith("memory."):
            candidate = candidate.split(".", 1)[1]
        permissions = {
            MEMORY_ACTION_READ: self.memory_read,
            MEMORY_ACTION_WRITE: self.memory_write,
            MEMORY_ACTION_UPDATE: self.memory_update,
            MEMORY_ACTION_DELETE: self.memory_delete,
        }
        return bool(permissions.get(candidate, False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "defaults": {
                "allow_skills_without_allowlist": bool(self.default_allow_skills),
                "allow_memory_without_explicit_binding": bool(self.default_allow_memory),
            },
            "skills": {
                "include_groups": [list(group) for group in self.skill_include_groups],
                "exclude": list(self.skill_excludes),
            },
            "knowledge": {
                "read": list(self.knowledge_reads),
                "write": list(self.knowledge_writes),
                "delete": list(self.knowledge_deletes),
                "deny": list(self.knowledge_denies),
                "read_deny": list(self.knowledge_read_denies),
                "write_deny": list(self.knowledge_write_denies),
                "delete_deny": list(self.knowledge_delete_denies),
            },
            "memory": {
                MEMORY_ACTION_READ: self.memory_read,
                MEMORY_ACTION_WRITE: self.memory_write,
                MEMORY_ACTION_UPDATE: self.memory_update,
                MEMORY_ACTION_DELETE: self.memory_delete,
            },
        }


def build_effective_runtime_policy(
    *,
    global_config: Any = None,
    template: Any = None,
    binding: Any = None,
    global_enabled: bool = True,
    template_enabled: bool = True,
    binding_enabled: bool = True,
    agent_id: str = "",
    require_explicit_binding: bool = False,
    default_allow_skills: bool | None = None,
    default_allow_tools: bool | None = None,  # Retired compatibility argument; ignored.
    default_allow_memory: bool | None = None,
) -> EffectiveRuntimePolicy:
    active_layers: list[dict[str, dict[str, Any]]] = []
    default_allow_skills_value = (
        bool(default_allow_skills)
        if default_allow_skills is not None
        else not require_explicit_binding
    )
    default_allow_memory_value = (
        bool(default_allow_memory)
        if default_allow_memory is not None
        else not require_explicit_binding
    )

    if require_explicit_binding and not binding_enabled:
        return EffectiveRuntimePolicy(
            mode="explicit_binding_required",
            skill_include_groups=(),
            skill_excludes=(),
            knowledge_reads=(),
            knowledge_writes=(),
            knowledge_deletes=(),
            knowledge_denies=(),
            knowledge_read_denies=(),
            knowledge_write_denies=(),
            knowledge_delete_denies=(),
            memory_read=default_allow_memory_value,
            memory_write=default_allow_memory_value,
            memory_update=default_allow_memory_value,
            memory_delete=default_allow_memory_value,
            default_allow_skills=False,
            default_allow_memory=default_allow_memory_value,
        )

    if global_enabled and global_config:
        global_payload = extract_global_permission_payload(global_config, agent_id=agent_id)
        if global_payload:
            active_layers.append(extract_permission_layer(global_payload))
    if binding_enabled and template_enabled and template:
        active_layers.append(extract_permission_layer(template))
    if binding_enabled and binding:
        active_layers.append(extract_permission_layer(binding))

    skill_include_groups = tuple(
        layer["skills"]["include"]
        for layer in active_layers
        if layer["skills"]["include"]
    )
    skill_excludes = normalize_pattern_list(
        pattern
        for layer in active_layers
        for pattern in layer["skills"]["exclude"]
    )
    knowledge_reads: tuple[str, ...] = ()
    knowledge_writes: tuple[str, ...] = ()
    knowledge_deletes: tuple[str, ...] = ()
    for layer in active_layers:
        layer_reads = layer["knowledge"]["read"]
        layer_writes = layer["knowledge"]["write"]
        layer_deletes = layer["knowledge"]["delete"]
        if layer_reads:
            knowledge_reads = _intersect_scope_lists(knowledge_reads, layer_reads) if knowledge_reads else layer_reads
        if layer_writes:
            knowledge_writes = _intersect_scope_lists(knowledge_writes, layer_writes) if knowledge_writes else layer_writes
        if layer_deletes:
            knowledge_deletes = _intersect_scope_lists(knowledge_deletes, layer_deletes) if knowledge_deletes else layer_deletes
    knowledge_denies = normalize_scope_list(
        scope
        for layer in active_layers
        for scope in layer["knowledge"]["deny"]
    )
    knowledge_reads = tuple(
        scope
        for scope in knowledge_reads
        if scope and not any(_scope_matches(deny_scope, scope) for deny_scope in knowledge_denies)
    )
    knowledge_read_denies = normalize_scope_list(
        scope
        for layer in active_layers
        for scope in layer["knowledge"]["read_deny"]
    )
    knowledge_write_denies = normalize_scope_list(
        scope
        for layer in active_layers
        for scope in layer["knowledge"]["write_deny"]
    )
    knowledge_delete_denies = normalize_scope_list(
        scope
        for layer in active_layers
        for scope in layer["knowledge"]["delete_deny"]
    )
    knowledge_writes = tuple(
        scope
        for scope in knowledge_writes
        if scope and not any(_scope_matches(deny_scope, scope) for deny_scope in knowledge_denies)
    )
    knowledge_deletes = tuple(
        scope
        for scope in knowledge_deletes
        if scope and not any(_scope_matches(deny_scope, scope) for deny_scope in knowledge_denies)
    )
    memory_permissions: dict[str, bool] = {}
    for action in MEMORY_ACTIONS:
        # Permission layers merge restrictively: one explicit deny wins.
        explicit_values = [
            layer["memory"][action]
            for layer in active_layers
            if layer["memory"][action] is not None
        ]
        memory_permissions[action] = (
            all(explicit_values)
            if explicit_values
            else default_allow_memory_value
        )

    return EffectiveRuntimePolicy(
        mode="explicit_binding_required" if require_explicit_binding else "compatibility",
        skill_include_groups=skill_include_groups,
        skill_excludes=skill_excludes,
        knowledge_reads=knowledge_reads,
        knowledge_writes=knowledge_writes,
        knowledge_deletes=knowledge_deletes,
        knowledge_denies=knowledge_denies,
        knowledge_read_denies=knowledge_read_denies,
        knowledge_write_denies=knowledge_write_denies,
        knowledge_delete_denies=knowledge_delete_denies,
        memory_read=memory_permissions[MEMORY_ACTION_READ],
        memory_write=memory_permissions[MEMORY_ACTION_WRITE],
        memory_update=memory_permissions[MEMORY_ACTION_UPDATE],
        memory_delete=memory_permissions[MEMORY_ACTION_DELETE],
        default_allow_skills=default_allow_skills_value,
        default_allow_memory=default_allow_memory_value,
    )


def select_active_bound_skills(
    *,
    skills: Iterable[dict[str, Any]],
    bindings: Iterable[dict[str, Any]],
    policy: EffectiveRuntimePolicy,
) -> list[dict[str, Any]]:
    skills_by_id = {
        str(item.get("skill_id") or "").strip(): item
        for item in skills
        if isinstance(item, dict) and str(item.get("skill_id") or "").strip()
    }
    ordered_bindings = sorted(
        (
            item
            for item in bindings
            if isinstance(item, dict)
        ),
        key=lambda entry: (
            int(entry.get("sort_order") or 0),
            str(entry.get("skill_id") or ""),
        ),
    )
    active_skills: list[dict[str, Any]] = []
    for binding in ordered_bindings:
        if not bool(binding.get("enabled", False)):
            continue
        skill_id = str(binding.get("skill_id") or "").strip()
        if not skill_id:
            continue
        skill = skills_by_id.get(skill_id)
        if not isinstance(skill, dict):
            continue
        if not bool(skill.get("enabled", True)):
            continue
        if str(skill.get("status") or "").strip().lower() == "deprecated":
            continue
        if not policy.allows_skill(skill_id):
            continue
        active_skills.append(
            {
                "binding": binding,
                "skill": skill,
            }
        )
    return active_skills
