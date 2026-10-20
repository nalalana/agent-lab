"""
/backend/apps/runtime/services.py
OpenClaw runtime services for the V2 platform control plane.
"""
import base64
import hashlib
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable, Optional
from urllib.parse import urlparse, urlunparse

import requests
from django.conf import settings

from apps.company.models import (
    Company,
    CompanyApiCredential,
    CompanySystemSetting,
    CompanyModelPolicy,
    CompanyPermissionConfig,
    CompanySessionMemoryPolicy,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentVikingSpace,
    TransparentCompanyAgentSkillBinding,
    TransparentCompanyPermissionTemplate,
    TransparentCompanySkill,
    TransparentCompanySkillApiCall,
)
from apps.company.permissions import can_access_formal_company
from apps.company.openclaw_model_catalog import (
    DEFAULT_OPENCLAW_VERSION,
    get_openclaw_model_catalog,
    normalize_catalog_model_id,
    normalize_model_source_mode,
)
from apps.iam.models import Grant, Principal
from apps.moderation.models import ImageModerationAsset
from apps.moderation.services import ImageModerationService, is_image_file
from apps.runtime.models import (
    CompanyRuntimeBinding,
    OpenClawAuditLog,
    OpenClawConfigVersion,
    OpenClawInstance,
    OpenClawProvisionTask,
    OpenClawRuntimeSnapshot,
    RuntimeAlertPolicy,
    RuntimeDriftEvent,
    RuntimeRolloutPolicy,
    RuntimeServerResource,
    WorkspaceFile,
    WorkspaceTransferTask,
)
from apps.runtime.policy import (
    WORKSPACE_TOOL_LIST,
    WORKSPACE_TOOL_LOCATE,
    WORKSPACE_TOOL_READ,
    WORKSPACE_TOOL_UNZIP,
    WORKSPACE_TOOL_WRITE,
    WORKSPACE_TOOL_RENAME,
    WORKSPACE_TOOL_DELETE,
    build_effective_runtime_policy,
    normalize_workspace_reference,
    select_active_bound_skills,
)
from apps.runtime.security import (
    sanitize_agent_test_runtime_payload,
    sanitize_runtime_debug_payload,
    sanitize_user_visible_error,
    validate_workspace_logical_path,
)
from apps.user.models import User
from common.utils.Tengcentcos_utils import get_object, get_presigned_upload_url, get_presigned_url, put_object_with_key
from apps.notification.services import NotificationService


logger = logging.getLogger("touming")


DEFAULT_DOCKER_HOST_ID = "docker-shared-host-01"
DEFAULT_GATEWAY_PORT = 18789
DEFAULT_RUNTIME_ROOT = "/data/tsds/tenants"
DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS = 3600
RUNTIME_REQUEST_SCOPE_INTERNAL_AGENT_TEST = "internal_admin_agent_test"
RUNTIME_REQUEST_SCOPE_CLIENT_ENTRY = "client_entry_public"
TOUMING_WORKSPACE_TOOL_LIST = "touming_workspace_list"
TOUMING_WORKSPACE_TOOL_LOCATE = "touming_workspace_locate"
TOUMING_WORKSPACE_TOOL_READ = "touming_workspace_read"
TOUMING_WORKSPACE_TOOL_UNZIP = "touming_workspace_unzip"
TOUMING_WORKSPACE_TOOL_WRITE = "touming_workspace_write"
TOUMING_WORKSPACE_TOOL_RENAME = "touming_workspace_rename"
TOUMING_WORKSPACE_TOOL_DELETE = "touming_workspace_delete"

TOUMING_WORKSPACE_TOOL_MAP = {
    TOUMING_WORKSPACE_TOOL_LIST: WORKSPACE_TOOL_LIST,
    TOUMING_WORKSPACE_TOOL_LOCATE: WORKSPACE_TOOL_LOCATE,
    TOUMING_WORKSPACE_TOOL_READ: WORKSPACE_TOOL_READ,
    TOUMING_WORKSPACE_TOOL_UNZIP: WORKSPACE_TOOL_UNZIP,
    TOUMING_WORKSPACE_TOOL_WRITE: WORKSPACE_TOOL_WRITE,
    TOUMING_WORKSPACE_TOOL_RENAME: WORKSPACE_TOOL_RENAME,
    TOUMING_WORKSPACE_TOOL_DELETE: WORKSPACE_TOOL_DELETE,
    WORKSPACE_TOOL_LIST: WORKSPACE_TOOL_LIST,
    WORKSPACE_TOOL_LOCATE: WORKSPACE_TOOL_LOCATE,
    WORKSPACE_TOOL_READ: WORKSPACE_TOOL_READ,
    WORKSPACE_TOOL_UNZIP: WORKSPACE_TOOL_UNZIP,
    WORKSPACE_TOOL_WRITE: WORKSPACE_TOOL_WRITE,
    WORKSPACE_TOOL_RENAME: WORKSPACE_TOOL_RENAME,
    WORKSPACE_TOOL_DELETE: WORKSPACE_TOOL_DELETE,
}

WORKSPACE_WRITE_ALLOWED_ENCODINGS = {"utf-8", "utf8", "text"}
WORKSPACE_WRITE_BLOCKED_SEGMENTS = {".ssh", ".git", ".svn", ".hg", "__pycache__"}
DEFAULT_AGENT_WORKSPACE_ARTIFACT_ROOT = "artifacts"


def _now_ts() -> str:
    return str(time.time())[:20]


def _is_knowledge_resource_path(logical_path: Any) -> bool:
    normalized = str(logical_path or "").strip().strip("/")
    return normalized == "knowledge" or normalized.startswith("knowledge/")


def _agent_workspace_artifact_root() -> str:
    raw_root = str(
        getattr(settings, "RUNTIME_AGENT_ARTIFACT_LOGICAL_ROOT", DEFAULT_AGENT_WORKSPACE_ARTIFACT_ROOT)
        or DEFAULT_AGENT_WORKSPACE_ARTIFACT_ROOT
    ).strip()
    try:
        validated_root = validate_workspace_logical_path(raw_root, "artifact_root")
    except ValueError:
        validated_root = DEFAULT_AGENT_WORKSPACE_ARTIFACT_ROOT
    normalized_root = normalize_workspace_reference(validated_root)
    return normalized_root if normalized_root not in {"", "*"} else DEFAULT_AGENT_WORKSPACE_ARTIFACT_ROOT


def _prefix_agent_workspace_artifact_path(logical_path: str) -> str:
    normalized_path = normalize_workspace_reference(logical_path)
    artifact_root = _agent_workspace_artifact_root()
    if normalized_path == artifact_root or normalized_path.startswith(f"{artifact_root}/"):
        return normalized_path
    return f"{artifact_root}/{normalized_path}"


def _runtime_timestamp(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _is_runtime_timestamp_due(value: Any, now_ts: float | None = None) -> bool:
    parsed = _runtime_timestamp(value)
    if parsed is None:
        return False
    return parsed <= (now_ts if now_ts is not None else time.time())


def _dump_payload(payload: Optional[dict[str, Any]]) -> str:
    if not payload:
        return ""
    return json.dumps(payload, ensure_ascii=False)


def _load_payload(payload: str) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_markdown_section(content: str, title: str) -> str:
    match = re.search(rf"^##\s+{re.escape(title)}\s*\n([\s\S]*?)(?=\n##\s+|\Z)", str(content or ""), re.M)
    return (match.group(1).strip() if match else "")


def _parse_skill_content(content: str) -> dict[str, Any]:
    raw = str(content or "").strip()
    upload_history = [
        line.strip()[2:].strip()
        for line in _extract_markdown_section(raw, "上传历史").splitlines()
        if line.strip().startswith("- ")
    ]
    shell_script = _extract_markdown_section(raw, "SH 脚本")
    if shell_script == "未上传脚本。":
        shell_script = ""
    return {
        "usage": _extract_markdown_section(raw, "使用方法"),
        "description": _extract_markdown_section(raw, "描述"),
        "instructions": _extract_markdown_section(raw, "指令") or raw,
        "shell_script": shell_script,
        "upload_history": upload_history,
    }


def _runtime_namespace(company: Company) -> str:
    return f"tsds-{company.cid[:12]}"


def _runtime_root(company: Company) -> str:
    return f"{DEFAULT_RUNTIME_ROOT}/{_runtime_namespace(company)}"


def _runtime_config_path(company: Company) -> str:
    return f"{_runtime_root(company)}/config/openclaw.json"


def _runtime_workspace_prefix(company: Company) -> str:
    return f"{_runtime_root(company)}/workspace"


def _client_entry_key(agent: TransparentCompanyAgentDefinition) -> str:
    route_key = str(getattr(agent, "routeKey", "") or "").strip()
    if route_key:
        return route_key
    if bool(getattr(agent, "isDefaultEntry", False)):
        return "__default__"
    return ""


def _is_public_client_entry_agent(agent: TransparentCompanyAgentDefinition) -> bool:
    client_visibility = str(getattr(agent, "clientVisibility", "") or "").strip()
    return (
        bool(getattr(agent, "enabled", False))
        and bool(getattr(agent, "exposedToClient", False))
        and client_visibility
        in {
            TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
            TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_LISTED,
            TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT,
        }
        and not bool(getattr(agent, "isCoreAgent", False))
    )


def _collect_client_entry_bindings(
    exposed_agents: Iterable[TransparentCompanyAgentDefinition],
    *,
    preferred_default_agent_id: str = "",
) -> tuple[list[dict[str, Any]], Optional[TransparentCompanyAgentDefinition]]:
    preferred_default = str(preferred_default_agent_id or "").strip()
    bindings: list[dict[str, Any]] = []
    default_binding: Optional[dict[str, Any]] = None

    for agent in exposed_agents:
        entry_key = _client_entry_key(agent)
        if not entry_key:
            continue
        binding = {
            "entry_key": entry_key,
            "agent_id": agent.agentId,
            "name": agent.name,
            "route_key": str(getattr(agent, "routeKey", "") or "").strip(),
            "exposure_order": int(getattr(agent, "exposureOrder", 100) or 100),
            "is_default_entry": bool(getattr(agent, "isDefaultEntry", False)),
        }
        bindings.append(binding)
        if preferred_default and binding["agent_id"] == preferred_default:
            default_binding = binding
        elif default_binding is None and binding["is_default_entry"]:
            default_binding = binding

    if default_binding is None and bindings:
        default_binding = bindings[0]
    default_agent_id = str(default_binding.get("agent_id") or "").strip() if default_binding else ""
    default_agent = next(
        (agent for agent in exposed_agents if str(getattr(agent, "agentId", "") or "").strip() == default_agent_id),
        None,
    )
    return bindings, default_agent


def _provider_secret_env_names(provider_id: str) -> list[str]:
    provider = str(provider_id or "").strip().lower()
    mapping = {
        "openai": ["OPENAI_API_KEY"],
        "anthropic": ["ANTHROPIC_API_KEY"],
        "deepseek": ["DEEPSEEK_API_KEY"],
        "qwen": ["QWEN_API_KEY"],
        "moonshot": ["MOONSHOT_API_KEY"],
        "kimi": ["MOONSHOT_API_KEY"],
        "minimax": ["MINIMAX_API_KEY"],
        "glm": ["GLM_API_KEY"],
        "openrouter": ["OPENROUTER_API_KEY"],
        "groq": ["GROQ_API_KEY"],
        "mistral": ["MISTRAL_API_KEY"],
        "together": ["TOGETHER_API_KEY"],
        "cerebras": ["CEREBRAS_API_KEY"],
        "xai": ["XAI_API_KEY"],
        "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"],
        "gemini": ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"],
    }
    return mapping.get(provider, [])


def _decode_secret(secret_encrypted: str) -> str:
    if not secret_encrypted:
        return ""
    try:
        return base64.b64decode(secret_encrypted.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def _selected_provider_env(company: Company) -> dict[str, str]:
    # Company-provided model credentials are forbidden.
    return {}


def _gateway_endpoint(company: Company) -> str:
    return f"http://{_runtime_namespace(company)}-gateway:{DEFAULT_GATEWAY_PORT}"


class OpenClawConfigService:
    @staticmethod
    def _build_version_name(company: Company) -> str:
        return f"{company.cid}-{int(time.time())}"

    @staticmethod
    def _build_skill_item(skill: TransparentCompanySkill) -> dict[str, Any]:
        api_calls = TransparentCompanySkillApiCall.objects.filter(skill=skill).order_by("id")
        parsed_content = _parse_skill_content(skill.content or "")
        description = parsed_content.get("description") or skill.shortDescription or ""
        instructions = parsed_content.get("instructions") or skill.content or ""
        return {
            "skill_id": skill.skillId,
            "name": skill.name,
            "description": description,
            "usage": parsed_content.get("usage") or "",
            "instructions": instructions,
            "prompt": skill.content or instructions,
            "shell_script": parsed_content.get("shell_script") or "",
            "upload_history": parsed_content.get("upload_history") or [],
            "enabled": bool(skill.enabled),
            "status": skill.status,
            "source_type": getattr(skill, "sourceType", "repo"),
            "runtime_status": getattr(skill, "runtimeStatus", "pending"),
            "is_system_skill": bool(getattr(skill, "isSystemSkill", False)),
            "category": skill.category,
            "priority": skill.priority,
            "author": skill.author or "",
            "last_published_at": getattr(skill, "lastPublishedAt", None),
            "apis": [
                {
                    "name": api_call.apiName,
                    "method": api_call.method,
                    "url": api_call.url,
                    "description": api_call.description or "",
                    "auth_type": api_call.authType or "",
                    "timeout_seconds": api_call.timeoutSeconds,
                    "retry_count": api_call.retryCount,
                    "enabled": bool(api_call.enabled),
                }
                for api_call in api_calls
            ],
        }

    @staticmethod
    def _build_agent_item(agent: TransparentCompanyAgentDefinition) -> dict[str, Any]:
        return {
            "agent_id": agent.agentId,
            "name": agent.name,
            "role": agent.role,
            "description": agent.description or "",
            "system_prompt": agent.systemPrompt or "",
            "visibility": agent.visibility,
            "client_visibility": getattr(agent, "clientVisibility", "hidden"),
            "exposed_to_client": bool(getattr(agent, "exposedToClient", False)),
            "is_default_entry": bool(getattr(agent, "isDefaultEntry", False)),
            "route_key": getattr(agent, "routeKey", "") or "",
            "exposure_order": int(getattr(agent, "exposureOrder", 100) or 100),
            "enabled": bool(agent.enabled),
            "is_core_agent": bool(agent.isCoreAgent),
            "status": agent.status,
            "sort_order": agent.sortOrder,
        }

    @staticmethod
    def _build_binding_item(binding: TransparentCompanyAgentSkillBinding) -> dict[str, Any]:
        return {
            "agent_id": binding.agent.agentId,
            "skill_id": binding.skill.skillId,
            "enabled": bool(binding.enabled),
            "sort_order": binding.sortOrder,
            "note": binding.note or "",
        }

    @staticmethod
    def _build_model_policy_item(company: Company) -> dict[str, Any]:
        policy = getattr(company, "modelPolicy", None)
        catalog = get_openclaw_model_catalog()
        provider_id = str(getattr(settings, "AGENT_RUNTIME_PLATFORM_PROVIDER_ID", "custom-qwen"))
        default_model_id = str(getattr(settings, "AGENT_RUNTIME_PLATFORM_MODEL_ID", "qwen3.8-27b"))
        default_model_id = normalize_catalog_model_id(default_model_id, catalog.version)
        return {
            "source_mode": CompanyModelPolicy.SOURCE_PLATFORM,
            "status": policy.status if policy else CompanyModelPolicy.STATUS_ACTIVE,
            "package_id": policy.package.packageId if policy and policy.package else "",
            "provider_id": provider_id,
            "default_model_id": default_model_id,
            "selected_credential_name": "",
            "rate_limit_rpm": policy.rateLimitRpm if policy else 60,
            "rate_limit_tpm": policy.rateLimitTpm if policy else 120000,
            "fallback_to_platform": True,
            "openclaw_version": catalog.version or DEFAULT_OPENCLAW_VERSION,
            "catalog_source": "openclaw_version_catalog",
            "active_credentials": [],
        }

    @staticmethod
    def _build_memory_policy_item(company: Company) -> dict[str, Any]:
        policy = getattr(company, "sessionMemoryPolicy", None)
        return {
            "mode": policy.mode if policy else CompanySessionMemoryPolicy.MODE_SESSION,
            "status": policy.status if policy else CompanySessionMemoryPolicy.STATUS_ACTIVE,
            "enabled": bool(policy.enabled) if policy else True,
            "recent_message_window": policy.recentMessageWindow if policy else 12,
            "max_tokens": policy.maxTokens if policy else 4000,
            "summary_trigger_tokens": policy.summaryTriggerTokens if policy else 2800,
            "summary_max_tokens": policy.summaryMaxTokens if policy else 800,
            "summary_style": policy.summaryStyle if policy else CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE,
            "session_ttl_days": policy.sessionTtlDays if policy else 30,
            "redact_pii": bool(policy.redactPii) if policy else False,
        }

    @staticmethod
    def _build_system_setting_item(company: Company) -> dict[str, Any]:
        setting = getattr(company, "systemSetting", None)
        return {
            "display_name": setting.displayName if setting else company.company_name,
            "system_description": setting.systemDescription if setting else (company.description or ""),
            "enable_public_entry": bool(setting.enablePublicEntry) if setting else True,
            "show_monitoring_cards": bool(setting.showMonitoringCards) if setting else True,
            "require_diff_before_publish": bool(setting.requireDiffBeforePublish) if setting else True,
            "alert_on_drift": bool(setting.alertOnDrift) if setting else True,
            "alert_webhook": setting.alertWebhook if setting else "",
            "allow_browser_direct_cos_upload": bool(setting.allowBrowserDirectCosUpload) if setting else True,
            "workspace_prefix": setting.workspacePrefix if setting else f"/workspace/{company.cid}",
            "sync_timeout_seconds": setting.syncTimeoutSeconds if setting else 30,
            "default_entry_agent_id": setting.defaultEntryAgentId if setting else "",
            "default_model_id": setting.defaultModelId if setting else "",
            "save_agent_test_sessions": bool(setting.saveAgentTestSessions) if setting else True,
            "notify_publish_success": bool(setting.notifyPublishSuccess) if setting else True,
            "notify_audit_tasks": bool(setting.notifyAuditTasks) if setting else True,
            "notify_drift_detected": bool(setting.notifyDriftDetected) if setting else True,
            "enabled": bool(setting.enabled) if setting else True,
        }

    @staticmethod
    def _build_agent_memory_binding_item(binding: TransparentCompanyAgentMemoryBinding) -> dict[str, Any]:
        return {
            "agent_id": binding.agent.agentId,
            "memory_scope": binding.memoryScope,
            "enabled": bool(binding.enabled),
            "inherit_company_policy": bool(binding.inheritCompanyPolicy),
            "max_tokens_override": binding.maxTokensOverride,
            "summary_style_override": binding.summaryStyleOverride or "",
            "note": binding.note or "",
            "status": binding.status,
        }

    @staticmethod
    def _build_permission_template_item(template: TransparentCompanyPermissionTemplate) -> dict[str, Any]:
        return {
            "template_id": template.templateId,
            "name": template.name,
            "description": template.description or "",
            "agent_type": template.agentType or "",
            "skills": {
                "include": list(template.skillsInclude or []),
                "exclude": list(template.skillsExclude or []),
            },
            "knowledge": {
                "read": list(template.knowledgeRead or []),
                "write": list(template.knowledgeWrite or []),
                "delete": list(template.knowledgeDelete or []),
                "deny": list(template.knowledgeDeny or []),
                "read_deny": list(template.knowledgeReadDeny or []),
                "write_deny": list(template.knowledgeWriteDeny or []),
                "delete_deny": list(template.knowledgeDeleteDeny or []),
            },
            "memory": {
                "read": template.memoryRead,
                "write": template.memoryWrite,
                "update": template.memoryUpdate,
                "delete": template.memoryDelete,
            },
            "enabled": bool(template.enabled),
            "status": template.status,
        }

    @staticmethod
    def _build_agent_permission_binding_item(binding: TransparentCompanyAgentPermissionBinding) -> dict[str, Any]:
        return {
            "agent_id": binding.agent.agentId,
            "template_id": binding.template.templateId if binding.template else "",
            "template_name": binding.template.name if binding.template else "自定义",
            "skills": {
                "include": list(binding.skillsInclude or []),
                "exclude": list(binding.skillsExclude or []),
            },
            "knowledge": {
                "read": list(binding.knowledgeRead or []),
                "write": list(binding.knowledgeWrite or []),
                "delete": list(binding.knowledgeDelete or []),
                "deny": list(binding.knowledgeDeny or []),
                "read_deny": list(binding.knowledgeReadDeny or []),
                "write_deny": list(binding.knowledgeWriteDeny or []),
                "delete_deny": list(binding.knowledgeDeleteDeny or []),
            },
            "memory": {
                "read": binding.memoryRead,
                "write": binding.memoryWrite,
                "update": binding.memoryUpdate,
                "delete": binding.memoryDelete,
            },
            "enabled": bool(binding.enabled),
            "status": binding.status,
        }

    @staticmethod
    def _build_agent_viking_space_item(space: TransparentCompanyAgentVikingSpace) -> dict[str, Any]:
        """Publish routing metadata without exposing OpenViking credentials."""

        return {
            "agent_id": space.agent.agentId,
            "viking_space_id": space.vikingSpaceId,
            "account_id": space.accountId,
            "user_id": space.userId,
            "root_uri": space.rootUri,
            "status": space.status,
        }

    @staticmethod
    def build_company_preview(company: Company) -> tuple[bool, dict[str, Any] | str]:
        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, "璇ュ叕鍙歌繕鏈崌绾т负閫忔槑鍏徃"

        skills = list(
            TransparentCompanySkill.objects.filter(transparentCompany=transparent_company)
            .order_by("priority", "id")
        )
        agent_definitions = list(
            TransparentCompanyAgentDefinition.objects.filter(transparentCompany=transparent_company)
            .order_by("sortOrder", "id")
        )
        skill_bindings = list(
            TransparentCompanyAgentSkillBinding.objects.filter(transparentCompany=transparent_company)
            .select_related("agent", "skill")
            .order_by("sortOrder", "id")
        )
        agent_memory_bindings = list(
            TransparentCompanyAgentMemoryBinding.objects.filter(transparentCompany=transparent_company)
            .select_related("agent")
            .order_by("agent__sortOrder", "id")
        )
        permission_templates = list(
            TransparentCompanyPermissionTemplate.objects.filter(transparentCompany=transparent_company)
            .order_by("name", "id")
        )
        agent_permission_bindings = list(
            TransparentCompanyAgentPermissionBinding.objects.filter(transparentCompany=transparent_company)
            .select_related("agent", "template")
            .order_by("agent__sortOrder", "id")
        )
        agent_viking_spaces = list(
            # Publish routing metadata with runtime config; credentials stay in
            # bridge environment variables and never enter company snapshots.
            TransparentCompanyAgentVikingSpace.objects.filter(transparentCompany=transparent_company)
            .select_related("agent")
            .order_by("agent__sortOrder", "id")
        )
        permission_config = CompanyPermissionConfig.objects.filter(company=company).first()

        if not agent_definitions:
            now = _now_ts()
            ceo_agent_id = f"{company.cid}-ceo"
            ceo_agent = TransparentCompanyAgentDefinition(
                transparentCompany=transparent_company,
                agentId=ceo_agent_id,
                name=f"{company.company_name} CEO",
                role="main_agent",
                description="ceo",
                systemPrompt=transparent_company.prompts or "",
                visibility=TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL,
                clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
                exposedToClient=False,
                isDefaultEntry=False,
                routeKey="",
                exposureOrder=100,
                enabled=True,
                isCoreAgent=True,
                status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
                sortOrder=0,
                created_at=now,
                updated_at=now,
            )
            agent_definitions = [ceo_agent]
            default_bindings: list[TransparentCompanyAgentSkillBinding] = []
            for index, skill in enumerate(skills, start=1):
                default_bindings.append(
                    TransparentCompanyAgentSkillBinding(
                        transparentCompany=transparent_company,
                        agent=ceo_agent,
                        skill=skill,
                        enabled=False,
                        sortOrder=index,
                        note="legacy fallback binding",
                        created_at=now,
                        updated_at=now,
                    )
                )
            skill_bindings = default_bindings

        exposed_agents = sorted(
            [
                agent for agent in agent_definitions
                if _is_public_client_entry_agent(agent)
            ],
            key=lambda item: (
                0 if bool(getattr(item, "isDefaultEntry", False)) else 1,
                int(getattr(item, "exposureOrder", 100) or 100),
                int(getattr(item, "sortOrder", 100) or 100),
                str(getattr(item, "agentId", "")),
            ),
        )
        system_setting = getattr(company, "systemSetting", None)
        client_entry_bindings, default_entry_agent = _collect_client_entry_bindings(
            exposed_agents,
            preferred_default_agent_id=(
                str(getattr(system_setting, "defaultEntryAgentId", "") or "").strip()
                if system_setting is not None
                else ""
            ),
        )

        config = {
            "schema_version": "v2-preview",
            "company": {
                "cid": company.cid,
                "name": company.company_name,
                "description": company.description or "",
                "company_size": company.company_size or "",
                "contact_phone": company.contact_phone or "",
                "contact_email": company.contact_email or "",
                "website": company.website or "",
                "address": company.address or "",
                "logo": company.logo or "",
                "background_image": company.background_image or "",
                "is_transparent": True,
            },
            "runtime": {
                "provider": OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST,
                "delivery_mode": "docker_shared_host",
                "runtime_namespace": _runtime_namespace(company),
                "gateway": {
                    "port": DEFAULT_GATEWAY_PORT,
                    "endpoint": _gateway_endpoint(company),
                },
                "config": {
                    "path": _runtime_config_path(company),
                    "bidirectional_sync": True,
                    "readback_required": True,
                },
                "workspace": {
                    "mode": OpenClawInstance.WORKSPACE_MODE_COS_BACKED,
                    "mount_prefix": _runtime_workspace_prefix(company),
                },
                "file_gateway": {
                    "provider": WorkspaceFile.STORAGE_TENCENT_COS,
                    "credential_mode": "platform_only",
                    "runtime_transfer_mode": "presigned_url_only",
                },
                "activation_policy": {
                    "default_new_agent_status": "inactive",
                    "default_new_skill_status": "inactive",
                    "manual_enable_required": True,
                },
            },
            "human_governance": {
                "chief_human_officer_title": "CHO",
                "chief_human_officer_name": "棣栧腑浜虹被椤鹃棶",
            },
            "model_policy": OpenClawConfigService._build_model_policy_item(company),
            "memory_policy": OpenClawConfigService._build_memory_policy_item(company),
            "system_settings": OpenClawConfigService._build_system_setting_item(company),
            "client_entry": {
                "mode": "multi_agent",
                "default_agent_id": getattr(default_entry_agent, "agentId", "") if default_entry_agent else "",
                "entry_agent_id": getattr(default_entry_agent, "agentId", "") if default_entry_agent else "",
                "visible_agents": client_entry_bindings,
                "bindings": client_entry_bindings,
            },
            "agents": [
                OpenClawConfigService._build_agent_item(agent)
                for agent in agent_definitions
            ],
            "skills": [
                OpenClawConfigService._build_skill_item(skill)
                for skill in skills
            ],
            "agent_skill_bindings": [
                OpenClawConfigService._build_binding_item(binding)
                for binding in skill_bindings
            ],
            "agent_memory_bindings": [
                OpenClawConfigService._build_agent_memory_binding_item(binding)
                for binding in agent_memory_bindings
            ],
            "permission_templates": [
                OpenClawConfigService._build_permission_template_item(template)
                for template in permission_templates
            ],
            "agent_permission_bindings": [
                OpenClawConfigService._build_agent_permission_binding_item(binding)
                for binding in agent_permission_bindings
            ],
            "agent_viking_spaces": [
                OpenClawConfigService._build_agent_viking_space_item(space)
                for space in agent_viking_spaces
            ],
            "permission_config": permission_config.get_info() if permission_config else {
                "cid": company.cid,
                "configJson": {"permissions": {}},
                "enabled": True,
                "updated_at": "",
            },
            "generated_at": _now_ts(),
        }
        return True, config

    @staticmethod
    def list_company_versions(
        company: Company,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        queryset = OpenClawConfigVersion.objects.filter(company=company).order_by("-id")
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return {
            "total": total,
            "items": [item.get_info() for item in items],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def create_config_version(
        company: Company,
        operator_id: str = "",
        notes: str = "",
        source: str = OpenClawConfigVersion.SOURCE_PLATFORM_GUI,
        publish: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        now = _now_ts()
        if publish:
            try:
                from apps.company.models import TransparentCompany, TransparentCompanySkill

                transparent_company = TransparentCompany.objects.get(company=company)
                TransparentCompanySkill.objects.filter(
                    transparentCompany=transparent_company,
                    enabled=True,
                ).update(
                    runtimeStatus=TransparentCompanySkill.RUNTIME_STATUS_PENDING,
                    lastPublishedAt=now,
                    updated_at=now,
                )
            except Exception:
                pass

        ok, config_or_msg = OpenClawConfigService.build_company_preview(company)
        if not ok:
            return False, config_or_msg

        instance = (
            OpenClawInstance.objects.filter(company=company)
            .order_by("-id")
            .first()
        )
        version = OpenClawConfigVersion.objects.create(
            company=company,
            instance=instance,
            version=OpenClawConfigService._build_version_name(company),
            schema_version=str(config_or_msg.get("schema_version", "v2-preview")),
            source=source,
            status=OpenClawConfigVersion.STATUS_PUBLISHED if publish else OpenClawConfigVersion.STATUS_DRAFT,
            created_by=operator_id or "",
            notes=notes or "",
            config_payload=_dump_payload(config_or_msg),
            published_at=now if publish else "",
            created_at=now,
            updated_at=now,
        )

        if publish:
            (
                OpenClawConfigVersion.objects
                .filter(company=company, status=OpenClawConfigVersion.STATUS_PUBLISHED)
                .exclude(id=version.id)
                .update(status=OpenClawConfigVersion.STATUS_SUPERSEDED, updated_at=now)
            )
            if instance:
                instance.current_publish_version = version.version
                instance.config_status = OpenClawInstance.CONFIG_STATUS_PENDING
                instance.updated_at = now
                instance.save(
                    update_fields=["current_publish_version", "config_status", "updated_at"]
                )

        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            action="runtime.config.published" if publish else "runtime.config.snapshot.created",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=version.version,
            detail={
                "status": version.status,
                "schema_version": version.schema_version,
                "source": version.source,
            },
        )
        return True, version.get_info()

    @staticmethod
    def build_company_config_diff(company: Company) -> tuple[bool, dict[str, Any] | str]:
        ok, platform_config_or_msg = OpenClawConfigService.build_company_preview(company)
        if not ok:
            return False, platform_config_or_msg
        platform_config = platform_config_or_msg

        ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
        if not ok:
            return False, instance_or_msg
        instance = instance_or_msg

        ok, runtime_snapshot_or_msg = OpenClawConfigReadbackService.read_instance_config(instance)
        if not ok:
            return False, runtime_snapshot_or_msg

        runtime_snapshot = runtime_snapshot_or_msg if isinstance(runtime_snapshot_or_msg, dict) else {}
        runtime_config = _load_payload(str(runtime_snapshot.get("config_payload", "")))
        runtime_version = str(runtime_snapshot.get("version", "") or instance.current_runtime_version or "")

        published_version = (
            OpenClawConfigVersion.objects.filter(
                company=company,
                status=OpenClawConfigVersion.STATUS_PUBLISHED,
            )
            .order_by("-id")
            .first()
        )

        sections = [
            "company",
            "runtime",
            "human_governance",
            "model_policy",
            "memory_policy",
            "system_settings",
            "permission_templates",
            "agent_permission_bindings",
            "permission_config",
            "client_entry",
            "agents",
            "skills",
            "agent_skill_bindings",
            "agent_memory_bindings",
        ]
        mismatched_sections: list[str] = []
        for section in sections:
            if json.dumps(platform_config.get(section), ensure_ascii=False, sort_keys=True) != json.dumps(
                runtime_config.get(section), ensure_ascii=False, sort_keys=True
            ):
                mismatched_sections.append(section)

        diff_payload = {
            "platform_version": published_version.version if published_version else "",
            "runtime_version": runtime_version,
            "is_in_sync": len(mismatched_sections) == 0,
            "mismatched_sections": mismatched_sections,
            "platform_config": platform_config,
            "runtime_config": runtime_config,
            "readback_source": runtime_snapshot.get("source", ""),
            "config_path": runtime_snapshot.get("config_path", instance.config_path),
            "checked_at": _now_ts(),
        }
        now = diff_payload["checked_at"]
        instance.current_runtime_version = runtime_version or instance.current_runtime_version
        instance.last_readback_at = now
        instance.updated_at = now
        if mismatched_sections:
            instance.config_status = OpenClawInstance.CONFIG_STATUS_DRIFTED
            instance.save(
                update_fields=[
                    "current_runtime_version",
                    "config_status",
                    "last_readback_at",
                    "updated_at",
                ]
            )
            RuntimeGovernanceService.open_drift_event(
                company=company,
                instance=instance,
                drift_type=RuntimeDriftEvent.DRIFT_CONFIG,
                summary="openclaw.json differs from platform target",
                detail={
                    "mismatched_sections": mismatched_sections,
                    "platform_version": diff_payload["platform_version"],
                    "runtime_version": diff_payload["runtime_version"],
                },
            )
        else:
            instance.config_status = OpenClawInstance.CONFIG_STATUS_IN_SYNC
            instance.save(
                update_fields=[
                    "current_runtime_version",
                    "config_status",
                    "last_readback_at",
                    "updated_at",
                ]
            )
            RuntimeGovernanceService.resolve_open_drift_events(
                company=company,
                drift_type=RuntimeDriftEvent.DRIFT_CONFIG,
                resolution=RuntimeDriftEvent.RESOLUTION_REPUBLISH,
            )
        return True, diff_payload


class RuntimeAuditService:
    @staticmethod
    def log(
        company: Company,
        action: str,
        target_type: str,
        target_id: Optional[str] = None,
        instance: Optional[OpenClawInstance] = None,
        actor_type: str = OpenClawAuditLog.ACTOR_SYSTEM,
        actor_id: str = "",
        detail: Optional[dict[str, Any] | str] = None,
    ) -> OpenClawAuditLog:
        if isinstance(detail, dict):
            detail = _dump_payload(detail)

        return OpenClawAuditLog.objects.create(
            company=company,
            instance=instance,
            actor_type=actor_type,
            actor_id=actor_id or "",
            action=action,
            target_type=target_type,
            target_id=target_id or "",
            detail=detail or "",
            created_at=_now_ts(),
        )


class RuntimeGovernanceService:
    @staticmethod
    def get_or_create_rollout_policy(company: Company) -> RuntimeRolloutPolicy:
        now = _now_ts()
        policy, _ = RuntimeRolloutPolicy.objects.get_or_create(
            company=company,
            defaults={
                "strategy": RuntimeRolloutPolicy.STRATEGY_DIRECT,
                "status": RuntimeRolloutPolicy.STATUS_ACTIVE,
                "canary_percentage": 10,
                "auto_promote": False,
                "auto_promote_minutes": 30,
                "rollback_on_alert": True,
                "created_at": now,
                "updated_at": now,
            },
        )
        return policy

    @staticmethod
    def get_or_create_alert_policy(company: Company) -> RuntimeAlertPolicy:
        now = _now_ts()
        policy, _ = RuntimeAlertPolicy.objects.get_or_create(
            company=company,
            defaults={
                "status": RuntimeAlertPolicy.STATUS_ACTIVE,
                "channels": RuntimeAlertPolicy.CHANNEL_PLATFORM,
                "config_drift_enabled": True,
                "gateway_unhealthy_enabled": True,
                "workspace_sync_failed_enabled": True,
                "delivery_target": "",
                "created_at": now,
                "updated_at": now,
            },
        )
        return policy

    @staticmethod
    def upsert_rollout_policy(company: Company, payload: dict[str, Any], operator_id: str = "") -> dict[str, Any]:
        policy = RuntimeGovernanceService.get_or_create_rollout_policy(company)
        now = _now_ts()
        for field in ["strategy", "status", "canary_percentage", "auto_promote", "auto_promote_minutes", "rollback_on_alert"]:
            if field in payload:
                setattr(policy, field, payload[field])
        policy.updated_at = now
        policy.save()
        RuntimeAuditService.log(
            company=company,
            action="runtime.governance.rollout.updated",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=str(policy.id),
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            detail=policy.get_info(),
        )
        return policy.get_info()

    @staticmethod
    def upsert_alert_policy(company: Company, payload: dict[str, Any], operator_id: str = "") -> dict[str, Any]:
        policy = RuntimeGovernanceService.get_or_create_alert_policy(company)
        now = _now_ts()
        if "channels" in payload:
            policy.channels = ",".join(payload["channels"])
        for field in ["status", "config_drift_enabled", "gateway_unhealthy_enabled", "workspace_sync_failed_enabled", "delivery_target"]:
            if field in payload:
                setattr(policy, field, payload[field])
        policy.updated_at = now
        policy.save()
        RuntimeAuditService.log(
            company=company,
            action="runtime.governance.alert.updated",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=str(policy.id),
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            detail=policy.get_info(),
        )
        return policy.get_info()

    @staticmethod
    def list_drift_events(company: Company, status: str = "", limit: int = 20, offset: int = 0) -> dict[str, Any]:
        queryset = RuntimeDriftEvent.objects.filter(company=company).order_by("-id")
        if status:
            queryset = queryset.filter(status=status)
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return {
            "total": total,
            "items": [item.get_info() for item in items],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def open_drift_event(
        company: Company,
        drift_type: str,
        summary: str,
        detail: Optional[dict[str, Any]] = None,
        instance: Optional[OpenClawInstance] = None,
    ) -> RuntimeDriftEvent:
        serialized_detail = _dump_payload(detail)
        existing = RuntimeDriftEvent.objects.filter(
            company=company,
            drift_type=drift_type,
            status=RuntimeDriftEvent.STATUS_OPEN,
            summary=summary,
        ).order_by("-id").first()
        if existing:
            existing.detail = serialized_detail
            existing.updated_at = _now_ts()
            existing.save(update_fields=["detail", "updated_at"])
            return existing

        now = _now_ts()
        event = RuntimeDriftEvent.objects.create(
            company=company,
            instance=instance,
            drift_type=drift_type,
            status=RuntimeDriftEvent.STATUS_OPEN,
            summary=summary,
            detail=serialized_detail,
            created_at=now,
            updated_at=now,
        )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.drift.opened",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=str(event.id),
            detail=event.get_info(),
        )
        RuntimeGovernanceService._emit_platform_notification(company, event)
        return event

    @staticmethod
    def resolve_drift_event(company: Company, event_id: int, resolution: str, operator_id: str = "") -> tuple[bool, dict[str, Any] | str]:
        try:
            event = RuntimeDriftEvent.objects.get(id=event_id, company=company)
        except RuntimeDriftEvent.DoesNotExist:
            return False, "drift event not found"

        event.status = RuntimeDriftEvent.STATUS_RESOLVED
        event.resolution = resolution
        event.resolved_by = operator_id or ""
        event.resolved_at = _now_ts()
        event.updated_at = event.resolved_at
        event.save()
        RuntimeAuditService.log(
            company=company,
            instance=event.instance,
            action="runtime.drift.resolved",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=str(event.id),
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            detail=event.get_info(),
        )
        return True, event.get_info()

    @staticmethod
    def resolve_open_drift_events(
        company: Company,
        drift_type: str,
        resolution: str,
        operator_id: str = "",
        summary: str = "",
    ) -> int:
        queryset = RuntimeDriftEvent.objects.filter(
            company=company,
            drift_type=drift_type,
            status=RuntimeDriftEvent.STATUS_OPEN,
        )
        if summary:
            queryset = queryset.filter(summary=summary)

        now = _now_ts()
        resolved = 0
        for event in queryset:
            event.status = RuntimeDriftEvent.STATUS_RESOLVED
            event.resolution = resolution
            event.resolved_by = operator_id or ""
            event.resolved_at = now
            event.updated_at = now
            event.save(update_fields=["status", "resolution", "resolved_by", "resolved_at", "updated_at"])
            resolved += 1

        return resolved

    @staticmethod
    def _emit_platform_notification(company: Company, event: RuntimeDriftEvent) -> None:
        policy = RuntimeGovernanceService.get_or_create_alert_policy(company)
        if policy.status != RuntimeAlertPolicy.STATUS_ACTIVE:
            return
        if event.drift_type == RuntimeDriftEvent.DRIFT_CONFIG and not policy.config_drift_enabled:
            return
        if event.drift_type == RuntimeDriftEvent.DRIFT_GATEWAY and not policy.gateway_unhealthy_enabled:
            return
        if event.drift_type == RuntimeDriftEvent.DRIFT_WORKSPACE and not policy.workspace_sync_failed_enabled:
            return

        admin_grants = Grant.objects.filter(
            resource__external_id=company.cid,
            permissions__in=[Grant.PERM_ADMIN, Grant.PERM_SUPER_ADMIN],
        ).select_related("principal")
        for grant in admin_grants:
            principal = grant.principal
            if not principal or principal.type != Principal.TYPE_USER:
                continue
            user = User.objects.filter(uid=principal.external_id).first()
            if not user:
                continue
            detail = _load_payload(event.detail or "{}")
            issue_sections = []
            if isinstance(detail, dict):
                raw_sections = detail.get("mismatched_sections")
                if isinstance(raw_sections, list):
                    issue_sections = [str(item) for item in raw_sections if str(item).strip()]
            NotificationService.send_runtime_config_alert_notification(
                user=user,
                transparentCompany=getattr(company, "transparentCompany", None),
                company_name=company.company_name or company.cid,
                issue_sections=issue_sections,
                event_data=event.get_info(),
            )


class PresignedUrlIssueService:
    @staticmethod
    def issue_download_ticket(file: WorkspaceFile) -> dict[str, Any]:
        expires_at = str(time.time() + DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS)[:20]
        moderation_status = None
        if is_image_file(file.storage_key, file.mime_type or ""):
            moderation_asset = ImageModerationService.get_asset(file.storage_key)
            moderation_status = moderation_asset.get_info() if moderation_asset else {"status": "untracked"}
            if not ImageModerationService.is_publicly_viewable(file.storage_key, file.mime_type or ""):
                return {
                    "provider": WorkspaceFile.STORAGE_TENCENT_COS,
                    "mode": "presigned_url_only",
                    "direction": WorkspaceTransferTask.DIRECTION_PLATFORM_TO_RUNTIME,
                    "logical_path": file.logical_path,
                    "storage_key": file.storage_key,
                    "expires_at": expires_at,
                    "presigned_url": None,
                    "wired": False,
                    "moderation": moderation_status,
                    "message": "image moderation has not approved this file",
                }
        presigned_url = get_presigned_url(
            file.storage_key,
            expires_in=DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS,
        )
        file.presigned_url_expire_at = expires_at
        file.updated_at = _now_ts()
        file.save(update_fields=["presigned_url_expire_at", "updated_at"])
        return {
            "provider": WorkspaceFile.STORAGE_TENCENT_COS,
            "mode": "presigned_url_only",
            "direction": WorkspaceTransferTask.DIRECTION_PLATFORM_TO_RUNTIME,
            "logical_path": file.logical_path,
            "storage_key": file.storage_key,
            "expires_at": expires_at,
            "presigned_url": presigned_url,
            "wired": True,
            "moderation": moderation_status,
        }

    @staticmethod
    def issue_upload_ticket(file: WorkspaceFile) -> dict[str, Any]:
        expires_at = str(time.time() + DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS)[:20]
        moderation_status = None
        if is_image_file(file.storage_key, file.mime_type or ""):
            moderation_asset = ImageModerationService.register_uploaded_image(
                file.storage_key,
                source=ImageModerationAsset.SOURCE_RUNTIME_WORKSPACE,
                source_field="workspace_file",
                owner_type="company",
                owner_id=file.company.cid,
                mime_type=file.mime_type or "",
                size=file.size or 0,
                submit=False,
            )
            moderation_status = moderation_asset.get_info()
        presigned_url = get_presigned_upload_url(
            file.storage_key,
            expires_in=DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS,
        )
        file.presigned_url_expire_at = expires_at
        file.updated_at = _now_ts()
        file.save(update_fields=["presigned_url_expire_at", "updated_at"])
        return {
            "provider": WorkspaceFile.STORAGE_TENCENT_COS,
            "mode": "presigned_url_only",
            "direction": WorkspaceTransferTask.DIRECTION_RUNTIME_TO_PLATFORM,
            "company_id": file.company.cid,
            "logical_path": file.logical_path,
            "storage_key": file.storage_key,
            "expires_at": expires_at,
            "presigned_url": presigned_url,
            "wired": True,
            "moderation": moderation_status,
        }

    @staticmethod
    def issue_runtime_pull_ticket(file: WorkspaceFile) -> dict[str, Any]:
        expires_at = str(time.time() + DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS)[:20]
        presigned_url = get_presigned_url(
            file.storage_key,
            expires_in=DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS,
        )
        return {
            "provider": WorkspaceFile.STORAGE_TENCENT_COS,
            "mode": "presigned_url_only",
            "direction": WorkspaceTransferTask.DIRECTION_PLATFORM_TO_RUNTIME,
            "company_id": file.company.cid,
            "logical_path": file.logical_path,
            "storage_key": file.storage_key,
            "expires_at": expires_at,
            "presigned_url": presigned_url,
            "wired": True,
        }

    @staticmethod
    def issue_runtime_push_ticket(file: WorkspaceFile) -> dict[str, Any]:
        expires_at = str(time.time() + DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS)[:20]
        presigned_url = get_presigned_upload_url(
            file.storage_key,
            expires_in=DEFAULT_PRESIGNED_URL_EXPIRE_SECONDS,
        )
        return {
            "provider": WorkspaceFile.STORAGE_TENCENT_COS,
            "mode": "presigned_url_only",
            "direction": WorkspaceTransferTask.DIRECTION_RUNTIME_TO_PLATFORM,
            "company_id": file.company.cid,
            "logical_path": file.logical_path,
            "storage_key": file.storage_key,
            "expires_at": expires_at,
            "presigned_url": presigned_url,
            "wired": True,
        }


class CosFileGatewayService:
    @staticmethod
    def create_transfer_task(
        company: Company,
        direction: str,
        file: Optional[WorkspaceFile] = None,
        logical_path: str = "",
        request_payload: Optional[dict[str, Any]] = None,
    ) -> WorkspaceTransferTask:
        now = _now_ts()
        resolved_logical_path = logical_path or (file.logical_path if file else "")
        task = WorkspaceTransferTask.objects.create(
            company=company,
            instance=(
                OpenClawInstance.objects.filter(company=company).order_by("-id").first()
            ),
            file=file,
            file_logical_path=resolved_logical_path,
            file_name=(file.file_name if file else (resolved_logical_path.split("/")[-1] if resolved_logical_path else "")),
            file_mime_type=(file.mime_type if file else ""),
            file_size=(file.size if file else 0),
            direction=direction,
            status=WorkspaceTransferTask.STATUS_PENDING,
            request_payload=_dump_payload(request_payload),
            created_at=now,
            updated_at=now,
        )
        RuntimeAuditService.log(
            company=company,
            instance=task.instance,
            action="runtime.workspace.transfer.created",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(task.id),
            detail={"direction": direction, "logical_path": resolved_logical_path},
        )
        return task


class OpenClawGatewayClient:
    @staticmethod
    def read_runtime_state(instance: OpenClawInstance) -> tuple[bool, dict[str, Any]]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.read_runtime_state(instance.company, instance)

    @staticmethod
    def list_cron_jobs(instance: OpenClawInstance) -> tuple[bool, dict[str, Any] | str]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.list_cron_jobs(instance.company, instance)

    @staticmethod
    def list_cron_runs(instance: OpenClawInstance, job_id: str, limit: int = 20) -> tuple[bool, dict[str, Any] | str]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.list_cron_runs(instance.company, instance, job_id, limit=limit)

    @staticmethod
    def create_cron_job(instance: OpenClawInstance, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | str]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.create_cron_job(instance.company, instance, payload)

    @staticmethod
    def run_cron_job(instance: OpenClawInstance, job_id: str) -> tuple[bool, dict[str, Any] | str]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.run_cron_job(instance.company, instance, job_id)

    @staticmethod
    def remove_cron_job(instance: OpenClawInstance, job_id: str) -> tuple[bool, dict[str, Any] | str]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.remove_cron_job(instance.company, instance, job_id)


class OpenClawConfigReadbackService:
    @staticmethod
    def read_instance_config(instance: OpenClawInstance) -> tuple[bool, dict[str, Any]]:
        published = (
            OpenClawConfigVersion.objects.filter(
                company=instance.company,
                status=OpenClawConfigVersion.STATUS_PUBLISHED,
            )
            .order_by("-id")
            .first()
        )
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.read_config_snapshot(
            instance.company,
            instance,
            published=published,
        )


class OpenClawProviderAdapter:
    """
    Runtime provider abstraction.
    Phase 1 keeps the boundary stable while the real docker-shared-host
    implementation is filled in incrementally.
    """

    def provision_instance(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def bootstrap_openclaw(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def check_health(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def read_runtime_state(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def list_cron_jobs(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def list_cron_runs(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
        limit: int = 20,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def create_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def run_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def remove_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def read_config_snapshot(
        self,
        company: Company,
        instance: OpenClawInstance,
        published: Optional[OpenClawConfigVersion] = None,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def read_model_catalog(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def sync_workspace_transfer(
        self,
        company: Company,
        instance: OpenClawInstance,
        transfer_task: WorkspaceTransferTask,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def sync_openviking_resource(
        self,
        company: Company,
        instance: OpenClawInstance,
        *,
        operation: str,
        logical_path: str,
        previous_logical_path: str = "",
        mime_type: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def read_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def read_workspace_logical_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def list_workspace_tree(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str = "",
        recursive: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def delete_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def rename_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
        new_logical_path: str,
    ) -> tuple[bool, dict[str, Any]]:
        raise NotImplementedError

    def execute_workspace_tool(
        self,
        company: Company,
        instance: OpenClawInstance,
        *,
        agent_id: str,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def execute_agent_test_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def stream_agent_test_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        raise NotImplementedError

    def execute_client_entry_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError

    def stream_client_entry_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        raise NotImplementedError

    def inspect_agent_runtime_exports(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        raise NotImplementedError


class NoopOpenClawProviderAdapter(OpenClawProviderAdapter):
    """
    Default adapter for Phase 1.
    It returns structured placeholders while we replace the old
    single-host assumptions with real docker orchestration.
    """

    def provision_instance(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any]]:
        namespace = instance.runtime_namespace or _runtime_namespace(company)
        return True, {
            "provider": instance.provider,
            "host_id": instance.host_id or DEFAULT_DOCKER_HOST_ID,
            "runtime_namespace": namespace,
            "gateway_endpoint": instance.gateway_endpoint or _gateway_endpoint(company),
            "config_path": instance.config_path or _runtime_config_path(company),
            "workspace_prefix": instance.workspace_prefix or _runtime_workspace_prefix(company),
            "task_id": task.id,
            "message": "runtime provider adapter is not wired yet",
        }

    def bootstrap_openclaw(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any]]:
        return True, {
            "provider": instance.provider,
            "runtime_namespace": instance.runtime_namespace,
            "gateway_endpoint": instance.gateway_endpoint,
            "config_status": instance.config_status,
            "task_id": task.id,
            "message": "openclaw bootstrap is not wired yet",
        }

    def check_health(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        return True, {
            "provider": instance.provider,
            "gateway_endpoint": instance.gateway_endpoint,
            "config_status": instance.config_status,
            "message": "health check is not wired yet",
            "openclaw_status": OpenClawInstance.OPENCLAW_STATUS_UNKNOWN,
        }

    def read_runtime_state(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        return True, {
            "provider": instance.provider,
            "host_id": instance.host_id or DEFAULT_DOCKER_HOST_ID,
            "runtime_namespace": instance.runtime_namespace or _runtime_namespace(company),
            "endpoint": instance.gateway_endpoint or _gateway_endpoint(company),
            "gateway_port": instance.gateway_port,
            "status": instance.openclaw_status or OpenClawInstance.OPENCLAW_STATUS_UNKNOWN,
            "config_status": instance.config_status,
            "connection_mode": instance.connection_mode,
            "source": "runtime_provider_adapter",
            "message": "gateway read is routed through the runtime provider boundary",
        }

    def list_cron_jobs(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime cron is not available for the current runtime provider"

    def list_cron_runs(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
        limit: int = 20,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime cron is not available for the current runtime provider"

    def create_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime cron is not available for the current runtime provider"

    def run_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime cron is not available for the current runtime provider"

    def remove_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime cron is not available for the current runtime provider"

    def read_config_snapshot(
        self,
        company: Company,
        instance: OpenClawInstance,
        published: Optional[OpenClawConfigVersion] = None,
    ) -> tuple[bool, dict[str, Any]]:
        return True, {
            "config_path": instance.config_path,
            "current_runtime_version": instance.current_runtime_version or "",
            "published_version": published.version if published else "",
            "config_payload": published.config_payload if published else "",
            "runtime_namespace": instance.runtime_namespace or _runtime_namespace(company),
            "source": "runtime_provider_adapter",
            "message": "config readback is routed through the runtime provider boundary",
        }

    def read_model_catalog(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        return False, "runtime model catalog is not available for the current runtime provider"

    def read_skill_catalog(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime skill catalog is not available for the current runtime provider"

    def sync_workspace_transfer(
        self,
        company: Company,
        instance: OpenClawInstance,
        transfer_task: WorkspaceTransferTask,
    ) -> tuple[bool, dict[str, Any]]:
        request_payload = _load_payload(transfer_task.request_payload)
        return True, {
            "provider": instance.provider,
            "runtime_namespace": instance.runtime_namespace or _runtime_namespace(company),
            "direction": transfer_task.direction,
            "logical_path": transfer_task.file.logical_path if transfer_task.file else "",
            "request_payload": request_payload,
            "source": "runtime_provider_adapter",
            "message": "workspace transfer is routed through the runtime provider boundary",
        }

    def sync_openviking_resource(
        self,
        company: Company,
        instance: OpenClawInstance,
        *,
        operation: str,
        logical_path: str,
        previous_logical_path: str = "",
        mime_type: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "OpenViking resource synchronization is not available for the current runtime provider"

    def read_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
    ) -> tuple[bool, dict[str, Any]]:
        return False, "workspace file read is not available for the current runtime provider"

    def read_workspace_logical_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "workspace logical file read is not available for the current runtime provider"

    def delete_workspace_logical_path(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "workspace logical path delete is not available for the current runtime provider"

    def create_workspace_logical_path(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "workspace logical path create is not available for the current runtime provider"

    def rename_workspace_logical_path(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
        new_logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "workspace logical path rename is not available for the current runtime provider"

    def list_workspace_tree(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str = "",
        recursive: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "workspace tree read is not available for the current runtime provider"

    def delete_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
    ) -> tuple[bool, dict[str, Any]]:
        return False, "workspace file delete is not available for the current runtime provider"

    def rename_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
        new_logical_path: str,
    ) -> tuple[bool, dict[str, Any]]:
        return False, "workspace file rename is not available for the current runtime provider"

    def execute_workspace_tool(
        self,
        company: Company,
        instance: OpenClawInstance,
        *,
        agent_id: str,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "workspace tool execution is not available for the current runtime provider"

    def execute_agent_test_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "agent test execution is not available for the current runtime provider"

    def stream_agent_test_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        return False, "agent test streaming is not available for the current runtime provider"

    def execute_client_entry_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "client entry chat execution is not available for the current runtime provider"

    def stream_client_entry_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        return False, "client entry chat streaming is not available for the current runtime provider"

    def inspect_agent_runtime_exports(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "agent runtime exports inspection is not available for the current runtime provider"

    def export_runtime_state(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime state export is not available for the current runtime provider"

    def restore_runtime_state(
        self,
        company: Company,
        instance: OpenClawInstance,
        snapshot: OpenClawRuntimeSnapshot,
    ) -> tuple[bool, dict[str, Any] | str]:
        return False, "runtime state restore is not available for the current runtime provider"


class DockerSharedHostProviderAdapter(NoopOpenClawProviderAdapter):
    @staticmethod
    def _build_bridge_chat_payload(
        company: Company,
        request_payload: dict[str, Any],
        *,
        request_scope: str,
        allow_agent_selection: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "company_id": company.cid,
        }
        if isinstance(request_payload, dict):
            payload.update(request_payload)
        payload["request_scope"] = request_scope
        if not allow_agent_selection:
            payload.pop("agent_id", None)
        return payload

    @staticmethod
    def _build_headers() -> dict[str, str]:
        headers: dict[str, str] = {}
        token = getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_API_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _default_api_base() -> str:
        return str(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_API_BASE", "") or "").strip().rstrip("/")

    @classmethod
    def _api_base(cls, instance: OpenClawInstance) -> str:
        resource = getattr(instance, "server_resource", None)
        explicit_base = str(getattr(resource, "api_base", "") or "").strip().rstrip("/") if resource else ""
        if explicit_base:
            return explicit_base

        host = str(getattr(instance, "public_ip", "") or getattr(instance, "private_ip", "") or "").strip()
        default_base = cls._default_api_base()
        if not host or not default_base:
            return default_base

        parsed = urlparse(default_base)
        if not parsed.scheme or not parsed.netloc:
            return default_base

        username = f"{parsed.username}@" if parsed.username else ""
        port = f":{parsed.port}" if parsed.port else ""
        return urlunparse((parsed.scheme, f"{username}{host}{port}", parsed.path.rstrip("/"), "", "", "")).rstrip("/")

    @classmethod
    def _build_url(cls, instance: OpenClawInstance, suffix: str) -> str:
        base = cls._api_base(instance)
        namespace = instance.runtime_namespace or _runtime_namespace(instance.company)
        return f"{base}/runtime/{namespace}/{suffix.lstrip('/')}"

    @staticmethod
    def _response_error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "").strip()
            if message:
                return sanitize_user_visible_error(message)
        return f"shared host api returned {response.status_code}"

    @staticmethod
    def _shared_host_safe_error(payload: Any) -> str:
        message = str(payload or "").strip()
        if message.startswith((
            "workspace agent returned ",
            "shared host api returned ",
            "shared host api request failed:",
        )):
            return message
        return sanitize_user_visible_error(payload)
    def _get(self, instance: OpenClawInstance, suffix: str) -> tuple[bool, dict[str, Any] | str]:
        base = self._api_base(instance).strip()
        if not base:
            return False, "shared host api base is not configured"

        try:
            response = requests.get(
                self._build_url(instance, suffix),
                headers=self._build_headers(),
                timeout=getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10),
            )
        except requests.RequestException as exc:
            return False, f"shared host api request failed: {exc}"

        if not response.ok:
            return False, self._response_error_message(response)

        try:
            payload = response.json()
        except ValueError:
            return False, "shared host api returned invalid json"

        return True, payload if isinstance(payload, dict) else {"data": payload}

    def _get_with_params(
        self,
        instance: OpenClawInstance,
        suffix: str,
        params: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        base = self._api_base(instance).strip()
        if not base:
            return False, "shared host api base is not configured"

        try:
            response = requests.get(
                self._build_url(instance, suffix),
                headers=self._build_headers(),
                params=params or {},
                timeout=getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10),
            )
        except requests.RequestException as exc:
            return False, f"shared host api request failed: {exc}"

        if not response.ok:
            return False, self._response_error_message(response)

        try:
            payload = response.json()
        except ValueError:
            return False, "shared host api returned invalid json"

        return True, payload if isinstance(payload, dict) else {"data": payload}

    def _post(
        self,
        instance: OpenClawInstance,
        suffix: str,
        payload: Optional[dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        base = self._api_base(instance).strip()
        if not base:
            return False, "shared host api base is not configured"

        try:
            response = requests.post(
                self._build_url(instance, suffix),
                headers=self._build_headers(),
                json=payload or {},
                timeout=timeout if timeout is not None else getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10),
            )
        except requests.RequestException as exc:
            return False, f"shared host api request failed: {exc}"

        if not response.ok:
            return False, self._response_error_message(response)

        try:
            response_payload = response.json()
        except ValueError:
            return False, "shared host api returned invalid json"

        return True, response_payload if isinstance(response_payload, dict) else {"data": response_payload}

    def _post_stream(
        self,
        instance: OpenClawInstance,
        suffix: str,
        payload: Optional[dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> tuple[bool, requests.Response | str]:
        base = self._api_base(instance).strip()
        if not base:
            return False, "shared host api base is not configured"

        try:
            response = requests.post(
                self._build_url(instance, suffix),
                headers=self._build_headers(),
                json=payload or {},
                timeout=(10, timeout if timeout is not None else max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10)), 90)),
                stream=True,
            )
        except requests.RequestException as exc:
            return False, f"shared host api request failed: {exc}"

        if not response.ok:
            response.close()
            return False, f"shared host api returned {response.status_code}"

        return True, response

    def provision_instance(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = self._post(
            instance,
            "provision",
            payload={
                "company_id": company.cid,
                "task_id": task.id,
                "runtime_namespace": instance.runtime_namespace or _runtime_namespace(company),
                "gateway_port": instance.gateway_port,
                "config_path": instance.config_path or _runtime_config_path(company),
                "workspace_prefix": instance.workspace_prefix or _runtime_workspace_prefix(company),
            },
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("host_id", instance.host_id or DEFAULT_DOCKER_HOST_ID)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("gateway_endpoint", instance.gateway_endpoint or _gateway_endpoint(company))
            payload.setdefault("config_path", instance.config_path or _runtime_config_path(company))
            payload.setdefault("workspace_prefix", instance.workspace_prefix or _runtime_workspace_prefix(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, {
            "message": self._shared_host_safe_error(payload),
            "source": "shared_host_api",
        }

    def bootstrap_openclaw(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any]]:
        published = (
            OpenClawConfigVersion.objects.filter(
                company=company,
                status=OpenClawConfigVersion.STATUS_PUBLISHED,
            )
            .order_by("-id")
            .first()
        )
        ok, payload = self._post(
            instance,
            "bootstrap",
            payload={
                "company_id": company.cid,
                "task_id": task.id,
                "config_path": instance.config_path or _runtime_config_path(company),
                "current_publish_version": instance.current_publish_version or "",
                "config_payload": published.config_payload if published else "",
                "provider_env": _selected_provider_env(company),
            },
            timeout=max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_BOOTSTRAP_TIMEOUT", 900)), 180),
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("gateway_endpoint", instance.gateway_endpoint or _gateway_endpoint(company))
            payload.setdefault("config_status", instance.config_status)
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, {
            "message": self._shared_host_safe_error(payload),
            "source": "shared_host_api",
        }

    def read_runtime_state(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = self._get(instance, "gateway-state")
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("host_id", instance.host_id or DEFAULT_DOCKER_HOST_ID)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("endpoint", instance.gateway_endpoint or _gateway_endpoint(company))
            payload.setdefault("gateway_port", instance.gateway_port)
            payload.setdefault("connection_mode", instance.connection_mode)
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().read_runtime_state(company, instance)

    def list_cron_jobs(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._get(instance, "cron/jobs")
        if ok and isinstance(payload, dict):
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def list_cron_runs(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
        limit: int = 20,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._get_with_params(instance, "cron/runs", {"id": job_id, "limit": limit})
        if ok and isinstance(payload, dict):
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def create_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, result = self._post(instance, "cron/jobs", payload=payload)
        if ok and isinstance(result, dict):
            result.setdefault("source", "shared_host_api")
            RuntimeAuditService.log(
                company=company,
                instance=instance,
                action="runtime.cron.created",
                target_type=OpenClawAuditLog.TARGET_CRON,
                target_id=str(result.get("id") or result.get("job_id") or payload.get("name") or ""),
                detail={"name": payload.get("name"), "schedule_type": payload.get("schedule_type")},
            )
            return True, result
        return False, self._shared_host_safe_error(result)

    def run_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._post(instance, "cron/run", payload={"id": job_id})
        if ok and isinstance(payload, dict):
            payload.setdefault("source", "shared_host_api")
            RuntimeAuditService.log(
                company=company,
                instance=instance,
                action="runtime.cron.run_requested",
                target_type=OpenClawAuditLog.TARGET_CRON,
                target_id=job_id,
                detail=payload,
            )
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def remove_cron_job(
        self,
        company: Company,
        instance: OpenClawInstance,
        job_id: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._post(instance, "cron/remove", payload={"id": job_id})
        if ok and isinstance(payload, dict):
            payload.setdefault("source", "shared_host_api")
            RuntimeAuditService.log(
                company=company,
                instance=instance,
                action="runtime.cron.removed",
                target_type=OpenClawAuditLog.TARGET_CRON,
                target_id=job_id,
                detail=payload,
            )
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def check_health(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = self._get(instance, "health")
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("gateway_endpoint", instance.gateway_endpoint or _gateway_endpoint(company))
            payload.setdefault("config_status", instance.config_status)
            payload.setdefault("openclaw_status", payload.get("status", instance.openclaw_status))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().check_health(company, instance)

    def read_config_snapshot(
        self,
        company: Company,
        instance: OpenClawInstance,
        published: Optional[OpenClawConfigVersion] = None,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = self._get(instance, "config")
        if ok and isinstance(payload, dict):
            payload.setdefault("config_path", instance.config_path)
            payload.setdefault("published_version", published.version if published else "")
            payload.setdefault("config_payload", published.config_payload if published else "")
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().read_config_snapshot(company, instance, published=published)

    def read_model_catalog(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = self._get(instance, "model-catalog")
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().read_model_catalog(company, instance)

    def read_skill_catalog(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._get(instance, "skill-catalog")
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().read_skill_catalog(company, instance)

    def inspect_agent_runtime_exports(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._get(instance, "agent-runtime-exports")
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().inspect_agent_runtime_exports(company, instance)

    def list_memory_inventory(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Read the console inventory from the trusted shared-host bridge."""

        ok, payload = self._get(instance, "memory-console")
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "openviking_memory_v2")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def sync_workspace_transfer(
        self,
        company: Company,
        instance: OpenClawInstance,
        transfer_task: WorkspaceTransferTask,
    ) -> tuple[bool, dict[str, Any]]:
        request_payload = _load_payload(transfer_task.request_payload)
        ok, payload = self._post(
            instance,
            "workspace-transfer",
            payload={
                "company_id": company.cid,
                "transfer_task_id": transfer_task.id,
                "direction": transfer_task.direction,
                "logical_path": transfer_task.file.logical_path if transfer_task.file else "",
                "storage_backend": transfer_task.file.storage_backend if transfer_task.file else "",
                "storage_key": transfer_task.file.storage_key if transfer_task.file else "",
                "request_payload": request_payload,
            },
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("direction", transfer_task.direction)
            payload.setdefault(
                "logical_path",
                transfer_task.file.logical_path if transfer_task.file else "",
            )
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, payload

    def sync_openviking_resource(
        self,
        company: Company,
        instance: OpenClawInstance,
        *,
        operation: str,
        logical_path: str,
        previous_logical_path: str = "",
        mime_type: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._post(
            instance,
            "openviking-resource-sync",
            payload={
                "company_id": company.cid,
                "operation": operation,
                "logical_path": logical_path,
                "previous_logical_path": previous_logical_path,
                "mime_type": mime_type,
            },
            timeout=max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10)), 90),
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "openviking_resource_sync")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def read_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
    ) -> tuple[bool, dict[str, Any]]:
        return self.read_workspace_logical_file(
            company,
            instance,
            workspace_file.logical_path,
        )

    def read_workspace_logical_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        normalized_path = normalize_workspace_reference(logical_path)
        if not normalized_path or normalized_path == "*":
            return False, "logical_path is required"
        ok, payload = self._get_with_params(
            instance,
            "workspace-file",
            params={"path": normalized_path},
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("logical_path", normalized_path)
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def delete_workspace_logical_path(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        normalized_path = normalize_workspace_reference(logical_path)
        if not normalized_path or normalized_path == "*":
            return False, "logical_path is required"
        ok, payload = self._post(
            instance,
            "workspace-file/delete",
            payload={"logical_path": normalized_path},
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("logical_path", normalized_path)
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def create_workspace_logical_path(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        normalized_path = normalize_workspace_reference(logical_path)
        if not normalized_path or normalized_path == "*":
            return False, "logical_path is required"
        ok, payload = self._post(
            instance,
            "workspace-file/mkdir",
            payload={"logical_path": normalized_path},
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("logical_path", normalized_path)
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def rename_workspace_logical_path(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str,
        new_logical_path: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        normalized_path = normalize_workspace_reference(logical_path)
        normalized_new_path = normalize_workspace_reference(new_logical_path)
        if not normalized_path or normalized_path == "*":
            return False, "logical_path is required"
        if not normalized_new_path or normalized_new_path == "*":
            return False, "new_logical_path is required"
        ok, payload = self._post(
            instance,
            "workspace-file/rename",
            payload={
                "logical_path": normalized_path,
                "new_logical_path": normalized_new_path,
            },
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("logical_path", normalized_path)
            payload.setdefault("new_logical_path", normalized_new_path)
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def list_workspace_tree(
        self,
        company: Company,
        instance: OpenClawInstance,
        logical_path: str = "",
        recursive: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        params: dict[str, Any] = {"recursive": "1" if recursive else "0"}
        normalized_path = normalize_workspace_reference(logical_path)
        if normalized_path not in {"", "*"}:
            params["path"] = normalized_path
        ok, payload = self._get_with_params(
            instance,
            "workspace-tree",
            params=params,
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def execute_workspace_tool(
        self,
        company: Company,
        instance: OpenClawInstance,
        *,
        agent_id: str,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._post(
            instance,
            "agent-workspace-tool",
            payload={
                "company_id": company.cid,
                "agent_id": str(agent_id or "").strip(),
                "tool": str(tool_name or "").strip(),
                "arguments": arguments if isinstance(arguments, dict) else {},
            },
            timeout=max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10)), 90),
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return False, self._shared_host_safe_error(payload)

    def delete_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = self._post(
            instance,
            "workspace-file/delete",
            payload={"logical_path": workspace_file.logical_path},
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().delete_workspace_file(company, instance, workspace_file)

    def rename_workspace_file(
        self,
        company: Company,
        instance: OpenClawInstance,
        workspace_file: WorkspaceFile,
        new_logical_path: str,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = self._post(
            instance,
            "workspace-file/rename",
            payload={
                "logical_path": workspace_file.logical_path,
                "new_logical_path": new_logical_path,
            },
        )
        if ok and isinstance(payload, dict):
            payload.setdefault("provider", instance.provider)
            payload.setdefault("runtime_namespace", instance.runtime_namespace or _runtime_namespace(company))
            payload.setdefault("source", "shared_host_api")
            return True, payload
        return super().rename_workspace_file(company, instance, workspace_file, new_logical_path)

    def execute_agent_test_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._post(
            instance,
            "agent-test/chat-completions",
            payload=self._build_bridge_chat_payload(
                company,
                request_payload,
                request_scope=RUNTIME_REQUEST_SCOPE_INTERNAL_AGENT_TEST,
                allow_agent_selection=True,
            ),
            timeout=max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10)), 90),
        )
        if ok and isinstance(payload, dict):
            return True, sanitize_agent_test_runtime_payload(payload)
        logger.warning(
            "[runtime-agent-test] shared-host request failed cid=%s namespace=%s detail=%s",
            company.cid,
            instance.runtime_namespace or _runtime_namespace(company),
            sanitize_runtime_debug_payload(payload) if isinstance(payload, dict) else str(payload),
        )
        return False, sanitize_user_visible_error(payload)

    def stream_agent_test_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        ok, response_or_error = self._post_stream(
            instance,
            "agent-test/chat-completions/stream",
            payload=self._build_bridge_chat_payload(
                company,
                request_payload,
                request_scope=RUNTIME_REQUEST_SCOPE_INTERNAL_AGENT_TEST,
                allow_agent_selection=True,
            ),
            timeout=max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10)), 90),
        )
        if not ok:
            return False, sanitize_user_visible_error(response_or_error)

        response = response_or_error

        def iter_events():
            try:
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except ValueError:
                        logger.warning(
                            "[runtime-agent-test-stream] invalid ndjson cid=%s namespace=%s line=%s",
                            company.cid,
                            instance.runtime_namespace or _runtime_namespace(company),
                            raw_line[:500],
                        )
                        continue
                    if isinstance(payload, dict):
                        yield payload
            finally:
                response.close()

        return True, iter_events()

    def execute_client_entry_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = self._post(
            instance,
            "agent-test/chat-completions",
            payload=self._build_bridge_chat_payload(
                company,
                request_payload,
                request_scope=RUNTIME_REQUEST_SCOPE_CLIENT_ENTRY,
                allow_agent_selection=False,
            ),
            timeout=max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10)), 90),
        )
        if ok and isinstance(payload, dict):
            return True, sanitize_agent_test_runtime_payload(payload)
        logger.warning(
            "[runtime-client-entry] shared-host request failed cid=%s namespace=%s detail=%s",
            company.cid,
            instance.runtime_namespace or _runtime_namespace(company),
            sanitize_runtime_debug_payload(payload) if isinstance(payload, dict) else str(payload),
        )
        return False, sanitize_user_visible_error(payload)

    def stream_client_entry_chat(
        self,
        company: Company,
        instance: OpenClawInstance,
        request_payload: dict[str, Any],
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        ok, response_or_error = self._post_stream(
            instance,
            "agent-test/chat-completions/stream",
            payload=self._build_bridge_chat_payload(
                company,
                request_payload,
                request_scope=RUNTIME_REQUEST_SCOPE_CLIENT_ENTRY,
                allow_agent_selection=False,
            ),
            timeout=max(int(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_TIMEOUT", 10)), 90),
        )
        if not ok:
            return False, sanitize_user_visible_error(response_or_error)

        response = response_or_error

        def iter_events():
            try:
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    try:
                        event = json.loads(raw_line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
            finally:
                response.close()

        return True, iter_events()

class RuntimeResourcePoolService:
    @staticmethod
    def get_or_create_shared_host_resource() -> RuntimeServerResource | None:
        api_base = str(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_API_BASE", "") or "").strip()
        host_id = DEFAULT_DOCKER_HOST_ID
        now = _now_ts()
        resource, created = RuntimeServerResource.objects.get_or_create(
            provider=RuntimeServerResource.PROVIDER_DOCKER_SHARED_HOST,
            host_id=host_id,
            defaults={
                "cloud_vendor": RuntimeServerResource.CLOUD_VENDOR_UNICOM,
                "instance_name": host_id,
                "api_base": api_base,
                "status": RuntimeServerResource.STATUS_RUNNING if api_base else RuntimeServerResource.STATUS_AVAILABLE,
                "renewal_status": RuntimeServerResource.RENEWAL_UNKNOWN,
                "created_at": now,
                "updated_at": now,
            },
        )
        updates: list[str] = []
        if api_base and resource.api_base != api_base:
            resource.api_base = api_base
            updates.append("api_base")
        if not resource.instance_name:
            resource.instance_name = host_id
            updates.append("instance_name")
        if created is False and updates:
            resource.updated_at = now
            updates.append("updated_at")
            resource.save(update_fields=updates)
        return resource

    @staticmethod
    def get_or_create_lighthouse_resource(
        *,
        instance: OpenClawInstance,
        cloud_payload: dict[str, Any],
    ) -> RuntimeServerResource:
        now = _now_ts()
        cloud_instance_id = str(cloud_payload.get("instance_id") or instance.instance_id or "").strip()
        defaults = {
            "provider": RuntimeServerResource.PROVIDER_TENCENT_LIGHTHOUSE,
            "cloud_vendor": RuntimeServerResource.CLOUD_VENDOR_TENCENT,
            "instance_name": str(cloud_payload.get("instance_name") or instance.instance_name or "").strip(),
            "host_id": str(cloud_payload.get("host_id") or instance.host_id or "").strip(),
            "public_ip": str(cloud_payload.get("public_ip") or instance.public_ip or "").strip(),
            "private_ip": str(cloud_payload.get("private_ip") or instance.private_ip or "").strip(),
            "region": str(cloud_payload.get("region") or instance.region or "").strip(),
            "zone": str(cloud_payload.get("zone") or instance.zone or "").strip(),
            "status": RuntimeServerResource.STATUS_RUNNING,
            "resource_spec": _dump_payload(cloud_payload.get("resource_spec") if isinstance(cloud_payload.get("resource_spec"), dict) else {}),
            "expired_at": str(cloud_payload.get("expired_at") or instance.expired_at or "").strip(),
            "renewal_status": str(cloud_payload.get("renewal_status") or RuntimeServerResource.RENEWAL_MANUAL).strip(),
            "security_group_id": str(cloud_payload.get("security_group_id") or "").strip(),
            "image_id": str(cloud_payload.get("image_id") or getattr(settings, "TENCENT_LIGHTHOUSE_BLUEPRINT_ID", "") or "").strip(),
            "created_at": now,
            "updated_at": now,
        }
        if cloud_instance_id:
            resource, _created = RuntimeServerResource.objects.get_or_create(
                cloud_vendor=RuntimeServerResource.CLOUD_VENDOR_TENCENT,
                instance_id=cloud_instance_id,
                defaults={**defaults, "instance_id": cloud_instance_id},
            )
        else:
            resource = RuntimeServerResource.objects.create(**defaults)

        updates: list[str] = []
        for field_name, field_value in defaults.items():
            if field_name in {"created_at"}:
                continue
            if field_value not in (None, "") and getattr(resource, field_name) != field_value:
                setattr(resource, field_name, field_value)
                updates.append(field_name)
        if updates:
            resource.updated_at = now
            updates.append("updated_at")
            resource.save(update_fields=updates)
        return resource

    @staticmethod
    def bind_company_instance(
        *,
        company: Company,
        instance: OpenClawInstance,
        server_resource: RuntimeServerResource,
        billing_order_id: str = "",
        billing_plan_id: str = "",
    ) -> CompanyRuntimeBinding:
        now = _now_ts()
        CompanyRuntimeBinding.objects.filter(
            company=company,
            binding_status=CompanyRuntimeBinding.STATUS_ACTIVE,
        ).exclude(openclaw_instance=instance, server_resource=server_resource).update(
            binding_status=CompanyRuntimeBinding.STATUS_MIGRATED,
            updated_at=now,
        )
        binding, created = CompanyRuntimeBinding.objects.get_or_create(
            company=company,
            openclaw_instance=instance,
            server_resource=server_resource,
            defaults={
                "billing_order_id": billing_order_id or instance.billing_order_id or "",
                "billing_plan_id": billing_plan_id or instance.billing_plan_id or "",
                "binding_status": CompanyRuntimeBinding.STATUS_ACTIVE,
                "bound_at": now,
                "expired_at": instance.expired_at or server_resource.expired_at or "",
                "created_at": now,
                "updated_at": now,
            },
        )
        if not created:
            binding.binding_status = CompanyRuntimeBinding.STATUS_ACTIVE
            binding.billing_order_id = billing_order_id or binding.billing_order_id or instance.billing_order_id or ""
            binding.billing_plan_id = billing_plan_id or binding.billing_plan_id or instance.billing_plan_id or ""
            binding.expired_at = instance.expired_at or server_resource.expired_at or binding.expired_at or ""
            binding.updated_at = now
            binding.save(update_fields=["binding_status", "billing_order_id", "billing_plan_id", "expired_at", "updated_at"])
        return binding

    @staticmethod
    def sync_instance_from_resource(instance: OpenClawInstance, resource: RuntimeServerResource) -> OpenClawInstance:
        now = _now_ts()
        updates: dict[str, Any] = {
            "server_resource": resource,
            "host_id": resource.host_id or instance.host_id,
            "instance_id": resource.instance_id or instance.instance_id,
            "instance_name": resource.instance_name or instance.instance_name,
            "public_ip": resource.public_ip or instance.public_ip,
            "private_ip": resource.private_ip or instance.private_ip,
            "region": resource.region or instance.region,
            "zone": resource.zone or instance.zone,
            "expired_at": resource.expired_at or instance.expired_at,
            "renewal_status": resource.renewal_status or instance.renewal_status,
        }
        update_fields: list[str] = []
        for field_name, field_value in updates.items():
            if getattr(instance, field_name) != field_value:
                setattr(instance, field_name, field_value)
                update_fields.append(field_name)
        if update_fields:
            instance.updated_at = now
            update_fields.append("updated_at")
            instance.save(update_fields=update_fields)
        return instance

    @staticmethod
    def list_server_resources(
        *,
        company: Company | None = None,
        provider: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        queryset = RuntimeServerResource.objects.all().order_by("-id")
        if provider:
            queryset = queryset.filter(provider=provider)
        if status:
            queryset = queryset.filter(status=status)
        if company is not None:
            queryset = queryset.filter(company_bindings__company=company).distinct()
        total = queryset.count()
        resources = list(queryset[offset: offset + limit])
        binding_map: dict[int, list[dict[str, Any]]] = {item.id: [] for item in resources}
        for binding in CompanyRuntimeBinding.objects.filter(server_resource__in=resources).select_related("company", "openclaw_instance"):
            binding_map.setdefault(binding.server_resource_id, []).append(binding.get_info())
        return {
            "total": total,
            "items": [
                {**resource.get_info(), "bindings": binding_map.get(resource.id, [])}
                for resource in resources
            ],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def release_expired_resources(*, limit: int = 100, dry_run: bool = False) -> dict[str, Any]:
        now = _now_ts()
        now_float = time.time()
        queryset = CompanyRuntimeBinding.objects.filter(
            binding_status=CompanyRuntimeBinding.STATUS_ACTIVE,
        ).select_related("company", "openclaw_instance", "server_resource").order_by("id")[:limit]
        released: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for binding in queryset:
            instance = binding.openclaw_instance
            resource = binding.server_resource
            expires_at = binding.expired_at or instance.expired_at or resource.expired_at or ""
            if not _is_runtime_timestamp_due(expires_at, now_float):
                skipped.append({"binding_id": binding.id, "reason": "not expired", "expired_at": expires_at})
                continue
            if dry_run:
                released.append({"binding_id": binding.id, "instance_id": instance.id, "server_resource_id": resource.id, "expired_at": expires_at})
                continue
            ok, backup_payload = OpenClawRuntimeService.backup_runtime_state(instance)
            if not ok:
                message = str(backup_payload)
                resource.status = RuntimeServerResource.STATUS_EXPIRING
                resource.last_error = message
                resource.updated_at = now
                resource.save(update_fields=["status", "last_error", "updated_at"])
                failed.append({"binding_id": binding.id, "instance_id": instance.id, "server_resource_id": resource.id, "error": message})
                continue
            binding.binding_status = CompanyRuntimeBinding.STATUS_EXPIRED
            binding.released_at = now
            binding.updated_at = now
            binding.save(update_fields=["binding_status", "released_at", "updated_at"])

            instance.status = OpenClawInstance.STATUS_SUSPENDED
            instance.openclaw_status = OpenClawInstance.OPENCLAW_STATUS_STOPPED
            instance.last_error = "runtime server expired; platform metadata snapshot retained in COS"
            instance.updated_at = now
            instance.save(update_fields=["status", "openclaw_status", "last_error", "updated_at"])

            resource.status = RuntimeServerResource.STATUS_EXPIRED
            resource.renewal_status = RuntimeServerResource.RENEWAL_SKIPPED
            resource.last_error = "resource expired after successful runtime snapshot"
            resource.updated_at = now
            resource.save(update_fields=["status", "renewal_status", "last_error", "updated_at"])

            RuntimeAuditService.log(
                company=binding.company,
                instance=instance,
                action="runtime.resource.expired.released",
                target_type=OpenClawAuditLog.TARGET_BINDING,
                target_id=str(binding.id),
                detail={"binding": binding.get_info(), "snapshot": backup_payload},
            )
            released.append({"binding_id": binding.id, "instance_id": instance.id, "server_resource_id": resource.id, "snapshot": backup_payload})
        return {"released": released, "failed": failed, "skipped": skipped, "dry_run": dry_run}

class TencentLighthouseCloudClient:
    endpoint = "lighthouse.tencentcloudapi.com"

    def __init__(self, region: str = ""):
        self.region = str(region or getattr(settings, "TENCENT_LIGHTHOUSE_REGION", "") or "").strip()
        self.secret_id = str(
            getattr(settings, "TENCENT_CLOUD_SECRET_ID", "")
            or getattr(settings, "TENCENTCLOUD_SECRET_ID", "")
            or ""
        ).strip()
        self.secret_key = str(
            getattr(settings, "TENCENT_CLOUD_SECRET_KEY", "")
            or getattr(settings, "TENCENTCLOUD_SECRET_KEY", "")
            or ""
        ).strip()

    def _load_sdk(self):
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.lighthouse.v20200324 import lighthouse_client, models as lighthouse_models
        except ImportError as exc:
            raise RuntimeError("tencentcloud-sdk-python with lighthouse support is required") from exc
        return credential, ClientProfile, HttpProfile, lighthouse_client, lighthouse_models

    def _client(self):
        if not self.secret_id or not self.secret_key:
            raise RuntimeError("Tencent Cloud credentials are not configured")
        if not self.region:
            raise RuntimeError("TENCENT_LIGHTHOUSE_REGION is required")
        credential, ClientProfile, HttpProfile, lighthouse_client, _models = self._load_sdk()
        http_profile = HttpProfile()
        http_profile.endpoint = self.endpoint
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        cred = credential.Credential(self.secret_id, self.secret_key)
        return lighthouse_client.LighthouseClient(cred, self.region, client_profile)

    @staticmethod
    def _to_dict(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        for method_name in ("to_json_string", "ToJsonString"):
            method = getattr(response, method_name, None)
            if callable(method):
                return json.loads(method())
        return json.loads(json.dumps(response, default=lambda item: getattr(item, "__dict__", str(item))))

    @staticmethod
    def _from_json_request(request: Any, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload)
        for method_name in ("from_json_string", "FromJsonString"):
            method = getattr(request, method_name, None)
            if callable(method):
                method(body)
                return request
        for key, value in payload.items():
            setattr(request, key, value)
        return request

    @staticmethod
    def _first(values: Any) -> str:
        if isinstance(values, list) and values:
            return str(values[0] or "").strip()
        if isinstance(values, str):
            return values.strip()
        return ""

    @classmethod
    def _normalize_instance(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "instance_id": str(payload.get("InstanceId") or "").strip(),
            "instance_name": str(payload.get("InstanceName") or "").strip(),
            "host_id": str(payload.get("InstanceId") or payload.get("Uuid") or "").strip(),
            "public_ip": cls._first(payload.get("PublicAddresses")),
            "private_ip": cls._first(payload.get("PrivateAddresses")),
            "region": str(payload.get("Region") or "").strip(),
            "zone": str(payload.get("Zone") or "").strip(),
            "expired_at": str(payload.get("ExpiredTime") or "").strip(),
            "renewal_status": str(payload.get("RenewFlag") or RuntimeServerResource.RENEWAL_MANUAL).strip(),
            "image_id": str(payload.get("BlueprintId") or "").strip(),
            "resource_spec": {
                "bundle_id": str(payload.get("BundleId") or "").strip(),
                "cpu": payload.get("CPU"),
                "memory": payload.get("Memory"),
                "instance_state": payload.get("InstanceState"),
                "latest_operation": payload.get("LatestOperation"),
                "latest_operation_state": payload.get("LatestOperationState"),
            },
        }

    @staticmethod
    def _purchase_request(instance: OpenClawInstance, cloud_payload: dict[str, Any], task: OpenClawProvisionTask) -> dict[str, Any]:
        resource_spec = cloud_payload.get("resource_spec") if isinstance(cloud_payload.get("resource_spec"), dict) else {}
        bundle_id = str(resource_spec.get("bundle_id") or "").strip()
        blueprint_id = str(cloud_payload.get("image_id") or "").strip()
        period = int(resource_spec.get("period") or 0)
        if not bundle_id:
            raise RuntimeError("TENCENT_LIGHTHOUSE_BUNDLE_ID is required")
        if not blueprint_id:
            raise RuntimeError("TENCENT_LIGHTHOUSE_BLUEPRINT_ID is required")
        if period <= 0:
            raise RuntimeError("TENCENT_LIGHTHOUSE_PERIOD is required")
        request_payload = _load_payload(task.request_payload or "")
        client_token = str(
            request_payload.get("client_token")
            or f"tsds-{instance.company.cid}-{task.id}"
        ).strip()[:64]
        payload: dict[str, Any] = {
            "BundleId": bundle_id,
            "BlueprintId": blueprint_id,
            "InstanceChargePrepaid": {
                "Period": period,
                "RenewFlag": str(resource_spec.get("renew_flag") or "NOTIFY_AND_MANUAL_RENEW").strip(),
            },
            "InstanceName": str(cloud_payload.get("instance_name") or f"tsds-{instance.company.cid[:12]}").strip(),
            "InstanceCount": 1,
            "ClientToken": client_token,
            "DryRun": bool(request_payload.get("dry_run", False)),
            "Tags": [
                {"Key": "tsds-company-cid", "Value": str(instance.company.cid)},
                {"Key": "tsds-runtime-instance", "Value": str(instance.id or "dry-run")},
            ],
        }
        if cloud_payload.get("zone"):
            payload["Zones"] = [cloud_payload["zone"]]
        firewall_template_id = str(
            request_payload.get("firewall_template_id")
            or getattr(settings, "TENCENT_LIGHTHOUSE_FIREWALL_TEMPLATE_ID", "")
            or ""
        ).strip()
        if firewall_template_id:
            payload["FirewallTemplateId"] = firewall_template_id
        key_id = str(request_payload.get("key_id") or getattr(settings, "TENCENT_LIGHTHOUSE_KEY_ID", "") or "").strip()
        if key_id:
            payload["LoginConfiguration"] = {"KeyIds": [key_id]}
        return payload

    def create_instance(self, instance: OpenClawInstance, cloud_payload: dict[str, Any], task: OpenClawProvisionTask) -> dict[str, Any]:
        _credential, _ClientProfile, _HttpProfile, _lighthouse_client, models = self._load_sdk()
        request = self._from_json_request(models.CreateInstancesRequest(), self._purchase_request(instance, cloud_payload, task))
        response = self._client().CreateInstances(request)
        data = self._to_dict(response)
        instance_ids = data.get("InstanceIdSet") or data.get("Response", {}).get("InstanceIdSet") or []
        if not instance_ids:
            return {"dry_run": True, "raw_response": data}
        return {"instance_id": str(instance_ids[0]), "instance_id_set": instance_ids, "raw_response": data}

    def describe_instance(self, instance_id: str) -> dict[str, Any]:
        _credential, _ClientProfile, _HttpProfile, _lighthouse_client, models = self._load_sdk()
        request = self._from_json_request(models.DescribeInstancesRequest(), {"InstanceIds": [instance_id], "Limit": 1})
        response = self._client().DescribeInstances(request)
        data = self._to_dict(response)
        instance_set = data.get("InstanceSet") or data.get("Response", {}).get("InstanceSet") or []
        if not instance_set:
            return {"instance_id": instance_id, "resource_spec": {"instance_state": "UNKNOWN"}}
        normalized = self._normalize_instance(instance_set[0])
        normalized["raw_response"] = data
        return normalized

    def describe_firewall_rules(self, instance_id: str) -> dict[str, Any]:
        _credential, _ClientProfile, _HttpProfile, _lighthouse_client, models = self._load_sdk()
        request = self._from_json_request(
            models.DescribeFirewallRulesRequest(),
            {"InstanceId": instance_id, "Limit": 100, "Offset": 0},
        )
        data = self._to_dict(self._client().DescribeFirewallRules(request))
        response = data.get("Response") if isinstance(data.get("Response"), dict) else data
        return {
            "total": int(response.get("TotalCount") or 0),
            "items": response.get("FirewallRuleSet") or [],
            "raw_response": data,
        }

    @staticmethod
    def _firewall_rule_matches(rule: dict[str, Any], *, port: int, cidr: str) -> bool:
        protocol = str(rule.get("Protocol") or "").upper()
        action = str(rule.get("Action") or "").upper()
        rule_port = str(rule.get("Port") or "").strip()
        rule_cidr = str(rule.get("CidrBlock") or "").strip()
        if protocol not in {"TCP", "ALL"} or action not in {"", "ACCEPT"}:
            return False
        if rule_cidr and rule_cidr != cidr and rule_cidr != "0.0.0.0/0":
            return False
        if rule_port in {"ALL", str(port)}:
            return True
        if "-" in rule_port:
            try:
                start, end = [int(item.strip()) for item in rule_port.split("-", 1)]
                return start <= port <= end
            except ValueError:
                return False
        return False

    def ensure_bridge_firewall_rule(
        self,
        instance_id: str,
        *,
        port: int = 19080,
        cidr: str = "0.0.0.0/0",
    ) -> dict[str, Any]:
        instance_id = str(instance_id or "").strip()
        if not instance_id:
            raise RuntimeError("Tencent Lighthouse instance_id is required for firewall rule sync")
        rules = self.describe_firewall_rules(instance_id)
        for rule in rules.get("items") or []:
            if isinstance(rule, dict) and self._firewall_rule_matches(rule, port=port, cidr=cidr):
                return {"created": False, "rule": rule, "rules": rules}

        _credential, _ClientProfile, _HttpProfile, _lighthouse_client, models = self._load_sdk()
        payload = {
            "InstanceId": instance_id,
            "FirewallRules": [
                {
                    "Protocol": "TCP",
                    "Port": str(port),
                    "CidrBlock": cidr,
                    "Action": "ACCEPT",
                    "FirewallRuleDescription": "TSDS bridge API",
                }
            ],
        }
        request = self._from_json_request(models.CreateFirewallRulesRequest(), payload)
        data = self._to_dict(self._client().CreateFirewallRules(request))
        return {"created": True, "request": payload, "raw_response": data, "rules": rules}
    def describe_bundles(self, *, zones: list[str] | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        _credential, _ClientProfile, _HttpProfile, _lighthouse_client, models = self._load_sdk()
        payload: dict[str, Any] = {"Limit": limit, "Offset": offset}
        if zones:
            payload["Zones"] = zones
        payload["Filters"] = [{"Name": "support-platform-type", "Values": ["LINUX_UNIX"]}]
        request = self._from_json_request(models.DescribeBundlesRequest(), payload)
        data = self._to_dict(self._client().DescribeBundles(request))
        response = data.get("Response") if isinstance(data.get("Response"), dict) else data
        return {
            "total": int(response.get("TotalCount") or 0),
            "items": response.get("BundleSet") or [],
            "raw_response": data,
        }

    def describe_blueprints(
        self,
        *,
        blueprint_type: str = "",
        platform_type: str = "LINUX_UNIX",
        keyword: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        _credential, _ClientProfile, _HttpProfile, _lighthouse_client, models = self._load_sdk()
        filters: list[dict[str, Any]] = []
        if blueprint_type:
            filters.append({"Name": "blueprint-type", "Values": [blueprint_type]})
        if platform_type:
            filters.append({"Name": "platform-type", "Values": [platform_type]})
        payload: dict[str, Any] = {"Limit": limit, "Offset": offset}
        if filters:
            payload["Filters"] = filters
        request = self._from_json_request(models.DescribeBlueprintsRequest(), payload)
        data = self._to_dict(self._client().DescribeBlueprints(request))
        response = data.get("Response") if isinstance(data.get("Response"), dict) else data
        items = response.get("BlueprintSet") or []
        if keyword:
            lowered = keyword.lower()
            items = [
                item for item in items
                if lowered in json.dumps(item, ensure_ascii=False).lower()
            ]
        return {
            "total": int(response.get("TotalCount") or len(items) or 0),
            "items": items,
            "raw_response": data,
        }

    def dry_run_create_instance(self, instance: OpenClawInstance, cloud_payload: dict[str, Any], task: OpenClawProvisionTask) -> dict[str, Any]:
        request_payload = _load_payload(task.request_payload or "")
        request_payload["dry_run"] = True
        task.request_payload = _dump_payload(request_payload)
        return self.create_instance(instance, cloud_payload, task)

    def purchase_and_wait(self, instance: OpenClawInstance, cloud_payload: dict[str, Any], task: OpenClawProvisionTask) -> dict[str, Any]:
        create_result = self.create_instance(instance, cloud_payload, task)
        instance_id = str(create_result.get("instance_id") or "").strip()
        if not instance_id:
            raise RuntimeError("Tencent Lighthouse dry run completed; no instance was created")
        deadline = time.time() + int(getattr(settings, "TENCENT_LIGHTHOUSE_API_WAIT_TIMEOUT", 600) or 600)
        poll_interval = max(1, int(getattr(settings, "TENCENT_LIGHTHOUSE_API_POLL_INTERVAL", 10) or 10))
        last_payload: dict[str, Any] = {"instance_id": instance_id}
        while time.time() <= deadline:
            last_payload = self.describe_instance(instance_id)
            state = str((last_payload.get("resource_spec") or {}).get("instance_state") or "").upper()
            if last_payload.get("public_ip") and state in {"RUNNING", ""}:
                last_payload["purchase_response"] = create_result.get("raw_response")
                return last_payload
            time.sleep(poll_interval)
        raise RuntimeError(f"Tencent Lighthouse instance {instance_id} did not become ready before timeout")


class TencentLighthouseProviderAdapter(DockerSharedHostProviderAdapter):
    def _configured_cloud_payload(self, instance: OpenClawInstance, task: OpenClawProvisionTask) -> dict[str, Any]:
        request_payload = _load_payload(task.request_payload or "")
        instance_id = str(
            request_payload.get("instance_id")
            or instance.instance_id
            or getattr(settings, "TENCENT_LIGHTHOUSE_INSTANCE_ID", "")
            or ""
        ).strip()
        public_ip = str(
            request_payload.get("public_ip")
            or instance.public_ip
            or getattr(settings, "TENCENT_LIGHTHOUSE_PUBLIC_IP", "")
            or ""
        ).strip()
        private_ip = str(
            request_payload.get("private_ip")
            or instance.private_ip
            or getattr(settings, "TENCENT_LIGHTHOUSE_PRIVATE_IP", "")
            or ""
        ).strip()
        region = str(request_payload.get("region") or instance.region or getattr(settings, "TENCENT_LIGHTHOUSE_REGION", "") or "").strip()
        zone = str(request_payload.get("zone") or instance.zone or getattr(settings, "TENCENT_LIGHTHOUSE_ZONE", "") or "").strip()
        instance_name = str(
            request_payload.get("instance_name")
            or instance.instance_name
            or f"tsds-{instance.company.cid[:12]}"
        ).strip()
        return {
            "instance_id": instance_id,
            "instance_name": instance_name,
            "host_id": instance_id or instance.host_id or instance_name,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "region": region,
            "zone": zone,
            "expired_at": str(request_payload.get("expired_at") or instance.expired_at or "").strip(),
            "renewal_status": RuntimeServerResource.RENEWAL_MANUAL,
            "security_group_id": str(request_payload.get("security_group_id") or getattr(settings, "TENCENT_LIGHTHOUSE_SECURITY_GROUP_ID", "") or "").strip(),
            "image_id": str(request_payload.get("blueprint_id") or request_payload.get("image_id") or getattr(settings, "TENCENT_LIGHTHOUSE_BLUEPRINT_ID", "") or "").strip(),
            "ssh": {
                "user": str(request_payload.get("ssh_user") or getattr(settings, "TENCENT_LIGHTHOUSE_SSH_USER", "ubuntu") or "ubuntu").strip(),
                "port": int(request_payload.get("ssh_port") or getattr(settings, "TENCENT_LIGHTHOUSE_SSH_PORT", 22) or 22),
                "private_key_path": str(getattr(settings, "TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH", "") or "").strip(),
                "strict_host_key": bool(getattr(settings, "TENCENT_LIGHTHOUSE_SSH_STRICT_HOST_KEY", False)),
            },
            "resource_spec": {
                "bundle_id": str(request_payload.get("bundle_id") or getattr(settings, "TENCENT_LIGHTHOUSE_BUNDLE_ID", "") or "").strip(),
                "period": str(request_payload.get("period") or getattr(settings, "TENCENT_LIGHTHOUSE_PERIOD", "") or "").strip(),
                "renew_flag": str(request_payload.get("renew_flag") or getattr(settings, "TENCENT_LIGHTHOUSE_RENEW_FLAG", "NOTIFY_AND_MANUAL_RENEW") or "NOTIFY_AND_MANUAL_RENEW").strip(),
                "firewall_template_id": str(request_payload.get("firewall_template_id") or getattr(settings, "TENCENT_LIGHTHOUSE_FIREWALL_TEMPLATE_ID", "") or "").strip(),
                "key_id": str(request_payload.get("key_id") or getattr(settings, "TENCENT_LIGHTHOUSE_KEY_ID", "") or "").strip(),
            },
        }

    def _cloud_client(self) -> TencentLighthouseCloudClient:
        return TencentLighthouseCloudClient()

    def _ensure_bridge_firewall(self, instance: OpenClawInstance, task: Optional[OpenClawProvisionTask] = None) -> dict[str, Any]:
        if not bool(getattr(settings, "TENCENT_LIGHTHOUSE_API_ENABLED", False)):
            return {"enabled": False, "reason": "TENCENT_LIGHTHOUSE_API_ENABLED is false"}
        if not bool(getattr(settings, "TENCENT_LIGHTHOUSE_AUTO_OPEN_BRIDGE_FIREWALL", True)):
            return {"enabled": False, "reason": "TENCENT_LIGHTHOUSE_AUTO_OPEN_BRIDGE_FIREWALL is false"}
        instance_id = str(instance.instance_id or "").strip()
        if not instance_id:
            return {"enabled": False, "reason": "instance_id is empty"}
        bridge_port = int(getattr(settings, "TENCENT_LIGHTHOUSE_BRIDGE_PORT", 19080) or 19080)
        cidr = str(getattr(settings, "TENCENT_LIGHTHOUSE_BRIDGE_CIDR", "0.0.0.0/0") or "0.0.0.0/0").strip()
        result = self._cloud_client().ensure_bridge_firewall_rule(instance_id, port=bridge_port, cidr=cidr)
        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.lighthouse.firewall.synced",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail={
                "task_id": task.id if task else None,
                "instance_id": instance_id,
                "bridge_port": bridge_port,
                "cidr": cidr,
                "created": bool(result.get("created")),
                "request_id": (
                    (result.get("raw_response") or {}).get("RequestId")
                    or (result.get("raw_response") or {}).get("Response", {}).get("RequestId")
                ),
            },
        )
        return result
    @staticmethod
    def _clear_unverified_instance_target(instance: OpenClawInstance, reason: str) -> None:
        now = _now_ts()
        update_fields: list[str] = []
        for field_name in ["instance_id", "host_id", "public_ip", "private_ip", "gateway_endpoint"]:
            if getattr(instance, field_name, None):
                setattr(instance, field_name, "")
                update_fields.append(field_name)
        if instance.server_resource_id:
            instance.server_resource = None
            update_fields.append("server_resource")
        instance.status = OpenClawInstance.STATUS_PROVISION_FAILED
        instance.openclaw_status = OpenClawInstance.OPENCLAW_STATUS_UNKNOWN
        instance.last_error = reason
        instance.updated_at = now
        update_fields.extend(["status", "openclaw_status", "last_error", "updated_at"])
        instance.save(update_fields=list(dict.fromkeys(update_fields)))

    @staticmethod
    def _cloud_instance_exists(payload: dict[str, Any]) -> bool:
        state = str((payload.get("resource_spec") or {}).get("instance_state") or "").upper()
        return bool(payload.get("public_ip") or payload.get("private_ip")) and state not in {"", "UNKNOWN", "TERMINATED", "TERMINATING"}

    def _verify_configured_cloud_payload(
        self,
        instance: OpenClawInstance,
        cloud_payload: dict[str, Any],
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any] | str]:
        instance_id = str(cloud_payload.get("instance_id") or "").strip()
        if not instance_id:
            reason = "configured lighthouse target must include instance_id for cloud verification"
            self._clear_unverified_instance_target(instance, reason)
            return False, reason
        try:
            described_payload = self._cloud_client().describe_instance(instance_id)
        except Exception as exc:
            return False, f"configured lighthouse instance could not be verified: {exc}"
        if not self._cloud_instance_exists(described_payload):
            reason = f"configured lighthouse instance {instance_id} was not found in Tencent Cloud"
            self._clear_unverified_instance_target(instance, reason)
            RuntimeAuditService.log(
                company=task.company,
                instance=instance,
                action="runtime.lighthouse.target.unverified",
                target_type=OpenClawAuditLog.TARGET_INSTANCE,
                target_id=str(instance.id),
                detail={"instance_id": instance_id, "describe": described_payload, "reason": reason},
            )
            return False, reason
        merged_payload = {**cloud_payload, **{key: value for key, value in described_payload.items() if value not in (None, "")}}
        resource_spec = cloud_payload.get("resource_spec") if isinstance(cloud_payload.get("resource_spec"), dict) else {}
        described_spec = described_payload.get("resource_spec") if isinstance(described_payload.get("resource_spec"), dict) else {}
        merged_payload["resource_spec"] = {**resource_spec, **described_spec}
        merged_payload["source"] = "verified_configured_lighthouse_target"
        merged_payload["describe_response"] = described_payload.get("raw_response")
        return True, merged_payload

    def _resolve_cloud_payload(
        self,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any] | str]:
        cloud_payload = self._configured_cloud_payload(instance, task)
        if cloud_payload.get("instance_id") or cloud_payload.get("public_ip"):
            if bool(getattr(settings, "TENCENT_LIGHTHOUSE_API_ENABLED", False)):
                return self._verify_configured_cloud_payload(instance, cloud_payload, task)
            cloud_payload.setdefault("source", "configured_lighthouse_target")
            return True, cloud_payload
        if not bool(getattr(settings, "TENCENT_LIGHTHOUSE_API_ENABLED", False)):
            return False, "tencent lighthouse target ip is not configured and TENCENT_LIGHTHOUSE_API_ENABLED is false"
        try:
            purchased_payload = self._cloud_client().purchase_and_wait(instance, cloud_payload, task)
        except Exception as exc:
            return False, f"tencent lighthouse purchase failed: {exc}"
        merged_payload = {**cloud_payload, **{key: value for key, value in purchased_payload.items() if value not in (None, "")}}
        merged_payload["source"] = "tencent_lighthouse_api"
        merged_payload["create_response"] = purchased_payload.get("purchase_response")
        merged_payload["describe_response"] = purchased_payload.get("raw_response")
        resource_spec = cloud_payload.get("resource_spec") if isinstance(cloud_payload.get("resource_spec"), dict) else {}
        purchased_spec = purchased_payload.get("resource_spec") if isinstance(purchased_payload.get("resource_spec"), dict) else {}
        merged_payload["resource_spec"] = {**resource_spec, **purchased_spec}
        return True, merged_payload

    def provision_instance(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, resolved_payload = self._resolve_cloud_payload(instance, task)
        if not ok:
            return False, resolved_payload
        cloud_payload = resolved_payload
        assert isinstance(cloud_payload, dict)

        resource = RuntimeResourcePoolService.get_or_create_lighthouse_resource(instance=instance, cloud_payload=cloud_payload)
        RuntimeResourcePoolService.sync_instance_from_resource(instance, resource)
        binding = RuntimeResourcePoolService.bind_company_instance(
            company=company,
            instance=instance,
            server_resource=resource,
        )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.lighthouse.pool.bound",
            target_type=OpenClawAuditLog.TARGET_BINDING,
            target_id=str(binding.id),
            detail={"server_resource_id": resource.id, "instance_id": resource.instance_id, "public_ip": resource.public_ip},
        )
        firewall_sync: dict[str, Any] = {}
        if instance.instance_id:
            try:
                firewall_sync = self._ensure_bridge_firewall(instance, task)
            except Exception as exc:
                firewall_sync = {"error": f"failed to sync lighthouse bridge firewall: {exc}"}
                RuntimeAuditService.log(
                    company=company,
                    instance=instance,
                    action="runtime.lighthouse.firewall.sync_failed",
                    target_type=OpenClawAuditLog.TARGET_INSTANCE,
                    target_id=str(instance.id),
                    detail={"task_id": task.id, "message": firewall_sync["error"]},
                )

        payload = {
            **resource.get_info(),
            "provider": instance.provider,
            "host_id": instance.host_id or resource.host_id or resource.instance_id,
            "runtime_namespace": instance.runtime_namespace or _runtime_namespace(company),
            "gateway_endpoint": instance.gateway_endpoint or _gateway_endpoint(company),
            "config_path": instance.config_path or _runtime_config_path(company),
            "workspace_prefix": instance.workspace_prefix or _runtime_workspace_prefix(company),
            "source": str(cloud_payload.get("source") or "tencent_lighthouse_api"),
            "server_resource_id": resource.id,
            "binding_id": binding.id,
            "cloud": {
                "create_response": cloud_payload.get("create_response"),
                "describe_response": cloud_payload.get("describe_response"),
            },
            "firewall_sync": firewall_sync,
        }
        return True, payload

    def bootstrap_openclaw(
        self,
        company: Company,
        instance: OpenClawInstance,
        task: OpenClawProvisionTask,
    ) -> tuple[bool, dict[str, Any] | str]:
        if not (instance.public_ip or instance.private_ip or getattr(instance.server_resource, "api_base", "")):
            return False, "tencent lighthouse target ip is not configured"
        try:
            self._ensure_bridge_firewall(instance, task)
        except Exception as exc:
            return False, f"failed to sync lighthouse bridge firewall: {exc}"
        ok, payload = super().bootstrap_openclaw(company, instance, task)
        if ok and isinstance(payload, dict):
            payload.setdefault("source", "tencent_lighthouse_shared_host_api")
            payload.setdefault("server_resource_id", instance.server_resource_id)
        return ok, payload
    def check_health(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = super().check_health(company, instance)
        if ok and isinstance(payload, dict):
            payload.setdefault("source", "tencent_lighthouse_shared_host_api")
            payload.setdefault("server_resource_id", instance.server_resource_id)
            payload.setdefault("instance_id", instance.instance_id)
            payload.setdefault("public_ip", instance.public_ip)
        return ok, payload
    def export_runtime_state(
        self,
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any] | str]:
        config_version = OpenClawConfigVersion.objects.filter(company=company).order_by("-id").first()
        return True, {
            "snapshot_version": "v1",
            "runtime_namespace": instance.runtime_namespace or _runtime_namespace(company),
            "config_version": config_version.version if config_version else "",
            "config_payload": config_version.config_payload if config_version else "",
            "agent_count": TransparentCompanyAgentDefinition.objects.filter(transparentCompany__company=company).count(),
            "source": "platform_metadata_snapshot",
        }

    def restore_runtime_state(
        self,
        company: Company,
        instance: OpenClawInstance,
        snapshot: OpenClawRuntimeSnapshot,
    ) -> tuple[bool, dict[str, Any] | str]:
        if snapshot.status not in {OpenClawRuntimeSnapshot.STATUS_UPLOADED, OpenClawRuntimeSnapshot.STATUS_RESTORED}:
            return False, "snapshot is not restorable"
        return True, {
            "snapshot_id": snapshot.id,
            "cos_key": snapshot.cos_key,
            "restored_to_instance_id": instance.instance_id or str(instance.id),
            "source": "platform_metadata_snapshot",
        }


class OpenClawRuntimeService:
    @staticmethod
    def assert_company_admin(company: Company, user: User) -> tuple[bool, str]:
        if not can_access_formal_company(company, user, Grant.PERM_ADMIN):
            return False, "权限不足"
        return True, ""

    @staticmethod
    def get_provider_adapter(instance: Optional[OpenClawInstance] = None) -> OpenClawProviderAdapter:
        if instance and instance.provider == OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST:
            return DockerSharedHostProviderAdapter()
        if instance and instance.provider == OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE:
            return TencentLighthouseProviderAdapter()
        return NoopOpenClawProviderAdapter()

    @staticmethod
    def get_company_instance(company: Company) -> tuple[bool, OpenClawInstance | str]:
        instance = (
            OpenClawInstance.objects.filter(company=company)
            .order_by("-id")
            .first()
        )
        if not instance:
            return False, "杩愯鏃跺疄渚嬩笉瀛樺湪"
        return True, instance

    @staticmethod
    def list_memory_inventory(company: Company) -> tuple[bool, dict[str, Any] | str]:
        """Use the configured provider while keeping Bridge details out of views."""

        ok, instance_or_message = OpenClawRuntimeService.get_company_instance(company)
        if not ok:
            return False, instance_or_message
        instance = instance_or_message
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        if not hasattr(adapter, "list_memory_inventory"):
            return False, "当前运行时提供方不支持记忆库存查询"
        return adapter.list_memory_inventory(company, instance)

    @staticmethod
    def list_company_tasks(
        company: Company,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        queryset = OpenClawProvisionTask.objects.filter(company=company).order_by("-id")
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return {
            "total": total,
            "items": [item.get_info() for item in items],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def list_company_audit_logs(
        company: Company,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        queryset = OpenClawAuditLog.objects.filter(company=company).order_by("-id")
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return {
            "total": total,
            "items": [item.get_info() for item in items],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def list_workspace_files(
        company: Company,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        queryset = WorkspaceFile.objects.filter(company=company).order_by("-id")
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return {
            "total": total,
            "items": [item.get_info() for item in items],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def list_workspace_transfer_tasks(
        company: Company,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        queryset = WorkspaceTransferTask.objects.filter(company=company).order_by("-id")
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return {
            "total": total,
            "items": [item.get_info() for item in items],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def get_workspace_file(company: Company, file_id: int) -> tuple[bool, WorkspaceFile | str]:
        try:
            workspace_file = WorkspaceFile.objects.get(id=file_id, company=company)
        except WorkspaceFile.DoesNotExist:
            return False, "workspace file not found"
        return True, workspace_file

    @staticmethod
    def read_workspace_file(company: Company, file_id: int) -> tuple[bool, dict[str, Any] | str]:
        ok, workspace_file_or_msg = OpenClawRuntimeService.get_workspace_file(company, file_id)
        if not ok:
            return False, workspace_file_or_msg
        workspace_file = workspace_file_or_msg
        instance = workspace_file.instance or OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.read_workspace_file(company, instance, workspace_file)

    @staticmethod
    def read_workspace_logical_path(company: Company, logical_path: str) -> tuple[bool, dict[str, Any] | str]:
        normalized_path = validate_workspace_logical_path(logical_path, "logical_path")
        instance = OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        return adapter.read_workspace_logical_file(company, instance, normalized_path)

    @staticmethod
    def create_workspace_logical_path(company: Company, logical_path: str, operator_id: str = "") -> tuple[bool, dict[str, Any] | str]:
        normalized_path = validate_workspace_logical_path(logical_path, "logical_path")
        if not normalized_path or normalized_path == "knowledge":
            return False, "logical_path is required"
        instance = OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        create_logical_path = getattr(adapter, "create_workspace_logical_path", None)
        if not callable(create_logical_path):
            return False, "workspace logical path create is not available"
        ok, payload = create_logical_path(company, instance, normalized_path)
        if not ok:
            return False, payload
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN,
            actor_id=operator_id,
            action="runtime.workspace.path.created",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=normalized_path,
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        return True, payload

    @staticmethod
    def delete_workspace_file(company: Company, file_id: int, operator_id: str = "") -> tuple[bool, dict[str, Any] | str]:
        ok, workspace_file_or_msg = OpenClawRuntimeService.get_workspace_file(company, file_id)
        if not ok:
            return False, workspace_file_or_msg
        workspace_file = workspace_file_or_msg
        instance = workspace_file.instance or OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, payload = adapter.delete_workspace_file(company, instance, workspace_file)
        if not ok:
            return False, payload
        deleted_logical_path = workspace_file.logical_path
        if _is_knowledge_resource_path(deleted_logical_path):
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=company,
                file=None,
                operation="delete",
                logical_path=deleted_logical_path,
            )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN,
            actor_id=operator_id,
            action="runtime.workspace.file.deleted",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(workspace_file.id),
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        workspace_file.delete()
        return True, payload

    @staticmethod
    def delete_workspace_logical_path(company: Company, logical_path: str, operator_id: str = "") -> tuple[bool, dict[str, Any] | str]:
        normalized_path = validate_workspace_logical_path(logical_path, "logical_path")
        instance = OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        delete_logical_path = getattr(adapter, "delete_workspace_logical_path", None)
        if not callable(delete_logical_path):
            return False, "workspace logical path delete is not available"
        ok, payload = delete_logical_path(company, instance, normalized_path)
        if not ok:
            return False, payload
        if _is_knowledge_resource_path(normalized_path):
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=company,
                file=None,
                operation="delete",
                logical_path=normalized_path,
            )
        WorkspaceFile.objects.filter(company=company, logical_path=normalized_path).delete()
        WorkspaceFile.objects.filter(company=company, logical_path__startswith=f"{normalized_path}/").delete()
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN,
            actor_id=operator_id,
            action="runtime.workspace.path.deleted",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=normalized_path,
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        return True, payload

    @staticmethod
    def rename_workspace_file(
        company: Company,
        file_id: int,
        new_logical_path: str,
        operator_id: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, workspace_file_or_msg = OpenClawRuntimeService.get_workspace_file(company, file_id)
        if not ok:
            return False, workspace_file_or_msg
        workspace_file = workspace_file_or_msg
        instance = workspace_file.instance or OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        previous_logical_path = workspace_file.logical_path
        ok, payload = adapter.rename_workspace_file(company, instance, workspace_file, new_logical_path)
        if not ok:
            return False, payload
        now = _now_ts()
        workspace_file.logical_path = new_logical_path.strip()
        workspace_file.file_name = new_logical_path.strip().split("/")[-1] or workspace_file.file_name
        workspace_file.updated_at = now
        workspace_file.sync_status = WorkspaceFile.SYNC_IN_SYNC
        workspace_file.save(update_fields=["logical_path", "file_name", "updated_at", "sync_status"])
        previous_is_knowledge = _is_knowledge_resource_path(previous_logical_path)
        current_is_knowledge = _is_knowledge_resource_path(workspace_file.logical_path)
        if previous_is_knowledge and current_is_knowledge:
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=company,
                file=workspace_file,
                operation="rename",
                logical_path=workspace_file.logical_path,
                previous_logical_path=previous_logical_path,
                mime_type=workspace_file.mime_type or "",
            )
        elif previous_is_knowledge:
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=company,
                file=None,
                operation="delete",
                logical_path=previous_logical_path,
            )
        elif current_is_knowledge:
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=company,
                file=workspace_file,
                operation="upsert",
                logical_path=workspace_file.logical_path,
                mime_type=workspace_file.mime_type or "",
            )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN,
            actor_id=operator_id,
            action="runtime.workspace.file.renamed",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(workspace_file.id),
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        return True, payload

    @staticmethod
    def rename_workspace_logical_path(
        company: Company,
        logical_path: str,
        new_logical_path: str,
        operator_id: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        normalized_path = validate_workspace_logical_path(logical_path, "logical_path")
        normalized_new_path = validate_workspace_logical_path(new_logical_path, "new_logical_path")
        instance = OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        rename_logical_path = getattr(adapter, "rename_workspace_logical_path", None)
        if not callable(rename_logical_path):
            return False, "workspace logical path rename is not available"
        ok, payload = rename_logical_path(company, instance, normalized_path, normalized_new_path)
        if not ok:
            return False, payload

        now = _now_ts()
        source_prefix = f"{normalized_path}/"
        target_prefix = f"{normalized_new_path}/"
        workspace_records: dict[int, WorkspaceFile] = {}
        for workspace_file in WorkspaceFile.objects.filter(company=company, logical_path=normalized_path):
            workspace_records[workspace_file.id] = workspace_file
        for workspace_file in WorkspaceFile.objects.filter(company=company, logical_path__startswith=source_prefix):
            workspace_records[workspace_file.id] = workspace_file
        for workspace_file in workspace_records.values():
            current_path = normalize_workspace_reference(workspace_file.logical_path or "")
            if current_path == normalized_path:
                updated_path = normalized_new_path
            elif current_path.startswith(source_prefix):
                suffix = current_path[len(source_prefix):]
                updated_path = f"{target_prefix}{suffix}".rstrip("/")
            else:
                continue
            workspace_file.logical_path = updated_path
            workspace_file.file_name = updated_path.split("/")[-1] or workspace_file.file_name
            workspace_file.updated_at = now
            workspace_file.sync_status = WorkspaceFile.SYNC_IN_SYNC
            workspace_file.save(update_fields=["logical_path", "file_name", "updated_at", "sync_status"])
        previous_is_knowledge = _is_knowledge_resource_path(normalized_path)
        current_is_knowledge = _is_knowledge_resource_path(normalized_new_path)
        if previous_is_knowledge and current_is_knowledge:
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=company,
                file=None,
                operation="rename",
                logical_path=normalized_new_path,
                previous_logical_path=normalized_path,
            )
        elif previous_is_knowledge:
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=company,
                file=None,
                operation="delete",
                logical_path=normalized_path,
            )
        elif current_is_knowledge:
            for workspace_file in workspace_records.values():
                if _is_knowledge_resource_path(workspace_file.logical_path):
                    OpenClawRuntimeService.enqueue_openviking_resource_sync(
                        company=company,
                        file=workspace_file,
                        operation="upsert",
                        logical_path=workspace_file.logical_path,
                        mime_type=workspace_file.mime_type or "",
                    )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN,
            actor_id=operator_id,
            action="runtime.workspace.path.renamed",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=normalized_path,
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        return True, payload

    @staticmethod
    def _upsert_workspace_file(
        company: Company,
        logical_path: str,
        file_name: str,
        mime_type: str = "",
        size: int = 0,
        operator_id: str = "",
    ) -> WorkspaceFile:
        instance = OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        now = _now_ts()
        normalized_path = validate_workspace_logical_path(logical_path, "logical_path")
        normalized_name = file_name.strip() or normalized_path.split("/")[-1] or "workspace-file"
        storage_key = f"runtime/{company.cid}/{normalized_path}"
        is_knowledge_resource = _is_knowledge_resource_path(normalized_path)
        workspace_file, _created = WorkspaceFile.objects.update_or_create(
            company=company,
            logical_path=normalized_path,
            defaults={
                "instance": instance,
                "file_name": normalized_name,
                "storage_backend": WorkspaceFile.STORAGE_TENCENT_COS,
                "storage_key": storage_key,
                "mime_type": mime_type or "",
                "size": size or 0,
                "sync_status": WorkspaceFile.SYNC_PENDING,
                "resource_uri": f"viking://resources/{normalized_path}" if is_knowledge_resource else "",
                "resource_sync_status": (
                    WorkspaceFile.RESOURCE_SYNC_PENDING
                    if is_knowledge_resource
                    else WorkspaceFile.RESOURCE_SYNC_NOT_APPLICABLE
                ),
                "resource_task_id": "",
                "resource_last_error": "",
                "created_by": operator_id or "",
                "updated_at": now,
                "created_at": now,
            },
        )
        return workspace_file

    @staticmethod
    def issue_workspace_upload_ticket(
        company: Company,
        logical_path: str,
        file_name: str,
        mime_type: str = "",
        size: int = 0,
        operator_id: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        workspace_file = OpenClawRuntimeService._upsert_workspace_file(
            company=company,
            logical_path=logical_path,
            file_name=file_name,
            mime_type=mime_type,
            size=size,
            operator_id=operator_id,
        )

        ticket = PresignedUrlIssueService.issue_upload_ticket(workspace_file)
        runtime_ticket = PresignedUrlIssueService.issue_runtime_pull_ticket(workspace_file)
        transfer_task = CosFileGatewayService.create_transfer_task(
            company=company,
            direction=WorkspaceTransferTask.DIRECTION_PLATFORM_TO_RUNTIME,
            file=workspace_file,
            logical_path=workspace_file.logical_path,
            request_payload={
                "ticket": runtime_ticket,
                "mime_type": mime_type or "",
                "size": size or 0,
            },
        )
        OpenClawRuntimeService.enqueue_workspace_transfer(transfer_task)

        RuntimeAuditService.log(
            company=company,
            instance=workspace_file.instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            action="runtime.workspace.upload.ticket.issued",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(workspace_file.id),
            detail={
                "logical_path": workspace_file.logical_path,
                "storage_key": workspace_file.storage_key,
                "transfer_task_id": transfer_task.id,
            },
        )
        return True, {
            "file": workspace_file.get_info(),
            "transfer_task": transfer_task.get_info(),
            "ticket": ticket,
        }

    @staticmethod
    def upload_workspace_file(
        company: Company,
        logical_path: str,
        file_name: str,
        file_content: bytes,
        mime_type: str = "",
        size: int = 0,
        operator_id: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        workspace_file = OpenClawRuntimeService._upsert_workspace_file(
            company=company,
            logical_path=logical_path,
            file_name=file_name,
            mime_type=mime_type,
            size=size,
            operator_id=operator_id,
        )
        content_type = (mime_type or "").strip() or "application/octet-stream"
        try:
            storage_result = put_object_with_key(
                file_content=file_content,
                file_key=workspace_file.storage_key,
                content_type=content_type,
            )
        except Exception as exc:
            logger.exception("runtime workspace direct upload failed")
            return False, f"上传文件到对象存储失败: {exc}"

        moderation_asset = None
        if is_image_file(workspace_file.storage_key, content_type):
            moderation_asset = ImageModerationService.register_uploaded_image(
                workspace_file.storage_key,
                source=ImageModerationAsset.SOURCE_RUNTIME_WORKSPACE,
                source_field="workspace_file",
                owner_type="company",
                owner_id=company.cid,
                mime_type=content_type,
                size=size or len(file_content),
            )

        runtime_ticket = PresignedUrlIssueService.issue_runtime_pull_ticket(workspace_file)
        transfer_task = CosFileGatewayService.create_transfer_task(
            company=company,
            direction=WorkspaceTransferTask.DIRECTION_PLATFORM_TO_RUNTIME,
            file=workspace_file,
            logical_path=workspace_file.logical_path,
            request_payload={
                "ticket": runtime_ticket,
                "mime_type": content_type,
                "size": size or len(file_content),
                "upload_mode": "backend_direct",
                "storage_result": storage_result,
                "moderation": moderation_asset.get_info() if moderation_asset else None,
            },
        )
        OpenClawRuntimeService.enqueue_workspace_transfer(transfer_task)

        RuntimeAuditService.log(
            company=company,
            instance=workspace_file.instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            action="runtime.workspace.upload.completed",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(workspace_file.id),
            detail={
                "logical_path": workspace_file.logical_path,
                "storage_key": workspace_file.storage_key,
                "transfer_task_id": transfer_task.id,
                "upload_mode": "backend_direct",
            },
        )
        return True, {
            "file": workspace_file.get_info(),
            "transfer_task": transfer_task.get_info(),
            "storage": storage_result,
            "upload_mode": "backend_direct",
        }

    @staticmethod
    def issue_workspace_download_ticket(
        company: Company,
        file_id: int,
        operator_id: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            workspace_file = WorkspaceFile.objects.get(id=file_id, company=company)
        except WorkspaceFile.DoesNotExist:
            return False, "workspace file not found"

        ticket = PresignedUrlIssueService.issue_download_ticket(workspace_file)
        runtime_ticket = PresignedUrlIssueService.issue_runtime_push_ticket(workspace_file)
        transfer_task = CosFileGatewayService.create_transfer_task(
            company=company,
            direction=WorkspaceTransferTask.DIRECTION_RUNTIME_TO_PLATFORM,
            file=workspace_file,
            logical_path=workspace_file.logical_path,
            request_payload={"ticket": runtime_ticket},
        )
        ok, transfer_payload = OpenClawRuntimeService.run_workspace_transfer(transfer_task)
        if not ok:
            return False, transfer_payload

        RuntimeAuditService.log(
            company=company,
            instance=workspace_file.instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            action="runtime.workspace.download.ticket.issued",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(workspace_file.id),
            detail={
                "logical_path": workspace_file.logical_path,
                "transfer_task_id": transfer_task.id,
            },
        )
        return True, {
            "file": workspace_file.get_info(),
            "transfer_task": transfer_task.get_info(),
            "ticket": ticket,
        }

    @staticmethod
    def release_expired_runtime_resources(limit: int = 100, dry_run: bool = False) -> dict[str, Any]:
        return RuntimeResourcePoolService.release_expired_resources(limit=limit, dry_run=dry_run)

    @staticmethod
    def list_server_resources(
        company: Optional[Company] = None,
        provider: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return RuntimeResourcePoolService.list_server_resources(
            company=company,
            provider=provider,
            status=status,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _build_runtime_snapshot_payload(instance: OpenClawInstance, provider_payload: dict[str, Any]) -> dict[str, Any]:
        company = instance.company
        transparent_company = TransparentCompany.objects.filter(company=company).first()
        active_binding = CompanyRuntimeBinding.objects.filter(
            company=company,
            openclaw_instance=instance,
            binding_status=CompanyRuntimeBinding.STATUS_ACTIVE,
        ).select_related("server_resource").order_by("-id").first()
        return {
            "snapshot_schema": "runtime-state/v1",
            "exported_at": _now_ts(),
            "company": company.get_info() if hasattr(company, "get_info") else {"cid": company.cid},
            "instance": instance.get_info(),
            "server_resource": instance.server_resource.get_info() if instance.server_resource else None,
            "active_binding": active_binding.get_info() if active_binding else None,
            "provider_payload": provider_payload,
            "config_versions": [
                item.get_info()
                for item in OpenClawConfigVersion.objects.filter(company=company).order_by("id")
            ],
            "transparent_company": transparent_company.get_info() if transparent_company else None,
            "agent_definitions": [
                item.get_info()
                for item in TransparentCompanyAgentDefinition.objects.filter(transparentCompany=transparent_company).order_by("id")
            ] if transparent_company else [],
            "agent_skill_bindings": [
                item.get_info()
                for item in TransparentCompanyAgentSkillBinding.objects.filter(transparentCompany=transparent_company).order_by("id")
            ] if transparent_company else [],
        }

    @staticmethod
    def _load_runtime_snapshot_payload(snapshot: OpenClawRuntimeSnapshot) -> dict[str, Any]:
        if not snapshot.cos_key:
            return {}
        encoded_content = get_object(snapshot.cos_key)
        raw = base64.b64decode(encoded_content).decode("utf-8")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _restore_config_version_from_snapshot(instance: OpenClawInstance, snapshot_payload: dict[str, Any]) -> dict[str, Any]:
        config_versions = snapshot_payload.get("config_versions") if isinstance(snapshot_payload.get("config_versions"), list) else []
        if not config_versions:
            provider_payload = snapshot_payload.get("provider_payload") if isinstance(snapshot_payload.get("provider_payload"), dict) else {}
            config_payload = str(provider_payload.get("config_payload") or "")
            if not config_payload:
                return {"restored": False, "reason": "snapshot has no config payload"}
            config_versions = [{"config_payload": config_payload, "schema_version": "v2-preview", "status": OpenClawConfigVersion.STATUS_PUBLISHED}]
        selected = next((item for item in reversed(config_versions) if item.get("status") == OpenClawConfigVersion.STATUS_PUBLISHED), None) or config_versions[-1]
        config_payload = str(selected.get("config_payload") or "")
        if not config_payload:
            return {"restored": False, "reason": "selected config payload is empty"}
        now = _now_ts()
        snapshot_id = str(snapshot_payload.get("snapshot_id") or "").strip()
        suffix = f"{snapshot_id}-{int(time.time())}" if snapshot_id else str(int(time.time()))
        version = f"restore-{instance.company.cid[:8]}-{suffix}"[:64]
        restored = OpenClawConfigVersion.objects.create(
            company=instance.company,
            instance=instance,
            version=version,
            schema_version=str(selected.get("schema_version") or "v2-preview"),
            source=OpenClawConfigVersion.SOURCE_PLATFORM_GUI,
            status=OpenClawConfigVersion.STATUS_DRAFT,
            created_by="runtime_snapshot_restore",
            notes=f"restored from runtime snapshot {snapshot_payload.get('snapshot_id') or ''}".strip(),
            config_payload=config_payload,
            published_at="",
            created_at=now,
            updated_at=now,
        )
        return {"restored": True, "config_version": restored.get_info()}

    @staticmethod
    def list_runtime_snapshots(
        company: Company,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        queryset = OpenClawRuntimeSnapshot.objects.filter(company=company).order_by("-id")
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return {
            "total": total,
            "items": [item.get_info() for item in items],
            "hasMore": offset + limit < total,
        }

    @staticmethod
    def backup_runtime_state(instance: OpenClawInstance) -> tuple[bool, dict[str, Any] | str]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, provider_payload = adapter.export_runtime_state(instance.company, instance)
        now = _now_ts()
        snapshot_payload: dict[str, Any] = {}
        cos_key = ""
        checksum = ""
        last_error = ""
        retention_days = int(getattr(settings, "RUNTIME_SNAPSHOT_RETENTION_DAYS", 30) or 30)
        if ok and isinstance(provider_payload, dict):
            snapshot_payload = OpenClawRuntimeService._build_runtime_snapshot_payload(instance, provider_payload)
            snapshot_payload["retention_days"] = retention_days
            cos_key = str(provider_payload.get("cos_key") or f"runtime-backups/company/{instance.company.cid}/{int(time.time())}.json")
            body = json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            checksum = hashlib.sha256(body).hexdigest()
            try:
                put_object_with_key(body, cos_key, content_type="application/json")
            except Exception as exc:
                logger.exception("runtime state snapshot upload failed")
                ok = False
                last_error = f"upload runtime snapshot to COS failed: {exc}"
        elif not ok:
            last_error = str(provider_payload)

        retention_until = str(time.time() + retention_days * 86400)[:20]
        snapshot = OpenClawRuntimeSnapshot.objects.create(
            company=instance.company,
            openclaw_instance=instance,
            server_resource=instance.server_resource,
            snapshot_type=OpenClawRuntimeSnapshot.TYPE_FULL,
            snapshot_version=str(snapshot_payload.get("snapshot_schema") or "runtime-state/v1"),
            cos_bucket=str(getattr(settings, "COS_BUCKET", "") or getattr(settings, "COS_BUCKET_NAME", "") or getattr(settings, "TENCENT_COS_BUCKET", "") or ""),
            cos_key=cos_key,
            checksum=checksum,
            status=OpenClawRuntimeSnapshot.STATUS_UPLOADED if ok else OpenClawRuntimeSnapshot.STATUS_FAILED,
            retention_until=retention_until,
            source_instance_id=instance.instance_id or str(instance.id),
            last_error="" if ok else last_error,
            created_at=now,
            updated_at=now,
        )
        if ok:
            instance.latest_snapshot_id = str(snapshot.id)
            instance.updated_at = now
            instance.save(update_fields=["latest_snapshot_id", "updated_at"])
        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.lighthouse.backup.succeeded" if ok else "runtime.lighthouse.backup.failed",
            target_type=OpenClawAuditLog.TARGET_SNAPSHOT,
            target_id=str(snapshot.id),
            detail={"snapshot": snapshot.get_info(), "provider_payload": provider_payload if isinstance(provider_payload, dict) else str(provider_payload)},
        )
        return ok, snapshot.get_info() if ok else last_error

    @staticmethod
    def restore_runtime_state(instance: OpenClawInstance, snapshot_id: Optional[int] = None) -> tuple[bool, dict[str, Any] | str]:
        queryset = OpenClawRuntimeSnapshot.objects.filter(
            company=instance.company,
            status__in=[OpenClawRuntimeSnapshot.STATUS_UPLOADED, OpenClawRuntimeSnapshot.STATUS_RESTORED],
        ).order_by("-id")
        if snapshot_id:
            queryset = queryset.filter(id=snapshot_id)
        snapshot = queryset.first()
        if snapshot is None:
            return False, "no restorable runtime snapshot found"

        snapshot_payload: dict[str, Any] = {}
        try:
            snapshot_payload = OpenClawRuntimeService._load_runtime_snapshot_payload(snapshot)
        except Exception as exc:
            logger.exception("runtime state snapshot load failed")
            snapshot.last_error = f"load runtime snapshot from COS failed: {exc}"
            snapshot.updated_at = _now_ts()
            snapshot.save(update_fields=["last_error", "updated_at"])
            return False, snapshot.last_error
        snapshot_payload["snapshot_id"] = snapshot.id

        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, provider_restore = adapter.restore_runtime_state(instance.company, instance, snapshot)
        config_restore = OpenClawRuntimeService._restore_config_version_from_snapshot(instance, snapshot_payload) if ok else {"restored": False}
        now = _now_ts()
        if ok:
            snapshot.status = OpenClawRuntimeSnapshot.STATUS_RESTORED
            snapshot.restored_to_instance_id = instance.instance_id or str(instance.id)
            snapshot.last_error = ""
        else:
            snapshot.last_error = str(provider_restore)
        snapshot.updated_at = now
        snapshot.save(update_fields=["status", "restored_to_instance_id", "last_error", "updated_at"])
        result = {
            "provider_restore": provider_restore,
            "config_restore": config_restore,
            "snapshot": snapshot.get_info(),
            "snapshot_payload_summary": {
                "config_versions": len(snapshot_payload.get("config_versions") or []),
                "agent_definitions": len(snapshot_payload.get("agent_definitions") or []),
                "agent_skill_bindings": len(snapshot_payload.get("agent_skill_bindings") or []),
            },
        }
        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.lighthouse.restore.succeeded" if ok else "runtime.lighthouse.restore.failed",
            target_type=OpenClawAuditLog.TARGET_SNAPSHOT,
            target_id=str(snapshot.id),
            detail=result,
        )
        return ok, result

    @staticmethod
    def readback_config(instance: OpenClawInstance) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = OpenClawConfigReadbackService.read_instance_config(instance)
        now = _now_ts()
        if ok:
            published_version = payload.get("published_version", "")
            current_runtime_version = payload.get("current_runtime_version", "")
            config_path = str(payload.get("config_path") or "").strip()
            gateway_endpoint = str(payload.get("gateway_endpoint") or "").strip()
            workspace_prefix = str(payload.get("workspace_prefix") or "").strip()
            config_status = (
                OpenClawInstance.CONFIG_STATUS_IN_SYNC
                if published_version and published_version == current_runtime_version
                else OpenClawInstance.CONFIG_STATUS_PENDING
            )
            instance.current_runtime_version = current_runtime_version or instance.current_runtime_version
            instance.config_status = config_status
            if config_path:
                instance.config_path = config_path
            if gateway_endpoint:
                instance.gateway_endpoint = gateway_endpoint
            if workspace_prefix:
                instance.workspace_prefix = workspace_prefix
            instance.last_readback_at = now
            instance.updated_at = now
            instance.save(
                update_fields=[
                    "current_runtime_version",
                    "config_status",
                    "config_path",
                    "gateway_endpoint",
                    "workspace_prefix",
                    "last_readback_at",
                    "updated_at",
                ]
            )
        else:
            instance.config_status = OpenClawInstance.CONFIG_STATUS_READBACK_FAILED
            instance.last_readback_at = now
            instance.last_error = str(payload)
            instance.updated_at = now
            instance.save(
                update_fields=[
                    "config_status",
                    "last_readback_at",
                    "last_error",
                    "updated_at",
                ]
            )

        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.config.readback",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=instance.current_publish_version or "",
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        return ok, payload

    @staticmethod
    def enqueue_config_readback(instance: OpenClawInstance) -> tuple[bool, dict[str, Any]]:
        task = OpenClawRuntimeService.create_task(
            company=instance.company,
            instance=instance,
            task_type=OpenClawProvisionTask.TASK_CONFIG_READBACK,
            step="created",
        )
        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.config.readback.requested",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=instance.current_publish_version or "",
            detail={"task_id": task.id},
        )
        from apps.runtime.tasks import readback_openclaw_config_task

        readback_openclaw_config_task.delay(task.id)
        return True, {"instance": instance.get_info(), "task": task.get_info()}

    @staticmethod
    def get_gateway_state(instance: OpenClawInstance) -> tuple[bool, dict[str, Any] | str]:
        ok, payload = OpenClawGatewayClient.read_runtime_state(instance)
        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.gateway.state.read",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        if ok and isinstance(payload, dict) and str(payload.get("status", "")).lower() == OpenClawInstance.OPENCLAW_STATUS_UNHEALTHY:
            RuntimeGovernanceService.open_drift_event(
                company=instance.company,
                instance=instance,
                drift_type=RuntimeDriftEvent.DRIFT_GATEWAY,
                summary="gateway reports unhealthy runtime state",
                detail=payload,
            )
        return ok, payload

    @staticmethod
    def list_cron_jobs(instance: OpenClawInstance) -> tuple[bool, dict[str, Any] | str]:
        return OpenClawGatewayClient.list_cron_jobs(instance)

    @staticmethod
    def list_cron_runs(instance: OpenClawInstance, job_id: str, limit: int = 20) -> tuple[bool, dict[str, Any] | str]:
        return OpenClawGatewayClient.list_cron_runs(instance, job_id, limit=limit)

    @staticmethod
    def create_cron_job(instance: OpenClawInstance, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | str]:
        return OpenClawGatewayClient.create_cron_job(instance, payload)

    @staticmethod
    def run_cron_job(instance: OpenClawInstance, job_id: str) -> tuple[bool, dict[str, Any] | str]:
        return OpenClawGatewayClient.run_cron_job(instance, job_id)

    @staticmethod
    def remove_cron_job(instance: OpenClawInstance, job_id: str) -> tuple[bool, dict[str, Any] | str]:
        return OpenClawGatewayClient.remove_cron_job(instance, job_id)

    @staticmethod
    def inspect_agent_runtime_exports(
        company: Company,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
        if not ok:
            return False, instance_or_msg
        instance = instance_or_msg
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, payload = adapter.inspect_agent_runtime_exports(company, instance)
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.agent.exports.inspect",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        return ok, payload

    @staticmethod
    def get_or_create_instance(
        company: Company,
        provider: str = OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST,
        region: str = "",
        zone: str = "",
        create_source: str = OpenClawInstance.CREATE_SOURCE_PLATFORM,
    ) -> tuple[bool, OpenClawInstance]:
        instance = (
            OpenClawInstance.objects.filter(company=company)
            .order_by("-id")
            .first()
        )
        if instance:
            return True, instance

        now = _now_ts()
        instance = OpenClawInstance.objects.create(
            company=company,
            provider=provider,
            host_id=DEFAULT_DOCKER_HOST_ID if provider == OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST else "",
            runtime_namespace=_runtime_namespace(company),
            gateway_endpoint=_gateway_endpoint(company),
            gateway_port=DEFAULT_GATEWAY_PORT,
            config_path=_runtime_config_path(company),
            workspace_prefix=_runtime_workspace_prefix(company),
            workspace_mode=OpenClawInstance.WORKSPACE_MODE_COS_BACKED,
            connection_mode=OpenClawInstance.CONNECTION_MODE_PLATFORM_SSH,
            config_status=OpenClawInstance.CONFIG_STATUS_PENDING,
            region=region or "",
            zone=zone or "",
            create_source=create_source,
            status=OpenClawInstance.STATUS_PENDING,
            openclaw_status=OpenClawInstance.OPENCLAW_STATUS_UNKNOWN,
            created_at=now,
            updated_at=now,
        )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.instance.created",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail={
                "provider": provider,
                "region": region,
                "zone": zone,
                "runtime_namespace": instance.runtime_namespace,
                "host_id": instance.host_id,
            },
        )
        return True, instance

    @staticmethod
    def create_task(
        company: Company,
        task_type: str,
        instance: Optional[OpenClawInstance] = None,
        step: str = "",
        request_payload: Optional[dict[str, Any]] = None,
    ) -> OpenClawProvisionTask:
        now = _now_ts()
        task = OpenClawProvisionTask.objects.create(
            company=company,
            instance=instance,
            task_type=task_type,
            status=OpenClawProvisionTask.STATUS_PENDING,
            step=step or "",
            request_payload=_dump_payload(request_payload),
            created_at=now,
            updated_at=now,
        )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.task.created",
            target_type=OpenClawAuditLog.TARGET_TASK,
            target_id=str(task.id),
            detail={
                "task_type": task_type,
                "step": step,
            },
        )
        return task

    @staticmethod
    def mark_workspace_transfer_running(transfer_task: WorkspaceTransferTask) -> WorkspaceTransferTask:
        now = _now_ts()
        transfer_task.status = WorkspaceTransferTask.STATUS_RUNNING
        transfer_task.started_at = transfer_task.started_at or now
        transfer_task.updated_at = now
        transfer_task.save(update_fields=["status", "started_at", "updated_at"])
        return transfer_task

    @staticmethod
    def mark_workspace_transfer_succeeded(
        transfer_task: WorkspaceTransferTask,
        result_payload: Optional[dict[str, Any]] = None,
    ) -> WorkspaceTransferTask:
        now = _now_ts()
        transfer_task.status = WorkspaceTransferTask.STATUS_SUCCEEDED
        transfer_task.result_payload = _dump_payload(result_payload)
        transfer_task.finished_at = now
        transfer_task.updated_at = now
        transfer_task.save(
            update_fields=["status", "result_payload", "finished_at", "updated_at"]
        )
        transfer_task.file.sync_status = WorkspaceFile.SYNC_IN_SYNC
        transfer_task.file.updated_at = now
        transfer_task.file.save(update_fields=["sync_status", "updated_at"])
        return transfer_task

    @staticmethod
    def mark_workspace_transfer_failed(
        transfer_task: WorkspaceTransferTask,
        error_message: str,
        result_payload: Optional[dict[str, Any]] = None,
    ) -> WorkspaceTransferTask:
        now = _now_ts()
        transfer_task.status = WorkspaceTransferTask.STATUS_FAILED
        transfer_task.error_message = error_message
        transfer_task.result_payload = _dump_payload(result_payload)
        transfer_task.finished_at = now
        transfer_task.updated_at = now
        transfer_task.save(
            update_fields=["status", "error_message", "result_payload", "finished_at", "updated_at"]
        )
        transfer_task.file.sync_status = WorkspaceFile.SYNC_FAILED
        transfer_task.file.updated_at = now
        transfer_task.file.save(update_fields=["sync_status", "updated_at"])
        RuntimeGovernanceService.open_drift_event(
            company=transfer_task.company,
            instance=transfer_task.instance,
            drift_type=RuntimeDriftEvent.DRIFT_WORKSPACE,
            summary="workspace transfer failed",
            detail={
                "transfer_task_id": transfer_task.id,
                "file_id": transfer_task.file_id,
                "logical_path": getattr(transfer_task.file, "logical_path", ""),
                "message": error_message,
            },
        )
        return transfer_task

    @staticmethod
    def enqueue_workspace_transfer(transfer_task: WorkspaceTransferTask) -> tuple[bool, dict[str, Any]]:
        RuntimeAuditService.log(
            company=transfer_task.company,
            instance=transfer_task.instance,
            action="runtime.workspace.transfer.queued",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(transfer_task.file_id),
            detail={"transfer_task_id": transfer_task.id},
        )
        from apps.runtime.tasks import sync_workspace_transfer_task

        sync_workspace_transfer_task.delay(transfer_task.id)
        return True, transfer_task.get_info()

    @staticmethod
    def enqueue_openviking_resource_sync(
        *,
        company: Company,
        file: Optional[WorkspaceFile],
        operation: str,
        logical_path: str,
        previous_logical_path: str = "",
        mime_type: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        normalized_operation = str(operation or "").strip().lower()
        normalized_path = validate_workspace_logical_path(logical_path, "logical_path")
        if normalized_operation not in {"upsert", "rename", "delete"}:
            return False, "unsupported OpenViking resource operation"
        if not _is_knowledge_resource_path(normalized_path):
            return True, {"queued": False, "reason": "not a knowledge resource"}
        normalized_previous_path = ""
        if normalized_operation == "rename":
            normalized_previous_path = validate_workspace_logical_path(
                previous_logical_path,
                "previous_logical_path",
            )

        if file is not None:
            file.resource_uri = f"viking://resources/{normalized_path}"
            file.resource_sync_status = WorkspaceFile.RESOURCE_SYNC_PENDING
            file.resource_task_id = ""
            file.resource_last_error = ""
            file.updated_at = _now_ts()
            file.save(
                update_fields=[
                    "resource_uri",
                    "resource_sync_status",
                    "resource_task_id",
                    "resource_last_error",
                    "updated_at",
                ]
            )

        from apps.runtime.tasks import sync_openviking_resource_task

        async_result = sync_openviking_resource_task.delay(
            company.cid,
            file.id if file is not None else None,
            normalized_operation,
            normalized_path,
            normalized_previous_path,
            str(mime_type or ""),
        )
        celery_task_id = str(getattr(async_result, "id", "") or "")
        if file is not None and celery_task_id:
            file.resource_task_id = celery_task_id
            file.save(update_fields=["resource_task_id"])
        RuntimeAuditService.log(
            company=company,
            instance=file.instance if file is not None else OpenClawInstance.objects.filter(company=company).order_by("-id").first(),
            action="runtime.openviking.resource.queued",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(file.id if file is not None else normalized_path),
            detail={
                "operation": normalized_operation,
                "logical_path": normalized_path,
                "previous_logical_path": normalized_previous_path,
                "celery_task_id": celery_task_id,
            },
        )
        return True, {"queued": True, "task_id": celery_task_id}

    @staticmethod
    def run_openviking_resource_sync(
        *,
        company_id: str,
        file_id: Optional[int],
        operation: str,
        logical_path: str,
        previous_logical_path: str = "",
        mime_type: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        company = Company.objects.get(cid=company_id)
        workspace_file = WorkspaceFile.objects.filter(id=file_id, company=company).first() if file_id else None
        instance = (
            (workspace_file.instance if workspace_file is not None else None)
            or OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        )
        if instance is None:
            error = "runtime instance missing"
            if workspace_file is not None:
                workspace_file.resource_sync_status = WorkspaceFile.RESOURCE_SYNC_FAILED
                workspace_file.resource_last_error = error
                workspace_file.updated_at = _now_ts()
                workspace_file.save(update_fields=["resource_sync_status", "resource_last_error", "updated_at"])
            return False, error

        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, payload = adapter.sync_openviking_resource(
            company,
            instance,
            operation=operation,
            logical_path=logical_path,
            previous_logical_path=previous_logical_path,
            mime_type=mime_type,
        )
        now = _now_ts()
        if not ok:
            error = sanitize_user_visible_error(payload)
            if workspace_file is not None:
                workspace_file.resource_sync_status = WorkspaceFile.RESOURCE_SYNC_FAILED
                workspace_file.resource_last_error = error
                workspace_file.updated_at = now
                workspace_file.save(update_fields=["resource_sync_status", "resource_last_error", "updated_at"])
            RuntimeAuditService.log(
                company=company,
                instance=instance,
                action="runtime.openviking.resource.failed",
                target_type=OpenClawAuditLog.TARGET_FILE,
                target_id=str(file_id or logical_path),
                detail={"operation": operation, "logical_path": logical_path, "message": error},
            )
            return False, error

        result = payload if isinstance(payload, dict) else {"message": str(payload)}
        openviking_result = result.get("openviking") if isinstance(result.get("openviking"), dict) else {}
        openviking_task_id = str(
            openviking_result.get("task_id")
            or result.get("task_id")
            or ""
        )
        if workspace_file is not None:
            workspace_file.resource_uri = str(result.get("target_uri") or f"viking://resources/{logical_path}")
            workspace_file.resource_sync_status = WorkspaceFile.RESOURCE_SYNC_SUBMITTED
            if openviking_task_id:
                workspace_file.resource_task_id = openviking_task_id
            workspace_file.resource_last_error = ""
            workspace_file.resource_synced_at = now
            workspace_file.updated_at = now
            workspace_file.save(
                update_fields=[
                    "resource_uri",
                    "resource_sync_status",
                    "resource_task_id",
                    "resource_last_error",
                    "resource_synced_at",
                    "updated_at",
                ]
            )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.openviking.resource.submitted",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(file_id or logical_path),
            detail={"operation": operation, "logical_path": logical_path, **result},
        )
        return True, result

    @staticmethod
    def mark_task_running(task: OpenClawProvisionTask, step: str = "") -> OpenClawProvisionTask:
        now = _now_ts()
        task.status = OpenClawProvisionTask.STATUS_RUNNING
        task.step = step or task.step
        task.started_at = task.started_at or now
        task.updated_at = now
        task.save(update_fields=["status", "step", "started_at", "updated_at"])
        return task

    @staticmethod
    def mark_task_succeeded(
        task: OpenClawProvisionTask,
        result_payload: Optional[dict[str, Any]] = None,
        step: str = "",
    ) -> OpenClawProvisionTask:
        now = _now_ts()
        task.status = OpenClawProvisionTask.STATUS_SUCCEEDED
        task.step = step or task.step
        task.result_payload = _dump_payload(result_payload)
        task.finished_at = now
        task.updated_at = now
        task.save(
            update_fields=["status", "step", "result_payload", "finished_at", "updated_at"]
        )
        return task

    @staticmethod
    def mark_task_failed(
        task: OpenClawProvisionTask,
        error_message: str,
        step: str = "",
        result_payload: Optional[dict[str, Any]] = None,
    ) -> OpenClawProvisionTask:
        now = _now_ts()
        task.status = OpenClawProvisionTask.STATUS_FAILED
        task.step = step or task.step
        task.error_message = error_message
        task.result_payload = _dump_payload(result_payload)
        task.finished_at = now
        task.updated_at = now
        task.save(
            update_fields=["status", "step", "error_message", "result_payload", "finished_at", "updated_at"]
        )
        if task.instance:
            task.instance.last_error = error_message
            task.instance.updated_at = now
            task.instance.save(update_fields=["last_error", "updated_at"])
        return task

    @staticmethod
    def update_instance_status(
        instance: OpenClawInstance,
        status: str,
        openclaw_status: Optional[str] = None,
        last_error: Optional[str] = None,
        extra_updates: Optional[dict[str, Any]] = None,
    ) -> OpenClawInstance:
        now = _now_ts()
        instance.status = status
        if openclaw_status is not None:
            instance.openclaw_status = openclaw_status
        if last_error is not None:
            instance.last_error = last_error
        if extra_updates:
            for field_name, field_value in extra_updates.items():
                setattr(instance, field_name, field_value)
        instance.updated_at = now
        update_fields = ["status", "updated_at"]
        if openclaw_status is not None:
            update_fields.append("openclaw_status")
        if last_error is not None:
            update_fields.append("last_error")
        if extra_updates:
            update_fields.extend(extra_updates.keys())
        instance.save(update_fields=update_fields)
        return instance

    @staticmethod
    def list_lighthouse_purchase_catalog(
        *,
        region: str = "",
        zone: str = "",
        blueprint_type: str = "",
        platform_type: str = "LINUX_UNIX",
        keyword: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            selected_region = region or str(getattr(settings, "TENCENT_LIGHTHOUSE_REGION", "") or "")
            client = TencentLighthouseCloudClient(region=selected_region)
            bundles = client.describe_bundles(zones=[zone] if zone else None, limit=limit, offset=offset)
            blueprints = client.describe_blueprints(
                blueprint_type=blueprint_type,
                platform_type=platform_type or "LINUX_UNIX",
                keyword=keyword,
                limit=limit,
                offset=offset,
            )
            return True, {
                "region": selected_region,
                "zone": zone,
                "bundles": {"total": bundles["total"], "items": bundles["items"]},
                "blueprints": {"total": blueprints["total"], "items": blueprints["items"]},
            }
        except Exception as exc:
            return False, sanitize_user_visible_error(str(exc))

    @staticmethod
    def dry_run_lighthouse_purchase(
        *,
        company: Company,
        request_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        provider = OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE
        request_payload = dict(request_payload or {})
        request_payload.setdefault("client_token", f"tsds-dryrun-{company.cid}")
        instance = OpenClawInstance(
            company=company,
            provider=provider,
            region=str(request_payload.get("region") or getattr(settings, "TENCENT_LIGHTHOUSE_REGION", "") or ""),
            zone=str(request_payload.get("zone") or getattr(settings, "TENCENT_LIGHTHOUSE_ZONE", "") or ""),
            instance_name=str(request_payload.get("instance_name") or f"tsds-{company.cid[:12]}"),
            runtime_namespace=_runtime_namespace(company),
            status=OpenClawInstance.STATUS_PENDING,
        )
        now = _now_ts()
        task = OpenClawProvisionTask(
            company=company,
            instance=instance,
            task_type=OpenClawProvisionTask.TASK_CREATE_INSTANCE,
            status=OpenClawProvisionTask.STATUS_PENDING,
            step="dry_run",
            request_payload=_dump_payload({**request_payload, "dry_run": True}),
            created_at=now,
            updated_at=now,
        )
        adapter = TencentLighthouseProviderAdapter()
        cloud_payload = adapter._configured_cloud_payload(instance, task)
        try:
            result = adapter._cloud_client().dry_run_create_instance(instance, cloud_payload, task)
        except Exception as exc:
            return False, sanitize_user_visible_error(str(exc))
        return True, {
            "dry_run": True,
            "provider": provider,
            "request": {
                "region": request_payload.get("region") or getattr(settings, "TENCENT_LIGHTHOUSE_REGION", ""),
                "zone": request_payload.get("zone") or getattr(settings, "TENCENT_LIGHTHOUSE_ZONE", ""),
                "bundle_id": cloud_payload.get("resource_spec", {}).get("bundle_id"),
                "blueprint_id": cloud_payload.get("image_id"),
                "period": cloud_payload.get("resource_spec", {}).get("period"),
            },
            "response": result,
        }

    @staticmethod
    def start_instance_provision(
        company: Company,
        operator_id: str = "",
        provider: str = "",
        region: str = "",
        zone: str = "",
        request_payload: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any]]:
        create_provider = provider or OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST
        _, instance = OpenClawRuntimeService.get_or_create_instance(
            company=company,
            provider=create_provider,
            region=region,
            zone=zone,
        )
        selected_provider = provider or instance.provider or OpenClawInstance.PROVIDER_DOCKER_SHARED_HOST
        payload = request_payload if isinstance(request_payload, dict) else {}
        instance_updates: dict[str, Any] = {}
        if selected_provider and instance.provider != selected_provider:
            instance_updates["provider"] = selected_provider
        if region and instance.region != region:
            instance_updates["region"] = region
        if zone and instance.zone != zone:
            instance_updates["zone"] = zone
        for field_name in [
            "instance_id",
            "instance_name",
            "public_ip",
            "private_ip",
            "expired_at",
            "billing_order_id",
            "billing_plan_id",
        ]:
            field_value = str(payload.get(field_name) or "").strip()
            if field_value and getattr(instance, field_name) != field_value:
                instance_updates[field_name] = field_value
        if instance_updates:
            now = _now_ts()
            for field_name, field_value in instance_updates.items():
                setattr(instance, field_name, field_value)
            instance.updated_at = now
            instance.save(update_fields=[*instance_updates.keys(), "updated_at"])

        task_request_payload = {"region": region, "zone": zone, "provider": selected_provider}
        if payload:
            task_request_payload.update(payload)
        task = OpenClawRuntimeService.create_task(
            company=company,
            instance=instance,
            task_type=OpenClawProvisionTask.TASK_CREATE_INSTANCE,
            step="created",
            request_payload=task_request_payload,
        )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            action="runtime.provision.requested",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail={"task_id": task.id, "provider": selected_provider},
        )
        return True, {"instance": instance.get_info(), "task": task.get_info()}
    @staticmethod
    def enqueue_instance_provision(
        company: Company,
        operator_id: str = "",
        provider: str = "",
        region: str = "",
        zone: str = "",
        request_payload: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = OpenClawRuntimeService.start_instance_provision(
            company=company,
            operator_id=operator_id,
            provider=provider,
            region=region,
            zone=zone,
            request_payload=request_payload,
        )
        if not ok:
            return ok, payload

        from apps.runtime.tasks import provision_openclaw_instance_task

        provision_openclaw_instance_task.delay(payload["task"]["id"])
        return True, payload

    @staticmethod
    def retry_paid_company_runtime_provision(
        company: Company,
        operator_id: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        from apps.company.models import TransparentCompany

        if not TransparentCompany.objects.filter(company=company).exists():
            return False, "upgrade payment is required before retry"

        instance = OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        if instance is None:
            ok, instance = OpenClawRuntimeService.get_or_create_instance(
                company=company,
                provider=OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE,
            )
            if not ok:
                return False, "runtime instance create failed"

        active_task = (
            OpenClawProvisionTask.objects.filter(
                company=company,
                instance=instance,
                status__in=[OpenClawProvisionTask.STATUS_PENDING, OpenClawProvisionTask.STATUS_RUNNING],
            )
            .order_by("-id")
            .first()
        )
        if active_task:
            return True, {"instance": instance.get_info(), "task": active_task.get_info(), "retry": {"queued": False, "reason": "task already active"}}

        if (
            instance.status == OpenClawInstance.STATUS_RUNNING
            and instance.openclaw_status == OpenClawInstance.OPENCLAW_STATUS_HEALTHY
        ):
            return True, {"instance": instance.get_info(), "retry": {"queued": False, "reason": "runtime already ready"}}

        latest_failed = (
            OpenClawProvisionTask.objects.filter(
                company=company,
                instance=instance,
                status=OpenClawProvisionTask.STATUS_FAILED,
            )
            .order_by("-id")
            .first()
        )
        latest_create = (
            OpenClawProvisionTask.objects.filter(
                company=company,
                instance=instance,
                task_type=OpenClawProvisionTask.TASK_CREATE_INSTANCE,
            )
            .order_by("-id")
            .first()
        )

        now = _now_ts()
        instance.last_error = ""
        instance.updated_at = now
        instance.save(update_fields=["last_error", "updated_at"])

        if latest_failed and latest_failed.task_type == OpenClawProvisionTask.TASK_DEPLOY_BRIDGE:
            can_retry_bridge = True
            if instance.provider == OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE and bool(getattr(settings, "TENCENT_LIGHTHOUSE_API_ENABLED", False)):
                verification_task = latest_create or latest_failed
                adapter = TencentLighthouseProviderAdapter()
                cloud_payload = adapter._configured_cloud_payload(instance, verification_task)
                verified_ok, verified_payload = adapter._verify_configured_cloud_payload(instance, cloud_payload, verification_task)
                can_retry_bridge = bool(verified_ok)
                if not verified_ok:
                    RuntimeAuditService.log(
                        company=company,
                        instance=instance,
                        actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
                        actor_id=operator_id,
                        action="runtime.lighthouse.retry.reallocate",
                        target_type=OpenClawAuditLog.TARGET_INSTANCE,
                        target_id=str(instance.id),
                        detail={"reason": str(verified_payload), "previous_task_id": latest_failed.id},
                    )
            if can_retry_bridge:
                request_payload = _load_payload(latest_failed.request_payload or "")
                return OpenClawRuntimeService.enqueue_lighthouse_bridge_deploy(company, instance, request_payload=request_payload)

        if latest_failed and latest_failed.task_type == OpenClawProvisionTask.TASK_BOOTSTRAP_OPENCLAW:
            return OpenClawRuntimeService.enqueue_bootstrap(company=company, instance=instance)

        request_payload = _load_payload(latest_create.request_payload or "") if latest_create else {}
        request_payload.pop("provider", None)
        region = str(request_payload.pop("region", None) or instance.region or "").strip()
        zone = str(request_payload.pop("zone", None) or instance.zone or "").strip()
        if instance.provider == OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE and not request_payload.get("client_token"):
            token_seed = f"{company.cid}:transparent-upgrade:lighthouse"
            request_payload["client_token"] = f"upgrade-{hashlib.sha256(token_seed.encode()).hexdigest()[:24]}"

        instance.status = OpenClawInstance.STATUS_PENDING
        instance.openclaw_status = OpenClawInstance.OPENCLAW_STATUS_UNKNOWN
        instance.config_status = OpenClawInstance.CONFIG_STATUS_PENDING
        instance.updated_at = now
        instance.save(update_fields=["status", "openclaw_status", "config_status", "updated_at"])

        return OpenClawRuntimeService.enqueue_instance_provision(
            company=company,
            operator_id=operator_id,
            provider=instance.provider or OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE,
            region=region,
            zone=zone,
            request_payload=request_payload,
        )
    @staticmethod
    def run_instance_provision(task: OpenClawProvisionTask) -> tuple[bool, dict[str, Any] | str]:
        instance = task.instance
        if instance is None:
            OpenClawRuntimeService.mark_task_failed(task, "instance missing", step="validate")
            return False, "instance missing"

        OpenClawRuntimeService.mark_task_running(task, step="provisioning")
        OpenClawRuntimeService.update_instance_status(
            instance,
            status=OpenClawInstance.STATUS_PROVISIONING,
            last_error="",
        )
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, result = adapter.provision_instance(task.company, instance, task)
        if not ok:
            OpenClawRuntimeService.mark_task_failed(task, str(result), step="provisioning")
            OpenClawRuntimeService.update_instance_status(
                instance,
                status=OpenClawInstance.STATUS_PROVISION_FAILED,
                last_error=str(result),
            )
            return False, result

        OpenClawRuntimeService.mark_task_succeeded(task, result_payload=result, step="provisioned")
        should_enqueue_bridge_deploy = instance.provider == OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE
        OpenClawRuntimeService.update_instance_status(
            instance,
            status=OpenClawInstance.STATUS_BOOTSTRAPPING,
            openclaw_status=OpenClawInstance.OPENCLAW_STATUS_STARTING,
            last_error="",
            extra_updates={
                "host_id": result.get("host_id", instance.host_id),
                "runtime_namespace": result.get("runtime_namespace", instance.runtime_namespace),
                "gateway_endpoint": result.get("gateway_endpoint", instance.gateway_endpoint),
                "config_path": result.get("config_path", instance.config_path),
                "workspace_prefix": result.get("workspace_prefix", instance.workspace_prefix),
            },
        )
        if should_enqueue_bridge_deploy:
            OpenClawRuntimeService.enqueue_lighthouse_bridge_deploy(task.company, instance)
        return True, result

    @staticmethod
    def _sanitize_lighthouse_deploy_output(output: str, private_key_path: str = "", api_token: str = "") -> str:
        sanitized = str(output or "")
        if private_key_path:
            sanitized = sanitized.replace(private_key_path, "[ssh-key-path]")
        if api_token:
            sanitized = sanitized.replace(api_token, "[bridge-token]")
        return sanitized[-8000:]

    @staticmethod
    def _lighthouse_bridge_deploy_command(instance: OpenClawInstance, request_payload: Optional[dict[str, Any]] = None) -> tuple[bool, list[str] | str]:
        request_payload = request_payload or {}
        host = str(request_payload.get("public_ip") or instance.public_ip or getattr(settings, "TENCENT_LIGHTHOUSE_PUBLIC_IP", "") or "").strip()
        if not host:
            return False, "lighthouse public ip is required"
        private_key_path = str(getattr(settings, "TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH", "") or "").strip()
        if not private_key_path:
            return False, "TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH is required"
        script_path = Path(getattr(settings, "PROJECT_ROOT", Path.cwd())) / "scripts" / "runtime" / "deploy_lighthouse_bridge.py"
        if not script_path.exists():
            return False, f"deploy script not found: {script_path}"
        bridge_port = int(request_payload.get("bridge_port") or getattr(settings, "TENCENT_LIGHTHOUSE_BRIDGE_PORT", 19080) or 19080)
        command = [
            sys.executable,
            str(script_path),
            "--host",
            host,
            "--user",
            str(request_payload.get("ssh_user") or getattr(settings, "TENCENT_LIGHTHOUSE_SSH_USER", "ubuntu") or "ubuntu").strip(),
            "--port",
            str(int(request_payload.get("ssh_port") or getattr(settings, "TENCENT_LIGHTHOUSE_SSH_PORT", 22) or 22)),
            "--private-key",
            private_key_path,
            "--bridge-port",
            str(bridge_port),
            "--runtime-root",
            str(request_payload.get("runtime_root") or getattr(settings, "TENCENT_LIGHTHOUSE_RUNTIME_ROOT", "/data/tsds/tenants") or "/data/tsds/tenants"),
            "--openclaw-image",
            str(request_payload.get("openclaw_image") or getattr(settings, "TSDS_OPENCLAW_IMAGE", "node:22-bookworm-slim") or "node:22-bookworm-slim"),
            "--workspace-agent-image",
            str(request_payload.get("workspace_agent_image") or getattr(settings, "TSDS_WORKSPACE_AGENT_IMAGE", getattr(settings, "TSDS_OPENCLAW_IMAGE", "node:22-bookworm-slim")) or "node:22-bookworm-slim"),
            "--debian-mirror",
            str(request_payload.get("debian_mirror") or getattr(settings, "TSDS_DEBIAN_MIRROR", "") or getattr(settings, "TENCENT_LIGHTHOUSE_DEBIAN_MIRROR", "") or "http://mirrors.tencentyun.com/debian"),
            "--debian-security-mirror",
            str(request_payload.get("debian_security_mirror") or getattr(settings, "TSDS_DEBIAN_SECURITY_MIRROR", "") or getattr(settings, "TENCENT_LIGHTHOUSE_DEBIAN_SECURITY_MIRROR", "") or "http://mirrors.tencentyun.com/debian-security"),
            "--npm-registry",
            str(request_payload.get("npm_registry") or getattr(settings, "TSDS_NPM_REGISTRY", "") or getattr(settings, "TENCENT_LIGHTHOUSE_NPM_REGISTRY", "") or "https://registry.npmmirror.com"),
        ]
        if bool(request_payload.get("install_docker", False)):
            command.append("--install-docker")
        if bool(getattr(settings, "TENCENT_LIGHTHOUSE_SSH_STRICT_HOST_KEY", False)):
            command.append("--strict-host-key")
        return True, command

    @staticmethod
    def enqueue_lighthouse_bridge_deploy(
        company: Company,
        instance: OpenClawInstance,
        request_payload: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, dict[str, Any]]:
        task = OpenClawRuntimeService.create_task(
            company=company,
            instance=instance,
            task_type=OpenClawProvisionTask.TASK_DEPLOY_BRIDGE,
            step="created",
            request_payload=request_payload or {},
        )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.lighthouse.bridge.deploy.requested",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail={"task_id": task.id},
        )
        from apps.runtime.tasks import deploy_lighthouse_bridge_task

        deploy_lighthouse_bridge_task.delay(task.id)
        return True, {"instance": instance.get_info(), "task": task.get_info()}

    @staticmethod
    def run_lighthouse_bridge_deploy(task: OpenClawProvisionTask) -> tuple[bool, dict[str, Any] | str]:
        instance = task.instance
        if instance is None:
            OpenClawRuntimeService.mark_task_failed(task, "instance missing", step="validate")
            return False, "instance missing"
        if instance.provider != OpenClawInstance.PROVIDER_TENCENT_LIGHTHOUSE:
            message = "bridge deploy is only available for Tencent Lighthouse runtime"
            OpenClawRuntimeService.mark_task_failed(task, message, step="validate")
            return False, message

        request_payload = _load_payload(task.request_payload or "")
        ok, command_or_msg = OpenClawRuntimeService._lighthouse_bridge_deploy_command(instance, request_payload)
        if not ok:
            OpenClawRuntimeService.mark_task_failed(task, str(command_or_msg), step="validate")
            return False, str(command_or_msg)

        command = command_or_msg
        assert isinstance(command, list)
        private_key_path = str(getattr(settings, "TENCENT_LIGHTHOUSE_SSH_PRIVATE_KEY_PATH", "") or "")
        api_token = str(getattr(settings, "RUNTIME_DOCKER_SHARED_HOST_API_TOKEN", "") or "")
        OpenClawRuntimeService.mark_task_running(task, step="deploying_bridge")
        try:
            completed = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=int(getattr(settings, "TENCENT_LIGHTHOUSE_BOOTSTRAP_TIMEOUT", 600) or 600),
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            message = f"deploy lighthouse bridge failed: {exc}"
            OpenClawRuntimeService.mark_task_failed(task, message, step="deploying_bridge")
            return False, message

        output = OpenClawRuntimeService._sanitize_lighthouse_deploy_output(completed.stdout, private_key_path, api_token)
        host = str(request_payload.get("public_ip") or instance.public_ip or getattr(settings, "TENCENT_LIGHTHOUSE_PUBLIC_IP", "") or "").strip()
        bridge_port = int(request_payload.get("bridge_port") or getattr(settings, "TENCENT_LIGHTHOUSE_BRIDGE_PORT", 19080) or 19080)
        api_base = f"http://{host}:{bridge_port}" if host else ""
        result = {
            "returncode": completed.returncode,
            "stdout": output,
            "api_base": api_base,
            "host": host,
            "bridge_port": bridge_port,
        }
        if completed.returncode != 0:
            OpenClawRuntimeService.mark_task_failed(task, "deploy lighthouse bridge script failed", step="deploying_bridge", result_payload=result)
            return False, result

        now = _now_ts()
        if instance.server_resource and api_base:
            instance.server_resource.api_base = api_base
            instance.server_resource.status = RuntimeServerResource.STATUS_RUNNING
            instance.server_resource.last_error = ""
            instance.server_resource.updated_at = now
            instance.server_resource.save(update_fields=["api_base", "status", "last_error", "updated_at"])
        if api_base:
            instance.gateway_endpoint = api_base
        instance.last_error = ""
        instance.updated_at = now
        instance.save(update_fields=["gateway_endpoint", "last_error", "updated_at"])
        auto_bootstrap = request_payload.get("auto_bootstrap", True)
        if auto_bootstrap:
            active_bootstrap_exists = OpenClawProvisionTask.objects.filter(
                company=task.company,
                instance=instance,
                task_type=OpenClawProvisionTask.TASK_BOOTSTRAP_OPENCLAW,
                status__in=[OpenClawProvisionTask.STATUS_PENDING, OpenClawProvisionTask.STATUS_RUNNING],
            ).exists()
            if active_bootstrap_exists:
                result["bootstrap"] = {"queued": False, "reason": "bootstrap task already active"}
            else:
                _bootstrap_ok, bootstrap_payload = OpenClawRuntimeService.enqueue_bootstrap(task.company, instance)
                result["bootstrap"] = {
                    "queued": bool(_bootstrap_ok),
                    "task": bootstrap_payload.get("task") if isinstance(bootstrap_payload, dict) else None,
                }
        else:
            result["bootstrap"] = {"queued": False, "reason": "auto_bootstrap disabled"}

        OpenClawRuntimeService.mark_task_succeeded(task, result_payload=result, step="bridge_deployed")
        RuntimeAuditService.log(
            company=task.company,
            instance=instance,
            action="runtime.lighthouse.bridge.deployed",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail={"task_id": task.id, "api_base": api_base, "bootstrap": result.get("bootstrap")},
        )
        return True, result

    @staticmethod
    def run_config_readback(task: OpenClawProvisionTask) -> tuple[bool, dict[str, Any] | str]:
        instance = task.instance
        if instance is None:
            OpenClawRuntimeService.mark_task_failed(task, "instance missing", step="validate")
            return False, "instance missing"

        OpenClawRuntimeService.mark_task_running(task, step="readback")
        ok, payload = OpenClawRuntimeService.readback_config(instance)
        if ok:
            OpenClawRuntimeService.mark_task_succeeded(task, result_payload=payload, step="readback")
            return True, payload

        OpenClawRuntimeService.mark_task_failed(task, str(payload), step="readback")
        return False, payload

    @staticmethod
    def run_workspace_transfer(
        transfer_task: WorkspaceTransferTask,
    ) -> tuple[bool, dict[str, Any] | str]:
        if transfer_task.instance is None:
            OpenClawRuntimeService.mark_workspace_transfer_failed(
                transfer_task,
                "runtime instance missing",
                result_payload={"step": "validate"},
            )
            return False, "runtime instance missing"

        OpenClawRuntimeService.mark_workspace_transfer_running(transfer_task)
        adapter = OpenClawRuntimeService.get_provider_adapter(transfer_task.instance)
        ok, payload = adapter.sync_workspace_transfer(
            transfer_task.company,
            transfer_task.instance,
            transfer_task,
        )
        if not ok:
            OpenClawRuntimeService.mark_workspace_transfer_failed(
                transfer_task,
                str(payload),
                result_payload={"step": "workspace_transfer"},
            )
            RuntimeAuditService.log(
                company=transfer_task.company,
                instance=transfer_task.instance,
                action="runtime.workspace.transfer.failed",
                target_type=OpenClawAuditLog.TARGET_FILE,
                target_id=str(transfer_task.file_id),
                detail={
                    "transfer_task_id": transfer_task.id,
                    "message": str(payload),
                },
            )
            return False, payload

        OpenClawRuntimeService.mark_workspace_transfer_succeeded(
            transfer_task,
            result_payload=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        RuntimeAuditService.log(
            company=transfer_task.company,
            instance=transfer_task.instance,
            action="runtime.workspace.transfer.completed",
            target_type=OpenClawAuditLog.TARGET_FILE,
            target_id=str(transfer_task.file_id),
            detail={
                "transfer_task_id": transfer_task.id,
                **(payload if isinstance(payload, dict) else {"message": str(payload)}),
            },
        )
        if (
            transfer_task.direction == WorkspaceTransferTask.DIRECTION_PLATFORM_TO_RUNTIME
            and transfer_task.file is not None
            and _is_knowledge_resource_path(transfer_task.file.logical_path)
        ):
            OpenClawRuntimeService.enqueue_openviking_resource_sync(
                company=transfer_task.company,
                file=transfer_task.file,
                operation="upsert",
                logical_path=transfer_task.file.logical_path,
                mime_type=transfer_task.file.mime_type or "",
            )
        return True, payload

    @staticmethod
    def ensure_published_config(
        company: Company,
        instance: Optional[OpenClawInstance] = None,
        operator_id: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        published = (
            OpenClawConfigVersion.objects.filter(
                company=company,
                status=OpenClawConfigVersion.STATUS_PUBLISHED,
            )
            .order_by("-id")
            .first()
        )
        if published and str(published.config_payload or "").strip():
            return True, published.get_info()

        ok, payload = OpenClawConfigService.create_config_version(
            company=company,
            operator_id=operator_id,
            notes="auto-published before runtime bootstrap",
            source=OpenClawConfigVersion.SOURCE_PLATFORM_GUI,
            publish=True,
        )
        if not ok:
            return False, payload
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            actor_type=OpenClawAuditLog.ACTOR_ADMIN if operator_id else OpenClawAuditLog.ACTOR_SYSTEM,
            actor_id=operator_id,
            action="runtime.config.auto_published_for_bootstrap",
            target_type=OpenClawAuditLog.TARGET_CONFIG,
            target_id=str(payload.get("version") if isinstance(payload, dict) else ""),
            detail=payload if isinstance(payload, dict) else {"message": str(payload)},
        )
        return True, payload

    @staticmethod
    def start_bootstrap(company: Company, instance: OpenClawInstance) -> tuple[bool, dict[str, Any]]:
        config_ok, config_payload = OpenClawRuntimeService.ensure_published_config(company, instance)
        if not config_ok:
            return False, {"message": str(config_payload)}

        task = OpenClawRuntimeService.create_task(
            company=company,
            instance=instance,
            task_type=OpenClawProvisionTask.TASK_BOOTSTRAP_OPENCLAW,
            step="created",
        )
        RuntimeAuditService.log(
            company=company,
            instance=instance,
            action="runtime.bootstrap.requested",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail={"task_id": task.id},
        )
        return True, {"instance": instance.get_info(), "task": task.get_info()}

    @staticmethod
    def enqueue_bootstrap(
        company: Company,
        instance: OpenClawInstance,
    ) -> tuple[bool, dict[str, Any]]:
        ok, payload = OpenClawRuntimeService.start_bootstrap(company=company, instance=instance)
        if not ok:
            return ok, payload

        from apps.runtime.tasks import bootstrap_openclaw_instance_task

        bootstrap_openclaw_instance_task.delay(payload["task"]["id"])
        return True, payload

    @staticmethod
    def run_bootstrap(task: OpenClawProvisionTask) -> tuple[bool, dict[str, Any] | str]:
        instance = task.instance
        if instance is None:
            OpenClawRuntimeService.mark_task_failed(task, "instance missing", step="validate")
            return False, "instance missing"

        OpenClawRuntimeService.mark_task_running(task, step="bootstrapping")
        OpenClawRuntimeService.update_instance_status(
            instance,
            status=OpenClawInstance.STATUS_BOOTSTRAPPING,
            openclaw_status=OpenClawInstance.OPENCLAW_STATUS_STARTING,
            last_error="",
        )
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, result = adapter.bootstrap_openclaw(task.company, instance, task)
        if not ok:
            OpenClawRuntimeService.mark_task_failed(task, str(result), step="bootstrapping")
            OpenClawRuntimeService.update_instance_status(
                instance,
                status=OpenClawInstance.STATUS_BOOTSTRAP_FAILED,
                openclaw_status=OpenClawInstance.OPENCLAW_STATUS_UNHEALTHY,
                last_error=str(result),
            )
            return False, result

        bootstrap_updates: dict[str, Any] = {}
        if isinstance(result, dict):
            gateway_endpoint = str(result.get("gateway_endpoint") or "").strip()
            config_path = str(result.get("config_path") or "").strip()
            workspace_prefix = str(result.get("workspace_prefix") or "").strip()
            runtime_namespace = str(result.get("runtime_namespace") or "").strip()
            if gateway_endpoint:
                bootstrap_updates["gateway_endpoint"] = gateway_endpoint
            if config_path:
                bootstrap_updates["config_path"] = config_path
            if workspace_prefix:
                bootstrap_updates["workspace_prefix"] = workspace_prefix
            if runtime_namespace:
                bootstrap_updates["runtime_namespace"] = runtime_namespace

        OpenClawRuntimeService.mark_task_succeeded(task, result_payload=result, step="bootstrapped")
        OpenClawRuntimeService.update_instance_status(
            instance,
            status=OpenClawInstance.STATUS_RUNNING,
            openclaw_status=OpenClawInstance.OPENCLAW_STATUS_HEALTHY,
            last_error="",
            extra_updates={
                "config_status": OpenClawInstance.CONFIG_STATUS_PENDING,
                **bootstrap_updates,
            },
        )
        return True, result

    @staticmethod
    def record_health_check(instance: OpenClawInstance) -> tuple[bool, dict[str, Any] | str]:
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, result = adapter.check_health(instance.company, instance)
        now = _now_ts()
        instance.last_health_check_at = now
        instance.updated_at = now
        if ok:
            reported_status = result.get("openclaw_status", OpenClawInstance.OPENCLAW_STATUS_UNKNOWN)
            instance.openclaw_status = reported_status
            if reported_status == OpenClawInstance.OPENCLAW_STATUS_HEALTHY:
                instance.status = OpenClawInstance.STATUS_RUNNING
                instance.last_error = ""
            elif reported_status == OpenClawInstance.OPENCLAW_STATUS_UNHEALTHY:
                instance.status = OpenClawInstance.STATUS_DEGRADED
            instance.save(
                update_fields=[
                    "last_health_check_at",
                    "updated_at",
                    "openclaw_status",
                    "status",
                    "last_error",
                ]
            )
        else:
            instance.status = OpenClawInstance.STATUS_DEGRADED
            instance.openclaw_status = OpenClawInstance.OPENCLAW_STATUS_UNHEALTHY
            instance.last_error = str(result)
            instance.save(
                update_fields=[
                    "last_health_check_at",
                    "updated_at",
                    "status",
                    "openclaw_status",
                    "last_error",
                ]
            )

        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.health.checked",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail=result if isinstance(result, dict) else {"message": str(result)},
        )
        return ok, result

    @staticmethod
    def enqueue_health_check(instance: OpenClawInstance) -> tuple[bool, dict[str, Any]]:
        task = OpenClawRuntimeService.create_task(
            company=instance.company,
            instance=instance,
            task_type=OpenClawProvisionTask.TASK_HEALTH_CHECK,
            step="created",
        )
        RuntimeAuditService.log(
            company=instance.company,
            instance=instance,
            action="runtime.health.requested",
            target_type=OpenClawAuditLog.TARGET_INSTANCE,
            target_id=str(instance.id),
            detail={"task_id": task.id},
        )

        from apps.runtime.tasks import check_openclaw_health_task

        check_openclaw_health_task.delay(instance.id)
        return True, {"instance": instance.get_info(), "task": task.get_info()}


class AgentWorkspaceAccessService:
    @staticmethod
    def _requires_explicit_binding(agent: TransparentCompanyAgentDefinition) -> bool:
        return not (
            bool(getattr(agent, "isCoreAgent", False))
            or str(getattr(agent, "role", "") or "").strip() == TransparentCompanyAgentDefinition.ROLE_MAIN_AGENT
        )

    @staticmethod
    def _get_permission_binding(
        agent: TransparentCompanyAgentDefinition,
    ) -> TransparentCompanyAgentPermissionBinding | None:
        return (
            TransparentCompanyAgentPermissionBinding.objects.filter(
                transparentCompany=agent.transparentCompany,
                agent=agent,
            )
            .select_related("template")
            .first()
        )

    @staticmethod
    def get_effective_policy(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
    ):
        binding = AgentWorkspaceAccessService._get_permission_binding(agent)
        template = binding.template if binding and binding.template else None
        permission_config = getattr(company, "permissionConfig", None)
        require_explicit_binding = AgentWorkspaceAccessService._requires_explicit_binding(agent)
        return build_effective_runtime_policy(
            global_config=permission_config.get_info() if permission_config else {},
            template=(
                OpenClawConfigService._build_permission_template_item(template)
                if template is not None
                else {}
            ),
            binding=(
                OpenClawConfigService._build_agent_permission_binding_item(binding)
                if binding is not None
                else {}
            ),
            global_enabled=bool(permission_config and permission_config.enabled),
            template_enabled=bool(binding and binding.enabled and template and template.enabled),
            binding_enabled=bool(binding and binding.enabled),
            agent_id=agent.agentId,
            require_explicit_binding=require_explicit_binding,
            default_allow_skills=not require_explicit_binding,
            # Every Agent owns its own memory even when workspace access requires
            # a separate explicit permission binding.
            default_allow_memory=True,
        )

    @staticmethod
    def list_active_bound_skills(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
    ) -> list[dict[str, Any]]:
        policy = AgentWorkspaceAccessService.get_effective_policy(company, agent)
        skills = [
            OpenClawConfigService._build_skill_item(item)
            for item in TransparentCompanySkill.objects.filter(
                transparentCompany=agent.transparentCompany
            ).order_by("priority", "id")
        ]
        bindings = [
            OpenClawConfigService._build_binding_item(item)
            for item in TransparentCompanyAgentSkillBinding.objects.filter(
                transparentCompany=agent.transparentCompany,
                agent=agent,
            )
            .select_related("skill")
            .order_by("sortOrder", "id")
        ]
        return select_active_bound_skills(
            skills=skills,
            bindings=bindings,
            policy=policy,
        )

    @staticmethod
    def _list_accessible_workspace_files(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
    ) -> tuple[Any, dict[str, WorkspaceFile]]:
        policy = AgentWorkspaceAccessService.get_effective_policy(company, agent)
        latest_by_path: dict[str, WorkspaceFile] = {}
        queryset = WorkspaceFile.objects.filter(company=company).order_by("logical_path", "-id")
        for workspace_file in queryset:
            logical_path = normalize_workspace_reference(workspace_file.logical_path)
            if not logical_path or logical_path in latest_by_path:
                continue
            if not policy.allows_path(logical_path):
                continue
            latest_by_path[logical_path] = workspace_file
        return policy, latest_by_path

    @staticmethod
    def _get_runtime_instance(company: Company) -> OpenClawInstance | None:
        ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
        if ok:
            return instance_or_msg
        try:
            return OpenClawInstance.objects.filter(company=company).order_by("-id").first()
        except Exception:
            return None

    @staticmethod
    def _runtime_file_stub(logical_path: str, item: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_path = normalize_workspace_reference(logical_path)
        name = ""
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        if not name and normalized_path:
            name = normalized_path.rsplit("/", 1)[-1]
        payload = {
            "id": None,
            "logical_path": normalized_path,
            "file_name": name,
            "sync_status": "runtime_only",
        }
        if isinstance(item, dict) and item.get("size") is not None:
            payload["size"] = item.get("size")
        return payload

    @staticmethod
    def _visible_workspace_file_info(file_info: WorkspaceFile | dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(file_info, WorkspaceFile):
            payload = file_info.get_info()
        elif hasattr(file_info, "get_info") and callable(getattr(file_info, "get_info")):
            payload = file_info.get_info()
        elif isinstance(file_info, dict):
            payload = dict(file_info)
        else:
            payload = {}
        logical_path = normalize_workspace_reference(payload.get("logical_path") or "")
        visible = {
            "id": payload.get("id"),
            "logical_path": logical_path,
            "file_name": str(payload.get("file_name") or "").strip()
            or (logical_path.rsplit("/", 1)[-1] if logical_path else ""),
            "sync_status": str(payload.get("sync_status") or "").strip(),
        }
        for key in ("size", "mime_type", "checksum", "created_at", "updated_at"):
            value = payload.get(key)
            if value not in (None, ""):
                visible[key] = value
        return visible

    @staticmethod
    def _sanitize_read_result_payload(
        payload: dict[str, Any],
        *,
        logical_path: str,
        file_info: WorkspaceFile | dict[str, Any] | None,
    ) -> dict[str, Any]:
        safe_payload = dict(payload)
        normalized_path = normalize_workspace_reference(
            logical_path or safe_payload.get("logical_path") or safe_payload.get("path") or ""
        )
        raw_path = normalize_workspace_reference(safe_payload.get("path") or "")
        if raw_path not in {"", "*"}:
            safe_payload["path"] = raw_path
        else:
            safe_payload.pop("path", None)
        safe_payload["logical_path"] = normalized_path
        safe_payload["tool_name"] = WORKSPACE_TOOL_READ
        visible_file = AgentWorkspaceAccessService._visible_workspace_file_info(file_info or safe_payload.get("file"))
        if visible_file:
            safe_payload["file"] = visible_file
        else:
            safe_payload.pop("file", None)
        return safe_payload

    @staticmethod
    def _list_runtime_tree_items(
        company: Company,
        *,
        logical_path: str = "",
        recursive: bool = False,
    ) -> tuple[OpenClawInstance | None, dict[str, dict[str, Any]]]:
        instance = AgentWorkspaceAccessService._get_runtime_instance(company)
        if instance is None:
            return None, {}
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        list_workspace_tree = getattr(adapter, "list_workspace_tree", None)
        if not callable(list_workspace_tree):
            return instance, {}
        ok, payload = list_workspace_tree(
            company,
            instance,
            logical_path=logical_path,
            recursive=recursive,
        )
        if not ok or not isinstance(payload, dict):
            return instance, {}
        items_by_path: dict[str, dict[str, Any]] = {}
        for raw_item in payload.get("items", []) or []:
            if not isinstance(raw_item, dict):
                continue
            runtime_path = normalize_workspace_reference(
                raw_item.get("logical_path") or raw_item.get("path") or ""
            )
            kind = str(raw_item.get("kind") or "").strip().lower()
            if not runtime_path or kind not in {"file", "directory"}:
                continue
            item_name = str(raw_item.get("name") or "").strip() or runtime_path.rsplit("/", 1)[-1]
            items_by_path[runtime_path] = {
                **raw_item,
                "logical_path": runtime_path,
                "name": item_name,
                "kind": kind,
            }
        return instance, items_by_path

    @staticmethod
    def _normalize_directory_path(logical_path: str) -> str:
        normalized = normalize_workspace_reference(logical_path)
        if normalized == "*":
            return ""
        return normalized

    @staticmethod
    def _directory_query_allowed(policy: Any, logical_path: str) -> bool:
        normalized = AgentWorkspaceAccessService._normalize_directory_path(logical_path)
        return policy.allows_directory(normalized)

    @staticmethod
    def list_directory(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
        logical_path: str = "",
        *,
        include_runtime_tree: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        policy, accessible_files = AgentWorkspaceAccessService._list_accessible_workspace_files(company, agent)
        if not policy.allows_tool(WORKSPACE_TOOL_LIST):
            return False, "当前 Agent 没有目录读取权限"

        normalized_dir = AgentWorkspaceAccessService._normalize_directory_path(logical_path)
        if not policy.knowledge_reads:
            return False, "当前 Agent 没有可访问的 knowledge scope"
        if normalized_dir and not AgentWorkspaceAccessService._directory_query_allowed(policy, normalized_dir):
            return False, "当前 Agent 无权访问该 logical path"

        entries: dict[str, dict[str, Any]] = {}
        runtime_items: dict[str, dict[str, Any]] = {}
        exact_file_info = None
        exact_file = accessible_files.get(normalized_dir)
        if exact_file is not None:
            exact_file_info = AgentWorkspaceAccessService._visible_workspace_file_info(exact_file)

        for file_path, workspace_file in accessible_files.items():
            if normalized_dir:
                prefix = f"{normalized_dir}/"
                if not file_path.startswith(prefix):
                    continue
                relative_path = file_path[len(prefix):]
            else:
                relative_path = file_path
            if not relative_path:
                continue
            child_name, _, remainder = relative_path.partition("/")
            child_path = f"{normalized_dir}/{child_name}" if normalized_dir else child_name
            if remainder:
                entry = entries.setdefault(
                    child_path,
                    {
                        "name": child_name,
                        "logical_path": child_path,
                        "kind": "directory",
                        "runtime_visible": False,
                    },
                )
                entry.setdefault("children", 0)
                entry["children"] += 1
                continue
            entries[child_path] = {
                "name": child_name,
                "logical_path": file_path,
                "kind": "file",
                "file": AgentWorkspaceAccessService._visible_workspace_file_info(workspace_file),
                "runtime_visible": False,
            }

        if not entries and exact_file_info is not None:
            entries[normalized_dir] = {
                "name": str(exact_file_info.get("file_name") or ""),
                "logical_path": normalize_workspace_reference(exact_file_info.get("logical_path") or normalized_dir),
                "kind": "file",
                "file": exact_file_info,
                "runtime_visible": False,
            }

        if include_runtime_tree:
            _instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
                company,
                logical_path=normalized_dir,
                recursive=False,
            )
            for runtime_path, item in runtime_items.items():
                if item.get("kind") == "directory":
                    if not policy.allows_directory(runtime_path):
                        continue
                    entry = entries.setdefault(
                        runtime_path,
                        {
                            "name": str(item.get("name") or ""),
                            "logical_path": runtime_path,
                            "kind": "directory",
                            "runtime_visible": True,
                        },
                    )
                    entry["runtime_visible"] = True
                    continue
                if not policy.allows_path(runtime_path):
                    continue
                file_entry = entries.setdefault(
                    runtime_path,
                    {
                        "name": str(item.get("name") or ""),
                        "logical_path": runtime_path,
                        "kind": "file",
                        "file": AgentWorkspaceAccessService._runtime_file_stub(runtime_path, item),
                        "runtime_visible": True,
                    },
                )
                file_entry["runtime_visible"] = True

        items = sorted(
            entries.values(),
            key=lambda item: (
                0 if item.get("kind") == "directory" else 1,
                str(item.get("logical_path") or ""),
            ),
        )
        if exact_file_info is None and normalized_dir:
            runtime_exact = runtime_items.get(normalized_dir)
            if isinstance(runtime_exact, dict) and runtime_exact.get("kind") == "file" and policy.allows_path(normalized_dir):
                exact_file_info = AgentWorkspaceAccessService._runtime_file_stub(normalized_dir, runtime_exact)

        return True, {
            "tool_name": WORKSPACE_TOOL_LIST,
            "logical_path": normalized_dir,
            "items": items,
            "exact_file": exact_file_info,
        }

    @staticmethod
    def locate_files(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
        *,
        file_name: str = "",
        logical_path: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        policy, accessible_files = AgentWorkspaceAccessService._list_accessible_workspace_files(company, agent)
        if not policy.allows_tool(WORKSPACE_TOOL_LOCATE):
            return False, "当前 Agent 没有文件定位权限"
        if not policy.knowledge_reads:
            return False, "当前 Agent 没有可访问的 knowledge scope"

        normalized_path = normalize_workspace_reference(logical_path)
        if normalized_path and normalized_path != "*" and not (
            policy.allows_path(normalized_path)
            or policy.allows_directory(normalized_path)
        ):
            return False, "当前 Agent 无权访问该 logical path"

        candidate_map: dict[str, dict[str, Any]] = {
            path: AgentWorkspaceAccessService._visible_workspace_file_info(workspace_file)
            for path, workspace_file in accessible_files.items()
        }
        search_root = ""
        if normalized_path and normalized_path != "*":
            last_segment = normalized_path.rsplit("/", 1)[-1]
            if "." in last_segment and "/" in normalized_path:
                search_root = normalized_path.rsplit("/", 1)[0]
            elif "." not in last_segment:
                search_root = normalized_path
        _instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
            company,
            logical_path=search_root,
            recursive=True,
        )
        for runtime_path, item in runtime_items.items():
            if item.get("kind") != "file" or not policy.allows_path(runtime_path):
                continue
            candidate_map.setdefault(
                runtime_path,
                AgentWorkspaceAccessService._runtime_file_stub(runtime_path, item),
            )

        candidates = list(candidate_map.values())
        if normalized_path and normalized_path != "*":
            exact_match = [
                item for item in candidates
                if normalize_workspace_reference(item.get("logical_path") or "") == normalized_path
            ]
            if exact_match:
                candidates = exact_match
            else:
                prefix = f"{normalized_path}/"
                candidates = [
                    item for item in candidates
                    if normalize_workspace_reference(item.get("logical_path") or "").startswith(prefix)
                ]

        normalized_file_name = str(file_name or "").strip().lower()
        if normalized_file_name:
            exact_name_matches = [
                item for item in candidates
                if str(item.get("file_name") or "").strip().lower() == normalized_file_name
            ]
            if exact_name_matches:
                candidates = exact_name_matches
            else:
                candidates = [
                    item for item in candidates
                    if normalized_file_name in str(item.get("file_name") or "").strip().lower()
                ]

        candidates = sorted(
            candidates,
            key=lambda item: (
                normalize_workspace_reference(item.get("logical_path") or ""),
                str(item.get("file_name") or ""),
            ),
        )
        return True, {
            "tool_name": WORKSPACE_TOOL_LOCATE,
            "logical_path": normalized_path if normalized_path != "*" else "",
            "items": candidates,
        }

    @staticmethod
    def read_file(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
        *,
        logical_path: str = "",
        file_name: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        policy, accessible_files = AgentWorkspaceAccessService._list_accessible_workspace_files(company, agent)
        if not policy.allows_tool(WORKSPACE_TOOL_READ):
            return False, "当前 Agent 没有文件读取权限"
        if not policy.knowledge_reads:
            return False, "当前 Agent 没有可访问的 knowledge scope"

        normalized_path = normalize_workspace_reference(logical_path)
        normalized_file_name = str(file_name or "").strip().lower()
        if not normalized_path and not normalized_file_name:
            return False, "读取文件时必须明确指定 logical path 或 file_name"

        instance = AgentWorkspaceAccessService._get_runtime_instance(company)
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)

        if normalized_path and normalized_path != "*":
            if not policy.allows_path(normalized_path):
                return False, "当前 Agent 无权访问该 logical path"
            target_file = accessible_files.get(normalized_path)
            if target_file is not None:
                ok, payload = adapter.read_workspace_file(company, instance, target_file)
                if not ok:
                    return False, payload
                if not isinstance(payload, dict):
                    return False, "workspace file read returned invalid payload"
                return True, AgentWorkspaceAccessService._sanitize_read_result_payload(
                    payload,
                    logical_path=target_file.logical_path,
                    file_info=target_file,
                )

            ok, payload = adapter.read_workspace_logical_file(company, instance, normalized_path)
            if not ok:
                return False, payload
            if not isinstance(payload, dict):
                return False, "workspace file read returned invalid payload"
            return True, AgentWorkspaceAccessService._sanitize_read_result_payload(
                payload,
                logical_path=normalized_path,
                file_info=AgentWorkspaceAccessService._runtime_file_stub(normalized_path),
            )

        candidate_map: dict[str, dict[str, Any]] = {
            path: {
                "logical_path": path,
                "file_name": workspace_file.file_name,
                "workspace_file": workspace_file,
            }
            for path, workspace_file in accessible_files.items()
        }
        _runtime_instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
            company,
            logical_path="",
            recursive=True,
        )
        for runtime_path, item in runtime_items.items():
            if item.get("kind") != "file" or not policy.allows_path(runtime_path):
                continue
            candidate_map.setdefault(
                runtime_path,
                {
                    "logical_path": runtime_path,
                    "file_name": str(item.get("name") or runtime_path.rsplit("/", 1)[-1]),
                    "workspace_file": None,
                },
            )

        matches = [
            item
            for item in candidate_map.values()
            if str(item.get("file_name") or "").strip().lower() == normalized_file_name
        ]
        if not matches:
            return False, "未找到匹配的 workspace 文件"
        if len(matches) > 1:
            return False, "匹配到多个文件，请使用 logical path 指定具体文件"

        target = matches[0]
        workspace_file = target.get("workspace_file")
        if isinstance(workspace_file, WorkspaceFile):
            ok, payload = adapter.read_workspace_file(company, instance, workspace_file)
        else:
            ok, payload = adapter.read_workspace_logical_file(
                company,
                instance,
                str(target.get("logical_path") or ""),
            )
        if not ok:
            return False, payload
        if not isinstance(payload, dict):
            return False, "workspace file read returned invalid payload"
        return True, AgentWorkspaceAccessService._sanitize_read_result_payload(
            payload,
            logical_path=str(target.get("logical_path") or ""),
            file_info=workspace_file
            if isinstance(workspace_file, WorkspaceFile)
            else AgentWorkspaceAccessService._runtime_file_stub(str(target.get("logical_path") or "")),
        )


    @staticmethod
    def write_file(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        args = arguments if isinstance(arguments, dict) else {}

        policy, accessible_files = AgentWorkspaceAccessService._list_accessible_workspace_files(company, agent)
        if not policy.allows_tool(WORKSPACE_TOOL_WRITE):
            return False, "当前 Agent 没有文件写入权限"
        if not policy.knowledge_reads:
            return False, "当前 Agent 没有可访问的 knowledge scope"
        # 提取并校验 logical_path（防绝对路径、路径穿越、保留目录等）
        raw_logical_path = str(args.get("logical_path") or args.get("path") or "").strip()
        try:
            validated_path = validate_workspace_logical_path(raw_logical_path, "logical_path")
        except ValueError as exc:
            return False, str(exc)

        normalized_write_path = normalize_workspace_reference(validated_path)
        if not normalized_write_path or normalized_write_path == "*":
            return False, "logical_path is not allowed"
        normalized_write_path = _prefix_agent_workspace_artifact_path(normalized_write_path)
        if not policy.allows_path(normalized_write_path):
            return False, "当前 Agent 无权访问该 logical path"
        # 额外路径黑名单（防写入高风险隐藏目录）
        if any(seg.lower() in WORKSPACE_WRITE_BLOCKED_SEGMENTS for seg in normalized_write_path.split("/")):
            return False, "logical_path is not allowed"
        # 父目录级权限校验（防跨目录写入）
        parent_path = normalized_write_path.rsplit("/", 1)[0] if "/" in normalized_write_path else ""
        if parent_path and not policy.allows_directory(parent_path):
            return False, "当前 Agent 无权写入该目录"
        # 类型与字符安全校验
        content = args.get("content")
        if not isinstance(content, str):
            return False, "content must be a UTF-8 string"
        if "\x00" in content:
            return False, "content contains invalid characters"
        # 编码格式校验，只允许utf-8
        encoding = str(args.get("encoding") or "utf-8").strip().lower()
        if encoding not in WORKSPACE_WRITE_ALLOWED_ENCODINGS:
            return False, "encoding is not supported"
        # UTF-8 可编码性与大小上限校验
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            return False, "content must be valid UTF-8"

        max_write_bytes_raw = getattr(settings, "RUNTIME_WORKSPACE_WRITE_MAX_BYTES", 1024 * 1024)
        try:
            max_write_bytes = int(max_write_bytes_raw)
        except (TypeError, ValueError):
            max_write_bytes = 1024 * 1024
        max_write_bytes = max(1024, min(max_write_bytes, 10 * 1024 * 1024))
        if len(content_bytes) > max_write_bytes:
            return False, f"content is too large (max {max_write_bytes} bytes)"
        # 目标文件已存在校验（防重复写入）
        overwrite = args.get("overwrite", False)
        if not isinstance(overwrite, bool):
            return False, "overwrite must be a boolean"
        # 创建目录校验（防目录穿越）
        create_dirs = args.get("create_dirs", False)
        if not isinstance(create_dirs, bool):
            return False, "create_dirs must be a boolean"
        # 目标文件已存在校验（防重复写入）
        existing = accessible_files.get(normalized_write_path)
        if existing is not None and not overwrite:
            return False, "目标文件已存在，请设置 overwrite=true 后重试"
        # 目标文件不存在校验（防文件不存在）
        _instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
            company,
            logical_path=normalized_write_path,
            recursive=False,
        )
        runtime_entry = runtime_items.get(normalized_write_path)
        if isinstance(runtime_entry, dict):
            if runtime_entry.get("kind") == "directory":
                return False, "目标路径是目录，不能写入文件"
            if runtime_entry.get("kind") == "file" and not overwrite:
                return False, "目标文件已存在，请设置 overwrite=true 后重试"

        instance = AgentWorkspaceAccessService._get_runtime_instance(company)
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)

        write_args = {
            "logical_path": normalized_write_path,
            "content": content,
            "overwrite": overwrite,
            "create_dirs": create_dirs,
            "encoding": "utf-8",
        }

        ok, payload = adapter.execute_workspace_tool(
            company,
            instance,
            agent_id=agent.agentId,
            tool_name=TOUMING_WORKSPACE_TOOL_WRITE,
            arguments=write_args,
        )
        if not ok:
            return False, payload
        if not isinstance(payload, dict):
            return False, "workspace file write returned invalid payload"

        safe_payload = dict(payload)
        safe_payload.setdefault("tool_name", WORKSPACE_TOOL_WRITE)
        safe_payload.setdefault("logical_path", normalized_write_path)
        safe_payload.setdefault("encoding", "utf-8")
        safe_payload.setdefault("overwrite", overwrite)
        safe_payload.setdefault("create_dirs", create_dirs)
        safe_payload.pop("content", None) # 防止回显写入正文
        return True, safe_payload

    @staticmethod
    def rename_path(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
        *,
        logical_path: str = "",
        file_name: str = "",
        new_logical_path: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        policy, accessible_files = AgentWorkspaceAccessService._list_accessible_workspace_files(company, agent)
        if not policy.allows_tool(WORKSPACE_TOOL_RENAME):
            return False, "当前 Agent 没有重命名权限"
        if not policy.knowledge_reads:
            return False, "当前 Agent 没有可访问的 knowledge scope"

        normalized_source_path = normalize_workspace_reference(logical_path)
        if normalized_source_path == "*":
            normalized_source_path = ""
        normalized_file_name = str(file_name or "").strip().lower()
        raw_new_path = str(new_logical_path or "").strip()
        if not raw_new_path:
            return False, "重命名时必须提供 new_logical_path"
        try:
            validated_new_path = validate_workspace_logical_path(raw_new_path, "new_logical_path")
        except ValueError as exc:
            return False, str(exc)
        normalized_target_path = normalize_workspace_reference(validated_new_path)
        if not normalized_target_path or normalized_target_path == "*":
            return False, "new_logical_path is not allowed"
        if any(seg.lower() in WORKSPACE_WRITE_BLOCKED_SEGMENTS for seg in normalized_target_path.split("/")):
            return False, "new_logical_path is not allowed"

        if not normalized_source_path and not normalized_file_name:
            return False, "重命名时必须明确指定 logical path 或 file_name"

        source_path = ""
        source_kind = ""
        if normalized_source_path:
            if not (policy.allows_path(normalized_source_path) or policy.allows_directory(normalized_source_path)):
                return False, "当前 Agent 无权访问该 logical path"
            source_path = normalized_source_path
            if normalized_source_path in accessible_files:
                source_kind = "file"
            else:
                parent_path = normalized_source_path.rsplit("/", 1)[0] if "/" in normalized_source_path else ""
                _runtime_instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
                    company,
                    logical_path=parent_path,
                    recursive=False,
                )
                runtime_exact = runtime_items.get(normalized_source_path)
                if isinstance(runtime_exact, dict):
                    source_kind = str(runtime_exact.get("kind") or "").strip().lower()
        else:
            candidate_map: dict[str, dict[str, str]] = {}
            for path, workspace_file in accessible_files.items():
                candidate_name = str(workspace_file.file_name or "").strip().lower() or path.rsplit("/", 1)[-1].lower()
                if candidate_name == normalized_file_name and policy.allows_path(path):
                    candidate_map[path] = {"logical_path": path, "kind": "file"}
            _runtime_instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
                company,
                logical_path="",
                recursive=True,
            )
            for runtime_path, item in runtime_items.items():
                kind = str(item.get("kind") or "").strip().lower()
                if kind not in {"file", "directory"}:
                    continue
                item_name = str(item.get("name") or runtime_path.rsplit("/", 1)[-1]).strip().lower()
                if item_name != normalized_file_name:
                    continue
                if kind == "directory":
                    if not policy.allows_directory(runtime_path):
                            continue
                elif not policy.allows_path(runtime_path):
                    continue
                candidate_map.setdefault(runtime_path, {"logical_path": runtime_path, "kind": kind})

            candidates = sorted(candidate_map.values(), key=lambda item: str(item.get("logical_path") or ""))
            if not candidates:
                return False, "未找到匹配的 workspace 文件或目录"
            if len(candidates) > 1:
                return False, "匹配到多个目标，请使用 logical path 指定具体文件或目录"
            source_path = str(candidates[0].get("logical_path") or "")
            source_kind = str(candidates[0].get("kind") or "")

        if not source_path:     # 如果未找到要重命名的 workspace 文件或目录，则返回错误
            return False, "未找到要重命名的 workspace 文件或目录"
        if source_path == normalized_target_path:
            return False, "源路径与目标路径相同，无需重命名"

        target_parent = normalized_target_path.rsplit("/", 1)[0] if "/" in normalized_target_path else ""
        if target_parent and not policy.allows_directory(target_parent):
            return False, "当前 Agent 无权写入目标目录"
        if not (policy.allows_path(normalized_target_path) or policy.allows_directory(normalized_target_path)):
            return False, "当前 Agent 无权访问目标 logical path"

        instance = AgentWorkspaceAccessService._get_runtime_instance(company)
        if instance is None:  # 如果 runtime instance 不存在，则返回错误
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)

        ok, payload = adapter.execute_workspace_tool(
            company,
            instance,
            agent_id=agent.agentId,
            tool_name=WORKSPACE_TOOL_RENAME,
            arguments={
                "logical_path": source_path,
                "new_logical_path": normalized_target_path,
            },
        )
        if not ok:
            return False, payload
        if not isinstance(payload, dict):
            return False, "workspace rename returned invalid payload"

        now = _now_ts()
        source_prefix = f"{source_path}/"
        target_prefix = f"{normalized_target_path}/"
        workspace_records: dict[int, WorkspaceFile] = {}
        for workspace_file in WorkspaceFile.objects.filter(company=company, logical_path=source_path):
            workspace_records[workspace_file.id] = workspace_file
        for workspace_file in WorkspaceFile.objects.filter(company=company, logical_path__startswith=source_prefix):
            workspace_records[workspace_file.id] = workspace_file

        for workspace_file in workspace_records.values():
            current_path = normalize_workspace_reference(workspace_file.logical_path or "")
            if current_path == source_path:
                updated_path = normalized_target_path
            elif current_path.startswith(source_prefix):
                suffix = current_path[len(source_prefix):]
                updated_path = f"{target_prefix}{suffix}".rstrip("/")
            else:
                continue
            workspace_file.logical_path = updated_path
            workspace_file.file_name = updated_path.split("/")[-1] or workspace_file.file_name
            workspace_file.updated_at = now
            workspace_file.sync_status = WorkspaceFile.SYNC_IN_SYNC
            workspace_file.save(update_fields=["logical_path", "file_name", "updated_at", "sync_status"])

        return True, {
            **payload,
            "tool_name": WORKSPACE_TOOL_RENAME,
            "logical_path": source_path,
            "new_logical_path": normalized_target_path,
            "renamed_kind": source_kind or str(payload.get("kind") or "").strip().lower(),
        }


    @staticmethod
    def delete_path(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
        *,
        logical_path: str = "",
        file_name: str = "",
    ) -> tuple[bool, dict[str, Any] | str]:
        policy, accessible_files = AgentWorkspaceAccessService._list_accessible_workspace_files(company, agent)
        if not policy.allows_tool(WORKSPACE_TOOL_DELETE):
            return False, "当前 Agent 没有删除权限"
        if not policy.knowledge_reads:
            return False, "当前 Agent 没有可访问的 knowledge scope"

        normalized_path = normalize_workspace_reference(logical_path)
        if normalized_path == "*":
            normalized_path = ""
        normalized_file_name = str(file_name or "").strip().lower()

        if not normalized_path and not normalized_file_name:
            return False, "删除时必须明确指定 logical path 或 file_name"

        instance = AgentWorkspaceAccessService._get_runtime_instance(company)
        if instance is None:
            return False, "runtime instance missing"
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)

        target_path = ""
        target_kind = ""

        if normalized_path:
            if not (policy.allows_path(normalized_path) or policy.allows_directory(normalized_path)):
                return False, "当前 Agent 无权访问该 logical path"
            target_path = normalized_path

            if normalized_path in accessible_files:
                target_kind = "file"
            else:
                parent_path = normalized_path.rsplit("/", 1)[0] if "/" in normalized_path else ""
                _runtime_instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
                    company,
                    logical_path=parent_path,
                    recursive=False,
                )
                runtime_exact = runtime_items.get(normalized_path)
                if isinstance(runtime_exact, dict):
                    target_kind = str(runtime_exact.get("kind") or "").strip().lower()
        else:
            candidate_map: dict[str, dict[str, str]] = {}

            for path, workspace_file in accessible_files.items():
                candidate_name = str(workspace_file.file_name or "").strip().lower() or path.rsplit("/", 1)[-1].lower()
                if candidate_name == normalized_file_name and policy.allows_path(path):
                    candidate_map[path] = {"logical_path": path, "kind": "file"}

            _runtime_instance, runtime_items = AgentWorkspaceAccessService._list_runtime_tree_items(
                company,
                logical_path="",
                recursive=True,
            )
            for runtime_path, item in runtime_items.items():
                kind = str(item.get("kind") or "").strip().lower()
                if kind not in {"file", "directory"}:
                    continue
                item_name = str(item.get("name") or runtime_path.rsplit("/", 1)[-1]).strip().lower()
                if item_name != normalized_file_name:
                    continue
                if kind == "directory":
                    if not policy.allows_directory(runtime_path):
                        continue
                elif not policy.allows_path(runtime_path):
                    continue
                candidate_map.setdefault(runtime_path, {"logical_path": runtime_path, "kind": kind})

            candidates = sorted(candidate_map.values(), key=lambda item: str(item.get("logical_path") or ""))
            if not candidates:
                return False, "未找到匹配的 workspace 文件或目录"
            if len(candidates) > 1:
                return False, "匹配到多个目标，请使用 logical path 指定具体文件或目录"

            target_path = str(candidates[0].get("logical_path") or "")
            target_kind = str(candidates[0].get("kind") or "")

        ok, payload = adapter.execute_workspace_tool(
            company,
            instance,
            agent_id=agent.agentId,
            tool_name=WORKSPACE_TOOL_DELETE,
            arguments={"logical_path": target_path},
        )
        if not ok:
            return False, payload
        if not isinstance(payload, dict):
            return False, "workspace delete returned invalid payload"

        WorkspaceFile.objects.filter(company=company, logical_path=target_path).delete()
        WorkspaceFile.objects.filter(company=company, logical_path__startswith=f"{target_path}/").delete()

        return True, {
            **payload,
            "tool_name": WORKSPACE_TOOL_DELETE,
            "logical_path": target_path,
            "deleted_kind": target_kind or str(payload.get("kind") or "").strip().lower(),
        }

class AgentWorkspaceToolGatewayService:
    @staticmethod
    def _resolve_agent(
        company: Company,
        agent_id: str,
    ) -> tuple[bool, TransparentCompanyAgentDefinition | str]:
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            return False, "agent_id is required"
        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, "transparent company not found"
        try:
            return True, TransparentCompanyAgentDefinition.objects.get(
                transparentCompany=transparent_company,
                agentId=normalized_agent_id,
            )
        except TransparentCompanyAgentDefinition.DoesNotExist:
            return False, "agent not found"

    @staticmethod
    def normalize_tool_name(tool_name: str) -> str:
        return TOUMING_WORKSPACE_TOOL_MAP.get(str(tool_name or "").strip(), "")

    @staticmethod
    def execute(
        company: Company,
        *,
        agent_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        normalized_tool = AgentWorkspaceToolGatewayService.normalize_tool_name(tool_name)
        if not normalized_tool:
            return False, "unsupported workspace tool"

        ok, agent_or_msg = AgentWorkspaceToolGatewayService._resolve_agent(company, agent_id)
        if not ok:
            return False, str(agent_or_msg)
        agent = agent_or_msg

        args = arguments if isinstance(arguments, dict) else {}
        logical_path = str(args.get("logical_path") or args.get("path") or "").strip()
        file_name = str(args.get("file_name") or args.get("name") or "").strip()
        new_logical_path = str(args.get("new_logical_path") or args.get("new_path") or args.get("target_path") or "").strip()

        if normalized_tool == WORKSPACE_TOOL_LIST:
            ok, payload = AgentWorkspaceAccessService.list_directory(
                company,
                agent,
                logical_path=logical_path,
                include_runtime_tree=True,
            )
        elif normalized_tool == WORKSPACE_TOOL_LOCATE:
            ok, payload = AgentWorkspaceAccessService.locate_files(
                company,
                agent,
                file_name=file_name,
                logical_path=logical_path,
            )
        elif normalized_tool == WORKSPACE_TOOL_WRITE:
            ok, payload = AgentWorkspaceAccessService.write_file(
                company,
                agent,
                arguments=args,
            )


        elif normalized_tool == WORKSPACE_TOOL_UNZIP:
            instance = AgentWorkspaceAccessService._get_runtime_instance(company)
            if instance is None:
                return False, "runtime instance missing"
            adapter = OpenClawRuntimeService.get_provider_adapter(instance)
            ok, payload = adapter.execute_workspace_tool(
                company,
                instance,
                agent_id=agent.agentId,
                tool_name=tool_name,
                arguments=args,
            )
        elif normalized_tool == WORKSPACE_TOOL_RENAME:
            ok, payload = AgentWorkspaceAccessService.rename_path(
                company,
                agent,
                file_name=file_name,
                logical_path=logical_path,
                new_logical_path=new_logical_path,
            )
        elif normalized_tool == WORKSPACE_TOOL_DELETE:
            ok, payload = AgentWorkspaceAccessService.delete_path(
                company,
                agent,
                file_name=file_name,
                logical_path=logical_path,
            )
        else:
            ok, payload = AgentWorkspaceAccessService.read_file(
                company,
                agent,
                file_name=file_name,
                logical_path=logical_path,
            )

        if not ok or not isinstance(payload, dict):
            return ok, payload
        return True, {
            **payload,
            "tool_name": normalized_tool,
            "openclaw_tool_name": str(tool_name or "").strip(),
            "source": "explicit_workspace_tool",
        }

