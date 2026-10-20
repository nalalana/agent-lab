"""
/backend/apps/company/services.py
公司服务类
"""
import time
import logging
import uuid
import hashlib
import json
import difflib
from typing import Any, Iterable, Optional

import base64
import re
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from common.utils.Tengcentcos_utils import put_object, normalize_cos_key
from apps.moderation.models import TextModerationAsset
from apps.moderation.services import ImageModerationService, TextModerationService
from apps.company.openclaw_model_catalog import (
    DEFAULT_OPENCLAW_VERSION,
    get_catalog_model_metadata,
    get_catalog_provider_metadata,
    get_openclaw_model_catalog,
    normalize_catalog_model_id,
    normalize_model_source_mode,
)
from apps.company.models import (
    Company,
    CompanyApiCredential,
    CompanyMember,
    CompanyScopedAdmin,
    CompanyVerification,
    CompanyPermissionConfig,
    CompanySessionMemoryPolicy,
    CompanyUserMemoryItem,
    CompanyUserMemoryWriteLog,
    CompanyUserMemoryPolicy,
    CompanyVikingBinding,
    CompanySystemSetting,
    CompanyModelPolicy,
    ModelDefinition,
    ModelPackage,
    ModelProvider,
    OpenVikingCluster,
    OpenClawVersionCatalog,
    OpenClawVersionModel,
    OpenClawVersionPackage,
    OpenClawVersionProvider,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentPromptVersion,
    TransparentCompanyAgentTestMessage,
    TransparentCompanyAgentTestSession,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentVikingSpace,
    TransparentCompanyAgentUserMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentSkillBinding,
    TransparentCompanyPermissionTemplate,
    TransparentCompanySkill,
    companyGate,
)
from apps.company.default_skills_v1 import ensure_default_skills_v1
from apps.company.validators import validate_social_credit_code
from apps.company.verification_cleanup import (
    DraftVerificationPurgeError,
    purge_draft_verification_history,
)
from apps.company.verification_storage import (
    VerificationStorageError,
    get_verification_storage,
)
from apps.company.permissions import (
    can_access_company_draft,
    can_access_formal_company,
    has_company_iam_permission,
    has_iam_permission,
    is_company_verified_for_formal_access,
)
from apps.company.admin_access import agent_access, agent_allowed, agent_visible, scope_for
from apps.opengate.models import Gates
from apps.user.models import User
from apps.iam.services import (
    get_or_create_organization_tenant,
    get_or_create_personal_tenant,
    get_or_create_principal,
    get_or_create_resource,
    grant_permissions,
    is_granted,
    is_user_action_allowed,
    revoke_permissions,
    sync_organization_tenant_member,
    visible_resource_ids,
)
from apps.iam.models import Principal, Resource, Grant
from apps.runtime.security import (
    sanitize_agent_test_runtime_payload,
    sanitize_assistant_content_for_user,
    sanitize_runtime_debug_payload,
    sanitize_tool_calls,
    sanitize_user_visible_error,
)


logger = logging.getLogger('touming')


def _is_registration_no_unique_violation(exc: IntegrityError) -> bool:
    cause = getattr(exc, '__cause__', None)
    constraint_name = getattr(getattr(cause, 'diag', None), 'constraint_name', '')
    if constraint_name == 'company_registration_no_nonblank_uniq':
        return True
    return 'UNIQUE constraint failed: company_company.registration_no' in str(exc)


def _split_assistant_reasoning_content(value: Any) -> dict[str, Any]:
    """Split OpenAI-style <think> blocks without exposing markup to SSE clients.

    The raw content remains unchanged for persistence and backwards compatibility.
    During streaming, a trailing partial tag is withheld until enough bytes arrive
    to decide whether it is markup or ordinary answer text.
    """

    source = str(value or '')
    lower_source = source.lower()
    thinking_parts: list[str] = []
    answer_parts: list[str] = []
    cursor = 0
    thinking_active = False

    def trailing_prefix_index(start: int, tag: str) -> int:
        lower_tag = tag.lower()
        max_length = min(len(lower_tag) - 1, len(source) - start)
        for length in range(max_length, 0, -1):
            index = len(source) - length
            if index >= start and lower_tag.startswith(lower_source[index:]):
                return index
        return len(source)

    while cursor < len(source):
        if thinking_active:
            close_index = lower_source.find('</think>', cursor)
            if close_index < 0:
                partial_index = trailing_prefix_index(cursor, '</think>')
                thinking_parts.append(source[cursor:partial_index])
                cursor = len(source)
                break
            thinking_parts.append(source[cursor:close_index])
            cursor = close_index + len('</think>')
            thinking_active = False
            continue

        open_index = lower_source.find('<think>', cursor)
        if open_index < 0:
            partial_index = trailing_prefix_index(cursor, '<think>')
            answer_parts.append(source[cursor:partial_index])
            cursor = len(source)
            break
        answer_parts.append(source[cursor:open_index])
        cursor = open_index + len('<think>')
        thinking_active = True

    return {
        'thinking': '\n\n'.join(thinking_parts).strip(),
        'answer': ''.join(answer_parts).strip(),
        'thinking_active': thinking_active,
    }


def _build_agent_viking_identity(cid: str, agent_id: str) -> dict[str, str]:
    """Build stable OpenViking account/user IDs without leaking business IDs."""

    account_digest = hashlib.sha256(str(cid).encode('utf-8')).hexdigest()[:24]
    user_digest = hashlib.sha256(f"{cid}:{agent_id}".encode('utf-8')).hexdigest()[:24]
    account_id = f"tm-{account_digest}"
    user_id = f"agent-{user_digest}"
    return {
        'vikingSpaceId': f"{account_id}:{user_id}",
        'accountId': account_id,
        'userId': user_id,
        'rootUri': f"viking://user/{user_id}/memories/",
    }


AGENT_TEST_CHAT_STYLE_PROMPT = """聊天回答规范：
- 保留标准 Markdown 能力，可使用小标题、列表、短段落、引用、代码块和链接。
- 先判断问题复杂度：简单问题优先直接回答，复杂问题再分段说明。
- 标题层级保持简洁，尽量只使用短小标题，不要写成长篇文档。
- 回答适合聊天窗口阅读：段落要短，列表要清晰，避免大段连续文字。
- 当需要给步骤、选项或结论时，优先使用项目符号或有序列表。
- 代码或命令请放入 Markdown 代码块；行内名词、路径、参数可用行内代码。
- 不要输出过重的报告体、论文体或模板化前言；先给结论，再补充必要说明。
- 如果用户只问一句简短问题，优先给简短直接的回答，不要主动扩写成多节长文。"""

SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
SKILL_TEXT_LIMITS = {
    "name": 80,
    "usage": 240,
    "description": 500,
    "instructions": 1800,
    "shell_script": 8000,
}


def _sanitize_skill_markdown_text(value: Any, limit: int) -> str:
    text_value = str(value or "")[: max(int(limit or 0), 0)]
    text_value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text_value)
    text_value = re.sub(r"```+", "`\u200b``", text_value)
    text_value = re.sub(r"<!--[\s\S]*?-->", "", text_value)
    text_value = re.sub(r"</?(script|style|iframe|object|embed|meta|link)\b[^>]*>", "", text_value, flags=re.I)
    text_value = re.sub(
        r"^(\s{0,3})(#{1,6})\s+",
        lambda match: f"{match.group(1)}{match.group(2)}\u200b ",
        text_value,
        flags=re.M,
    )
    return text_value.strip()


def _extract_skill_section(content: str, title: str) -> str:
    match = re.search(rf"^##\s+{re.escape(title)}\s*\n([\s\S]*?)(?=\n##\s+|\Z)", str(content or ""), re.M)
    return match.group(1).strip() if match else ""


def _safe_shell_script_name(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in raw).strip(".-_")
    if not safe:
        return "run.sh"
    if not safe.lower().endswith(".sh"):
        safe = f"{safe}.sh"
    return safe[:80]


def _build_openclaw_skill_markdown(
    *,
    title: str,
    usage: str,
    description: str,
    instructions: str,
    shell_script: str = "",
    shell_script_name: str = "",
    previous_content: str = "",
) -> str:
    previous_history = [
        line.strip()[2:].strip()
        for line in _extract_skill_section(previous_content, "上传历史").splitlines()
        if line.strip().startswith("- ")
    ]
    script_part = f"，脚本：{_safe_shell_script_name(shell_script_name)}" if shell_script else ""
    upload_record = f"{time.strftime('%Y-%m-%d %H:%M:%S')} 上传「{title or '未命名 Skill'}」{script_part}"
    history = [upload_record, *previous_history][:12]
    return "\n".join([
        f"# {title or '自定义 Skill'}",
        "",
        "## 使用方法",
        usage,
        "",
        "## 描述",
        description,
        "",
        "## 指令",
        instructions,
        "",
        "## SH 脚本",
        shell_script or "未上传脚本。",
        "",
        "## 上传历史",
        *[f"- {item}" for item in history],
    ])


def _ip_suffix(client_ip: Optional[str]) -> str:
    return f" ip={client_ip}" if client_ip else ""


def _moderate_company_texts(
    *,
    items: list[tuple[str, Any]],
    source: str,
    owner_id: str = "",
    actor_id: str = "",
) -> tuple[bool, str]:
    payload = [
        {
            "text": value,
            "source": source,
            "source_field": field,
            "owner_type": "company" if owner_id else "",
            "owner_id": owner_id,
            "actor_type": "user" if actor_id else "",
            "actor_id": actor_id,
        }
        for field, value in items
        if value is not None
    ]
    ok, message, _asset = TextModerationService.check_texts(payload)
    return ok, message


class CompanyService:
    COMPANY_PHILOSOPHY_PROMPT_START = "[公司理念]"
    COMPANY_PHILOSOPHY_PROMPT_END = "[/公司理念]"

    @staticmethod
    def _build_default_main_agent_prompt(transparent_company: TransparentCompany) -> str:
        company = transparent_company.company
        company_name = (company.company_name or '').strip()
        return (
            f"你是{company_name or '当前公司'}的 CEO（Main Agent），负责统筹智能公司内部 Agent 协作，"
            "并在必要时将任务分派给员工 Agent。"
        )

    @staticmethod
    def _ensure_agent_viking_space(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
    ) -> TransparentCompanyAgentVikingSpace:
        """Create or repair the deterministic OpenViking space owned by an Agent."""

        now = str(time.time())[:20]
        identity = _build_agent_viking_identity(
            transparent_company.company.cid,
            agent.agentId,
        )
        binding = CompanyService._ensure_company_viking_binding(transparent_company.company)
        viking_space, created = TransparentCompanyAgentVikingSpace.objects.get_or_create(
            transparentCompany=transparent_company,
            agent=agent,
            defaults={
                **identity,
                'companyBinding': binding,
                'credentialRef': '',
                'status': TransparentCompanyAgentVikingSpace.STATUS_PENDING,
                'lastError': '',
                'created_at': now,
                'updated_at': now,
            },
        )
        if agent.status == TransparentCompanyAgentDefinition.STATUS_DEPRECATED:
            desired_status = TransparentCompanyAgentVikingSpace.STATUS_ARCHIVED
        elif created or viking_space.status in {
            TransparentCompanyAgentVikingSpace.STATUS_ARCHIVED,
            TransparentCompanyAgentVikingSpace.STATUS_FAILED,
        }:
            desired_status = TransparentCompanyAgentVikingSpace.STATUS_PENDING
        else:
            desired_status = viking_space.status
        update_fields = []
        for field, value in {
            **identity,
            'companyBinding': binding,
            'status': desired_status,
        }.items():
            if getattr(viking_space, field) == value:
                continue
            setattr(viking_space, field, value)
            update_fields.append(field)
        if update_fields:
            viking_space.updated_at = now
            update_fields.append('updated_at')
            viking_space.save(update_fields=update_fields)
        return viking_space

    @staticmethod
    def _ensure_company_viking_binding(company: Company) -> CompanyVikingBinding:
        """Create the durable pending binding without contacting OpenViking."""

        from django.conf import settings

        endpoint = str(settings.OPENVIKING_BASE_URL or '').rstrip('/')
        cluster, _ = OpenVikingCluster.objects.get_or_create(
            name=settings.OPENVIKING_CLUSTER_NAME,
            defaults={
                'endpoint': endpoint,
                'status': OpenVikingCluster.STATUS_ACTIVE,
                'capacity': settings.OPENVIKING_CLUSTER_CAPACITY,
                'rootCredentialRef': settings.OPENVIKING_ROOT_CREDENTIAL_REF,
                'isDefault': True,
            },
        )
        changed = []
        for field, value in {
            'endpoint': endpoint,
            'capacity': settings.OPENVIKING_CLUSTER_CAPACITY,
            'rootCredentialRef': settings.OPENVIKING_ROOT_CREDENTIAL_REF,
        }.items():
            if getattr(cluster, field) != value:
                setattr(cluster, field, value)
                changed.append(field)
        if changed:
            cluster.save(update_fields=[*changed, 'updated_at'])
        account_id = f'tm-{hashlib.sha256(str(company.cid).encode()).hexdigest()[:24]}'
        binding, _ = CompanyVikingBinding.objects.get_or_create(
            company=company,
            defaults={
                'cluster': cluster,
                'accountId': account_id,
                'adminUserId': 'touming-admin',
                'runtimeUserId': 'touming-runtime',
                'status': CompanyVikingBinding.STATUS_PENDING,
            },
        )
        return binding

    @staticmethod
    def _enqueue_viking_provisioning(binding: CompanyVikingBinding, agent_id=None) -> None:
        from apps.agent_runtime.memory.provisioning import enqueue_company_provisioning

        transaction.on_commit(
            lambda: enqueue_company_provisioning(binding.pk, agent_id=agent_id)
        )

    @staticmethod
    def _append_company_philosophy_to_prompt(system_prompt: str, philosophy: str) -> str:
        base_prompt = str(system_prompt or '').strip()
        cleaned_prompt = re.sub(
            rf"\n*\s*{re.escape(CompanyService.COMPANY_PHILOSOPHY_PROMPT_START)}[\s\S]*?"
            rf"{re.escape(CompanyService.COMPANY_PHILOSOPHY_PROMPT_END)}\s*",
            "\n",
            base_prompt,
        ).strip()
        normalized_philosophy = str(philosophy or '').strip()
        if not normalized_philosophy:
            return cleaned_prompt
        philosophy_block = (
            f"{CompanyService.COMPANY_PHILOSOPHY_PROMPT_START}\n"
            f"{normalized_philosophy}\n"
            f"{CompanyService.COMPANY_PHILOSOPHY_PROMPT_END}"
        )
        return f"{cleaned_prompt}\n\n{philosophy_block}".strip() if cleaned_prompt else philosophy_block

    @staticmethod
    def _sync_main_agent_company_philosophy(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
    ) -> TransparentCompanyAgentDefinition:
        next_prompt = CompanyService._append_company_philosophy_to_prompt(
            agent.systemPrompt or CompanyService._build_default_main_agent_prompt(transparent_company),
            transparent_company.prompts or '',
        )
        if next_prompt != (agent.systemPrompt or ''):
            agent.systemPrompt = next_prompt
            agent.updated_at = str(time.time())[:20]
            agent.save(update_fields=['systemPrompt', 'updated_at'])
        return agent

    @staticmethod
    def _estimate_agent_test_token_count(content: Any) -> int:
        text = str(content or '').strip()
        if not text:
            return 0
        cjk_chars = sum(1 for char in text if '\u4e00' <= char <= '\u9fff')
        other_chars = max(len(text) - cjk_chars, 0)
        return max(1, cjk_chars + (other_chars + 3) // 4)

    @staticmethod
    def _coerce_agent_test_token_count(value: Any, fallback_content: Any = '') -> int:
        # Conversation usage must come from OpenClaw/runtime usage reports only.
        # Local text estimation is reserved for internal context budgeting, not UI billing.
        if isinstance(value, dict):
            usage = value.get('usage')
            candidates = [
                value.get('token_count'),
                value.get('total_tokens'),
                value.get('totalTokens'),
            ]
            if isinstance(usage, dict):
                candidates.extend([
                    usage.get('total_tokens'),
                    usage.get('totalTokens'),
                    usage.get('token_count'),
                    usage.get('tokenCount'),
                ])
            for candidate in candidates:
                token_count = CompanyService._coerce_agent_test_token_count(candidate)
                if token_count > 0:
                    return token_count
            return 0
        try:
            return max(int(float(value or 0)), 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _coerce_agent_test_stream_usage_token_count(runtime_event: Any) -> int:
        if not isinstance(runtime_event, dict):
            return 0
        usage = runtime_event.get('usage')
        candidates = [
            runtime_event.get('total_tokens'),
            runtime_event.get('totalTokens'),
        ]
        if isinstance(usage, dict):
            candidates.extend([
                usage.get('total_tokens'),
                usage.get('totalTokens'),
                usage.get('token_count'),
                usage.get('tokenCount'),
            ])
        for candidate in candidates:
            token_count = CompanyService._coerce_agent_test_token_count(candidate)
            if token_count > 0:
                return token_count
        return 0

    @staticmethod
    def _iter_agent_test_text_chunks(content: Any, chunk_size: int = 24):
        text = str(content or '')
        if not text:
            return
        size = max(int(chunk_size or 24), 1)
        for index in range(0, len(text), size):
            yield text[index:index + size]

    @staticmethod
    def ensure_default_transparent_agent(
        transparent_company: TransparentCompany,
    ) -> TransparentCompanyAgentDefinition:
        existing = (
            TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                agentId='main-agent',
            )
            .order_by('sortOrder', 'id')
            .first()
        )
        if existing:
            tenant = get_or_create_organization_tenant(transparent_company.company)
            get_or_create_resource(
                Resource.TYPE_AGENT,
                f'agent:{transparent_company.company.cid}:{existing.agentId}',
                tenant=tenant,
                creator=transparent_company.company.account,
            )
            existing = CompanyService._sync_main_agent_company_philosophy(
                transparent_company,
                existing,
            )
            CompanyService._ensure_agent_viking_space(transparent_company, existing)
            return existing

        now = str(time.time())[:20]
        company = transparent_company.company
        company_name = (company.company_name or '').strip()
        display_name = f"{company_name} CEO" if company_name else "CEO"
        system_prompt = CompanyService._append_company_philosophy_to_prompt(
            CompanyService._build_default_main_agent_prompt(transparent_company),
            transparent_company.prompts or '',
        )

        agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=transparent_company,
            agentId='main-agent',
            name=display_name,
            role=TransparentCompanyAgentDefinition.ROLE_MAIN_AGENT,
            description='ceo',
            systemPrompt=system_prompt,
            visibility=TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL,
            clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
            exposedToClient=False,
            isDefaultEntry=False,
            routeKey='',
            exposureOrder=0,
            enabled=True,
            isCoreAgent=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            sortOrder=0,
            created_at=now,
            updated_at=now,
        )
        tenant = get_or_create_organization_tenant(company)
        get_or_create_resource(
            Resource.TYPE_AGENT,
            f'agent:{company.cid}:{agent.agentId}',
            tenant=tenant,
            creator=company.account,
        )
        logger.info(
            f"[transparent-agent-bootstrap] created default main agent "
            f"cid={company.cid} agent_id={agent.agentId}"
        )
        CompanyService._ensure_agent_viking_space(transparent_company, agent)
        return agent

    @staticmethod
    def _serialize_catalog_provider(provider, *, version: str):
        data = provider.get_info()
        data['openclawVersion'] = version
        data['catalogSource'] = 'openclaw_version_catalog'
        return data

    @staticmethod
    def _serialize_catalog_model(model, *, version: str):
        data = model.get_info()
        data['openclawVersion'] = version
        data['catalogSource'] = 'openclaw_version_catalog'
        return data

    @staticmethod
    def _serialize_catalog_package(package, *, version: str):
        data = package.get_info()
        data['openclawVersion'] = version
        data['catalogSource'] = 'openclaw_version_catalog'
        return data

    @staticmethod
    def _build_catalog_provider_stub(provider_id: str, *, version: str):
        metadata = get_catalog_provider_metadata(provider_id, version)
        return {
            'id': 0,
            'providerId': provider_id,
            'name': metadata.get('name') or provider_id,
            'slug': metadata.get('slug') or provider_id,
            'baseUrl': metadata.get('baseUrl'),
            'authMode': metadata.get('authMode') or ModelProvider.AUTH_API_KEY,
            'status': ModelProvider.STATUS_ACTIVE,
            'isPlatformManaged': bool(metadata.get('isPlatformManaged', True)),
            'metadata': metadata.get('metadata'),
            'created_at': '',
            'updated_at': '',
            'openclawVersion': version,
            'catalogSource': 'openclaw_version_catalog',
        }

    @staticmethod
    def _load_runtime_model_catalog(cid: str) -> Optional[dict]:
        # Model catalogs are database-owned in the LangGraph runtime.
        return None
        cid = str(cid or '').strip()
        if not cid:
            return None
        try:
            from apps.runtime.models import OpenClawInstance
            from apps.runtime.services import OpenClawRuntimeService
        except Exception:
            return None

        instance = (
            OpenClawInstance.objects.select_related('company')
            .filter(company__cid=cid)
            .order_by('-id')
            .first()
        )
        if not instance:
            return None

        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, payload = adapter.read_model_catalog(instance.company, instance)
        if not ok or not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _normalize_runtime_catalog_provider(item: dict, *, version: str, source: str) -> dict:
        provider_id = str(item.get('providerId') or item.get('provider_id') or '').strip()
        metadata = get_catalog_provider_metadata(provider_id, version)
        return {
            'id': int(item.get('id') or 0),
            'providerId': provider_id,
            'name': str(item.get('name') or metadata.get('name') or provider_id),
            'slug': str(item.get('slug') or metadata.get('slug') or provider_id),
            'status': str(item.get('status') or 'active'),
            'authMode': str(item.get('authMode') or item.get('auth_mode') or metadata.get('authMode') or 'api_key'),
            'isPlatformManaged': bool(
                item.get('isPlatformManaged')
                if item.get('isPlatformManaged') is not None
                else metadata.get('isPlatformManaged', True)
            ),
            'created_at': str(item.get('created_at') or ''),
            'updated_at': str(item.get('updated_at') or ''),
            'openclawVersion': version,
            'catalogSource': source,
        }

    @staticmethod
    def _normalize_runtime_catalog_model(item: dict, *, version: str, source: str) -> dict:
        model_id = str(item.get('modelId') or item.get('model_id') or '').strip()
        metadata = get_catalog_model_metadata(model_id, version)
        return {
            'id': int(item.get('id') or 0),
            'providerId': str(item.get('providerId') or item.get('provider_id') or metadata.get('providerId') or ''),
            'modelId': model_id,
            'displayName': str(item.get('displayName') or item.get('display_name') or metadata.get('displayName') or model_id),
            'category': str(item.get('category') or metadata.get('category') or 'chat'),
            'status': str(item.get('status') or 'active'),
            'supportsTools': bool(
                item.get('supportsTools')
                if item.get('supportsTools') is not None
                else metadata.get('supportsTools', False)
            ),
            'contextWindow': int(item.get('contextWindow') or item.get('context_window') or metadata.get('contextWindow') or 0),
            'isPlatformDefault': bool(
                item.get('isPlatformDefault')
                if item.get('isPlatformDefault') is not None
                else metadata.get('isPlatformDefault', False)
            ),
            'created_at': str(item.get('created_at') or ''),
            'updated_at': str(item.get('updated_at') or ''),
            'openclawVersion': version,
            'catalogSource': source,
        }

    @staticmethod
    def _normalize_runtime_catalog_package(item: dict, *, version: str, source: str) -> dict:
        return {
            'id': int(item.get('id') or 0),
            'packageId': str(item.get('packageId') or item.get('package_id') or ''),
            'name': str(item.get('name') or item.get('packageId') or item.get('package_id') or ''),
            'description': str(item.get('description') or ''),
            'status': str(item.get('status') or 'active'),
            'isDefault': bool(item.get('isDefault') if item.get('isDefault') is not None else item.get('is_default', False)),
            'priority': int(item.get('priority') or 0),
            'created_at': str(item.get('created_at') or ''),
            'updated_at': str(item.get('updated_at') or ''),
            'openclawVersion': version,
            'catalogSource': source,
        }

    @staticmethod
    def _get_or_create_catalog_provider(provider_id: str, *, version: str):
        metadata = get_catalog_provider_metadata(provider_id, version)
        provider = ModelProvider.objects.filter(providerId=provider_id).first()
        if provider:
            changed_fields = []
            base_url = metadata.get('baseUrl')
            if base_url and provider.baseUrl != base_url:
                provider.baseUrl = str(base_url)
                changed_fields.append('baseUrl')
            auth_mode = str(metadata.get('authMode') or provider.authMode or ModelProvider.AUTH_API_KEY)
            if auth_mode and provider.authMode != auth_mode:
                provider.authMode = auth_mode
                changed_fields.append('authMode')
            if changed_fields:
                provider.updated_at = str(time.time())[:20]
                changed_fields.append('updated_at')
                provider.save(update_fields=changed_fields)
            return provider

        now = str(time.time())[:20]
        provider = ModelProvider(
            providerId=provider_id,
            name=str(metadata.get('name') or provider_id),
            slug=str(metadata.get('slug') or provider_id),
            baseUrl=metadata.get('baseUrl'),
            authMode=str(metadata.get('authMode') or ModelProvider.AUTH_API_KEY),
            status=ModelProvider.STATUS_ACTIVE,
            isPlatformManaged=bool(metadata.get('isPlatformManaged', True)),
            created_at=now,
            updated_at=now,
        )
        provider.save()
        return provider

    @staticmethod
    def _build_catalog_model_stub(model_id: str, *, version: str):
        metadata = get_catalog_model_metadata(model_id, version)
        return {
            'id': 0,
            'providerId': str(metadata.get('providerId') or ''),
            'modelId': model_id,
            'displayName': metadata.get('displayName') or model_id,
            'category': metadata.get('category') or 'chat',
            'status': ModelDefinition.STATUS_ACTIVE,
            'supportsTools': bool(metadata.get('supportsTools', True)),
            'contextWindow': int(metadata.get('contextWindow') or 0),
            'inputPricePer1k': float(metadata.get('inputPricePer1k') or 0),
            'outputPricePer1k': float(metadata.get('outputPricePer1k') or 0),
            'isPlatformDefault': bool(metadata.get('isPlatformDefault', False)),
            'metadata': metadata.get('metadata'),
            'created_at': '',
            'updated_at': '',
            'openclawVersion': version,
            'catalogSource': 'openclaw_version_catalog',
        }

    @staticmethod
    def _get_or_create_catalog_model(provider: ModelProvider, model_id: str, *, version: str):
        model = ModelDefinition.objects.filter(provider=provider, modelId=model_id).first()
        if model:
            return model

        metadata = get_catalog_model_metadata(model_id, version)
        now = str(time.time())[:20]
        model = ModelDefinition(
            provider=provider,
            modelId=model_id,
            displayName=str(metadata.get('displayName') or model_id),
            category=str(metadata.get('category') or 'chat'),
            status=ModelDefinition.STATUS_ACTIVE,
            supportsTools=bool(metadata.get('supportsTools', True)),
            contextWindow=int(metadata.get('contextWindow') or 0),
            isPlatformDefault=bool(metadata.get('isPlatformDefault', False)),
            created_at=now,
            updated_at=now,
        )
        model.save()
        return model

    @staticmethod
    def _select_default_model_for_provider(provider: ModelProvider, *, version: str) -> Optional[ModelDefinition]:
        catalog = get_openclaw_model_catalog(version)
        preferred_model_id = ''
        fallback_model_id = ''
        for model_id in catalog.model_ids:
            metadata = get_catalog_model_metadata(model_id, version)
            if str(metadata.get('providerId') or '') != provider.providerId:
                continue
            if not fallback_model_id:
                fallback_model_id = model_id
            if bool(metadata.get('isPlatformDefault', False)):
                preferred_model_id = model_id
                break
        model_id = preferred_model_id or fallback_model_id
        if not model_id:
            return None
        return CompanyService._get_or_create_catalog_model(
            provider,
            model_id,
            version=version,
        )

    @staticmethod
    def _sync_company_model_policy_from_active_credential(
        *,
        company: Company,
        provider: ModelProvider,
        credential_name: str,
        version: str,
    ) -> Optional[CompanyModelPolicy]:
        default_model = CompanyService._select_default_model_for_provider(
            provider,
            version=version,
        )
        if default_model is None:
            logger.warning(
                '[model-policy-sync] no default model for provider cid=%s provider=%s',
                company.cid,
                provider.providerId,
            )
            return None
        now = str(time.time())[:20]
        policy, _created = CompanyModelPolicy.objects.get_or_create(
            company=company,
            defaults={
                'created_at': now,
                'updated_at': now,
            },
        )
        policy.provider = provider
        policy.defaultModel = default_model
        policy.selectedCredentialName = credential_name
        policy.sourceMode = CompanyModelPolicy.SOURCE_BYOK
        policy.status = CompanyModelPolicy.STATUS_ACTIVE
        policy.fallbackToPlatform = False
        if not policy.created_at:
            policy.created_at = now
        policy.updated_at = now
        policy.save()
        return policy

    @staticmethod
    def _mask_secret(secret: str) -> str:
        if not secret:
            return ''
        if len(secret) <= 8:
            return '*' * len(secret)
        return f"{secret[:4]}{'*' * max(len(secret) - 8, 4)}{secret[-4:]}"

    @staticmethod
    def _encode_secret(secret: str) -> str:
        if not secret:
            return ''
        return base64.b64encode(secret.encode('utf-8')).decode('utf-8')

    @staticmethod
    def _get_company_for_admin(cid: str, user: Optional[User], client_ip: Optional[str] = None):
        try:
            company = Company.objects.get(cid=cid)
        except Company.DoesNotExist:
            return False, '公司不存在'

        if not has_company_iam_permission(company, user, Grant.PERM_ADMIN):
            logger.error(
                f"[company-admin] 权限不足 cid={cid} "
                f"operator={getattr(user, 'uid', '')}{_ip_suffix(client_ip)}"
            )
            return False, '权限不足'
        if not is_company_verified_for_formal_access(company):
            return False, '企业尚未认证'
        return True, company

    @staticmethod
    def _get_company_for_member_manager(cid: str, user: Optional[User], client_ip: Optional[str] = None):
        ok, company_or_message = CompanyService._get_company_for_admin(cid, user, client_ip)
        if not ok:
            return False, company_or_message
        if not has_company_iam_permission(company_or_message, user, Grant.ROLE_MANAGER):
            return False, '只有可管理权限的用户可以管理员工'
        return True, company_or_message

    @staticmethod
    def _get_transparent_company_for_admin(
        cid: str,
        user: Optional[User],
        client_ip: Optional[str] = None,
    ):
        try:
            company = Company.objects.get(cid=cid)
        except Company.DoesNotExist:
            return False, '公司不存在'

        if not has_company_iam_permission(company, user, Grant.PERM_ADMIN):
            logger.error(
                f"[transparent-company-admin] 权限不足 cid={cid} "
                f"operator={getattr(user, 'uid', '')}{_ip_suffix(client_ip)}"
            )
            return False, '权限不足'
        if not is_company_verified_for_formal_access(company):
            return False, '企业尚未认证'

        try:
            transparent_company = TransparentCompany.objects.select_related(
                'company__account'
            ).get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, '透明公司不存在'

        return True, transparent_company

    @staticmethod
    def _get_transparent_company_for_agent_access(
        cid: str,
        user: Optional[User],
        client_ip: Optional[str] = None,
    ):
        """Keep the existing IAM-admin path and add restricted staff fallback."""
        ok, transparent_or_msg = CompanyService._get_transparent_company_for_admin(
            cid, user, client_ip,
        )
        if ok:
            return True, transparent_or_msg, None
        return agent_access(cid, user)

    @staticmethod
    def _get_transparent_company_for_agent_configuration(
        cid: str,
        agent_id: str,
        user: Optional[User],
        client_ip: Optional[str] = None,
    ):
        ok, transparent_or_msg = CompanyService._get_transparent_company_for_admin(
            cid, user, client_ip,
        )
        if ok:
            return True, transparent_or_msg
        access_ok, transparent_or_msg, _scope = agent_access(cid, user)
        resource = Resource.objects.filter(
            type=Resource.TYPE_AGENT,
            external_id=f'agent:{cid}:{agent_id}',
        ).first()
        if (
            not access_ok
            or resource is None
            or not is_user_action_allowed(user, resource, 'edit')
        ):
            return False, '权限不足'
        return True, transparent_or_msg

    @staticmethod
    def _get_company_allowed_skills(company: Company) -> list[str]:
        from apps.company.models import TransparentCompanySkill

        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return []

        return list(
            TransparentCompanySkill.objects.filter(
                transparentCompany=transparent_company, enabled=True
            ).values_list("skillId", flat=True)
        )

    @staticmethod
    def _publish_transparent_company_runtime(
        company: Company,
        operator_id: str = "",
        notes: str = "",
    ) -> tuple[bool, object]:
        return True, {"skipped": True, "reason": "langgraph_native", "engine": "langgraph"}
        # Phase 4 keeps company-facing writes successful without recreating the
        # retired OpenClaw control-plane records after the cutover gate.
        from apps.runtime.legacy_control import (
            legacy_runtime_is_read_only,
            legacy_runtime_skip_payload,
        )

        if legacy_runtime_is_read_only():
            return True, legacy_runtime_skip_payload()

        try:
            from apps.runtime.models import OpenClawProvisionTask
            from apps.runtime.services import OpenClawConfigService, OpenClawRuntimeService

            ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
            if not ok:
                ok, instance_or_msg = OpenClawRuntimeService.get_or_create_instance(company=company)
                if not ok:
                    return False, instance_or_msg
            instance = instance_or_msg

            ok, version_or_msg = OpenClawConfigService.create_config_version(
                company=company,
                operator_id=operator_id or "system",
                notes=notes or "runtime sync",
                publish=True,
            )
            if not ok:
                return False, version_or_msg

            result: dict[str, Any] = {"version": version_or_msg}
            bootstrap_ok, bootstrap_result = OpenClawRuntimeService.start_bootstrap(company, instance)
            result["bootstrap"] = bootstrap_result
            if bootstrap_ok:
                bootstrap_task = OpenClawProvisionTask.objects.filter(
                    id=(bootstrap_result.get("task") or {}).get("id")
                ).first()
                if bootstrap_task:
                    run_ok, run_result = OpenClawRuntimeService.run_bootstrap(bootstrap_task)
                    result["bootstrap_run"] = run_result if run_ok else {"error": run_result}
                else:
                    result["bootstrap_run"] = {"error": "bootstrap task missing"}

            readback_ok, readback_payload = OpenClawRuntimeService.readback_config(instance)
            diff_ok, diff_payload = OpenClawConfigService.build_company_config_diff(company)
            result["readback"] = readback_payload
            result["diff"] = diff_payload if diff_ok else {"error": diff_payload}
            result["verified"] = bool(
                bootstrap_ok
                and readback_ok
                and diff_ok
                and isinstance(diff_payload, dict)
                and bool(diff_payload.get("is_in_sync"))
            )
            return True, result
        except Exception as exc:
            logger.error(f"[runtime] transparent runtime publish error cid={company.cid} err={exc}")
            return False, str(exc)

    @staticmethod
    def _sync_transparent_company_bootstrap(company: Company, repo_full_name: Optional[str] = None) -> tuple[bool, object]:
        return CompanyService._publish_transparent_company_runtime(
            company=company,
            operator_id="system",
            notes="legacy bootstrap sync",
        )

    @staticmethod
    def _sync_company_to_llm_service(company: Company, allowed_skills: Optional[list[dict]] = None) -> None:
        try:
            ok, resp = CompanyService._publish_transparent_company_runtime(
                company=company,
                operator_id="system",
                notes="legacy company sync redirect",
            )
            if not ok:
                logger.warning(
                    f"[runtime] company sync failed cid={company.cid} err={resp}"
                )
        except Exception as exc:
            logger.error(f"[runtime] company sync error cid={company.cid} err={exc}")
    @staticmethod
    def _public_company_info(company: Company) -> dict[str, Any]:
        data = company.get_info()
        data.pop('account', None)
        return data

    @staticmethod
    def _public_transparent_company_info(
        transparent_company: TransparentCompany,
    ) -> dict[str, Any]:
        data = transparent_company.get_info()
        company_data = data.get('company')
        if isinstance(company_data, dict):
            company_data.pop('account', None)
        return data

    @staticmethod
    def list_companies(keyword: str = "", limit: int = 20, offset: int = 0):
        qs = Company.objects.filter(
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            verification_suspended_at__isnull=True,
            service_suspended_at__isnull=True,
        )
        if keyword:
            qs = qs.filter(company_name__icontains=keyword)
        total = qs.count()
        companies = list(qs.order_by('id')[offset: offset + limit])
        return {
            'total': total,
            'items': [
                CompanyService._public_company_info(company)
                for company in companies
            ],
        }

    @staticmethod
    def list_companies_by_user(user: User, limit: int, offset: int):
        """List verified companies the user belongs to plus their own drafts.

        ``Company.account`` is deliberately not used for verified companies.
        Once verified, a company appears here only through an ADMIN or
        SUPER_ADMIN IAM grant.
        """

        principal = Principal.objects.filter(
            type=Principal.TYPE_USER,
            external_id=user.uid,
        ).first()
        resource_ids = []
        if principal:
            resource_ids = Grant.objects.filter(
                principal=principal,
                resource__type=Resource.TYPE_COMPANY,
                permissions__in=[Grant.PERM_SUPER_ADMIN, Grant.PERM_ADMIN],
            ).values_list('resource__external_id', flat=True)
        scoped_ids = CompanyScopedAdmin.objects.filter(
            user=user, active=True,
            company__members__user=user,
            company__members__status=CompanyMember.STATUS_ACTIVE,
        ).values_list('company__cid', flat=True)
        member_ids = CompanyMember.objects.filter(
            user=user, status=CompanyMember.STATUS_ACTIVE,
        ).values_list('company__cid', flat=True)
        companies = Company.objects.filter(
            Q(
                Q(cid__in=resource_ids) | Q(cid__in=scoped_ids) | Q(cid__in=member_ids),
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
            | (
                Q(account=user)
                & ~Q(verification_status=Company.VERIFICATION_STATUS_VERIFIED)
            )
        ).distinct()
        total = companies.count()
        companies = list(companies.order_by('id')[offset: offset + limit])
        items = [
            (
                CompanyService._public_company_info(company)
                if is_company_verified_for_formal_access(company)
                else company.get_info()
            )
            for company in companies
        ]
        return True, {'total': total, 'items': items, 'hasMore': (offset + limit) < total}

    @staticmethod
    def create_company(
        company_name: str,
        description: str = "",
        logo: str = "",
        background_image: str = "",
        owner=None,
        client_ip: Optional[str] = None,
        registration_no: str = "",
        establishment_date: str = "",
        company_size: str = "",
        contact_phone: str = "",
        contact_email: str = "",
        website: str = "",
        address: str = "",
    ):
        """
        创建未认证企业草稿和对应 IAM Resource。

        创建人只记录在 ``Company.account``，这里不创建 Principal、Grant
        或 Owner CompanyMember。正式权限只能由后续认领授权流程产生。
        返回: (True, company) 或 (False, '错误信息')
        """
        import uuid

        # 上限检查：每个用户最多创建100个公司
        count = Company.objects.filter(account=owner).count()
        if count >= 100:
            logger.error(f"[create_company] 您创建的公司数量已达上限 operator={owner.username} {_ip_suffix(client_ip)}")
            return False, '您拥有的公司数量已达上限'

        try:
            registration_no = validate_social_credit_code(registration_no, allow_blank=True)
        except ValidationError as exc:
            return False, exc.messages[0]

        try:
            # 生成公司ID
            cid = str(uuid.uuid4()).replace('-', '')
            moderation_ok, moderation_msg = _moderate_company_texts(
                items=[
                    ("company_name", company_name),
                    ("description", description),
                    ("website", website),
                    ("address", address),
                ],
                source=TextModerationAsset.SOURCE_COMPANY_PROFILE,
                owner_id=cid,
                actor_id=getattr(owner, "uid", ""),
            )
            if not moderation_ok:
                return False, moderation_msg
            logo_ok, logo_key, logo_msg = ImageModerationService.require_approved_public_image(logo)
            if not logo_ok:
                return False, logo_msg
            bg_ok, background_key, bg_msg = ImageModerationService.require_approved_public_image(background_image)
            if not bg_ok:
                return False, bg_msg

            try:
                with transaction.atomic():
                    company = Company.objects.create(
                        cid=cid,
                        company_name=company_name,
                        description=description or "",
                        logo=logo_key,
                        background_image=background_key,
                        account=owner,
                        registration_no=registration_no,
                        establishment_date=establishment_date or "",
                        company_size=company_size or "",
                        contact_phone=contact_phone or "",
                        contact_email=contact_email or "",
                        website=website or "",
                        address=address or "",
                        created_at=str(time.time())[:20],
                        updated_at=str(time.time())[:20],
                        status=Company.COMPANY_STATUS_COMMON,
                        verification_status=Company.VERIFICATION_STATUS_UNVERIFIED,
                    )
                    get_or_create_resource(Resource.TYPE_COMPANY, cid)
            except IntegrityError as exc:
                if _is_registration_no_unique_violation(exc):
                    existing = Company.objects.filter(
                        registration_no=registration_no,
                    ).first()
                    if existing and can_access_company_draft(existing, owner):
                        logger.info(
                            f"[create_company] 继续已有草稿 cid={existing.cid} "
                            f"operator={owner.username} {_ip_suffix(client_ip)}"
                        )
                        return True, existing
                    logger.info(
                        f"[create_company] 企业主体已存在 "
                        f"operator={owner.username} {_ip_suffix(client_ip)}"
                    )
                    return False, '企业已存在'
                logger.exception(
                    f"[create_company] 数据库写入失败 operator={owner.username} {_ip_suffix(client_ip)}"
                )
                return False, '创建失败'
            logger.info(f"[create_company] 创建成功 cid={cid} operator={owner.username} {_ip_suffix(client_ip)}")
            return True, company
        except Exception as e:
            logger.error(f"[create_company] 创建失败 operator={owner.username} {_ip_suffix(client_ip)} err={e}")
            return False, '创建失败'

    @staticmethod
    def get_company_detail(cid: str, user: Optional[User] = None):
        try:
            company = Company.objects.get(cid=cid)
        except Company.DoesNotExist:
            return False, '公司不存在'
        if company.verification_status == Company.VERIFICATION_STATUS_VERIFIED:
            if not is_company_verified_for_formal_access(company):
                return False, '公司不存在'
            return True, CompanyService._public_company_info(company)
        if not can_access_company_draft(company, user):
            # Do not disclose whether another user's private draft exists.
            return False, '公司不存在'
        return True, company.get_info()

    @staticmethod
    def list_company_programs(user: User, cid: str, limit: int, offset: int):
        try:
            company = Company.objects.get(
                cid=cid,
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
        except Company.DoesNotExist:
            return False, '公司不存在'
        readable_gids = visible_resource_ids(user, Resource.TYPE_PROJECT)
        current_company_gids = Resource.objects.filter(
            type=Resource.TYPE_PROJECT,
            tenant__type='organization',
            tenant__external_id=f'organization:{cid}',
        ).values('external_id')
        bindings = companyGate.objects.filter(
            company=company,
            opengate__gid__in=readable_gids,
        ).filter(opengate__gid__in=current_company_gids).select_related('opengate').order_by('id')
        total = bindings.count()
        items = [binding.get_info() for binding in bindings[offset: offset + limit]]
        has_more = (offset + limit) < total
        return True, {'total': total, 'items': items, 'hasMore': has_more}


    @staticmethod
    def delete_company_program(
        user: User,
        cid: str,
        gid: str,
        client_ip: Optional[str] = None,
    ):
        try:
            company = Company.objects.get(cid=cid)
            gate = Gates.objects.get(gid=gid)
        except (Company.DoesNotExist, Gates.DoesNotExist):
            return False, '参数错误'
        if (
            not can_access_formal_company(company, user, Grant.PERM_SUPER_ADMIN)
            or not has_iam_permission(
                user, Resource.TYPE_PROJECT, gid, Grant.PERM_SUPER_ADMIN
            )
        ):
            logger.error(
                f"[company-gate-delete] 权限不足 cid={cid} gid={gid} "
                f"operator={user.uid}{_ip_suffix(client_ip)}"
            )
            return False, '权限不足'
        with transaction.atomic():
            gate = Gates.objects.select_for_update().get(pk=gate.pk)
            project_resource = Resource.objects.filter(type=Resource.TYPE_PROJECT, external_id=gid).select_related('tenant').first()
            if not companyGate.objects.filter(company=company, opengate=gate).exists():
                return False, '项目未绑定到该公司'
            if project_resource and (not project_resource.tenant or project_resource.tenant.external_id != f'organization:{cid}'):
                return False, '项目已归属其他公司'
            companyGate.objects.filter(company=company, opengate=gate).delete()
            gate.label = Gates.LABEL_PERSONAL
            gate.save(update_fields=['label'])
            personal_tenant = get_or_create_personal_tenant(gate.owner)
            project_resource = get_or_create_resource(
                Resource.TYPE_PROJECT,
                gate.gid,
                tenant=personal_tenant,
                creator=gate.owner,
            )
            Resource.objects.filter(parent=project_resource, type=Resource.TYPE_TABLE).update(
                tenant=personal_tenant,
            )
        logger.info(
            f"[company-gate-delete] 删除成功 cid={cid} gid={gid} "
            f"operator={user.uid}{_ip_suffix(client_ip)}"
        )
        return True, None


    @staticmethod
    def search_companies(keyword: str, offset: int, limit: int):
        from django.db.models import Q
        qs = Company.objects.filter(
            verification_status=Company.VERIFICATION_STATUS_VERIFIED,
            verification_suspended_at__isnull=True,
            service_suspended_at__isnull=True,
        )
        if keyword:
            qs = qs.filter(Q(company_name__icontains=keyword) | Q(description__icontains=keyword) | Q(registration_no__icontains=keyword))
        total = qs.count()
        items = list(qs.order_by('id')[offset: offset + limit])

        # 计算是否有更多数据
        has_more = (offset + limit) < total

        data = []
        for c in items:
            data.append(CompanyService._public_company_info(c))
        return {'total': total, 'items': data, 'hasMore': has_more}

    @staticmethod
    def transfer_owner(operator, company_id: str, target_email: str, client_ip: Optional[str] = None):
        """Deprecated: Company.account is not an ownership or authorization source."""
        logger.warning(
            f"[company-transfer-owner-deprecated] 已停用 company_id={company_id} "
            f"operator={getattr(operator, 'uid', '')}{_ip_suffix(client_ip)}"
        )
        return False, '企业所有权转移接口已停用'

    @staticmethod
    def update_company(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
        company_name: Optional[str] = None,
        description: Optional[str] = None,
        logo: Optional[str] = None,
        background_image: Optional[str] = None,
        status: Optional[str] = None,
        registration_no: Optional[str] = None,
        establishment_date: Optional[str] = None,
        company_size: Optional[str] = None,
        contact_phone: Optional[str] = None,
        contact_email: Optional[str] = None,
        website: Optional[str] = None,
        address: Optional[str] = None,
    ):
        def has_active_verification(target: Company) -> bool:
            return CompanyVerification.objects.filter(
                company=target,
                status__in=[
                    CompanyVerification.STATUS_INITIATING,
                    CompanyVerification.STATUS_PENDING,
                ],
            ).exists()

        def identity_change_requested(target: Company) -> bool:
            return any([
                company_name is not None and company_name != target.company_name,
                (
                    registration_no is not None
                    and registration_no != (target.registration_no or '')
                ),
                (
                    establishment_date is not None
                    and establishment_date != (target.establishment_date or '')
                ),
                address is not None and address != (target.address or ''),
            ])

        def check_access(target: Company) -> tuple[bool, bool]:
            draft_access = can_access_company_draft(target, user)
            if draft_access:
                return True, True
            return (
                can_access_formal_company(target, user, Grant.PERM_ADMIN),
                False,
            )

        try:
            company = Company.objects.get(cid=cid)
            allowed, has_draft_access = check_access(company)
            if not allowed:
                logger.error(
                    f"[company-update] 权限不足 cid={cid} "
                    f"operator={getattr(user, 'username', '')}{_ip_suffix(client_ip)}"
                )
                return False, '权限不足'
            if has_draft_access and status is not None:
                return False, '未认证企业草稿不能修改企业状态'

            if registration_no is not None:
                try:
                    registration_no = validate_social_credit_code(
                        registration_no,
                        allow_blank=True,
                    )
                except ValidationError as exc:
                    return False, exc.messages[0]

            if identity_change_requested(company):
                if is_company_verified_for_formal_access(company):
                    return False, '已认证企业主体信息不可修改'
                if has_active_verification(company):
                    return False, '认证申请进行中，企业主体信息不可修改'

            moderation_ok, moderation_msg = _moderate_company_texts(
                items=[
                    ("company_name", company_name),
                    ("description", description),
                    ("website", website),
                    ("address", address),
                ],
                source=TextModerationAsset.SOURCE_COMPANY_PROFILE,
                owner_id=cid,
                actor_id=getattr(user, "uid", ""),
            )
            if not moderation_ok:
                return False, moderation_msg

            logo_key = None
            if logo is not None:
                logo_ok, logo_key, logo_msg = (
                    ImageModerationService.require_approved_public_image(logo)
                )
                if not logo_ok:
                    return False, logo_msg

            background_key = None
            if background_image is not None:
                bg_ok, background_key, bg_msg = (
                    ImageModerationService.require_approved_public_image(
                        background_image
                    )
                )
                if not bg_ok:
                    return False, bg_msg

            try:
                with transaction.atomic():
                    company = Company.objects.select_for_update().get(cid=cid)
                    allowed, has_draft_access = check_access(company)
                    if not allowed:
                        return False, '权限不足'
                    if has_draft_access and status is not None:
                        return False, '未认证企业草稿不能修改企业状态'
                    if identity_change_requested(company):
                        if is_company_verified_for_formal_access(company):
                            return False, '已认证企业主体信息不可修改'
                        if has_active_verification(company):
                            return False, '认证申请进行中，企业主体信息不可修改'

                    field_values = {
                        'company_name': company_name,
                        'description': description,
                        'logo': logo_key if logo is not None else None,
                        'background_image': (
                            background_key
                            if background_image is not None
                            else None
                        ),
                        'status': status,
                        'registration_no': registration_no,
                        'establishment_date': establishment_date,
                        'company_size': company_size,
                        'contact_phone': contact_phone,
                        'contact_email': contact_email,
                        'website': website,
                        'address': address,
                    }
                    update_fields = []
                    for field_name, value in field_values.items():
                        if value is None:
                            continue
                        setattr(company, field_name, value)
                        update_fields.append(field_name)
                    company.updated_at = str(time.time())[:20]
                    update_fields.append('updated_at')
                    company.save(update_fields=update_fields)
                    is_transparent = TransparentCompany.objects.filter(
                        company=company
                    ).exists()
            except IntegrityError as exc:
                if _is_registration_no_unique_violation(exc):
                    return False, '企业已存在'
                raise

            logger.info(
                f"[company-update] 更新成功 cid={cid} "
                f"operator={user.uid}{_ip_suffix(client_ip)}"
            )
            if is_transparent:
                CompanyService._sync_company_to_llm_service(company)
                return True, None
            return True, '更新成功（非透明公司，未触发运行时同步）'
        except Company.DoesNotExist:
            return False, '公司不存在'
        except Exception as exc:
            logger.error(
                f"[company-update] 更新失败 cid={cid} "
                f"operator={getattr(user, 'uid', '')}{_ip_suffix(client_ip)} "
                f"err={exc}"
            )
            return False, '更新失败'

    @staticmethod
    def delete_company_draft(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        """Delete an unverified draft and all verification data owned by it."""

        storage_keys = ()
        try:
            with transaction.atomic():
                company = Company.objects.select_for_update().get(cid=cid)
                if not can_access_company_draft(company, user):
                    return False, '权限不足'

                resource = Resource.objects.select_for_update().filter(
                    type=Resource.TYPE_COMPANY,
                    external_id=cid,
                ).first()
                has_permission_state = bool(
                    resource
                    and (
                        Grant.objects.filter(resource=resource).exists()
                        or resource.children.exists()
                    )
                )
                applications = CompanyVerification.objects.select_for_update().filter(
                    company=company
                )
                has_verification_history = applications.exists()
                rollback_relation_labels = {
                    'company.CompanyVerification',
                    'company.VerificationOperation',
                    'company.VerificationAuditEvent',
                }
                has_company_state = any(
                    relation.related_model._default_manager.filter(
                        **{relation.field.name: company}
                    ).exists()
                    for relation in company._meta.related_objects
                    if relation.related_model._meta.label not in rollback_relation_labels
                )
                if has_permission_state or has_company_state:
                    return False, '草稿已有关联的正式数据，无法删除'

                if has_verification_history:
                    storage_keys = purge_draft_verification_history(
                        company=company
                    )
                    if storage_keys:
                        storage = get_verification_storage()
                        for storage_key in storage_keys:
                            storage.delete(storage_key)
                if resource:
                    resource.delete()
                company.delete()
        except Company.DoesNotExist:
            return False, '公司不存在'
        except DraftVerificationPurgeError:
            return False, '已认证企业不能按草稿删除'
        except VerificationStorageError as exc:
            logger.warning(
                '[company-draft-delete] 认证材料清理失败 cid=%s err=%s',
                cid,
                exc.code,
            )
            return False, '认证材料清理失败，请稍后重试'
        except Exception as exc:
            logger.exception(
                f"[company-draft-delete] 删除失败 cid={cid} "
                f"operator={getattr(user, 'uid', '')}{_ip_suffix(client_ip)} err={exc}"
            )
            return False, '删除失败'

        logger.info(
            f"[company-draft-delete] 删除成功 cid={cid} "
            f"operator={user.uid}{_ip_suffix(client_ip)}"
        )
        return True, None

    @staticmethod
    def unbind_company(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        """Remove only the caller's relationship with a verified company."""

        try:
            with transaction.atomic():
                company = Company.objects.select_for_update().get(cid=cid)
                if company.verification_status != Company.VERIFICATION_STATUS_VERIFIED:
                    return False, '未完成认证的企业请使用删除企业操作'

                principal = Principal.objects.select_for_update().filter(
                    type=Principal.TYPE_USER,
                    external_id=user.uid,
                ).first()
                resource = Resource.objects.select_for_update().filter(
                    type=Resource.TYPE_COMPANY,
                    external_id=cid,
                ).first()
                grant = None
                if principal and resource:
                    grant = Grant.objects.select_for_update().filter(
                        principal=principal,
                        resource=resource,
                        permissions__in=[Grant.PERM_ADMIN, Grant.PERM_SUPER_ADMIN],
                    ).first()
                if grant is None:
                    return False, '权限不足'

                grant.delete()
                CompanyMember.objects.filter(company=company, user=user).delete()
        except Company.DoesNotExist:
            return False, '公司不存在'
        except Exception as exc:
            logger.exception(
                f"[company-unbind] 解绑失败 cid={cid} "
                f"operator={getattr(user, 'uid', '')}{_ip_suffix(client_ip)} err={exc}"
            )
            return False, '解绑失败'

        logger.info(
            f"[company-unbind] 解绑成功 cid={cid} "
            f"operator={user.uid}{_ip_suffix(client_ip)}"
        )
        return True, None

    @staticmethod
    def bind_program(
        cid: str,
        gid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        try:
            company = Company.objects.get(cid=cid)
            gate = Gates.objects.get(gid=gid)
        except (Company.DoesNotExist, Gates.DoesNotExist):
            return False, '参数错误'
        if (
            not can_access_formal_company(company, user, Grant.PERM_SUPER_ADMIN)
            or not has_iam_permission(
                user, Resource.TYPE_PROJECT, gid, Grant.PERM_SUPER_ADMIN
            )
        ):
            logger.error(
                f"[company-gate-bind] 权限不足 cid={cid} gid={gid} "
                f"operator={user.uid}{_ip_suffix(client_ip)}"
            )
            return False, '权限不足'
        try:
            with transaction.atomic():
                gate = Gates.objects.select_for_update().get(pk=gate.pk)
                if companyGate.objects.filter(opengate=gate).exclude(company=company).exists():
                    return False, '项目已归属其他公司'
                project_resource = Resource.objects.filter(type=Resource.TYPE_PROJECT, external_id=gid).select_related('tenant').first()
                if project_resource and project_resource.tenant and project_resource.tenant.type == 'organization' and project_resource.tenant.external_id != f'organization:{cid}':
                    return False, '项目已归属其他公司'
                companyGate.objects.create(company=company, opengate=gate)
                gate.label = Gates.LABEL_COMPANY
                gate.save(update_fields=['label'])
                organization_tenant = get_or_create_organization_tenant(company)
                project_resource = get_or_create_resource(
                    Resource.TYPE_PROJECT,
                    gate.gid,
                    tenant=organization_tenant,
                    creator=gate.owner,
                )
                Resource.objects.filter(parent=project_resource, type=Resource.TYPE_TABLE).update(
                    tenant=organization_tenant,
                )
        except IntegrityError:
            return False, '已绑定'
        logger.info(
            f"[company-gate-bind] 绑定成功 cid={cid} gid={gid} "
            f"operator={user.uid}{_ip_suffix(client_ip)}"
        )
        return True, None

    @staticmethod
    def create_transparent_company(cid: str, avatar: str, prompts: str, user: User, client_ip: Optional[str] = None):
        try:
            company = Company.objects.get(cid=cid)
        except Company.DoesNotExist:
            return False, '公司不存在'
        if not can_access_formal_company(company, user, Grant.PERM_SUPER_ADMIN):
            logger.error(f"[transparent-company-create] 权限不足 cid={cid} operator={user.uid}{_ip_suffix(client_ip)}")
            return False, '权限不足'
        tenant = get_or_create_organization_tenant(company)
        get_or_create_resource(
            Resource.TYPE_KNOWLEDGE_BASE,
            f'knowledge:{cid}',
            tenant=tenant,
            creator=user,
        )
        candidate_avatar = avatar if avatar else company.logo if company.logo else ''
        avatar_ok, normalized_avatar, avatar_msg = ImageModerationService.require_approved_public_image(candidate_avatar)
        if not avatar_ok:
            return False, avatar_msg
        moderation_ok, moderation_msg = _moderate_company_texts(
            items=[("prompts", prompts)],
            source=TextModerationAsset.SOURCE_TRANSPARENT_COMPANY,
            owner_id=cid,
            actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            return False, moderation_msg
        with transaction.atomic():
            company = Company.objects.select_for_update().get(cid=cid)
            if not can_access_formal_company(company, user, Grant.PERM_SUPER_ADMIN):
                return False, '权限不足'
            if TransparentCompany.objects.filter(company=company).exists():
                return False, '透明公司已存在'
            company.status = Company.COMPANY_STATUS_TRANSPARENT
            company.save(update_fields=['status'])
            transparent_company = TransparentCompany.objects.create(
                company=company,
                avatar=normalized_avatar,
                prompts=prompts,
            )
            ensure_default_skills_v1(transparent_company, TransparentCompanySkill)
        default_agent = CompanyService.ensure_default_transparent_agent(transparent_company)
        binding = CompanyService._ensure_company_viking_binding(company)
        CompanyService._enqueue_viking_provisioning(binding, agent_id=default_agent.pk)
        from apps.agent_runtime.models import CompanyAgentRuntimePolicy

        CompanyAgentRuntimePolicy.objects.update_or_create(
            company=company,
            defaults={
                'mode': CompanyAgentRuntimePolicy.MODE_UNIFIED,
                'canary_percentage': 100,
            },
        )
        logger.info(f"[transparent-company-create] 创建成功 cid={cid} operator={user.uid}{_ip_suffix(client_ip)}")
        return True, None

    @staticmethod
    def upgrade_to_transparent_company(
        cid: str,
        philosophy: str = '',
        avatar: str = '',
        user: User = None,
        client_ip: Optional[str] = None,
        runtime_request: Optional[dict[str, Any]] = None,
    ):
        """将普通公司升级为透明公司并启用 LangGraph 统一运行时。"""

        try:
            company = Company.objects.get(cid=cid)
        except Company.DoesNotExist:
            logger.error(f"[company-upgrade] 公司不存在 cid={cid} operator={user.uid}{_ip_suffix(client_ip)}")
            return False, '公司不存在'

        if not can_access_formal_company(company, user, Grant.PERM_SUPER_ADMIN):
            logger.error(f"[company-upgrade] 权限不足 cid={cid} operator={user.uid}{_ip_suffix(client_ip)}")
            return False, '权限不足'

        candidate_avatar = avatar if avatar else company.logo if company.logo else ''
        avatar_ok, normalized_avatar, avatar_msg = ImageModerationService.require_approved_public_image(candidate_avatar)
        if not avatar_ok:
            return False, avatar_msg
        normalized_philosophy = (philosophy or '').strip()
        moderation_ok, moderation_msg = _moderate_company_texts(
            items=[("philosophy", normalized_philosophy)],
            source=TextModerationAsset.SOURCE_TRANSPARENT_COMPANY,
            owner_id=cid,
            actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            return False, moderation_msg

        with transaction.atomic():
            company = Company.objects.select_for_update().get(cid=cid)
            if not can_access_formal_company(company, user, Grant.PERM_SUPER_ADMIN):
                return False, '权限不足'

            already_paid = TransparentCompany.objects.filter(company=company).exists()
            payment_payload = {
                'provider': 'mock',
                'status': 'already_paid' if already_paid else 'mock_paid',
                'charged': False,
                'reusable': True,
            }

            company.status = Company.COMPANY_STATUS_TRANSPARENT
            company_update_fields = ['status']
            if normalized_avatar:
                company.logo = normalized_avatar
                company_update_fields.append('logo')
            company.save(update_fields=company_update_fields)

            transparent_company, created = TransparentCompany.objects.get_or_create(
                company=company,
                defaults={
                    'prompts': normalized_philosophy,
                    'avatar': normalized_avatar,
                },
            )
            if not created:
                transparent_update_fields = []
                if normalized_philosophy:
                    transparent_company.prompts = normalized_philosophy
                    transparent_update_fields.append('prompts')
                if normalized_avatar:
                    transparent_company.avatar = normalized_avatar
                    transparent_update_fields.append('avatar')
                if transparent_update_fields:
                    transparent_company.save(update_fields=transparent_update_fields)

            ensure_default_skills_v1(transparent_company, TransparentCompanySkill)

        default_agent = CompanyService.ensure_default_transparent_agent(transparent_company)
        binding = CompanyService._ensure_company_viking_binding(company)
        CompanyService._enqueue_viking_provisioning(binding, agent_id=default_agent.pk)
        from apps.agent_runtime.models import CompanyAgentRuntimePolicy

        policy, _ = CompanyAgentRuntimePolicy.objects.update_or_create(
            company=company,
            defaults={
                'mode': CompanyAgentRuntimePolicy.MODE_UNIFIED,
                'canary_percentage': 100,
            },
        )
        logger.info(f"[company-upgrade] upgrade succeeded with unified runtime cid={cid} operator={user.uid}{_ip_suffix(client_ip)}")
        return True, {
            'transparent_company_id': transparent_company.id,
            'company_id': company.cid,
            'payment': payment_payload,
            'runtime': {
                'engine': 'langgraph',
                'mode': policy.mode,
                'ready': binding.status == CompanyVikingBinding.STATUS_ACTIVE,
                'viking_status': binding.status,
            },
        }

    @staticmethod
    def update_transparent_company(
        cid: str,
        avatar: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        try:
            company = Company.objects.get(cid=cid)
        except Company.DoesNotExist:
            return False, '公司不存在'
        if not can_access_formal_company(company, user, Grant.PERM_ADMIN):
            logger.error(
                f"[transparent-company-update] 权限不足 cid={cid} "
                f"operator={user.uid}{_ip_suffix(client_ip)}"
            )
            return False, '权限不足'
        tenant = get_or_create_organization_tenant(company)
        get_or_create_resource(
            Resource.TYPE_KNOWLEDGE_BASE,
            f'knowledge:{cid}',
            tenant=tenant,
            creator=user,
        )
        avatar_ok, normalized_avatar, avatar_msg = (
            ImageModerationService.require_approved_public_image(avatar)
        )
        if not avatar_ok:
            return False, avatar_msg
        with transaction.atomic():
            company = Company.objects.select_for_update().get(cid=cid)
            if not can_access_formal_company(company, user, Grant.PERM_ADMIN):
                return False, '权限不足'
            try:
                transparent_company = TransparentCompany.objects.select_for_update().get(
                    company=company
                )
            except TransparentCompany.DoesNotExist:
                return False, '透明公司不存在'
            transparent_company.avatar = normalized_avatar
            transparent_company.save(update_fields=['avatar'])
        logger.info(
            f"[transparent-company-update] 更新成功 cid={cid} "
            f"operator={user.uid}{_ip_suffix(client_ip)}"
        )
        return True, None

    @staticmethod
    def get_transparent_company_detail(cid: str):
        try:
            company = Company.objects.get(
                cid=cid,
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
        except Company.DoesNotExist:
            return False, '公司不存在'
        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, '透明公司不存在'
        return True, CompanyService._public_transparent_company_info(transparent_company)

    @staticmethod
    def collect_company_data_for_llm(cid: str, user: User):
        """
        收集公司、项目、表格数据用于生成嵌入
        返回: (True, payload_dict) 或 (False, '错误信息')
        """
        from apps.opengate.models import GatesTable
        from common.immudbIO.services import ImmudbService

        try:
            company = Company.objects.get(cid=cid)
        except Company.DoesNotExist:
            return False, '公司不存在'

        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, '该公司不是透明公司'

        # 收集公司信息
        company_info = {
            'cid': company.cid,
            'company_name': company.company_name,
            'description': company.description or '',
            'created_at': company.created_at or '',
            'prompts': transparent_company.prompts or '',  # 理念
        }

        # 收集项目信息
        projects = []
        company_gates = companyGate.objects.filter(company=company)

        for company_gate in company_gates:
            gate = company_gate.opengate
            project_info = {
                'gid': gate.gid,
                'project_name': gate.gatename,
                'project_description': gate.description or '',
                'created_at': gate.created_at or '',
            }

            # 收集项目下的所有表格
            tables = GatesTable.objects.filter(gate=gate)
            table_list = []

            for table in tables:
                # 获取表格基本信息
                principal = get_or_create_principal(Principal.TYPE_USER, user.uid)
                resource = get_or_create_resource(Resource.TYPE_TABLE, table.tid)
                if not is_granted(principal, resource, Grant.PERM_READ):
                    logger.error(f"[collect_company_data] 权限不足 tid={table.tid} operator={user.uid}")
                    continue


                table_info = {
                    'tid': table.tid,
                    'table_name': table.name,
                    'description': table.description or '',
                    'columns': table.get_columns() or [],
                    'column_names': table.get_field_names() or [],
                    'column_titles': table.get_column_titles() or [],
                }

                # 获取表格内容摘要（前几行数据）
                try:
                    immudb_service = ImmudbService()
                    # 获取前5行数据作为摘要
                    # 注意: immudb的查询逻辑是 r > offset*limit and r < offset*limit+limit+1
                    # 所以offset=0, limit=5时, 查询条件是 r > 0 and r < 6, 会获取r=1到r=5的数据
                    # 为了获取前5行, 我们需要offset=0, limit=5, 但实际会获取第1-5行
                    success, data_result = immudb_service.get_data(gate.gid, table.tid, limit=5, offset=0)
                    if success and data_result.get('data'):
                        # 提取前几行数据(最多5行)
                        sample_data = data_result['data']
                        if isinstance(sample_data, list):
                            table_info['sample_rows'] = sample_data[:5]  # 前5行
                        else:
                            table_info['sample_rows'] = []
                    else:
                        table_info['sample_rows'] = []
                except Exception as e:
                    logger.warning(f"[collect_company_data] 获取表格数据失败 tid={table.tid}: {str(e)}")
                    table_info['sample_rows'] = []

                table_list.append(table_info)

            project_info['tables'] = table_list
            projects.append(project_info)

        # 构建payload
        payload = {
            'company': company_info,
            'projects': projects,
        }

        return True, payload

    # ========= 透明公司 & Skill 相关服务 =========

    @staticmethod
    def is_transparent_company(cid: str):
        """判断公司是否为透明公司"""
        try:
            company = Company.objects.get(
                cid=cid,
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
        except Company.DoesNotExist:
            return False, '公司不存在'

        company_info = company.get_info()
        is_transparent = TransparentCompany.objects.filter(company=company).exists()
        return True, {
            'cid': company.cid,
            'company_name': company.company_name,
            'status': company.status,
            'isTransparent': is_transparent,
            'is_transparent': is_transparent,
            'upgrade_paid': bool(company_info.get('upgrade_paid')),
            'runtime_ready': bool(company_info.get('runtime_ready')),
            'runtime_status': company_info.get('runtime_status') or '',
            'runtime_engine': company_info.get('runtime_engine') or '',
            'runtime_mode': company_info.get('runtime_mode') or '',
            'runtime_openclaw_status': company_info.get('runtime_openclaw_status') or '',
            'runtime_config_status': company_info.get('runtime_config_status') or '',
            'smart_company_status': company_info.get('smart_company_status') or 'common',
        }

    @staticmethod
    def list_transparent_company_skills(
        cid: str,
        status: Optional[str] = None,
        source_type: Optional[str] = None,
        runtime_status: Optional[str] = None,
        enabled: Optional[bool] = None,
        user: Optional[User] = None,
        client_ip: Optional[str] = None,
    ):
        """获取透明公司 skills 列表。"""
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg
        transparent_company = transparent_company_or_msg
        company = transparent_company.company

        qs = TransparentCompanySkill.objects.filter(transparentCompany=transparent_company)
        if status:
            qs = qs.filter(status=status)
        if source_type:
            qs = qs.filter(sourceType=source_type)
        if runtime_status:
            qs = qs.filter(runtimeStatus=runtime_status)
        if enabled is not None:
            qs = qs.filter(enabled=enabled)

        qs = qs.annotate(
            configured_agent_count=Count(
                'agentBindings',
                filter=Q(agentBindings__enabled=True),
                distinct=True,
            ),
        )
        skills = []
        for skill in qs.order_by('priority', 'id'):
            item = skill.get_info()
            configured_agent_count = int(
                getattr(skill, 'configured_agent_count', 0) or 0
            )
            item.update({
                'agentBindingCount': configured_agent_count,
                'agent_binding_count': configured_agent_count,
                'isConfigured': configured_agent_count > 0,
                'is_configured': configured_agent_count > 0,
            })
            skills.append(item)
        if not source_type:
            skills.extend(CompanyService._list_openclaw_native_skill_items(company, skills))
        return True, skills

    @staticmethod
    def _list_openclaw_native_skill_items(
        company: Company,
        existing_skills: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        # Runtime skills are persisted in TransparentCompanySkill.
        return []
        try:
            from apps.runtime.services import OpenClawRuntimeService
        except Exception:
            return []

        ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
        if not ok:
            return []
        instance = instance_or_msg
        adapter = OpenClawRuntimeService.get_provider_adapter(instance)
        ok, payload_or_msg = adapter.read_skill_catalog(company, instance)
        if not ok or not isinstance(payload_or_msg, dict):
            return []

        raw_skills = payload_or_msg.get("skills", [])
        if not isinstance(raw_skills, list):
            return []
        existing_ids = {
            str(item.get("skillId") or item.get("skill_id") or "").strip()
            for item in (existing_skills or [])
            if isinstance(item, dict)
        }
        result: list[dict[str, Any]] = []
        now = str(time.time())[:20]
        for index, raw_item in enumerate(raw_skills):
            if not isinstance(raw_item, dict):
                continue
            skill_name = str(raw_item.get("name") or "").strip()
            if not skill_name or skill_name in existing_ids:
                continue
            description = str(raw_item.get("description") or "").strip()
            eligible = bool(raw_item.get("eligible", False))
            disabled = bool(raw_item.get("disabled", False))
            source = str(raw_item.get("source") or "openclaw-bundled").strip()
            content = "\n".join([
                f"# {skill_name}",
                "",
                "## 使用方法",
                description or "OpenClaw 原生 Skill。",
                "",
                "## 描述",
                description or "OpenClaw 原生 Skill。",
                "",
                "## 指令",
                description or "由 OpenClaw 原生 skill catalog 提供。",
                "",
                "## 上传历史",
                "- OpenClaw native skill catalog",
            ])
            result.append({
                "id": f"openclaw-native:{skill_name}",
                "skillId": skill_name,
                "skill_id": skill_name,
                "name": skill_name,
                "version": str(payload_or_msg.get("openclawVersion") or ""),
                "category": "openclaw-native",
                "priority": 10000 + index,
                "enabled": eligible and not disabled,
                "shortDescription": description,
                "short_description": description,
                "author": "OpenClaw",
                "status": TransparentCompanySkill.STATUS_ACTIVE if eligible and not disabled else TransparentCompanySkill.STATUS_INACTIVE,
                "sourceType": "openclaw_native",
                "source_type": "openclaw_native",
                "runtimeStatus": TransparentCompanySkill.RUNTIME_STATUS_ACTIVE if eligible and not disabled else TransparentCompanySkill.RUNTIME_STATUS_PENDING,
                "runtime_status": TransparentCompanySkill.RUNTIME_STATUS_ACTIVE if eligible and not disabled else TransparentCompanySkill.RUNTIME_STATUS_PENDING,
                "isSystemSkill": True,
                "is_system_skill": True,
                "content": content,
                "updated_at": now,
                "lastPublishedAt": "",
                "last_published_at": "",
                "readOnly": True,
                "read_only": True,
                "openclawNative": True,
                "openclaw_native": True,
                "eligible": eligible,
                "disabled": disabled,
                "blockedByAllowlist": bool(raw_item.get("blockedByAllowlist", False)),
                "bundled": bool(raw_item.get("bundled", False)),
                "emoji": str(raw_item.get("emoji") or "").strip(),
                "homepage": str(raw_item.get("homepage") or "").strip(),
                "missing": raw_item.get("missing") if isinstance(raw_item.get("missing"), dict) else {},
                "catalogSource": source,
            })
        return result

    @staticmethod
    def _get_or_materialize_transparent_company_skill(
        transparent_company: TransparentCompany,
        skill_id: str,
    ):
        skill = TransparentCompanySkill.objects.filter(
            transparentCompany=transparent_company,
            skillId=skill_id,
        ).first()
        if skill:
            return True, skill

        native_items = CompanyService._list_openclaw_native_skill_items(
            transparent_company.company,
            existing_skills=[],
        )
        native_item = next(
            (
                item
                for item in native_items
                if str(item.get("skillId") or item.get("skill_id") or "").strip() == skill_id
            ),
            None,
        )
        if native_item is None:
            return False, '技能不存在'

        now = str(time.time())[:20]
        skill, _ = TransparentCompanySkill.objects.update_or_create(
            transparentCompany=transparent_company,
            skillId=skill_id,
            defaults={
                "name": str(native_item.get("name") or skill_id).strip()[:255],
                "version": str(native_item.get("version") or "native").strip()[:20],
                "category": str(native_item.get("category") or "openclaw-native").strip()[:50],
                "priority": int(native_item.get("priority") or 10000),
                "enabled": bool(native_item.get("enabled", True)),
                "shortDescription": str(
                    native_item.get("shortDescription") or native_item.get("short_description") or ""
                ).strip()[:255],
                "author": str(native_item.get("author") or "OpenClaw").strip()[:255],
                "status": str(native_item.get("status") or TransparentCompanySkill.STATUS_ACTIVE).strip()[:20],
                "sourceType": "openclaw_native",
                "runtimeStatus": str(
                    native_item.get("runtimeStatus")
                    or native_item.get("runtime_status")
                    or TransparentCompanySkill.RUNTIME_STATUS_ACTIVE
                ).strip()[:20],
                "isSystemSkill": True,
                "lastPublishedAt": str(
                    native_item.get("lastPublishedAt") or native_item.get("last_published_at") or ""
                ).strip()[:20],
                "content": str(native_item.get("content") or ""),
                "updated_at": now,
            },
        )
        return True, skill

    @staticmethod
    @transaction.atomic
    def upsert_transparent_company_skill(
        cid: str,
        user: User,
        name: str,
        skill_id: Optional[str] = None,
        version: str = '1.0.0',
        category: str = TransparentCompanySkill.CATEGORY_DEFAULT,
        priority: int = 10,
        enabled: Optional[bool] = None,
        short_description: str = '',
        author: str = '',
        status: str = TransparentCompanySkill.STATUS_INACTIVE,
        source_type: str = TransparentCompanySkill.SOURCE_MANUAL,
        runtime_status: str = TransparentCompanySkill.RUNTIME_STATUS_PENDING,
        is_system_skill: bool = False,
        content: str = '',
        usage: str = '',
        instructions: str = '',
        shell_script: str = '',
        shell_script_name: str = '',
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company

        now = str(time.time())[:20]
        normalized_skill_id = (skill_id or '').strip() or f"skill-{uuid.uuid4().hex}"
        if not SKILL_ID_PATTERN.match(normalized_skill_id):
            return False, 'skill_id 只能包含英文字母、数字、点、下划线和中划线，且必须以字母或数字开头'
        existing_skill = TransparentCompanySkill.objects.filter(
            transparentCompany=transparent_company,
            skillId=normalized_skill_id,
        ).first()
        safe_name = _sanitize_skill_markdown_text(name, SKILL_TEXT_LIMITS["name"])
        safe_description = _sanitize_skill_markdown_text(
            short_description or _extract_skill_section(content, "描述"),
            SKILL_TEXT_LIMITS["description"],
        )
        safe_usage = _sanitize_skill_markdown_text(
            usage or _extract_skill_section(content, "使用方法"),
            SKILL_TEXT_LIMITS["usage"],
        )
        safe_instructions = _sanitize_skill_markdown_text(
            instructions or _extract_skill_section(content, "指令") or content,
            SKILL_TEXT_LIMITS["instructions"],
        )
        safe_shell_script = _sanitize_skill_markdown_text(
            shell_script or _extract_skill_section(content, "SH 脚本"),
            SKILL_TEXT_LIMITS["shell_script"],
        )
        if safe_shell_script == "未上传脚本。":
            safe_shell_script = ""
        if not safe_name or not safe_usage or not safe_description or not safe_instructions:
            return False, '标题、使用方法、描述和指令不能为空'
        safe_content = _build_openclaw_skill_markdown(
            title=safe_name,
            usage=safe_usage,
            description=safe_description,
            instructions=safe_instructions,
            shell_script=safe_shell_script,
            shell_script_name=shell_script_name,
            previous_content=(existing_skill.content if existing_skill else ''),
        )
        moderation_ok, moderation_msg = _moderate_company_texts(
            items=[
                ("name", safe_name),
                ("shortDescription", safe_description),
                ("content", safe_content),
            ],
            source=TextModerationAsset.SOURCE_AGENT_PROMPT,
            owner_id=cid,
            actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            return False, moderation_msg
        normalized_category = (
            str(category or '').strip() or TransparentCompanySkill.CATEGORY_DEFAULT
        )
        defaults = {
            'name': safe_name,
            'version': version,
            'category': normalized_category,
            'priority': priority,
            'enabled': bool(enabled) if enabled is not None else False,
            'shortDescription': safe_description,
            'author': author.strip(),
            'status': status,
            'sourceType': TransparentCompanySkill.SOURCE_MANUAL,
            'runtimeStatus': runtime_status,
            'isSystemSkill': False,
            'content': safe_content,
            'updated_at': now,
        }
        skill, created = TransparentCompanySkill.objects.update_or_create(
            transparentCompany=transparent_company,
            skillId=normalized_skill_id,
            defaults=defaults,
        )
        if not created:
            skill.updated_at = now
            skill.save(update_fields=['updated_at'])
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"skill upsert:{skill.skillId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[transparent-skill-sync] runtime sync failed cid={cid} skill={skill.skillId} detail={sync_payload}"
            )
            return False, f"技能已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = skill.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    @transaction.atomic
    def delete_transparent_company_skill(
        cid: str,
        skill_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        """Delete a user-created skill and all of its agent bindings."""
        ok, transparent_company = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company

        skill = TransparentCompanySkill.objects.filter(
            transparentCompany=transparent_company,
            skillId=skill_id,
        ).first()
        if not skill:
            return False, '技能不存在'
        if skill.isSystemSkill or skill.sourceType != TransparentCompanySkill.SOURCE_MANUAL:
            return False, '系统技能不可删除'

        removed_agent_count = skill.agentBindings.filter(enabled=True).count()
        payload = skill.get_info()
        payload.update({
            'removedAgentCount': removed_agent_count,
            'removed_agent_count': removed_agent_count,
        })
        skill.delete()

        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"skill delete:{skill_id}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[transparent-skill-delete-sync] runtime sync failed cid={cid} "
                f"skill_id={skill_id} operator={user.uid}{_ip_suffix(client_ip)} "
                f"detail={sync_payload}"
            )
            return False, f"技能删除失败，同步到 OpenClaw 失败: {sync_payload}"

        payload['runtimeSync'] = sync_payload
        return True, payload

    @staticmethod
    @transaction.atomic
    def toggle_transparent_company_skill_status(
        cid: str,
        skill_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        """
        切换透明公司某个 skill 的 active / inactive 状态
        """
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg
        transparent_company = transparent_company_or_msg

        ok, skill_or_msg = CompanyService._get_or_materialize_transparent_company_skill(
            transparent_company=transparent_company,
            skill_id=skill_id,
        )
        if not ok:
            return False, skill_or_msg
        skill = skill_or_msg

        # 不允许对已废弃的技能进行切换
        if skill.status == TransparentCompanySkill.STATUS_DEPRECATED:
            return False, '废弃技能不可切换状态'

        # 切换 active / inactive，并保持 enabled 一致
        if skill.status == TransparentCompanySkill.STATUS_ACTIVE:
            skill.status = TransparentCompanySkill.STATUS_INACTIVE
            skill.enabled = False
        else:
            skill.status = TransparentCompanySkill.STATUS_ACTIVE
            skill.enabled = True

        skill.runtimeStatus = TransparentCompanySkill.RUNTIME_STATUS_PENDING
        skill.updated_at = str(time.time())[:20]
        skill.save()

        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"skill toggle:{skill.skillId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[transparent-skill-toggle-sync] runtime sync failed cid={cid} "
                f"skill_id={skill_id} operator={user.uid}{_ip_suffix(client_ip)} detail={sync_payload}"
            )
            return False, f"技能状态已保存，但同步到 OpenClaw 失败: {sync_payload}"

        payload = skill.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def list_transparent_company_agents(
        cid: str,
        status: Optional[str] = None,
        visibility: Optional[str] = None,
        client_visibility: Optional[str] = None,
        exposed_to_client: Optional[bool] = None,
        is_default_entry: Optional[bool] = None,
        enabled: Optional[bool] = None,
        user: Optional[User] = None,
        client_ip: Optional[str] = None,
        public_only: bool = False,
    ):
        if public_only:
            try:
                company = Company.objects.get(
                    cid=cid,
                    verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                    verification_suspended_at__isnull=True,
                    service_suspended_at__isnull=True,
                )
                transparent_company = TransparentCompany.objects.select_related(
                    'company'
                ).get(company=company)
            except (Company.DoesNotExist, TransparentCompany.DoesNotExist):
                return False, '透明公司不存在'
        else:
            ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
                cid=cid,
                user=user,
                client_ip=client_ip,
            )
            if not ok:
                access_ok, transparent_company_or_msg, _scope = (
                    CompanyService._get_transparent_company_for_agent_access(
                        cid=cid,
                        user=user,
                        client_ip=client_ip,
                    )
                )
                if not access_ok:
                    return False, transparent_company_or_msg
                restricted_editor = True
            else:
                restricted_editor = False
            transparent_company = transparent_company_or_msg

        qs = TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany=transparent_company
        )
        if not public_only and restricted_editor:
            editable_ids = []
            for agent in qs:
                resource = Resource.objects.filter(
                    type=Resource.TYPE_AGENT,
                    external_id=f'agent:{cid}:{agent.agentId}',
                ).first()
                if resource and is_user_action_allowed(user, resource, 'edit'):
                    editable_ids.append(agent.agentId)
            qs = qs.filter(agentId__in=editable_ids)
        if public_only:
            qs = qs.filter(
                status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
                role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
                visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
                clientVisibility__in=[
                    TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
                    TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_LISTED,
                    TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT,
                ],
                enabled=True,
                exposedToClient=True,
                isCoreAgent=False,
            )
        else:
            if status:
                qs = qs.filter(status=status)
            if visibility:
                qs = qs.filter(visibility=visibility)
            if client_visibility:
                qs = qs.filter(clientVisibility=client_visibility)
            if exposed_to_client is not None:
                qs = qs.filter(exposedToClient=exposed_to_client)
            if is_default_entry is not None:
                qs = qs.filter(isDefaultEntry=is_default_entry)
            if enabled is not None:
                qs = qs.filter(enabled=enabled)

        transparent_company_info = (
            CompanyService._public_transparent_company_info(transparent_company)
            if public_only
            else transparent_company.get_info()
        )
        agents = [
            agent.get_info(transparent_company_info=transparent_company_info)
            for agent in qs.select_related(
                'transparentCompany__company'
            ).order_by('-isCoreAgent', 'exposureOrder', 'created_at', 'id')
        ]
        return True, agents

    @staticmethod
    def _operator_display_name(user: Optional[User]) -> str:
        if user is None:
            return ''
        return (
            getattr(user, 'username', '')
            or getattr(user, 'name', '')
            or getattr(user, 'phone', '')
            or getattr(user, 'email', '')
            or getattr(user, 'uid', '')
            or ''
        )

    @staticmethod
    def _build_prompt_diff_summary(previous_content: str, next_content: str) -> dict[str, Any]:
        previous_lines = str(previous_content or '').splitlines()
        next_lines = str(next_content or '').splitlines()
        diff_lines = list(difflib.ndiff(previous_lines, next_lines))
        added_lines = [line[2:].strip() for line in diff_lines if line.startswith('+ ') and line[2:].strip()]
        removed_lines = [line[2:].strip() for line in diff_lines if line.startswith('- ') and line[2:].strip()]
        key_changes = []
        for line in added_lines[:3]:
            key_changes.append(f"Added: {line[:120]}")
        for line in removed_lines[:2]:
            key_changes.append(f"Removed: {line[:120]}")
        return {
            'added_lines': len(added_lines),
            'removed_lines': len(removed_lines),
            'key_changes': key_changes,
        }

    @staticmethod
    def _record_agent_prompt_version(
        *,
        agent: TransparentCompanyAgentDefinition,
        previous_content: str,
        content: str,
        user: Optional[User],
        version_label: str = '',
        change_reason: str = '',
        change_type: Optional[str] = None,
        force: bool = False,
    ) -> Optional[TransparentCompanyAgentPromptVersion]:
        normalized_content = str(content or '')
        if not force and str(previous_content or '') == normalized_content:
            return None

        versions_qs = TransparentCompanyAgentPromptVersion.objects.filter(agent=agent)
        existing_count = versions_qs.count()
        if not force and existing_count == 0 and not normalized_content:
            return None

        normalized_version_label = str(version_label or '').strip()
        if normalized_version_label:
            if versions_qs.filter(versionLabel__iexact=normalized_version_label).exists():
                raise ValueError(f'版本号已存在: {normalized_version_label}')
        else:
            normalized_version_label = f"v{existing_count + 1}.0"

        now = str(time.time())[:20]
        if not change_type and existing_count == 0:
            change_type = TransparentCompanyAgentPromptVersion.CHANGE_TYPE_INITIAL
        elif not change_type:
            change_type = TransparentCompanyAgentPromptVersion.CHANGE_TYPE_FEATURE
        elif change_type not in {item[0] for item in TransparentCompanyAgentPromptVersion.CHANGE_TYPE_CHOICES}:
            change_type = TransparentCompanyAgentPromptVersion.CHANGE_TYPE_FEATURE

        TransparentCompanyAgentPromptVersion.objects.filter(
            agent=agent,
            isCurrent=True,
        ).update(isCurrent=False)

        return TransparentCompanyAgentPromptVersion.objects.create(
            agent=agent,
            versionLabel=normalized_version_label,
            content=normalized_content,
            changeReason=change_reason or ('Initial prompt' if existing_count == 0 else 'Prompt updated'),
            changeType=change_type,
            createdBy=CompanyService._operator_display_name(user),
            approvedBy='',
            approvedAt='',
            isCurrent=True,
            diffSummary=CompanyService._build_prompt_diff_summary(previous_content, normalized_content),
            created_at=now,
        )

    @staticmethod
    def _can_read_agent_prompt_versions(
        agent: TransparentCompanyAgentDefinition,
        user: Optional[User],
    ) -> bool:
        company = agent.transparentCompany.company
        if not is_company_verified_for_formal_access(company):
            return False
        if (
            agent.status == TransparentCompanyAgentDefinition.STATUS_ACTIVE
            and agent.role == TransparentCompanyAgentDefinition.ROLE_SUBAGENT
            and agent.visibility == TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC
            and agent.clientVisibility in {
                TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE,
                TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_LISTED,
                TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT,
            }
            and bool(agent.exposedToClient)
            and bool(agent.enabled)
            and not bool(agent.isCoreAgent)
        ):
            return True
        if user is None:
            return False
        if has_company_iam_permission(company, user, Grant.PERM_ADMIN):
            return True
        resource = Resource.objects.filter(
            type=Resource.TYPE_AGENT,
            external_id=f'agent:{company.cid}:{agent.agentId}',
        ).first()
        return bool(resource and is_user_action_allowed(user, resource, 'edit'))

    @staticmethod
    def get_transparent_company_agent_prompt_versions(
        agent_id: str,
        *,
        cid: str = '',
        user: Optional[User] = None,
        limit: int = 30,
    ):
        normalized_agent_id = str(agent_id or '').strip()
        if not normalized_agent_id:
            return False, 'agent_id is required'

        qs = (
            TransparentCompanyAgentDefinition.objects
            .select_related('transparentCompany', 'transparentCompany__company')
            .filter(agentId=normalized_agent_id)
        )
        if cid:
            qs = qs.filter(transparentCompany__company__cid=cid)
        agents = list(qs.order_by('-exposedToClient', '-enabled', 'id')[:5])
        agent = next((item for item in agents if CompanyService._can_read_agent_prompt_versions(item, user)), None)
        if agent is None:
            return False, 'Agent not found or unauthorized'

        versions = list(
            TransparentCompanyAgentPromptVersion.objects
            .filter(agent=agent)
            .order_by('-id')[:max(int(limit or 30), 1)]
        )
        serialized_versions = [version.get_info() for version in versions]
        if not serialized_versions and str(agent.systemPrompt or '').strip():
            serialized_versions = [{
                'version_id': f'pv-{agent.agentId}-current',
                'version': 'v1.0',
                'content': agent.systemPrompt or '',
                'change_reason': 'Current Agent Prompt',
                'change_type': TransparentCompanyAgentPromptVersion.CHANGE_TYPE_INITIAL,
                'created_at': agent.updated_at or agent.created_at or '',
                'created_by': '',
                'is_current': True,
                'diff_summary': {
                    'added_lines': len(str(agent.systemPrompt or '').splitlines()),
                    'removed_lines': 0,
                    'key_changes': [],
                },
            }]

        data = {
            'agent_id': agent.agentId,
            'agent_name': agent.name,
            'versions': serialized_versions,
            'total': len(serialized_versions),
        }
        return True, data

    @staticmethod
    def _client_entry_key(agent: TransparentCompanyAgentDefinition) -> str:
        route_key = str(getattr(agent, 'routeKey', '') or '').strip()
        if route_key:
            return route_key
        if bool(getattr(agent, 'isDefaultEntry', False)):
            return '__default__'
        return ''

    @staticmethod
    def _get_client_entry_agents(
        transparent_company: TransparentCompany,
    ) -> list[TransparentCompanyAgentDefinition]:
        return list(
            TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
                visibility=TransparentCompanyAgentDefinition.VISIBILITY_PUBLIC,
                enabled=True,
                exposedToClient=True,
                isCoreAgent=False,
                role=TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
            )
            .filter(
                Q(clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_VISIBLE)
                | Q(clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_LISTED)
                | Q(clientVisibility=TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_DEFAULT)
            )
            .order_by('exposureOrder', 'created_at', 'id')
        )

    @staticmethod
    def _build_client_entry_payload(
        transparent_company: TransparentCompany,
    ) -> dict[str, Any]:
        company = transparent_company.company
        system_setting = getattr(company, 'systemSetting', None)
        preferred_default_agent_id = (
            str(getattr(system_setting, 'defaultEntryAgentId', '') or '').strip()
            if system_setting is not None
            else ''
        )
        visible_agents: list[dict[str, Any]] = []
        bindings: list[dict[str, Any]] = []
        default_binding: Optional[dict[str, Any]] = None

        for agent in CompanyService._get_client_entry_agents(transparent_company):
            entry_key = CompanyService._client_entry_key(agent)
            if not entry_key:
                continue
            binding = {
                'entry_key': entry_key,
                'agent_id': agent.agentId,
                'name': agent.name,
                'route_key': agent.routeKey or '',
                'is_default_entry': bool(agent.isDefaultEntry),
                'exposure_order': int(agent.exposureOrder or 100),
            }
            bindings.append(binding)
            agent_info = agent.get_info()
            agent_info['entryKey'] = entry_key
            visible_agents.append(agent_info)
            if preferred_default_agent_id and agent.agentId == preferred_default_agent_id:
                default_binding = binding
            elif default_binding is None and bool(agent.isDefaultEntry):
                default_binding = binding

        if default_binding is None and bindings:
            default_binding = bindings[0]
        default_agent_id = str(default_binding.get('agent_id') or '') if default_binding else ''
        return {
            'cid': company.cid,
            'mode': 'multi_agent',
            'default_agent_id': default_agent_id,
            'entry_agent_id': default_agent_id,
            'visible_agents': visible_agents,
            'bindings': bindings,
        }

    @staticmethod
    def resolve_transparent_company_client_entry_agent(
        cid: str,
        *,
        entry_key: str = '',
        route_key: str = '',
        agent_id: str = '',
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            company = Company.objects.get(
                cid=cid,
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
        except Company.DoesNotExist:
            return False, '公司不存在'

        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, '透明公司不存在'

        payload = CompanyService._build_client_entry_payload(transparent_company)
        bindings = payload.get('bindings') if isinstance(payload.get('bindings'), list) else []
        requested_entry_key = str(entry_key or '').strip()
        requested_route_key = str(route_key or '').strip()
        requested_agent_id = str(agent_id or '').strip()
        binding = None
        if requested_entry_key:
            binding = next(
                (item for item in bindings if str(item.get('entry_key') or '').strip() == requested_entry_key),
                None,
            )
        elif requested_route_key:
            binding = next(
                (item for item in bindings if str(item.get('route_key') or '').strip() == requested_route_key),
                None,
            )
        else:
            default_agent_id = str(payload.get('entry_agent_id') or '').strip()
            binding = next(
                (item for item in bindings if str(item.get('agent_id') or '').strip() == default_agent_id),
                None,
            )
        if binding is None:
            return False, '当前 C 端入口没有绑定可访问的 subagent'
        bound_agent_id = str(binding.get('agent_id') or '').strip()
        if requested_agent_id and requested_agent_id != bound_agent_id:
            logger.warning(
                '[client-entry-binding] ignore mismatched agent_id cid=%s requested=%s bound=%s route_key=%s',
                cid,
                requested_agent_id,
                bound_agent_id,
                requested_route_key,
            )
        return True, {
            'cid': cid,
            'entry_key': str(binding.get('entry_key') or ''),
            'route_key': str(binding.get('route_key') or ''),
            'agent_id': bound_agent_id,
            'name': str(binding.get('name') or ''),
            'requested_agent_id': requested_agent_id,
            'ignored_agent_id': requested_agent_id if requested_agent_id and requested_agent_id != bound_agent_id else '',
        }

    @staticmethod
    def get_transparent_company_client_entry(cid: str):
        try:
            company = Company.objects.get(
                cid=cid,
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
        except Company.DoesNotExist:
            return False, '公司不存在'

        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, '透明公司不存在'

        return True, CompanyService._build_client_entry_payload(transparent_company)

    @staticmethod
    def _get_transparent_company_for_client_entry(
        cid: str,
    ) -> tuple[bool, TransparentCompany | str]:
        try:
            company = Company.objects.get(
                cid=cid,
                verification_status=Company.VERIFICATION_STATUS_VERIFIED,
                verification_suspended_at__isnull=True,
                service_suspended_at__isnull=True,
            )
        except Company.DoesNotExist:
            return False, '公司不存在'

        try:
            transparent_company = TransparentCompany.objects.get(company=company)
        except TransparentCompany.DoesNotExist:
            return False, '透明公司不存在'

        return True, transparent_company

    @staticmethod
    def _sanitize_client_entry_chat_messages(
        messages: Any,
        *,
        limit: int = 24,
        max_chars: int = 8000,
    ) -> list[dict[str, str]]:
        if not isinstance(messages, list):
            return []

        sanitized: list[dict[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get('role') or '').strip().lower()
            if role not in {'user', 'assistant'}:
                continue
            content = str(item.get('content') or '').strip()
            if not content:
                continue
            sanitized.append(
                {
                    'role': role,
                    'content': content[:max(int(max_chars or 8000), 1)],
                }
            )
        return sanitized[-max(int(limit or 24), 1):]

    @staticmethod
    def _serialize_client_entry_messages_for_runtime(
        messages: Any,
    ) -> list[dict[str, str]]:
        serialized: list[dict[str, str]] = []
        for item in CompanyService._sanitize_client_entry_chat_messages(messages):
            serialized.append(
                {
                    'role': str(item.get('role') or ''),
                    'content': str(item.get('content') or ''),
                }
            )
        return serialized

    @staticmethod
    def _sync_transparent_company_runtime_config(
        company: Company,
        operator_id: str,
        notes: str,
    ):
        return True, {"skipped": True, "reason": "langgraph_native", "engine": "langgraph"}
        from apps.runtime.legacy_control import (
            legacy_runtime_is_read_only,
            legacy_runtime_skip_payload,
        )

        if legacy_runtime_is_read_only():
            return True, legacy_runtime_skip_payload()

        from apps.runtime.models import OpenClawProvisionTask
        from apps.runtime.services import OpenClawConfigService, OpenClawRuntimeService

        ok, instance_or_msg = OpenClawRuntimeService.get_company_instance(company)
        if not ok:
            ok, instance_or_msg = OpenClawRuntimeService.get_or_create_instance(company=company)
            if not ok:
                return False, CompanyService._summarize_runtime_sync_error(instance_or_msg)
        instance = instance_or_msg

        ok, version_or_msg = OpenClawConfigService.create_config_version(
            company=company,
            operator_id=operator_id,
            notes=notes,
            publish=True,
        )
        if not ok:
            return False, CompanyService._summarize_runtime_sync_error(version_or_msg)

        try:
            from common.immudbIO.audit_service import AgentPromptEvidenceService

            evidence_service = AgentPromptEvidenceService()
            evidence_schema_ok, evidence_schema_msg = evidence_service.ensure_schema()
        except Exception as exc:
            return False, CompanyService._summarize_runtime_sync_error(
                f"immudb prompt evidence preflight failed: {exc}"
            )
        if not evidence_schema_ok:
            return False, CompanyService._summarize_runtime_sync_error(evidence_schema_msg)

        bootstrap_ok, bootstrap_result = OpenClawRuntimeService.start_bootstrap(company, instance)
        if not bootstrap_ok:
            return False, CompanyService._summarize_runtime_sync_error(bootstrap_result)

        bootstrap_task = OpenClawProvisionTask.objects.filter(
            id=(bootstrap_result.get("task") or {}).get("id")
        ).first()
        if not bootstrap_task:
            return False, "bootstrap task missing"

        run_ok, run_result = OpenClawRuntimeService.run_bootstrap(bootstrap_task)
        if not run_ok:
            return False, CompanyService._summarize_runtime_sync_error(run_result)

        readback_ok, readback_payload = OpenClawRuntimeService.readback_config(instance)
        if not readback_ok:
            return False, CompanyService._summarize_runtime_sync_error(readback_payload)

        diff_ok, diff_payload = OpenClawConfigService.build_company_config_diff(company)
        if not diff_ok:
            return False, CompanyService._summarize_runtime_sync_error(diff_payload)
        if isinstance(diff_payload, dict) and not bool(diff_payload.get("is_in_sync")):
            return False, CompanyService._summarize_runtime_sync_error(diff_payload)

        try:
            evidence_ok, evidence_payload = evidence_service.write_prompt_versions(
                company=company,
                config_version=version_or_msg if isinstance(version_or_msg, dict) else {},
                operator_id=operator_id,
                notes=notes,
                readback_payload=readback_payload if isinstance(readback_payload, dict) else {},
                diff_payload=diff_payload if isinstance(diff_payload, dict) else {},
            )
        except Exception as exc:
            evidence_ok = False
            evidence_payload = f"immudb prompt evidence failed: {exc}"
        if not evidence_ok:
            return False, CompanyService._summarize_runtime_sync_error(evidence_payload)

        instance.refresh_from_db()
        return True, CompanyService._summarize_runtime_sync_payload({
            "version": version_or_msg,
            "bootstrap": run_result if isinstance(run_result, dict) else {"message": str(run_result)},
            "readback": readback_payload,
            "diff": diff_payload,
            "evidence": evidence_payload,
            "instance": instance.get_info(),
        })

    @staticmethod
    def _compact_runtime_sync_dict(source: Any, allowed_keys: Iterable[str]) -> dict[str, Any]:
        if not isinstance(source, dict):
            return {}
        compact: dict[str, Any] = {}
        for key in allowed_keys:
            value = source.get(key)
            if value in (None, "", [], {}):
                continue
            if key in {"message", "error", "detail"} and isinstance(value, str):
                value = CompanyService._sanitize_runtime_sync_text(value)
            compact[key] = value
        return compact

    @staticmethod
    def _sanitize_runtime_sync_text(value: Any, limit: int = 500) -> str:
        message = str(value or "").strip()
        if not message:
            return ""
        message = re.sub(
            r"(?i)\b[A-Z]:\\[^\s'\"<>]+",
            "[internal-path]",
            message,
        )
        message = re.sub(
            r"/(?:data|srv|private)(?:/[^\s'\"<>]+)+",
            "[internal-path]",
            message,
        )
        message = re.sub(
            r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?[^\s'\"<>]*",
            "[internal-endpoint]",
            message,
        )
        return message[:limit]

    @staticmethod
    def _summarize_runtime_sync_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            message = CompanyService._sanitize_runtime_sync_text(payload)
            return {"message": message[:500]} if message else {}

        version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
        bootstrap = payload.get("bootstrap") if isinstance(payload.get("bootstrap"), dict) else {}
        readback = payload.get("readback") if isinstance(payload.get("readback"), dict) else {}
        diff = payload.get("diff") if isinstance(payload.get("diff"), dict) else {}
        instance = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}

        bootstrap_task = bootstrap.get("task") if isinstance(bootstrap.get("task"), dict) else {}
        summary = {
            "version": CompanyService._compact_runtime_sync_dict(
                version,
                ("id", "version", "schema_version", "status", "published_at", "created_at"),
            ),
            "bootstrap": {
                **CompanyService._compact_runtime_sync_dict(
                    bootstrap,
                    ("status", "step", "message"),
                ),
                "task": CompanyService._compact_runtime_sync_dict(
                    bootstrap_task,
                    ("id", "task_id", "task_type", "status", "step", "updated_at"),
                ),
            },
            "readback": CompanyService._compact_runtime_sync_dict(
                readback,
                (
                    "published_version",
                    "current_runtime_version",
                    "source",
                    "status",
                    "checked_at",
                ),
            ),
            "diff": CompanyService._compact_runtime_sync_dict(
                diff,
                (
                    "platform_version",
                    "runtime_version",
                    "is_in_sync",
                    "mismatched_sections",
                    "readback_source",
                    "checked_at",
                ),
            ),
            "instance": CompanyService._compact_runtime_sync_dict(
                instance,
                (
                    "id",
                    "status",
                    "openclaw_status",
                    "config_status",
                    "current_publish_version",
                    "current_runtime_version",
                    "last_readback_at",
                    "updated_at",
                ),
            ),
        }
        return {
            key: value
            for key, value in summary.items()
            if value not in (None, "", [], {}, {"task": {}})
        }

    @staticmethod
    def _summarize_runtime_sync_error(payload: Any) -> str:
        if isinstance(payload, dict):
            mismatched_sections = payload.get("mismatched_sections")
            if isinstance(mismatched_sections, list):
                sections = ", ".join(str(item) for item in mismatched_sections[:8])
                return f"runtime config diff mismatch: {sections or 'unknown sections'}"
            for key in ("message", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return CompanyService._sanitize_runtime_sync_text(value)
            status = payload.get("status") or payload.get("step")
            if status:
                return f"runtime sync failed: status={status}"
            return "runtime sync failed"
        return CompanyService._sanitize_runtime_sync_text(payload or "runtime sync failed")

    @staticmethod
    @transaction.atomic
    def upsert_transparent_company_agent(
        cid: str,
        user: User,
        name: str,
        role: str = TransparentCompanyAgentDefinition.ROLE_SUBAGENT,
        agent_id: Optional[str] = None,
        description: str = '',
        system_prompt: str = '',
        accent_color: Optional[str] = None,
        visibility: str = TransparentCompanyAgentDefinition.VISIBILITY_INTERNAL,
        client_visibility: str = TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN,
        exposed_to_client: bool = False,
        is_default_entry: bool = False,
        route_key: str = '',
        exposure_order: Optional[int] = None,
        enabled: Optional[bool] = None,
        is_core_agent: bool = False,
        status: str = TransparentCompanyAgentDefinition.STATUS_INACTIVE,
        sort_order: int = 0,
        version_label: str = '',
        change_reason: str = '',
        change_type: Optional[str] = None,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            if not agent_id:
                return False, transparent_company_or_msg
            access_ok, transparent_company_or_msg, _scope = (
                CompanyService._get_transparent_company_for_agent_access(
                    cid=cid,
                    user=user,
                    client_ip=client_ip,
                )
            )
            resource = Resource.objects.filter(
                type=Resource.TYPE_AGENT,
                external_id=f'agent:{cid}:{agent_id}',
            ).first()
            if (
                not access_ok
                or resource is None
                or not is_user_action_allowed(user, resource, 'edit')
            ):
                return False, '权限不足'

        transparent_company = transparent_company_or_msg

        if agent_id:
            agent = TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                agentId=agent_id,
            ).first()
        else:
            agent = None

        is_new_agent = agent is None
        previous_prompt = ''
        if agent is None:
            generated_agent_id = agent_id or f"{cid}-agent-{int(time.time() * 1000)}"
            agent = TransparentCompanyAgentDefinition(
                transparentCompany=transparent_company,
                agentId=generated_agent_id,
            )
        else:
            previous_prompt = agent.systemPrompt or ''

        normalized_version_label = str(version_label or '').strip()
        if normalized_version_label and agent.id:
            if TransparentCompanyAgentPromptVersion.objects.filter(
                agent=agent,
                versionLabel__iexact=normalized_version_label,
            ).exists():
                return False, f'版本号已存在: {normalized_version_label}'

        moderation_ok, moderation_msg = _moderate_company_texts(
            items=[
                ("name", name),
                ("description", description),
                ("systemPrompt", system_prompt),
                ("changeReason", change_reason),
            ],
            source=TextModerationAsset.SOURCE_AGENT_PROMPT,
            owner_id=cid,
            actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            return False, moderation_msg

        agent.name = name
        agent.role = role
        agent.description = description or ''
        agent.systemPrompt = system_prompt or ''
        if accent_color is not None:
            agent.accentColor = str(accent_color).strip().lower()
        agent.visibility = visibility
        exposed_to_client = bool(exposed_to_client)
        is_default_entry = bool(is_default_entry) and exposed_to_client
        normalized_client_visibility = TransparentCompanyAgentDefinition.normalize_client_visibility(client_visibility)
        if is_core_agent:
            exposed_to_client = False
            normalized_client_visibility = TransparentCompanyAgentDefinition.CLIENT_VISIBILITY_HIDDEN
            is_default_entry = False

        # 生成 route_key 如果为空
        if not route_key:
            route_key = name.lower().replace(' ', '-')

        normalized_route_key = str(route_key or '').strip()
        if exposed_to_client and role != TransparentCompanyAgentDefinition.ROLE_SUBAGENT:
            return False, 'C 端入口只能绑定 subagent'
        if exposed_to_client and not is_default_entry and not normalized_route_key:
            return False, '公开 subagent 必须显式配置 route_key'
        if normalized_route_key:
            existing_route = TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                routeKey=normalized_route_key,
                exposedToClient=True,
            )
            if getattr(agent, 'id', None):
                existing_route = existing_route.exclude(id=agent.id)
            if existing_route.first() is not None:
                return False, 'route_key 已绑定到其他公开入口'

        if exposure_order is None:
            if is_new_agent and not is_core_agent:
                last_agent = (
                    TransparentCompanyAgentDefinition.objects.filter(
                        transparentCompany=transparent_company,
                        isCoreAgent=False,
                    )
                    .order_by('-exposureOrder', '-id')
                    .first()
                )
                exposure_order = int(getattr(last_agent, 'exposureOrder', 0) or 0) + 10
            else:
                exposure_order = int(getattr(agent, 'exposureOrder', 100) or 100)

        agent.clientVisibility = normalized_client_visibility
        agent.exposedToClient = exposed_to_client
        agent.isDefaultEntry = is_default_entry
        agent.routeKey = normalized_route_key
        agent.exposureOrder = exposure_order or 0
        agent.isCoreAgent = is_core_agent
        agent.sortOrder = sort_order or 0
        agent.status = status
        agent.enabled = enabled if enabled is not None else agent.enabled
        agent.updated_at = str(time.time())[:20]
        if not agent.created_at:
            agent.created_at = str(time.time())[:20]
        if agent.isDefaultEntry:
            TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                agentId='main-agent',
            ).exclude(id=agent.id).update(
                isDefaultEntry=False,
                updated_at=str(time.time())[:20],
            )
        agent.save()
        tenant = get_or_create_organization_tenant(transparent_company.company)
        get_or_create_resource(
            Resource.TYPE_AGENT,
            f'agent:{cid}:{agent.agentId}',
            tenant=tenant,
            creator=user,
        )
        # Agent creation is idempotently coupled to one logical OpenViking space.
        viking_space = CompanyService._ensure_agent_viking_space(transparent_company, agent)
        if agent.status != TransparentCompanyAgentDefinition.STATUS_DEPRECATED:
            CompanyService._enqueue_viking_provisioning(
                viking_space.companyBinding,
                agent_id=agent.pk,
            )
        try:
            CompanyService._record_agent_prompt_version(
                agent=agent,
                previous_content=previous_prompt,
                content=agent.systemPrompt or '',
                user=user,
                version_label=version_label,
                change_reason=change_reason,
                change_type=change_type,
                force=is_new_agent,
            )
        except ValueError as exc:
            return False, str(exc)
        if (
            is_new_agent
            and not bool(agent.isCoreAgent)
            and str(agent.role or '') == TransparentCompanyAgentDefinition.ROLE_SUBAGENT
        ):
            TransparentCompanyAgentPermissionBinding.objects.get_or_create(
                transparentCompany=transparent_company,
                agent=agent,
                defaults={
                    'skillsInclude': [],
                    'skillsExclude': [],
                    'knowledgeRead': [],
                    'knowledgeWrite': [],
                    'knowledgeDelete': [],
                    'knowledgeDeny': [],
                    'knowledgeReadDeny': [],
                    'knowledgeWriteDeny': [],
                    'knowledgeDeleteDeny': [],
                    'memoryRead': True,
                    'memoryWrite': True,
                    'memoryUpdate': True,
                    'memoryDelete': True,
                    'enabled': False,
                    'status': TransparentCompanyAgentPermissionBinding.STATUS_INACTIVE,
                    'created_at': str(time.time())[:20],
                    'updated_at': str(time.time())[:20],
                },
            )
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent upsert:{agent.agentId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[transparent-agent-sync] runtime sync failed cid={cid} agent={agent.agentId} detail={sync_payload}"
            )
            return False, f"Agent 已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = agent.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload


    @staticmethod
    @transaction.atomic
    def reorder_transparent_company_agents(
        cid: str,
        user: User,
        orders: list[dict[str, Any]],
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        normalized_orders = []
        seen_agent_ids = set()
        for item in orders or []:
            agent_id = str(item.get('agent_id') or '').strip()
            if not agent_id or agent_id in seen_agent_ids:
                continue
            seen_agent_ids.add(agent_id)
            normalized_orders.append({
                'agent_id': agent_id,
                'exposure_order': int(item.get('exposure_order') or 0),
            })

        if not normalized_orders:
            return False, 'orders cannot be empty'

        agents = {
            agent.agentId: agent
            for agent in TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                agentId__in=[item['agent_id'] for item in normalized_orders],
            )
        }
        missing = [item['agent_id'] for item in normalized_orders if item['agent_id'] not in agents]
        if missing:
            return False, f"Agent not found: {', '.join(missing)}"

        if any(bool(agents[item['agent_id']].isCoreAgent) for item in normalized_orders):
            return False, 'core agent cannot be reordered'

        now = str(time.time())[:20]
        for item in normalized_orders:
            agent = agents[item['agent_id']]
            agent.exposureOrder = item['exposure_order']
            agent.sortOrder = item['exposure_order']
            agent.updated_at = now
            agent.save(update_fields=['exposureOrder', 'sortOrder', 'updated_at'])

        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes='agent reorder',
        )
        sync_warning = None
        if not sync_ok:
            sync_warning = str(sync_payload)
            logger.error(
                f"[transparent-agent-reorder-sync] runtime sync failed cid={cid} detail={sync_payload}"
            )

        list_ok, payload = CompanyService.list_transparent_company_agents(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not list_ok:
            return False, payload
        return True, {
            'items': payload,
            'runtimeSync': sync_payload,
            'runtimeSyncOk': sync_ok,
            'runtimeSyncWarning': sync_warning,
        }

    @staticmethod
    @transaction.atomic
    def toggle_transparent_company_agent_status(
        cid: str,
        agent_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        try:
            agent = TransparentCompanyAgentDefinition.objects.get(
                transparentCompany=transparent_company,
                agentId=agent_id,
            )
        except TransparentCompanyAgentDefinition.DoesNotExist:
            return False, 'Agent 不存在'

        if agent.status == TransparentCompanyAgentDefinition.STATUS_DEPRECATED:
            return False, '废弃 Agent 不可切换状态'

        agent.enabled = not agent.enabled
        agent.status = (
            TransparentCompanyAgentDefinition.STATUS_ACTIVE
            if agent.enabled
            else TransparentCompanyAgentDefinition.STATUS_INACTIVE
        )
        agent.updated_at = str(time.time())[:20]
        agent.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent toggle:{agent.agentId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[transparent-agent-toggle-sync] runtime sync failed cid={cid} agent={agent.agentId} detail={sync_payload}"
            )
            return False, f"Agent 状态已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = agent.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def list_transparent_company_test_agents(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg, scope = CompanyService._get_transparent_company_for_agent_access(cid, user, client_ip)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        agents = TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany=transparent_company
        ).exclude(
            status=TransparentCompanyAgentDefinition.STATUS_DEPRECATED
        ).order_by('isCoreAgent', 'sortOrder', 'id')
        broad_admin = can_access_formal_company(
            transparent_company.company, user, Grant.PERM_ADMIN,
        )
        if not broad_admin:
            agents = agents.filter(agentId__in=scope.agent_ids)
            return True, [
                {
                    'agentId': agent.agentId,
                    'name': agent.name,
                    'role': agent.role,
                    'description': agent.description,
                    'enabled': agent.enabled,
                    'permissions': scope.permissions.get(agent.agentId, 0),
                    'can_chat': agent.enabled and scope.permissions.get(agent.agentId, 0) >= Grant.ROLE_CONTRIBUTOR,
                    'can_configure': scope.permissions.get(agent.agentId, 0) >= Grant.ROLE_EDITOR,
                }
                for agent in agents
            ]
        return True, [agent.get_info() for agent in agents]

    @staticmethod
    def _build_agent_test_context(agent: TransparentCompanyAgentDefinition):
        return {
            'entry_agent_id': agent.agentId,
            'visible_agents': [
                {
                    'agent_id': agent.agentId,
                    'name': agent.name,
                    'route_key': agent.routeKey or '',
                    'is_default_entry': True,
                }
            ],
        }

    @staticmethod
    def _now_str() -> str:
        return str(time.time())[:20]

    @staticmethod
    def _build_agent_test_session_title(
        agent: Optional[TransparentCompanyAgentDefinition],
        title: str = '',
    ) -> str:
        normalized = re.sub(r'\s+', ' ', (title or '').strip())
        if not normalized:
            return '\u672a\u547d\u540d\u4f1a\u8bdd'
        chars = list(normalized)
        value = ''.join(chars[:16])
        if len(chars) > 16:
            value = f'{value}...'
        return value[:255]

    @staticmethod
    def _is_default_agent_test_session_title(
        session: TransparentCompanyAgentTestSession,
    ) -> bool:
        title = (session.title or '').strip()
        if not title or title in {'\u672a\u547d\u540d\u4f1a\u8bdd', '\u65b0\u5bf9\u8bdd'}:
            return True
        agent_name = (session.agent.name or '').strip() if session.agent_id else ''
        return bool(agent_name) and title in {
            f'{agent_name} \u6d4b\u8bd5\u4f1a\u8bdd',
            f'{agent_name} \u5de5\u4f5c\u4f1a\u8bdd',
        }

    @staticmethod
    def _ensure_agent_test_session_title_from_first_message(
        session: TransparentCompanyAgentTestSession,
        content: str,
    ) -> None:
        if session.messages.exists():
            return
        if not CompanyService._is_default_agent_test_session_title(session):
            return
        next_title = CompanyService._build_agent_test_session_title(session.agent, content)
        if session.title != next_title:
            session.title = next_title
            session.save(update_fields=['title'])

    @staticmethod
    def _get_agent_test_session_display_info(
        session: TransparentCompanyAgentTestSession,
    ) -> dict[str, Any]:
        info = session.get_info()
        if session.messageCount > 0 and CompanyService._is_default_agent_test_session_title(session):
            first_user_message = (
                session.messages
                .filter(role=TransparentCompanyAgentTestMessage.ROLE_USER)
                .order_by('id')
                .first()
            )
            if first_user_message is not None:
                info['title'] = CompanyService._build_agent_test_session_title(
                    session.agent,
                    first_user_message.content,
                )
        return info

    @staticmethod
    def _get_agent_test_session(
        transparent_company: TransparentCompany,
        conversation_id: str,
    ) -> Optional[TransparentCompanyAgentTestSession]:
        return (
            TransparentCompanyAgentTestSession.objects.filter(
                transparentCompany=transparent_company,
                conversationId=conversation_id,
                source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
            )
            .select_related('agent', 'operator')
            .first()
        )

    @staticmethod
    def _get_public_client_entry_session(
        transparent_company: TransparentCompany,
        conversation_id: str,
        user: User,
    ) -> Optional[TransparentCompanyAgentTestSession]:
        return (
            TransparentCompanyAgentTestSession.objects.filter(
                transparentCompany=transparent_company,
                conversationId=conversation_id,
                operator=user,
                source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
            )
            .select_related('agent', 'operator')
            .first()
        )

    @staticmethod
    def _get_latest_public_client_entry_session(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
        user: User,
    ) -> Optional[TransparentCompanyAgentTestSession]:
        return (
            TransparentCompanyAgentTestSession.objects.filter(
                transparentCompany=transparent_company,
                agent=agent,
                operator=user,
                source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
            )
            .select_related('agent', 'operator')
            .order_by('-id')
            .first()
        )

    @staticmethod
    def _default_public_client_entry_memory_config() -> dict[str, Any]:
        return {
            'enabled': False,
            'mode': CompanySessionMemoryPolicy.MODE_SESSION,
            'status': CompanySessionMemoryPolicy.STATUS_ACTIVE,
            'recentMessageWindow': 12,
            'maxTokens': 4000,
            'summaryTriggerTokens': 2800,
            'summaryMaxTokens': 800,
            'summaryStyle': CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE,
            'sessionTtlDays': 30,
            'redactPii': False,
        }

    @staticmethod
    def _resolve_public_client_entry_memory_config(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
    ) -> dict[str, Any]:
        config = CompanyService._default_public_client_entry_memory_config()
        company_policy = getattr(transparent_company.company, 'sessionMemoryPolicy', None)
        company_policy_enabled = False
        if company_policy is not None:
            config.update(
                {
                    'mode': company_policy.mode,
                    'status': company_policy.status,
                    'enabled': bool(company_policy.enabled),
                    'recentMessageWindow': int(company_policy.recentMessageWindow or config['recentMessageWindow']),
                    'maxTokens': int(company_policy.maxTokens or config['maxTokens']),
                    'summaryTriggerTokens': int(company_policy.summaryTriggerTokens or config['summaryTriggerTokens']),
                    'summaryMaxTokens': int(company_policy.summaryMaxTokens or config['summaryMaxTokens']),
                    'summaryStyle': company_policy.summaryStyle or config['summaryStyle'],
                    'sessionTtlDays': int(company_policy.sessionTtlDays or config['sessionTtlDays']),
                    'redactPii': bool(company_policy.redactPii),
                }
            )
            company_policy_enabled = bool(
                company_policy.enabled
                and company_policy.status == CompanySessionMemoryPolicy.STATUS_ACTIVE
                and company_policy.mode == CompanySessionMemoryPolicy.MODE_SESSION
            )

        binding = (
            TransparentCompanyAgentMemoryBinding.objects.filter(
                transparentCompany=transparent_company,
                agent=agent,
            )
            .order_by('-id')
            .first()
        )
        if binding is not None:
            if binding.maxTokensOverride:
                config['maxTokens'] = int(binding.maxTokensOverride)
            if binding.summaryStyleOverride:
                config['summaryStyle'] = binding.summaryStyleOverride
            binding_enabled = bool(
                binding.enabled
                and binding.status == TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE
                and binding.memoryScope == TransparentCompanyAgentMemoryBinding.SCOPE_SESSION
            )
            config['enabled'] = binding_enabled and (
                company_policy_enabled if binding.inheritCompanyPolicy else True
            )
            config['bindingApplied'] = True
            config['bindingInheritCompanyPolicy'] = bool(binding.inheritCompanyPolicy)
            return config

        config['enabled'] = company_policy_enabled
        config['bindingApplied'] = False
        config['bindingInheritCompanyPolicy'] = True
        return config

    @staticmethod
    def _default_public_client_entry_user_memory_config() -> dict[str, Any]:
        return {
            'enabled': False,
            'status': CompanyUserMemoryPolicy.STATUS_ACTIVE,
            'maxRecallItems': 6,
            'maxRecallTokens': 600,
            'allowSensitive': False,
            'memoryScope': TransparentCompanyAgentUserMemoryBinding.SCOPE_COMPANY_USER,
            'writebackEnabled': False,
            'writebackMode': CompanyUserMemoryPolicy.WRITEBACK_MODE_DISABLED,
            'writeMinConfidence': 0.9,
        }

    @staticmethod
    def _public_client_entry_user_memory_mainline_enabled() -> bool:
        return False

    @staticmethod
    def _resolve_public_client_entry_user_memory_config(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
    ) -> dict[str, Any]:
        config = CompanyService._default_public_client_entry_user_memory_config()
        company_policy = getattr(transparent_company.company, 'userMemoryPolicy', None)
        company_policy_enabled = False
        if company_policy is not None:
            config.update(
                {
                    'enabled': bool(company_policy.enabled),
                    'status': company_policy.status,
                    'maxRecallItems': int(company_policy.maxRecallItems or config['maxRecallItems']),
                    'maxRecallTokens': int(company_policy.maxRecallTokens or config['maxRecallTokens']),
                    'allowSensitive': bool(company_policy.allowSensitive),
                    'writebackEnabled': bool(getattr(company_policy, 'writebackEnabled', False)),
                    'writebackMode': (
                        getattr(company_policy, 'writebackMode', CompanyUserMemoryPolicy.WRITEBACK_MODE_DISABLED)
                        or CompanyUserMemoryPolicy.WRITEBACK_MODE_DISABLED
                    ),
                    'writeMinConfidence': float(getattr(company_policy, 'writeMinConfidence', 0.9) or 0.9),
                }
            )
            company_policy_enabled = bool(
                company_policy.enabled and company_policy.status == CompanyUserMemoryPolicy.STATUS_ACTIVE
            )

        binding = (
            TransparentCompanyAgentUserMemoryBinding.objects.filter(
                transparentCompany=transparent_company,
                agent=agent,
            )
            .order_by('-id')
            .first()
        )
        if binding is not None:
            if binding.maxRecallItemsOverride:
                config['maxRecallItems'] = int(binding.maxRecallItemsOverride)
            if binding.maxRecallTokensOverride:
                config['maxRecallTokens'] = int(binding.maxRecallTokensOverride)
            config['memoryScope'] = binding.memoryScope or config['memoryScope']
            binding_enabled = bool(
                binding.enabled
                and binding.status == TransparentCompanyAgentUserMemoryBinding.STATUS_ACTIVE
                and binding.memoryScope
                in {
                    TransparentCompanyAgentUserMemoryBinding.SCOPE_COMPANY_USER,
                    TransparentCompanyAgentUserMemoryBinding.SCOPE_AGENT_USER,
                }
            )
            config['enabled'] = binding_enabled and (
                company_policy_enabled if binding.inheritCompanyPolicy else True
            )
            config['writebackEnabled'] = bool(
                config['enabled']
                and config.get('writebackEnabled')
                and config.get('writebackMode') == CompanyUserMemoryPolicy.WRITEBACK_MODE_SHADOW
            )
            config['bindingApplied'] = True
            config['bindingInheritCompanyPolicy'] = bool(binding.inheritCompanyPolicy)
            return config

        config['enabled'] = company_policy_enabled
        config['writebackEnabled'] = bool(
            config['enabled']
            and config.get('writebackEnabled')
            and config.get('writebackMode') == CompanyUserMemoryPolicy.WRITEBACK_MODE_SHADOW
        )
        config['bindingApplied'] = False
        config['bindingInheritCompanyPolicy'] = True
        return config

    @staticmethod
    def _select_public_client_entry_user_memory_items(
        session: TransparentCompanyAgentTestSession,
        *,
        config: Optional[dict[str, Any]] = None,
    ) -> list[CompanyUserMemoryItem]:
        resolved_config = config or CompanyService._resolve_public_client_entry_user_memory_config(
            session.transparentCompany,
            session.agent,
        )
        if not resolved_config.get('enabled') or not session.operator_id:
            return []

        queryset = CompanyUserMemoryItem.objects.filter(
            company=session.transparentCompany.company,
            user=session.operator,
            status=CompanyUserMemoryItem.STATUS_ACTIVE,
        )
        memory_scope = str(resolved_config.get('memoryScope') or '').strip()
        if memory_scope == TransparentCompanyAgentUserMemoryBinding.SCOPE_AGENT_USER:
            queryset = queryset.filter(
                scope=CompanyUserMemoryItem.SCOPE_AGENT,
                agent=session.agent,
            )
        else:
            queryset = queryset.filter(
                Q(scope=CompanyUserMemoryItem.SCOPE_COMPANY)
                | Q(scope=CompanyUserMemoryItem.SCOPE_AGENT, agent=session.agent)
            )

        max_items = max(int(resolved_config.get('maxRecallItems') or 0), 0)
        if max_items <= 0:
            return []
        return list(
            queryset.order_by('-lastConfirmedAt', '-lastSeenAt', '-updated_at', '-id')[:max_items]
        )

    @staticmethod
    def _build_public_client_entry_user_memory_block(
        session: TransparentCompanyAgentTestSession,
    ) -> str:
        config = CompanyService._resolve_public_client_entry_user_memory_config(
            session.transparentCompany,
            session.agent,
        )
        items = CompanyService._select_public_client_entry_user_memory_items(session, config=config)
        if not items:
            return ''

        scope = str(config.get('memoryScope') or '').strip()
        if scope == TransparentCompanyAgentUserMemoryBinding.SCOPE_AGENT_USER:
            header = '以下是该登录用户在当前公开 Agent 对话中的长期记忆，仅在仍然相关时参考；如果与用户本轮最新表达冲突，以本轮为准。'
        else:
            header = '以下是该登录用户在本公司公开对话中的长期记忆，仅在仍然相关时参考；如果与用户本轮最新表达冲突，以本轮为准。'

        type_labels = {
            CompanyUserMemoryItem.TYPE_IDENTITY: '身份',
            CompanyUserMemoryItem.TYPE_PREFERENCE: '偏好',
            CompanyUserMemoryItem.TYPE_CONSTRAINT: '约束',
            CompanyUserMemoryItem.TYPE_GOAL: '目标',
            CompanyUserMemoryItem.TYPE_FOLLOWUP: '待跟进',
        }
        line_budget = 220
        token_budget = max(int(config.get('maxRecallTokens') or 0), 120)
        redact_pii = not bool(config.get('allowSensitive'))
        lines: list[str] = []
        for item in items:
            content = CompanyService._sanitize_memory_summary_text(
                item.content,
                redact_pii=redact_pii,
                max_chars=line_budget,
            )
            if not content:
                continue
            lines.append(f"- {type_labels.get(item.memoryType, '记忆')}: {content}")
        if not lines:
            return ''

        kept_lines: list[str] = []
        running_tokens = CompanyService._estimate_agent_test_token_count(header)
        for line in lines:
            line_tokens = CompanyService._estimate_agent_test_token_count(line)
            if kept_lines and running_tokens + line_tokens > token_budget:
                break
            if not kept_lines and running_tokens + line_tokens > token_budget:
                trimmed_line = CompanyService._trim_text_to_estimated_tokens(
                    line,
                    max(token_budget - running_tokens, 24),
                )
                if trimmed_line:
                    kept_lines.append(trimmed_line)
                break
            kept_lines.append(line)
            running_tokens += line_tokens
        if not kept_lines:
            return ''
        return CompanyService._trim_text_to_estimated_tokens(
            '\n'.join([header] + kept_lines),
            token_budget,
        )

    @staticmethod
    def _build_public_client_entry_user_memory_key(
        *,
        memory_type: str,
        scope: str,
        agent: Optional[TransparentCompanyAgentDefinition],
        content: str,
    ) -> str:
        normalized = re.sub(r'\s+', ' ', str(content or '')).strip().lower()
        digest = hashlib.sha1(
            f"{memory_type}|{scope}|{getattr(agent, 'id', '')}|{normalized}".encode('utf-8')
        ).hexdigest()[:24]
        return f"{memory_type}:{digest}"

    @staticmethod
    def _create_public_client_entry_user_memory_write_log(
        session: TransparentCompanyAgentTestSession,
        *,
        source_message: Optional[TransparentCompanyAgentTestMessage],
        action: str,
        candidate_payload: dict[str, Any],
        result_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        if not session.operator_id:
            return
        normalized_candidate_payload = dict(candidate_payload or {})
        candidate_agent = normalized_candidate_payload.get('agent')
        if isinstance(candidate_agent, TransparentCompanyAgentDefinition):
            normalized_candidate_payload['agent'] = candidate_agent.agentId or candidate_agent.id
        CompanyUserMemoryWriteLog.objects.create(
            company=session.transparentCompany.company,
            user=session.operator,
            agent=session.agent,
            session=session,
            sourceMessageId=getattr(source_message, 'id', None),
            action=action,
            candidatePayload=normalized_candidate_payload,
            resultPayload=result_payload or {},
            created_at=CompanyService._now_str(),
        )

    @staticmethod
    def _build_public_client_entry_user_memory_extractor_messages(
        session: TransparentCompanyAgentTestSession,
        *,
        source_message: TransparentCompanyAgentTestMessage,
        config: dict[str, Any],
    ) -> tuple[list[dict[str, str]], str]:
        memory_scope = str(config.get('memoryScope') or '').strip()
        if memory_scope == TransparentCompanyAgentUserMemoryBinding.SCOPE_AGENT_USER:
            scope_hint = 'agent'
            scope_desc = '仅当前公开 Agent 可见'
        else:
            scope_hint = 'company'
            scope_desc = '同公司公开 Agent 可共享'

        system_prompt = (
            'You are a background memory extractor for a public customer-service chat. '
            'Return JSON only. Do not answer the user, do not roleplay, do not use markdown, '
            'and do not call tools. '
            'Extract only stable user memory candidates. '
            'Ignore business knowledge and ephemeral requests.'
        )
        schema_prompt = (
            'Output exactly one JSON object with this shape: '
            '{"items":[{"action":"create|update|suppress|skip","memory_type":"identity|preference|constraint|goal|followup",'
            '"scope":"company|agent","content":"string","confidence":0.0,"target_memory_key":"string","reason":"string"}]}. '
            'If nothing is worth keeping, return {"items":[]}.'
        )
        policy_prompt = (
            'Rules: only keep stable facts, durable preferences, enduring constraints, long-term goals, or genuine follow-up items. '
            'Prefer skip when uncertain. Never extract company knowledge or facts about other users. '
            f'Use scope "{scope_hint}" by default for this request ({scope_desc}).'
        )

        raw_messages = CompanyService._serialize_public_client_entry_raw_messages_for_runtime(session)
        recent_messages = raw_messages[-6:]
        conversation_lines: list[str] = []
        for item in recent_messages:
            role = str(item.get('role') or '').strip()
            content = CompanyService._sanitize_memory_summary_text(
                item.get('content') or '',
                redact_pii=not bool(config.get('allowSensitive')),
                max_chars=220,
            )
            if role and content:
                conversation_lines.append(f"- {role}: {content}")

        messages: list[dict[str, str]] = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'system', 'content': schema_prompt},
            {'role': 'system', 'content': policy_prompt},
        ]

        if session.memorySummary:
            messages.append(
                {
                    'role': 'system',
                    'content': f"Conversation summary:\n{session.memorySummary}",
                }
            )

        existing_memory_block = CompanyService._build_public_client_entry_user_memory_block(session)
        if existing_memory_block:
            messages.append(
                {
                    'role': 'system',
                    'content': f"Existing long-term memory:\n{existing_memory_block}",
                }
            )

        if conversation_lines:
            messages.append(
                {
                    'role': 'system',
                    'content': "Recent conversation:\n" + '\n'.join(conversation_lines),
                }
            )

        latest_user_message = CompanyService._sanitize_memory_summary_text(
            getattr(source_message, 'content', '') or '',
            redact_pii=not bool(config.get('allowSensitive')),
            max_chars=500,
        )
        content = (
            "Review the latest user message and the provided context. "
            "Suggest structured long-term memory actions only if strongly justified.\n"
            f"Latest user message:\n{latest_user_message}"
        )
        return messages, content

    @staticmethod
    def _execute_public_client_entry_user_memory_extractor_via_runtime(
        session: TransparentCompanyAgentTestSession,
        *,
        source_message: TransparentCompanyAgentTestMessage,
        config: dict[str, Any],
    ):
        from asgiref.sync import async_to_sync

        from apps.agent_runtime.contracts import ModelRequest
        from apps.agent_runtime.enums import ModelEventType
        from apps.agent_runtime.providers.model import (
            CompanyModelResolver,
            OpenAICompatibleModelGateway,
        )

        messages, content = CompanyService._build_public_client_entry_user_memory_extractor_messages(
            session,
            source_message=source_message,
            config=config,
        )
        model_messages = [*messages, {"role": "user", "content": content}]
        try:
            selection = CompanyModelResolver().resolve(session.transparentCompany.company)
        except Exception as exc:
            logger.warning(
                '[agent-runtime] memory extractor model config unavailable cid=%s session=%s error_type=%s',
                session.transparentCompany.company.cid,
                session.pk,
                exc.__class__.__name__,
            )
            return False, '统一运行时模型配置不可用'

        request = ModelRequest(
            run_id=f"memory-extractor-{session.pk}-{getattr(source_message, 'pk', 0) or 0}",
            provider=selection.provider,
            model=selection.model,
            messages=model_messages,
            tools=[],
            temperature=0.0,
            max_output_tokens=512,
            timeout_seconds=selection.timeout_seconds,
            credential_ref=selection.credential_ref,
        )

        async def collect_response():
            content_parts: list[str] = []
            token_count = 0
            async for event in OpenAICompatibleModelGateway().stream(request):
                event_type = str(event.event_type)
                data = dict(event.data or {})
                if event_type == str(ModelEventType.TEXT_DELTA):
                    content_parts.append(str(data.get('delta') or ''))
                elif event_type == str(ModelEventType.USAGE):
                    token_count = int(data.get('total_tokens') or token_count or 0)
                elif event_type == str(ModelEventType.ERROR):
                    return False, str(data.get('message') or '统一运行时模型调用失败')
            return True, {
                'assistant_content': ''.join(content_parts),
                'token_count': token_count,
                'runtime': 'unified',
            }

        try:
            return async_to_sync(collect_response)()
        except Exception as exc:
            logger.warning(
                '[agent-runtime] memory extractor failed cid=%s session=%s error_type=%s',
                session.transparentCompany.company.cid,
                session.pk,
                exc.__class__.__name__,
            )
            return False, '统一运行时模型调用失败'

    @staticmethod
    def _parse_public_client_entry_user_memory_extractor_output(
        session: TransparentCompanyAgentTestSession,
        *,
        extractor_content: Any,
        config: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_text = str(extractor_content or '').strip()
        if not raw_text:
            raise ValueError('extractor output is empty')

        payload_obj: Any
        try:
            payload_obj = json.loads(raw_text)
        except ValueError:
            fenced_match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', raw_text, re.DOTALL)
            if not fenced_match:
                raise ValueError('extractor output is not valid json')
            payload_obj = json.loads(fenced_match.group(1))

        items = payload_obj.get('items') if isinstance(payload_obj, dict) else None
        if not isinstance(items, list):
            raise ValueError('extractor output items must be a list')

        allowed_actions = {
            CompanyUserMemoryWriteLog.ACTION_CREATE,
            CompanyUserMemoryWriteLog.ACTION_UPDATE,
            CompanyUserMemoryWriteLog.ACTION_SUPPRESS,
            CompanyUserMemoryWriteLog.ACTION_SKIP,
        }
        allowed_types = {
            CompanyUserMemoryItem.TYPE_IDENTITY,
            CompanyUserMemoryItem.TYPE_PREFERENCE,
            CompanyUserMemoryItem.TYPE_CONSTRAINT,
            CompanyUserMemoryItem.TYPE_GOAL,
            CompanyUserMemoryItem.TYPE_FOLLOWUP,
        }
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            action = str(item.get('action') or '').strip().lower()
            if action not in allowed_actions:
                action = CompanyUserMemoryWriteLog.ACTION_SKIP

            memory_type = str(item.get('memory_type') or item.get('memoryType') or '').strip().lower()
            if memory_type not in allowed_types:
                memory_type = CompanyUserMemoryItem.TYPE_PREFERENCE

            requested_scope = str(item.get('scope') or '').strip().lower()
            memory_scope = str(config.get('memoryScope') or '').strip()
            if memory_scope == TransparentCompanyAgentUserMemoryBinding.SCOPE_AGENT_USER:
                scope = CompanyUserMemoryItem.SCOPE_AGENT
                agent = session.agent
            else:
                if requested_scope == CompanyUserMemoryItem.SCOPE_AGENT:
                    scope = CompanyUserMemoryItem.SCOPE_AGENT
                    agent = session.agent
                else:
                    scope = CompanyUserMemoryItem.SCOPE_COMPANY
                    agent = None

            content = CompanyService._sanitize_memory_summary_text(
                item.get('content') or '',
                redact_pii=not bool(config.get('allowSensitive')),
                max_chars=255,
            )
            try:
                confidence = float(item.get('confidence') or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = max(0.0, min(confidence, 1.0))
            reason = CompanyService._sanitize_memory_summary_text(
                item.get('reason') or '',
                redact_pii=False,
                max_chars=255,
            )
            target_memory_key = str(item.get('target_memory_key') or item.get('targetMemoryKey') or '').strip()
            normalized_item: dict[str, Any] = {
                'action': action,
                'memoryType': memory_type,
                'scope': scope,
                'agent': agent,
                'content': content,
                'confidence': confidence,
                'reason': reason,
                'targetMemoryKey': target_memory_key,
            }
            if content:
                normalized_item['memoryKey'] = CompanyService._build_public_client_entry_user_memory_key(
                    memory_type=memory_type,
                    scope=scope,
                    agent=agent,
                    content=content,
                )
            normalized_items.append(normalized_item)
        return normalized_items

    @staticmethod
    def _refresh_public_client_entry_user_memory(
        session: TransparentCompanyAgentTestSession,
        *,
        source_message: Optional[TransparentCompanyAgentTestMessage],
    ) -> None:
        if source_message is None or not session.operator_id:
            return

        config = CompanyService._resolve_public_client_entry_user_memory_config(
            session.transparentCompany,
            session.agent,
        )
        if not config.get('writebackEnabled'):
            return

        writeback_mode = str(
            config.get('writebackMode') or CompanyUserMemoryPolicy.WRITEBACK_MODE_SHADOW
        )
        min_confidence = float(config.get('writeMinConfidence') or 0.9)
        ok, extractor_response = CompanyService._execute_public_client_entry_user_memory_extractor_via_runtime(
            session,
            source_message=source_message,
            config=config,
        )
        if not ok:
            CompanyService._create_public_client_entry_user_memory_write_log(
                session,
                source_message=source_message,
                action=CompanyUserMemoryWriteLog.ACTION_SKIP,
                candidate_payload={
                    'mode': 'structured_shadow_extractor',
                    'writebackMode': writeback_mode,
                },
                result_payload={
                    'reason': 'extractor_runtime_error',
                    'writebackMode': writeback_mode,
                    'detail': sanitize_user_visible_error(extractor_response),
                },
            )
            return
        try:
            candidates = CompanyService._parse_public_client_entry_user_memory_extractor_output(
                session,
                extractor_content=(extractor_response or {}).get('assistant_content') or '',
                config=config,
            )
        except ValueError as exc:
            CompanyService._create_public_client_entry_user_memory_write_log(
                session,
                source_message=source_message,
                action=CompanyUserMemoryWriteLog.ACTION_SKIP,
                candidate_payload={
                    'mode': 'structured_shadow_extractor',
                    'writebackMode': writeback_mode,
                },
                result_payload={
                    'reason': 'extractor_invalid_json',
                    'writebackMode': writeback_mode,
                    'detail': str(exc),
                },
            )
            return
        if not candidates:
            return

        for candidate in candidates:
            confidence = float(candidate.get('confidence') or 0)
            extractor_action = str(candidate.get('action') or CompanyUserMemoryWriteLog.ACTION_SKIP)
            if confidence < min_confidence:
                CompanyService._create_public_client_entry_user_memory_write_log(
                    session,
                    source_message=source_message,
                    action=CompanyUserMemoryWriteLog.ACTION_SKIP,
                    candidate_payload=candidate,
                    result_payload={
                        'reason': 'low_confidence',
                        'min_confidence': min_confidence,
                        'shadowMode': True,
                        'writebackMode': writeback_mode,
                        'extractorAction': extractor_action,
                    },
                )
                continue

            CompanyService._create_public_client_entry_user_memory_write_log(
                session,
                source_message=source_message,
                action=CompanyUserMemoryWriteLog.ACTION_SKIP,
                candidate_payload=candidate,
                result_payload={
                    'reason': 'shadow_mode',
                    'shadowMode': True,
                    'writebackMode': writeback_mode,
                    'extractorAction': extractor_action,
                    'memoryKey': candidate.get('memoryKey') or '',
                    'targetMemoryKey': candidate.get('targetMemoryKey') or '',
                    'scope': candidate.get('scope') or '',
                    'memoryType': candidate.get('memoryType') or '',
                },
            )

    @staticmethod
    def _serialize_public_client_entry_raw_messages_for_runtime(
        session: TransparentCompanyAgentTestSession,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for item in session.messages.order_by('id'):
            role = str(item.role or '').strip().lower()
            content = str(item.content or '')
            if role not in {'user', 'assistant'} or not content:
                continue
            messages.append(
                {
                    'role': role,
                    'content': content,
                }
            )
        return messages

    @staticmethod
    def _estimate_runtime_messages_token_count(messages: Iterable[dict[str, str]]) -> int:
        total = 0
        for item in messages or []:
            role = str((item or {}).get('role') or '').strip()
            content = str((item or {}).get('content') or '')
            total += 6 + CompanyService._estimate_agent_test_token_count(f"{role}: {content}")
        return total

    @staticmethod
    def _trim_text_to_estimated_tokens(text: str, max_tokens: int) -> str:
        normalized = str(text or '')
        budget = max(int(max_tokens or 0), 1)
        if CompanyService._estimate_agent_test_token_count(normalized) <= budget:
            return normalized
        low = 0
        high = len(normalized)
        best = ''
        while low <= high:
            middle = (low + high) // 2
            candidate = normalized[:middle].rstrip()
            candidate_text = f"{candidate}..." if candidate else ''
            if CompanyService._estimate_agent_test_token_count(candidate_text) <= budget:
                best = candidate_text
                low = middle + 1
            else:
                high = middle - 1
        return best

    @staticmethod
    def _sanitize_memory_summary_text(
        text: Any,
        *,
        redact_pii: bool = False,
        max_chars: int = 160,
    ) -> str:
        normalized = re.sub(r'\s+', ' ', str(text or '')).strip()
        if not normalized:
            return ''
        if redact_pii:
            normalized = re.sub(
                r'([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})',
                '[redacted-email]',
                normalized,
            )
            normalized = re.sub(r'(?<!\d)(?:\+?\d[\d -]{6,}\d)(?!\d)', '[redacted-phone]', normalized)
        if len(normalized) > max_chars:
            normalized = f"{normalized[: max_chars - 3].rstrip()}..."
        return normalized

    @staticmethod
    def _build_public_client_entry_session_summary(
        messages: list[dict[str, str]],
        *,
        summary_style: str,
        summary_max_tokens: int,
        redact_pii: bool,
    ) -> str:
        if not messages:
            return ''
        line_char_limit = 220 if summary_style == CompanySessionMemoryPolicy.SUMMARY_STYLE_DETAILED else 120
        raw_lines: list[str] = []
        for item in messages:
            role = str(item.get('role') or '').strip().lower()
            content = CompanyService._sanitize_memory_summary_text(
                item.get('content') or '',
                redact_pii=redact_pii,
                max_chars=line_char_limit,
            )
            if role not in {'user', 'assistant'} or not content:
                continue
            role_label = '用户' if role == 'user' else '助手'
            raw_lines.append(f"- {role_label}: {content}")
        if not raw_lines:
            return ''

        token_budget = max(int(summary_max_tokens or 0), 120)
        header = "以下是当前公开会话较早历史的压缩摘要。请把它作为上下文事实使用，不要逐字复述。"
        kept_lines: list[str] = []
        running_tokens = CompanyService._estimate_agent_test_token_count(header)
        for line in reversed(raw_lines):
            line_tokens = CompanyService._estimate_agent_test_token_count(line)
            if kept_lines and running_tokens + line_tokens > token_budget:
                break
            if not kept_lines and running_tokens + line_tokens > token_budget:
                trimmed_line = CompanyService._trim_text_to_estimated_tokens(
                    line,
                    max(token_budget - running_tokens, 24),
                )
                if trimmed_line:
                    kept_lines.append(trimmed_line)
                break
            kept_lines.append(line)
            running_tokens += line_tokens
        kept_lines.reverse()
        omitted_count = max(len(raw_lines) - len(kept_lines), 0)
        summary_lines = [header]
        if omitted_count:
            summary_lines.append(f"- 更早还有 {omitted_count} 条历史已压缩省略。")
        summary_lines.extend(kept_lines)
        summary_text = '\n'.join(line for line in summary_lines if line)
        return CompanyService._trim_text_to_estimated_tokens(summary_text, token_budget)

    @staticmethod
    def _build_public_client_entry_runtime_messages(
        session: TransparentCompanyAgentTestSession,
        raw_messages: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        company_id = str(getattr(getattr(session, 'transparentCompany', None), 'company_id', '') or '')
        agent_id = str(getattr(session, 'agent_id', '') or '')
        operator_id = str(getattr(session, 'operator_id', '') or '')
        conversation_id = str(getattr(session, 'conversationId', '') or '')
        memory_config = CompanyService._resolve_public_client_entry_memory_config(
            session.transparentCompany,
            session.agent,
        )
        user_memory_block = ''
        if CompanyService._public_client_entry_user_memory_mainline_enabled():
            user_memory_block = CompanyService._build_public_client_entry_user_memory_block(session)
        preface_messages: list[dict[str, str]] = []
        if user_memory_block:
            preface_messages.append({'role': 'system', 'content': user_memory_block})

        if not raw_messages:
            logger.info(
                "[public-client-entry-memory] runtime-messages-empty-history company=%s agent=%s user=%s conversation=%s l1_enabled=%s",
                company_id,
                agent_id,
                operator_id,
                conversation_id,
                bool(memory_config.get('enabled')),
            )
            return preface_messages
        full_history_tokens = CompanyService._estimate_runtime_messages_token_count(raw_messages)
        if not memory_config.get('enabled'):
            logger.info(
                "[public-client-entry-memory] runtime-messages-full-replay company=%s agent=%s user=%s conversation=%s reason=%s full_history_tokens=%s final_payload_tokens=%s",
                company_id,
                agent_id,
                operator_id,
                conversation_id,
                'memory_disabled',
                full_history_tokens,
                CompanyService._estimate_runtime_messages_token_count(preface_messages + raw_messages),
            )
            return preface_messages + raw_messages

        summary_trigger_tokens = max(int(memory_config.get('summaryTriggerTokens') or 0), 1)
        if full_history_tokens <= summary_trigger_tokens:
            logger.info(
                "[public-client-entry-memory] runtime-messages-full-replay company=%s agent=%s user=%s conversation=%s reason=%s full_history_tokens=%s final_payload_tokens=%s",
                company_id,
                agent_id,
                operator_id,
                conversation_id,
                'below_trigger',
                full_history_tokens,
                CompanyService._estimate_runtime_messages_token_count(preface_messages + raw_messages),
            )
            return preface_messages + raw_messages

        recent_window = max(int(memory_config.get('recentMessageWindow') or 0), 0)
        recent_messages = list(raw_messages[-recent_window:]) if recent_window > 0 else []
        summary_cutoff = max(len(raw_messages) - len(recent_messages), 0)
        if summary_cutoff <= 0:
            logger.info(
                "[public-client-entry-memory] runtime-messages-full-replay company=%s agent=%s user=%s conversation=%s reason=%s full_history_tokens=%s final_payload_tokens=%s",
                company_id,
                agent_id,
                operator_id,
                conversation_id,
                'empty_summary_cutoff',
                full_history_tokens,
                CompanyService._estimate_runtime_messages_token_count(preface_messages + raw_messages),
            )
            return preface_messages + raw_messages

        summary_source = 'stored'
        summary_rebuild_reason = ''
        if session.memorySummary and int(session.memorySummaryMessageCount or 0) == summary_cutoff:
            summary_text = str(session.memorySummary or '')
        else:
            summary_source = 'rebuilt'
            summary_rebuild_reason = 'count_mismatch' if session.memorySummary else 'missing'
            summary_text = CompanyService._build_public_client_entry_session_summary(
                raw_messages[:summary_cutoff],
                summary_style=str(memory_config.get('summaryStyle') or CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE),
                summary_max_tokens=int(memory_config.get('summaryMaxTokens') or 800),
                redact_pii=bool(memory_config.get('redactPii')),
            )
        payload: list[dict[str, str]] = []
        if summary_text:
            payload.append({'role': 'system', 'content': summary_text})
        payload.extend(preface_messages)

        max_tokens = max(int(memory_config.get('maxTokens') or 0), 1)
        bounded_recent_messages = list(recent_messages)
        while bounded_recent_messages and CompanyService._estimate_runtime_messages_token_count(
            payload + bounded_recent_messages
        ) > max_tokens:
            bounded_recent_messages = bounded_recent_messages[1:]

        if payload and CompanyService._estimate_runtime_messages_token_count(payload) > max_tokens:
            fitted_payload: list[dict[str, str]] = []
            for item in payload:
                remaining_budget = max_tokens - CompanyService._estimate_runtime_messages_token_count(fitted_payload)
                if remaining_budget <= 8:
                    break
                trimmed_content = CompanyService._trim_text_to_estimated_tokens(
                    str(item.get('content') or ''),
                    max(remaining_budget - 6, 16),
                )
                if trimmed_content:
                    fitted_payload.append({'role': str(item.get('role') or 'system'), 'content': trimmed_content})
            payload = fitted_payload

        final_messages = payload + bounded_recent_messages
        logger.info(
            "[public-client-entry-memory] runtime-messages-summary company=%s agent=%s user=%s conversation=%s summary_source=%s summary_rebuild_reason=%s full_history_tokens=%s final_payload_tokens=%s summary_cutoff=%s recent_window=%s recent_trimmed=%s",
            company_id,
            agent_id,
            operator_id,
            conversation_id,
            summary_source,
            summary_rebuild_reason,
            full_history_tokens,
            CompanyService._estimate_runtime_messages_token_count(final_messages),
            summary_cutoff,
            recent_window,
            len(bounded_recent_messages) < len(recent_messages),
        )
        return final_messages

    @staticmethod
    def _refresh_public_client_entry_session_memory_summary(
        session: TransparentCompanyAgentTestSession,
    ) -> None:
        raw_messages = CompanyService._serialize_public_client_entry_raw_messages_for_runtime(session)
        memory_config = CompanyService._resolve_public_client_entry_memory_config(
            session.transparentCompany,
            session.agent,
        )
        summary_text = ''
        summarized_count = 0
        summary_updated_at = ''
        refresh_reason = 'memory_disabled'
        full_history_tokens = 0

        if raw_messages and memory_config.get('enabled'):
            full_history_tokens = CompanyService._estimate_runtime_messages_token_count(raw_messages)
            summary_trigger_tokens = max(int(memory_config.get('summaryTriggerTokens') or 0), 1)
            recent_window = max(int(memory_config.get('recentMessageWindow') or 0), 0)
            recent_messages = list(raw_messages[-recent_window:]) if recent_window > 0 else []
            summarized_count = max(len(raw_messages) - len(recent_messages), 0)
            if full_history_tokens > summary_trigger_tokens and summarized_count > 0:
                summary_text = CompanyService._build_public_client_entry_session_summary(
                    raw_messages[:summarized_count],
                    summary_style=str(memory_config.get('summaryStyle') or CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE),
                    summary_max_tokens=int(memory_config.get('summaryMaxTokens') or 800),
                    redact_pii=bool(memory_config.get('redactPii')),
                )
                if summary_text:
                    summary_updated_at = CompanyService._now_str()
                    refresh_reason = 'summary_refreshed'
                else:
                    refresh_reason = 'summary_empty'
            else:
                refresh_reason = 'below_trigger'
        elif not raw_messages:
            refresh_reason = 'no_messages'

        session.memorySummary = summary_text
        session.memorySummaryMessageCount = summarized_count if summary_text else 0
        session.memorySummaryUpdatedAt = summary_updated_at
        session.save(update_fields=['memorySummary', 'memorySummaryMessageCount', 'memorySummaryUpdatedAt'])
        logger.info(
            "[public-client-entry-memory] summary-refresh company=%s agent=%s user=%s conversation=%s refresh_success=%s summarized_count=%s full_history_tokens=%s reason=%s",
            str(getattr(getattr(session, 'transparentCompany', None), 'company_id', '') or ''),
            str(getattr(session, 'agent_id', '') or ''),
            str(getattr(session, 'operator_id', '') or ''),
            str(getattr(session, 'conversationId', '') or ''),
            bool(summary_text),
            session.memorySummaryMessageCount,
            full_history_tokens,
            refresh_reason,
        )

    @staticmethod
    def _create_public_client_entry_session(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
        user: User,
    ) -> TransparentCompanyAgentTestSession:
        now = CompanyService._now_str()
        return TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=transparent_company,
            agent=agent,
            operator=user,
            conversationId=uuid.uuid4().hex,
            title=CompanyService._build_agent_test_session_title(agent, 'public'),
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
            messageCount=0,
            lastMessage='',
            sessionKey=uuid.uuid4().hex,
            source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _resolve_public_client_entry_session(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
        user: User,
        *,
        conversation_id: str = '',
    ) -> TransparentCompanyAgentTestSession:
        normalized_conversation_id = str(conversation_id or '').strip()
        session = None
        if normalized_conversation_id:
            session = CompanyService._get_public_client_entry_session(
                transparent_company,
                normalized_conversation_id,
                user,
            )
            if session is not None and session.agent_id != agent.id:
                session = None
        if session is None:
            session = CompanyService._get_latest_public_client_entry_session(
                transparent_company,
                agent,
                user,
            )
        if session is None:
            session = CompanyService._create_public_client_entry_session(
                transparent_company,
                agent,
                user,
            )
        return session

    @staticmethod
    def _serialize_public_client_entry_session_messages_for_runtime(
        session: TransparentCompanyAgentTestSession,
    ) -> list[dict[str, str]]:
        raw_messages = CompanyService._serialize_public_client_entry_raw_messages_for_runtime(session)
        return CompanyService._build_public_client_entry_runtime_messages(session, raw_messages)

    @staticmethod
    def _serialize_agent_test_messages_for_runtime(
        session: TransparentCompanyAgentTestSession,
        content: str,
    ) -> list[dict[str, str]]:
        company_name = (
            getattr(getattr(session, 'transparentCompany', None), 'company', None).company_name
            if getattr(getattr(session, 'transparentCompany', None), 'company', None)
            else ''
        )
        agent = session.agent
        transparent_company = getattr(session, 'transparentCompany', None)
        roster_lines: list[str] = []
        if transparent_company is not None:
            roster_queryset = TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company,
                enabled=True,
            ).order_by('isCoreAgent', 'exposureOrder', 'created_at', 'id')
            if hasattr(session, 'operator') and not can_access_formal_company(
                transparent_company.company, session.operator, Grant.PERM_ADMIN,
            ):
                allowed_ids = [
                    colleague.agentId for colleague in roster_queryset
                    if agent_allowed(
                        transparent_company.company,
                        session.operator,
                        colleague.agentId,
                    )
                ]
                roster_queryset = roster_queryset.filter(agentId__in=allowed_ids)
            for colleague in roster_queryset:
                visibility = '公开入口' if getattr(colleague, 'exposedToClient', False) else '内部员工'
                default_label = '，默认入口' if getattr(colleague, 'isDefaultEntry', False) else ''
                current_label = '（你自己）' if colleague.id == agent.id else ''
                roster_lines.append(
                    f"- {colleague.name or colleague.agentId} [{colleague.agentId}] / {colleague.role or 'agent'} / {visibility}{default_label}{current_label}"
                )
        identity_parts = [
            f"你当前正在扮演公司的 Agent：{agent.name or agent.agentId}。",
            f"公司名称：{company_name or '当前公司'}。",
            f"Agent ID：{agent.agentId}。",
        ]
        if agent.role:
            identity_parts.append(f"角色：{agent.role}。")
        if agent.description:
            identity_parts.append(f"角色说明：{agent.description}。")
        if agent.systemPrompt:
            identity_parts.append(f"系统提示词：{agent.systemPrompt}")
        identity_parts.extend(
            [
                "回答时必须始终以当前 Agent 的身份自我介绍和行动。",
                "不要说自己是空白身份、默认助手、OpenClaw 通用助手，也不要说还没有名字或身份文件为空。",
                "如果用户问“你是谁”或类似问题，直接说明你当前的公司身份、职责和对外定位。",
                "如果用户问公司里有哪些员工、有哪些 Agent、团队构成如何，优先按照下方名册回答，不要虚构不存在的员工或岗位。",
                AGENT_TEST_CHAT_STYLE_PROMPT,
            ]
        )

        if roster_lines:
            identity_parts.append("公司当前已启用的 Agent 名册：")
            identity_parts.extend(roster_lines)

        messages: list[dict[str, str]] = [
            {
                'role': 'system',
                'content': '\n'.join(part for part in identity_parts if part),
            }
        ]
        for item in session.messages.order_by('id'):
            from apps.agent_runtime.attachments import message_content_with_attachments
            messages.append(
                {
                    'role': item.role,
                    'content': message_content_with_attachments(
                        item.content or '',
                        (getattr(item, 'rawPayload', {}) or {}).get('attachments'),
                    ),
                }
            )
        return messages

    @staticmethod
    def _create_agent_test_message(
        session: TransparentCompanyAgentTestSession,
        *,
        role: str,
        content: str,
        token_count: int = 0,
        tool_calls: Optional[list] = None,
        raw_payload: Optional[dict] = None,
    ) -> TransparentCompanyAgentTestMessage:
        now = CompanyService._now_str()
        return TransparentCompanyAgentTestMessage.objects.create(
            session=session,
            role=role,
            content=content or '',
            tokenCount=token_count or 0,
            toolCalls=tool_calls or [],
            rawPayload=raw_payload or {},
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _update_agent_test_session_summary(
        session: TransparentCompanyAgentTestSession,
        *,
        last_message: str,
        status: Optional[str] = None,
    ) -> None:
        session.messageCount = session.messages.count()
        session.lastMessage = (last_message or '')[:2000]
        session.updated_at = CompanyService._now_str()
        if status:
            session.status = status
        session.save(update_fields=['messageCount', 'lastMessage', 'updated_at', 'status'])

    @staticmethod
    def list_transparent_company_test_sessions(
        cid: str,
        user: User,
        agent_id: Optional[str] = None,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg, scope = CompanyService._get_transparent_company_for_agent_access(cid, user, client_ip)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        queryset = (
            TransparentCompanyAgentTestSession.objects.filter(
                transparentCompany=transparent_company,
                source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
            )
            .select_related('agent', 'operator')
            .order_by('-id')
        )
        if agent_id:
            queryset = queryset.filter(agent__agentId=agent_id)
        if scope:
            queryset = queryset.filter(operator=user, agent__agentId__in=scope.agent_ids)
        return True, [
            CompanyService._get_agent_test_session_display_info(item)
            for item in queryset
        ]

    @staticmethod
    def create_transparent_company_test_session(
        cid: str,
        agent_id: str,
        user: User,
        title: str = '',
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg, scope = CompanyService._get_transparent_company_for_agent_access(cid, user, client_ip)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        if scope and not agent_allowed(transparent_company.company, user, agent_id):
            return False, '无权访问该 Agent'
        try:
            agent = TransparentCompanyAgentDefinition.objects.get(
                transparentCompany=transparent_company,
                agentId=agent_id,
            )
        except TransparentCompanyAgentDefinition.DoesNotExist:
            return False, 'Agent 不存在'

        now = CompanyService._now_str()
        session = TransparentCompanyAgentTestSession.objects.create(
            transparentCompany=transparent_company,
            agent=agent,
            operator=user,
            conversationId=uuid.uuid4().hex,
            title=CompanyService._build_agent_test_session_title(agent, title),
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
            messageCount=0,
            lastMessage='',
            sessionKey=uuid.uuid4().hex,
            source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
            created_at=now,
            updated_at=now,
        )
        return True, session.get_info()

    @staticmethod
    def get_transparent_company_test_messages(
        cid: str,
        conversation_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg, scope = CompanyService._get_transparent_company_for_agent_access(cid, user, client_ip)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        session = CompanyService._get_agent_test_session(transparent_company, conversation_id)
        if session is None:
            return False, '测试会话不存在'
        if scope and (session.operator_id != user.id or not agent_visible(transparent_company.company, user, session.agent.agentId)):
            return False, '无权访问该会话'
        return True, [item.get_info() for item in session.messages.order_by('id')]

    @staticmethod
    def _send_agent_test_message_via_runtime(
        transparent_company: TransparentCompany,
        session: TransparentCompanyAgentTestSession,
        content: str,
    ):
        ok, stream_or_message = CompanyService._stream_agent_test_message_via_runtime(
            transparent_company,
            session,
            content,
        )
        if not ok:
            return False, stream_or_message

        result = {
            'assistant_content': '',
            'token_count': 0,
            'tool_calls': [],
            'run_id': '',
        }
        for runtime_event in stream_or_message:
            event_type = str(runtime_event.get('event') or '').strip().lower()
            result['run_id'] = str(runtime_event.get('run_id') or result['run_id'])
            if event_type == 'delta':
                result['assistant_content'] += str(runtime_event.get('delta') or '')
            elif event_type == 'complete':
                result['assistant_content'] = str(
                    runtime_event.get('content') or result['assistant_content']
                )
                result['token_count'] = int(runtime_event.get('token_count') or 0)
                result['tool_calls'] = list(runtime_event.get('tool_calls') or [])
            elif event_type == 'error':
                return False, runtime_event
        if not result['assistant_content']:
            return False, 'Agent 未返回任何内容，请稍后重试'
        return True, result

    @staticmethod
    def _agent_runtime_session_revision(
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
    ) -> str:
        candidates = [
            str(getattr(agent, 'updated_at', '') or ''),
            str(getattr(transparent_company, 'updated_at', '') or ''),
        ]
        binding = (
            TransparentCompanyAgentPermissionBinding.objects.filter(
                transparentCompany=transparent_company,
                agent=agent,
            )
            .order_by('-id')
            .first()
        )
        if binding is not None:
            candidates.append(str(getattr(binding, 'updated_at', '') or ''))
        return '|'.join(item for item in candidates if item)

    @staticmethod
    def _stream_agent_test_message_via_runtime(
        transparent_company: TransparentCompany,
        session: TransparentCompanyAgentTestSession,
        content: str,
    ):
        return CompanyService._stream_agent_runtime_by_route(
            transparent_company=transparent_company,
            session=session,
            source='agent_test',
            request_payload={
                'agent_id': session.agent.agentId,
                'conversation_id': session.conversationId,
                'session_key': session.sessionKey or session.conversationId,
                'content': content,
            },
        )

    @staticmethod
    def _stream_client_entry_message_via_runtime(
        transparent_company: TransparentCompany,
        request_payload: dict[str, Any],
    ):
        session_id = request_payload.get('_runtime_session_id')
        input_message_id = request_payload.get('_runtime_input_message_id')
        session = (
            TransparentCompanyAgentTestSession.objects
            .select_related('agent', 'operator', 'transparentCompany__company')
            .filter(pk=session_id, transparentCompany=transparent_company)
            .first()
        )
        if session is None:
            return False, '会话不存在或不属于当前公司'
        input_message = session.messages.filter(
            pk=input_message_id,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
        ).first()
        if input_message is None:
            return False, '输入消息不存在或不属于当前会话'
        return CompanyService._stream_agent_runtime_by_route(
            transparent_company=transparent_company,
            session=session,
            source='public_client',
            input_message=input_message,
            request_payload=request_payload,
        )

    @staticmethod
    def _stream_agent_runtime_by_route(
        *,
        transparent_company: TransparentCompany,
        session: TransparentCompanyAgentTestSession,
        source: str,
        input_message: Optional[TransparentCompanyAgentTestMessage] = None,
        request_payload: Optional[dict[str, Any]] = None,
    ):
        from apps.agent_runtime.models import CompanyAgentRuntimePolicy
        from apps.agent_runtime.routing import RuntimeEngineRouter
        from apps.runtime.legacy_control import legacy_runtime_is_read_only

        router = RuntimeEngineRouter()
        route = router.select(
            company_id=transparent_company.company_id,
            session_id=session.pk,
        )
        if route.reason == 'missing_policy' and legacy_runtime_is_read_only():
            policy, created = CompanyAgentRuntimePolicy.objects.get_or_create(
                company=transparent_company.company,
                defaults={
                    'mode': CompanyAgentRuntimePolicy.MODE_UNIFIED,
                    'canary_percentage': 100,
                },
            )
            if created:
                logger.info(
                    '[agent-runtime] backfilled unified route policy cid=%s session=%s',
                    transparent_company.company.cid,
                    session.pk,
                )
            route = router.select_policy(
                policy,
                company_id=transparent_company.company_id,
                session_id=session.pk,
            )
        if route.engine == 'unified':
            return CompanyService._stream_unified_agent_runtime(
                transparent_company=transparent_company,
                session=session,
                source=source,
                input_message=input_message,
                route=route,
            )
        logger.error(
            '[agent-runtime] blocked retired OpenClaw online route cid=%s session=%s mode=%s reason=%s',
            transparent_company.company.cid,
            session.pk,
            route.policy_mode,
            route.reason,
        )
        return False, '统一运行时已全量切换，当前公司缺少 unified 路由策略，请联系管理员'

    @staticmethod
    def _stream_unified_agent_runtime(
        *,
        transparent_company: TransparentCompany,
        session: TransparentCompanyAgentTestSession,
        source: str,
        input_message: Optional[TransparentCompanyAgentTestMessage] = None,
        route=None,
    ):
        """Start the unified engine with trusted ORM objects only."""

        from apps.agent_runtime.runtime import UnifiedRuntimeService

        if input_message is None:
            input_message = session.messages.filter(
                role=TransparentCompanyAgentTestMessage.ROLE_USER,
            ).order_by('-id').first()
        if input_message is None:
            return False, '输入消息不存在或不属于当前会话'
        try:
            return True, UnifiedRuntimeService().stream(
                company=transparent_company.company,
                agent=session.agent,
                session=session,
                input_message=input_message,
                source=source,
            )
        except Exception as exc:
            # Exception strings from SDK/configuration paths may contain an
            # endpoint or response body. Log only the class at this boundary.
            logger.warning(
                '[agent-runtime] unified startup failed cid=%s session=%s error_type=%s',
                transparent_company.company.cid,
                session.pk,
                exc.__class__.__name__,
            )
            return False, '统一运行时暂时不可用，请稍后重试'

    @staticmethod
    def _attach_agent_runtime_output(runtime_event: dict[str, Any], assistant_message) -> None:
        """Associate CompanyService's assistant message with its AgentRun."""

        run_id = str(runtime_event.get('run_id') or '').strip()
        if not run_id:
            return
        from apps.agent_runtime.runtime import attach_output_message

        attach_output_message(run_id, assistant_message)

    @staticmethod
    def _resolve_agent_test_session_for_send(
        transparent_company: TransparentCompany,
        conversation_id: str,
        agent_id: str,
        user: User,
        title: str = '',
    ) -> tuple[bool, TransparentCompanyAgentTestSession | str]:
        session = None
        if conversation_id:
            session = CompanyService._get_agent_test_session(transparent_company, conversation_id)
            if session is None:
                return False, '工作会话不存在'

        if session is None or (agent_id and agent_id != session.agent.agentId):
            if not agent_id:
                return False, 'Agent 不存在'
            try:
                agent = TransparentCompanyAgentDefinition.objects.get(
                    transparentCompany=transparent_company,
                    agentId=agent_id,
                )
            except TransparentCompanyAgentDefinition.DoesNotExist:
                return False, 'Agent 不存在'
            now = CompanyService._now_str()
            session = TransparentCompanyAgentTestSession.objects.create(
                transparentCompany=transparent_company,
                agent=agent,
                operator=user,
                conversationId=uuid.uuid4().hex,
                title=CompanyService._build_agent_test_session_title(agent, title),
                status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
                messageCount=0,
                lastMessage='',
                sessionKey=uuid.uuid4().hex,
                source=TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
                created_at=now,
                updated_at=now,
            )
        return True, session

    @staticmethod
    def _scoped_agent_test_send_allowed(
        transparent_company: TransparentCompany,
        user: User,
        agent_id: str,
        conversation_id: str,
    ) -> bool:
        company = transparent_company.company
        if agent_id and not agent_allowed(company, user, agent_id):
            return False
        if conversation_id:
            session = CompanyService._get_agent_test_session(transparent_company, conversation_id)
            return bool(
                session and session.operator_id == user.id
                and agent_allowed(company, user, session.agent.agentId)
            )
        return bool(agent_id and agent_allowed(company, user, agent_id))

    @staticmethod
    def _extract_agent_test_runtime_error(response_payload: dict[str, Any]) -> str:
        return sanitize_user_visible_error(response_payload)

    @staticmethod
    def stream_transparent_company_client_entry_message(
        cid: str,
        content: str,
        user: User,
        *,
        entry_key: str = '',
        route_key: str = '',
        conversation_id: str = '',
        session_key: str = '',
        messages: Any = None,
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_client_entry(cid)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        ok, binding_or_msg = CompanyService.resolve_transparent_company_client_entry_agent(
            cid,
            entry_key=entry_key,
            route_key=route_key,
        )
        if not ok:
            return False, binding_or_msg

        binding = binding_or_msg
        agent_id = str(binding.get('agent_id') or '').strip()
        try:
            agent = TransparentCompanyAgentDefinition.objects.get(
                transparentCompany=transparent_company,
                agentId=agent_id,
            )
        except TransparentCompanyAgentDefinition.DoesNotExist:
            return False, '公开 Agent 不存在'

        session = CompanyService._resolve_public_client_entry_session(
            transparent_company,
            agent,
            user,
            conversation_id=conversation_id,
        )
        resolved_conversation_id = session.conversationId
        resolved_session_key = session.sessionKey or str(session_key or '').strip() or resolved_conversation_id
        moderation_ok, moderation_msg = _moderate_company_texts(
            items=[("content", content)],
            source=TextModerationAsset.SOURCE_PUBLIC_CLIENT_ENTRY,
            owner_id=cid,
            actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            return False, moderation_msg

        runtime_messages = CompanyService._serialize_public_client_entry_session_messages_for_runtime(session)
        if not runtime_messages:
            runtime_messages = CompanyService._serialize_client_entry_messages_for_runtime(messages)
        user_message = CompanyService._create_agent_test_message(
            session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content=content,
        )

        ok, runtime_stream_or_msg = CompanyService._stream_client_entry_message_via_runtime(
            transparent_company,
            request_payload={
                'entry_key': str(binding.get('entry_key') or ''),
                'route_key': str(binding.get('route_key') or ''),
                'conversation_id': resolved_conversation_id,
                'session_key': resolved_session_key,
                'messages': runtime_messages,
                'content': content,
                'native_session_isolation': True,
                'native_session_revision': CompanyService._agent_runtime_session_revision(
                    transparent_company,
                    agent,
                ),
                'session_source': 'public-client-entry',
                # Trusted internal identifiers used only by the unified route;
                # they are removed before invoking the legacy adapter.
                '_runtime_session_id': session.pk,
                '_runtime_input_message_id': user_message.pk,
            },
        )
        if not ok:
            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            return False, sanitize_user_visible_error(runtime_stream_or_msg)

        runtime_stream = runtime_stream_or_msg
        user_memory_mainline_enabled = CompanyService._public_client_entry_user_memory_mainline_enabled()

        def iter_events():
            accumulated_content = ''
            token_count = 0
            agent_payload = {
                'agent_id': str(binding.get('agent_id') or ''),
                'name': str(binding.get('name') or ''),
                'entry_key': str(binding.get('entry_key') or ''),
                'route_key': str(binding.get('route_key') or ''),
            }
            yield {
                'event': 'start',
                'data': {
                    'conversation_id': resolved_conversation_id,
                    'session_key': resolved_session_key,
                    'agent': agent_payload,
                    'user_message': user_message.get_info(),
                },
            }
            try:
                for runtime_event in runtime_stream:
                    if not isinstance(runtime_event, dict):
                        continue
                    event_type = str(runtime_event.get('event') or '').strip().lower()
                    if event_type == 'start':
                        yield {
                            'event': 'status',
                            'data': {
                                'message': '已连接公开智能体，正在等待回复。',
                            },
                        }
                        continue
                    if event_type == 'status':
                        status_message = sanitize_assistant_content_for_user(
                            runtime_event.get('message') or runtime_event.get('status') or ''
                        )
                        if status_message:
                            yield {
                                'event': 'status',
                                'data': {
                                    'message': status_message,
                                },
                            }
                        continue
                    if event_type == 'delta':
                        delta = sanitize_assistant_content_for_user(runtime_event.get('delta') or '')
                        if not delta:
                            continue
                        accumulated_content += delta
                        reasoning_content = _split_assistant_reasoning_content(accumulated_content)
                        yield {
                            'event': 'delta',
                            'data': {
                                'delta': delta,
                                'content': accumulated_content,
                                **reasoning_content,
                            },
                        }
                        continue
                    if event_type == 'complete':
                        final_content = sanitize_assistant_content_for_user(
                            runtime_event.get('content') or accumulated_content
                        )
                        if not final_content:
                            raise ValueError('智能体暂未返回内容，请稍后重试')
                        if final_content != accumulated_content:
                            if final_content.startswith(accumulated_content):
                                remaining_content = final_content[len(accumulated_content):]
                            else:
                                remaining_content = final_content
                                accumulated_content = ''
                            for delta in CompanyService._iter_agent_test_text_chunks(remaining_content):
                                accumulated_content += delta
                                reasoning_content = _split_assistant_reasoning_content(accumulated_content)
                                yield {
                                    'event': 'delta',
                                    'data': {
                                        'delta': delta,
                                        'content': accumulated_content,
                                        **reasoning_content,
                                    },
                                }
                                time.sleep(0.02)
                        else:
                            accumulated_content = final_content
                        token_count = CompanyService._coerce_agent_test_stream_usage_token_count(runtime_event)
                        assistant_message = CompanyService._create_agent_test_message(
                            session,
                            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
                            content=accumulated_content,
                            token_count=token_count,
                            tool_calls=sanitize_tool_calls(runtime_event.get('tool_calls')),
                            raw_payload={},
                        )
                        CompanyService._attach_agent_runtime_output(
                            runtime_event,
                            assistant_message,
                        )
                        CompanyService._update_agent_test_session_summary(
                            session,
                            last_message=accumulated_content or content,
                            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
                        )
                        CompanyService._refresh_public_client_entry_session_memory_summary(session)
                        if user_memory_mainline_enabled:
                            CompanyService._refresh_public_client_entry_user_memory(
                                session,
                                source_message=user_message,
                            )
                        yield {
                            'event': 'complete',
                            'data': {
                                'content': accumulated_content,
                                'token_count': token_count,
                                'conversation_id': resolved_conversation_id,
                                'session_key': resolved_session_key,
                                'agent': agent_payload,
                                'tool_calls': sanitize_tool_calls(runtime_event.get('tool_calls')),
                                'assistant_message': assistant_message.get_info(),
                                'conversation': session.get_info(),
                            },
                        }
                        return
                    if event_type == 'error':
                        raise ValueError(sanitize_user_visible_error(runtime_event.get('message')))
            except Exception as exc:
                CompanyService._update_agent_test_session_summary(
                    session,
                    last_message=content,
                    status=TransparentCompanyAgentTestSession.STATUS_FAILED,
                )
                yield {
                    'event': 'error',
                    'data': {
                        'message': sanitize_user_visible_error(exc),
                        'content': accumulated_content,
                        'conversation_id': resolved_conversation_id,
                        'session_key': resolved_session_key,
                        'agent': agent_payload,
                    },
                }
                return

            if accumulated_content:
                token_count = CompanyService._coerce_agent_test_token_count(
                    token_count,
                    accumulated_content,
                )
                assistant_message = CompanyService._create_agent_test_message(
                    session,
                    role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
                    content=accumulated_content,
                    token_count=token_count,
                    tool_calls=[],
                    raw_payload={},
                )
                CompanyService._update_agent_test_session_summary(
                    session,
                    last_message=accumulated_content,
                    status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
                )
                CompanyService._refresh_public_client_entry_session_memory_summary(session)
                if user_memory_mainline_enabled:
                    CompanyService._refresh_public_client_entry_user_memory(
                        session,
                        source_message=user_message,
                    )
                yield {
                    'event': 'complete',
                    'data': {
                        'content': accumulated_content,
                        'token_count': token_count,
                        'conversation_id': resolved_conversation_id,
                        'session_key': resolved_session_key,
                        'agent': agent_payload,
                        'tool_calls': [],
                        'assistant_message': assistant_message.get_info(),
                        'conversation': session.get_info(),
                    },
                }
                return

            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            yield {
                'event': 'error',
                'data': {
                    'message': '智能体暂未返回内容，请稍后重试',
                    'content': '',
                    'conversation_id': resolved_conversation_id,
                    'session_key': resolved_session_key,
                    'agent': agent_payload,
                },
            }

        return True, iter_events()

    @staticmethod
    def send_transparent_company_test_message(
        cid: str,
        conversation_id: str,
        content: str,
        user: User,
        agent_id: str = '',
        title: str = '',
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg, scope = CompanyService._get_transparent_company_for_agent_access(cid, user, client_ip)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        if scope and not CompanyService._scoped_agent_test_send_allowed(
            transparent_company, user, agent_id, conversation_id
        ):
            return False, '无权访问该 Agent 或会话'
        ok, session_or_msg = CompanyService._resolve_agent_test_session_for_send(
            transparent_company=transparent_company,
            conversation_id=conversation_id,
            agent_id=agent_id,
            user=user,
            title=title,
        )
        if not ok:
            return False, session_or_msg
        session = session_or_msg
        CompanyService._ensure_agent_test_session_title_from_first_message(session, content)
        moderation_ok, moderation_msg = _moderate_company_texts(
            items=[("content", content)],
            source=TextModerationAsset.SOURCE_AGENT_TEST_MESSAGE,
            owner_id=cid,
            actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            return False, moderation_msg

        user_message = CompanyService._create_agent_test_message(
            session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content=content,
        )
        ok, response = CompanyService._send_agent_test_message_via_runtime(
            transparent_company,
            session,
            content,
        )
        if not ok:
            logger.warning(
                '[agent-test] runtime execution failed cid=%s conversation=%s detail=%s',
                transparent_company.company.cid,
                session.conversationId,
                sanitize_runtime_debug_payload(response) if isinstance(response, dict) else str(response),
            )
            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            return False, sanitize_user_visible_error(response)

        response_payload = response if isinstance(response, dict) else {}
        safe_runtime_payload = sanitize_agent_test_runtime_payload(
            response_payload,
            include_tool_calls=True,
        )
        assistant_content = sanitize_assistant_content_for_user(
            safe_runtime_payload.get('assistant_content') or ''
        )
        if not assistant_content:
            runtime_error = CompanyService._extract_agent_test_runtime_error(response_payload)
            if runtime_error:
                CompanyService._update_agent_test_session_summary(
                    session,
                    last_message=content,
                    status=TransparentCompanyAgentTestSession.STATUS_FAILED,
                )
                return False, runtime_error
            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            return False, 'Agent 未返回任何内容，请先检查模型配置或稍后重试'
        assistant_message = CompanyService._create_agent_test_message(
            session,
            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
            content=assistant_content,
            token_count=CompanyService._coerce_agent_test_token_count(
                safe_runtime_payload.get('token_count'),
                assistant_content,
            ),
            tool_calls=sanitize_tool_calls(safe_runtime_payload.get('tool_calls')),
            raw_payload=sanitize_runtime_debug_payload(response_payload),
        )
        CompanyService._attach_agent_runtime_output(response_payload, assistant_message)
        CompanyService._update_agent_test_session_summary(
            session,
            last_message=assistant_content or content,
            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
        )
        return True, {
            'user_message': user_message.get_info(),
            'assistant_message': assistant_message.get_info(),
            'conversation': session.get_info(),
            'token_count': assistant_message.tokenCount,
            'tool_calls': sanitize_tool_calls(
                safe_runtime_payload.get('tool_calls')
            ),
        }

    @staticmethod
    def stream_transparent_company_test_message(
        cid: str,
        conversation_id: str,
        content: str,
        user: User,
        agent_id: str = '',
        title: str = '',
        attachments: Optional[list[dict[str, Any]]] = None,
        client_ip: Optional[str] = None,
    ) -> tuple[bool, Iterable[dict[str, Any]] | str]:
        ok, transparent_company_or_msg, scope = CompanyService._get_transparent_company_for_agent_access(cid, user, client_ip)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        if scope and not CompanyService._scoped_agent_test_send_allowed(
            transparent_company, user, agent_id, conversation_id
        ):
            return False, '无权访问该 Agent 或会话'
        ok, session_or_msg = CompanyService._resolve_agent_test_session_for_send(
            transparent_company=transparent_company,
            conversation_id=conversation_id,
            agent_id=agent_id,
            user=user,
            title=title,
        )
        if not ok:
            return False, session_or_msg
        session = session_or_msg
        attachments = list(attachments or [])
        if not str(content or '').strip() and not attachments:
            return False, '消息内容和附件不能同时为空'
        display_content = str(content or '').strip() or f"请读取附件：{', '.join(item.get('name') or '附件' for item in attachments)}"
        CompanyService._ensure_agent_test_session_title_from_first_message(session, display_content)
        moderation_ok, moderation_msg = _moderate_company_texts(
            items=[("content", display_content)],
            source=TextModerationAsset.SOURCE_AGENT_TEST_MESSAGE,
            owner_id=cid,
            actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            return False, moderation_msg

        user_message = CompanyService._create_agent_test_message(
            session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content=display_content,
            raw_payload={'attachments': attachments},
        )
        ok, runtime_stream_or_msg = CompanyService._stream_agent_test_message_via_runtime(
            transparent_company,
            session,
            display_content,
        )
        if not ok:
            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            return False, sanitize_user_visible_error(runtime_stream_or_msg)

        runtime_stream = runtime_stream_or_msg

        def iter_events():
            accumulated_content = ''
            token_count = 0
            yield {
                'event': 'start',
                'data': {
                    'conversation': session.get_info(),
                    'user_message': user_message.get_info(),
                },
            }
            try:
                for runtime_event in runtime_stream:
                    if not isinstance(runtime_event, dict):
                        continue
                    event_type = str(runtime_event.get('event') or '').strip().lower()
                    if event_type == 'start':
                        yield {
                            'event': 'status',
                            'data': {
                                'message': 'Runtime 已连接，正在等待 Agent 输出。',
                            },
                        }
                        continue
                    if event_type == 'status':
                        status_message = sanitize_assistant_content_for_user(
                            runtime_event.get('message') or runtime_event.get('status') or ''
                        )
                        if status_message:
                            yield {
                                'event': 'status',
                                'data': {
                                    'message': status_message,
                                },
                            }
                        continue
                    if event_type == 'delta':
                        delta = sanitize_assistant_content_for_user(runtime_event.get('delta') or '')
                        if not delta:
                            continue
                        accumulated_content += delta
                        reasoning_content = _split_assistant_reasoning_content(accumulated_content)
                        yield {
                            'event': 'delta',
                            'data': {
                                'delta': delta,
                                'content': accumulated_content,
                                **reasoning_content,
                            },
                        }
                        continue
                    if event_type == 'complete':
                        final_content = sanitize_assistant_content_for_user(
                            runtime_event.get('content') or accumulated_content
                        )
                        if not final_content:
                            raise ValueError('Agent 未返回任何内容，请先检查模型配置或稍后重试')
                        if final_content != accumulated_content:
                            if final_content.startswith(accumulated_content):
                                remaining_content = final_content[len(accumulated_content):]
                            else:
                                remaining_content = final_content
                                accumulated_content = ''
                            for delta in CompanyService._iter_agent_test_text_chunks(remaining_content):
                                accumulated_content += delta
                                reasoning_content = _split_assistant_reasoning_content(accumulated_content)
                                yield {
                                    'event': 'delta',
                                    'data': {
                                        'delta': delta,
                                        'content': accumulated_content,
                                        **reasoning_content,
                                    },
                                }
                                # If the runtime can only return final text, pace synthetic deltas
                                # so the browser can render a readable streaming progression.
                                time.sleep(0.02)
                        else:
                            accumulated_content = final_content
                        token_count = CompanyService._coerce_agent_test_stream_usage_token_count(runtime_event)
                        assistant_message = CompanyService._create_agent_test_message(
                            session,
                            role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
                            content=accumulated_content,
                            token_count=token_count,
                            tool_calls=sanitize_tool_calls(runtime_event.get('tool_calls')),
                            raw_payload={},
                        )
                        CompanyService._attach_agent_runtime_output(
                            runtime_event,
                            assistant_message,
                        )
                        CompanyService._update_agent_test_session_summary(
                            session,
                            last_message=accumulated_content or content,
                            status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
                        )
                        yield {
                            'event': 'complete',
                            'data': {
                                'content': accumulated_content,
                                **_split_assistant_reasoning_content(accumulated_content),
                                'assistant_message': assistant_message.get_info(),
                                'conversation': session.get_info(),
                                'token_count': assistant_message.tokenCount,
                                'tool_calls': sanitize_tool_calls(runtime_event.get('tool_calls')),
                            },
                        }
                        return
                    if event_type == 'error':
                        raise ValueError(sanitize_user_visible_error(runtime_event.get('message')))
            except Exception as exc:
                logger.warning(
                    '[agent-test-stream] runtime execution failed cid=%s conversation=%s detail=%s',
                    transparent_company.company.cid,
                    session.conversationId,
                    sanitize_user_visible_error(exc),
                )
                CompanyService._update_agent_test_session_summary(
                    session,
                    last_message=content,
                    status=TransparentCompanyAgentTestSession.STATUS_FAILED,
                )
                yield {
                    'event': 'error',
                    'data': {
                        'message': sanitize_user_visible_error(exc),
                        'content': accumulated_content,
                    },
                }
                return

            if accumulated_content:
                token_count = CompanyService._coerce_agent_test_token_count(token_count, accumulated_content)
                assistant_message = CompanyService._create_agent_test_message(
                    session,
                    role=TransparentCompanyAgentTestMessage.ROLE_ASSISTANT,
                    content=accumulated_content,
                    token_count=token_count,
                    tool_calls=[],
                    raw_payload={},
                )
                CompanyService._update_agent_test_session_summary(
                    session,
                    last_message=accumulated_content,
                    status=TransparentCompanyAgentTestSession.STATUS_ACTIVE,
                )
                yield {
                    'event': 'complete',
                    'data': {
                        'content': accumulated_content,
                        **_split_assistant_reasoning_content(accumulated_content),
                        'assistant_message': assistant_message.get_info(),
                        'conversation': session.get_info(),
                        'token_count': assistant_message.tokenCount,
                        'tool_calls': [],
                    },
                }
                return

            CompanyService._update_agent_test_session_summary(
                session,
                last_message=content,
                status=TransparentCompanyAgentTestSession.STATUS_FAILED,
            )
            yield {
                'event': 'error',
                'data': {
                    'message': 'Agent 未返回任何内容，请先检查模型配置或稍后重试',
                    'content': '',
                },
            }

        return True, iter_events()

    @staticmethod
    def list_transparent_company_agent_skill_bindings(
        cid: str,
        agent_id: Optional[str] = None,
        skill_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        user: Optional[User] = None,
        client_ip: Optional[str] = None,
    ):
        access_resolver = (
            CompanyService._get_transparent_company_for_agent_configuration
            if agent_id
            else CompanyService._get_transparent_company_for_admin
        )
        access_args = (cid, agent_id, user, client_ip) if agent_id else (cid, user, client_ip)
        ok, transparent_company_or_msg = access_resolver(*access_args)
        if not ok:
            access_ok, transparent_company_or_msg, _scope = (
                CompanyService._get_transparent_company_for_agent_access(
                    cid=cid,
                    user=user,
                    client_ip=client_ip,
                )
            )
            resource = Resource.objects.filter(
                type=Resource.TYPE_AGENT,
                external_id=f'agent:{cid}:{agent_id}',
            ).first()
            if (
                not access_ok
                or resource is None
                or not is_user_action_allowed(user, resource, 'configure')
            ):
                return False, '权限不足'
        transparent_company = transparent_company_or_msg

        qs = TransparentCompanyAgentSkillBinding.objects.filter(
            transparentCompany=transparent_company
        ).select_related('agent', 'skill')
        if agent_id:
            qs = qs.filter(agent__agentId=agent_id)
        if skill_id:
            qs = qs.filter(skill__skillId=skill_id)
        if enabled is not None:
            qs = qs.filter(enabled=enabled)

        bindings = [binding.get_info() for binding in qs.order_by('sortOrder', 'id')]
        return True, bindings

    @staticmethod
    @transaction.atomic
    def upsert_transparent_company_agent_skill_binding(
        cid: str,
        agent_id: str,
        skill_id: str,
        user: User,
        enabled: Optional[bool] = None,
        sort_order: int = 0,
        note: str = '',
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_agent_configuration(
            cid, agent_id, user, client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        try:
            agent = TransparentCompanyAgentDefinition.objects.get(
                transparentCompany=transparent_company,
                agentId=agent_id,
            )
        except TransparentCompanyAgentDefinition.DoesNotExist:
            return False, 'Agent 不存在'

        ok, skill_or_msg = CompanyService._get_or_materialize_transparent_company_skill(
            transparent_company=transparent_company,
            skill_id=skill_id,
        )
        if not ok:
            return False, skill_or_msg
        skill = skill_or_msg

        binding = TransparentCompanyAgentSkillBinding.objects.filter(
            transparentCompany=transparent_company,
            agent=agent,
            skill=skill,
        ).first()

        if binding is None:
            binding = TransparentCompanyAgentSkillBinding(
                transparentCompany=transparent_company,
                agent=agent,
                skill=skill,
            )

        binding.enabled = enabled if enabled is not None else binding.enabled
        binding.sortOrder = sort_order or 0
        binding.note = note or ''
        binding.updated_at = str(time.time())[:20]
        if not binding.created_at:
            binding.created_at = str(time.time())[:20]
        binding.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent skill binding upsert:{agent.agentId}:{skill.skillId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[agent-skill-binding-sync] runtime sync failed cid={cid} agent={agent.agentId} "
                f"skill={skill.skillId} detail={sync_payload}"
            )
            return False, f"Agent 技能绑定已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = binding.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    @transaction.atomic
    def toggle_transparent_company_agent_skill_binding(
        cid: str,
        agent_id: str,
        skill_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_agent_configuration(
            cid, agent_id, user, client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        try:
            binding = TransparentCompanyAgentSkillBinding.objects.select_related(
                'agent',
                'skill',
            ).get(
                transparentCompany=transparent_company,
                agent__agentId=agent_id,
                skill__skillId=skill_id,
            )
        except TransparentCompanyAgentSkillBinding.DoesNotExist:
            return False, 'Agent 技能绑定不存在'

        binding.enabled = not binding.enabled
        binding.updated_at = str(time.time())[:20]
        binding.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent skill binding toggle:{agent_id}:{skill_id}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[agent-skill-binding-toggle-sync] runtime sync failed cid={cid} agent={agent_id} "
                f"skill={skill_id} detail={sync_payload}"
            )
            return False, f"Agent 技能绑定状态已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = binding.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def list_model_providers(
        cid: Optional[str] = None,
        status: Optional[str] = None,
        auth_mode: Optional[str] = None,
        platform_managed: Optional[bool] = None,
    ):
        runtime_catalog = CompanyService._load_runtime_model_catalog(cid or '')
        if runtime_catalog:
            version = str(runtime_catalog.get('openclawVersion') or runtime_catalog.get('version') or DEFAULT_OPENCLAW_VERSION)
            source = str(runtime_catalog.get('catalogSource') or runtime_catalog.get('source') or 'runtime_model_catalog')
            items = [
                CompanyService._normalize_runtime_catalog_provider(item, version=version, source=source)
                for item in list(runtime_catalog.get('providers') or [])
            ]
            if status:
                items = [item for item in items if item.get('status') == status]
            if auth_mode:
                items = [item for item in items if item.get('authMode') == auth_mode]
            if platform_managed is not None:
                items = [item for item in items if bool(item.get('isPlatformManaged')) == platform_managed]
            return True, items

        catalog = get_openclaw_model_catalog()
        qs = ModelProvider.objects.all().order_by('name', 'id')
        qs = qs.filter(providerId__in=tuple(catalog.provider_ids))
        if status:
            qs = qs.filter(status=status)
        if auth_mode:
            qs = qs.filter(authMode=auth_mode)
        if platform_managed is not None:
            qs = qs.filter(isPlatformManaged=platform_managed)
        existing = {
            provider.providerId: CompanyService._serialize_catalog_provider(provider, version=catalog.version)
            for provider in qs
        }
        items = []
        for provider_id in catalog.provider_ids:
            data = existing.get(provider_id) or CompanyService._build_catalog_provider_stub(
                provider_id,
                version=catalog.version,
            )
            if status and data.get('status') != status:
                continue
            if auth_mode and data.get('authMode') != auth_mode:
                continue
            if platform_managed is not None and bool(data.get('isPlatformManaged')) != platform_managed:
                continue
            items.append(data)
        return True, items

    @staticmethod
    def list_model_definitions(
        cid: Optional[str] = None,
        provider_id: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        supports_tools: Optional[bool] = None,
    ):
        runtime_catalog = CompanyService._load_runtime_model_catalog(cid or '')
        if runtime_catalog:
            version = str(runtime_catalog.get('openclawVersion') or runtime_catalog.get('version') or DEFAULT_OPENCLAW_VERSION)
            source = str(runtime_catalog.get('catalogSource') or runtime_catalog.get('source') or 'runtime_model_catalog')
            items = [
                CompanyService._normalize_runtime_catalog_model(item, version=version, source=source)
                for item in list(runtime_catalog.get('models') or [])
            ]
            if provider_id:
                items = [item for item in items if str(item.get('providerId') or '') == provider_id]
            if status:
                items = [item for item in items if item.get('status') == status]
            if category:
                items = [item for item in items if item.get('category') == category]
            if supports_tools is not None:
                items = [item for item in items if bool(item.get('supportsTools')) == supports_tools]
            return True, items

        catalog = get_openclaw_model_catalog()
        qs = ModelDefinition.objects.select_related('provider').all().order_by('provider__name', 'displayName', 'id')
        qs = qs.filter(
            modelId__in=tuple(catalog.model_ids),
            provider__providerId__in=tuple(catalog.provider_ids),
        )
        if provider_id:
            qs = qs.filter(provider__providerId=provider_id)
        if status:
            qs = qs.filter(status=status)
        if category:
            qs = qs.filter(category=category)
        if supports_tools is not None:
            qs = qs.filter(supportsTools=supports_tools)
        existing = {
            model.modelId: CompanyService._serialize_catalog_model(model, version=catalog.version)
            for model in qs
        }
        items = []
        for model_id in catalog.model_ids:
            data = existing.get(model_id) or CompanyService._build_catalog_model_stub(
                model_id,
                version=catalog.version,
            )
            if provider_id and str(data.get('providerId') or '') != provider_id:
                continue
            if status and data.get('status') != status:
                continue
            if category and data.get('category') != category:
                continue
            if supports_tools is not None and bool(data.get('supportsTools')) != supports_tools:
                continue
            items.append(data)
        return True, items

    @staticmethod
    def list_model_packages(
        cid: Optional[str] = None,
        status: Optional[str] = None,
        is_default: Optional[bool] = None,
    ):
        runtime_catalog = CompanyService._load_runtime_model_catalog(cid or '')
        if runtime_catalog:
            version = str(runtime_catalog.get('openclawVersion') or runtime_catalog.get('version') or DEFAULT_OPENCLAW_VERSION)
            source = str(runtime_catalog.get('catalogSource') or runtime_catalog.get('source') or 'runtime_model_catalog')
            items = [
                CompanyService._normalize_runtime_catalog_package(item, version=version, source=source)
                for item in list(runtime_catalog.get('packages') or [])
            ]
            if status:
                items = [item for item in items if item.get('status') == status]
            if is_default is not None:
                items = [item for item in items if bool(item.get('isDefault')) == is_default]
            return True, items

        catalog = get_openclaw_model_catalog()
        qs = ModelPackage.objects.all().order_by('priority', 'name', 'id')
        if catalog.package_ids:
            qs = qs.filter(packageId__in=tuple(catalog.package_ids))
        if status:
            qs = qs.filter(status=status)
        if is_default is not None:
            qs = qs.filter(isDefault=is_default)
        return True, [
            CompanyService._serialize_catalog_package(package, version=catalog.version)
            for package in qs
        ]

    @staticmethod
    def get_company_model_policy(cid: str, user: User, client_ip: Optional[str] = None):
        ok, company_or_msg = CompanyService._get_company_for_admin(cid, user, client_ip)
        if not ok:
            return False, company_or_msg

        company = company_or_msg
        policy = getattr(company, 'modelPolicy', None)
        provider_id = str(getattr(settings, 'AGENT_RUNTIME_PLATFORM_PROVIDER_ID', 'custom-qwen'))
        default_model_id = str(getattr(settings, 'AGENT_RUNTIME_PLATFORM_MODEL_ID', 'qwen3.8-27b'))
        default_model_id = normalize_catalog_model_id(default_model_id)
        data = policy.get_info() if policy else {
            'cid': company.cid,
            'packageId': '',
            'status': CompanyModelPolicy.STATUS_ACTIVE,
            'rateLimitRpm': 60,
            'rateLimitTpm': 120000,
            'created_at': '',
            'updated_at': '',
        }
        data.update({
            'providerId': provider_id,
            'defaultModelId': default_model_id,
            'selectedCredentialName': '',
            'sourceMode': CompanyModelPolicy.SOURCE_PLATFORM,
            'fallbackToPlatform': True,
            'defaultModelCatalogKnown': default_model_id in get_openclaw_model_catalog().model_ids,
            'openclawVersion': DEFAULT_OPENCLAW_VERSION,
            'catalogSource': 'openclaw_version_catalog',
        })
        return True, data

    @staticmethod
    def upsert_company_model_policy(
        cid: str,
        user: User,
        package_id: str = '',
        provider_id: str = '',
        default_model_id: str = '',
        selected_credential_name: str = '',
        source_mode: str = CompanyModelPolicy.SOURCE_PLATFORM,
        status: str = CompanyModelPolicy.STATUS_ACTIVE,
        rate_limit_rpm: int = 60,
        rate_limit_tpm: int = 120000,
        fallback_to_platform: bool = True,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(cid, user, client_ip)
        if not ok:
            return False, company_or_msg
        company = company_or_msg
        # Platform policy is authoritative; clients cannot select BYOK credentials.
        source_mode = CompanyModelPolicy.SOURCE_PLATFORM
        selected_credential_name = ''
        fallback_to_platform = True
        package_id = ''
        provider_id = str(getattr(settings, 'AGENT_RUNTIME_PLATFORM_PROVIDER_ID', 'custom-qwen'))
        default_model_id = str(getattr(settings, 'AGENT_RUNTIME_PLATFORM_MODEL_ID', 'qwen3.8-27b'))
        catalog = get_openclaw_model_catalog()
        default_model_id = normalize_catalog_model_id(default_model_id, catalog.version)

        package = None
        provider = None
        default_model = None

        if package_id:
            if catalog.package_ids and package_id not in catalog.package_ids:
                return False, '当前 OpenClaw 版本不支持该模型套餐'
            package = ModelPackage.objects.filter(packageId=package_id).first()
            if not package:
                return False, '妯″瀷濂楅涓嶅瓨鍦?'
        if provider_id:
            if provider_id not in catalog.provider_ids:
                return False, '当前 OpenClaw 版本不支持该模型提供方'
            provider = CompanyService._get_or_create_catalog_provider(provider_id, version=catalog.version)
        if default_model_id:
            if not provider:
                return False, '请先选择模型提供方'
            model_qs = ModelDefinition.objects.filter(modelId=default_model_id)
            if provider:
                model_qs = model_qs.filter(provider=provider)
            default_model = model_qs.first()
            if not default_model:
                default_model = CompanyService._get_or_create_catalog_model(
                    provider,
                    default_model_id,
                    version=catalog.version,
                )
            if provider is None:
                provider = default_model.provider

        default_model_id = normalize_catalog_model_id(default_model_id, catalog.version)
        if default_model_id and default_model and default_model.modelId != default_model_id:
            default_model = CompanyService._get_or_create_catalog_model(
                provider,
                default_model_id,
                version=catalog.version,
            )

        selected_credential_name = (selected_credential_name or '').strip()
        if selected_credential_name:
            if not provider:
                return False, '请先选择模型提供方'
            credential = CompanyApiCredential.objects.filter(
                company=company,
                provider=provider,
                credentialName=selected_credential_name,
            ).first()
            if not credential:
                return False, '所选模型密钥不存在'

        policy, _ = CompanyModelPolicy.objects.get_or_create(
            company=company,
            defaults={
                'created_at': str(time.time())[:20],
                'updated_at': str(time.time())[:20],
            },
        )
        policy.package = package
        policy.provider = provider
        policy.defaultModel = default_model
        policy.selectedCredentialName = selected_credential_name
        policy.sourceMode = source_mode
        policy.status = status
        policy.rateLimitRpm = rate_limit_rpm
        policy.rateLimitTpm = rate_limit_tpm
        policy.fallbackToPlatform = bool(fallback_to_platform)
        if not policy.created_at:
            policy.created_at = str(time.time())[:20]
        policy.updated_at = str(time.time())[:20]
        policy.save()
        data = policy.get_info()
        data['sourceMode'] = normalize_model_source_mode(data.get('sourceMode'))
        data['defaultModelId'] = normalize_catalog_model_id(data.get('defaultModelId'), catalog.version)
        data['defaultModelCatalogKnown'] = data.get('defaultModelId') in catalog.model_ids
        data['openclawVersion'] = catalog.version
        data['catalogSource'] = 'openclaw_version_catalog'
        return True, data

    @staticmethod
    def list_company_api_credentials(
        cid: str,
        user: User,
        provider_id: Optional[str] = None,
        status: Optional[str] = None,
        client_ip: Optional[str] = None,
    ):
        return False, 'platform-managed model credentials only'
        ok, company_or_msg = CompanyService._get_company_for_admin(cid, user, client_ip)
        if not ok:
            return False, company_or_msg
        company = company_or_msg

        catalog = get_openclaw_model_catalog()
        if provider_id and provider_id not in catalog.provider_ids:
            return False, '当前 OpenClaw 版本不支持该模型提供方'
        qs = CompanyApiCredential.objects.filter(company=company).select_related('provider').order_by('provider__name', 'credentialName', 'id')
        if provider_id:
            qs = qs.filter(provider__providerId=provider_id)
        qs = qs.filter(provider__providerId__in=tuple(catalog.provider_ids))
        if status:
            qs = qs.filter(status=status)
        return True, [credential.get_info() for credential in qs]

    @staticmethod
    def upsert_company_api_credential(
        cid: str,
        provider_id: str,
        credential_name: str,
        user: User,
        api_key: str = '',
        status: str = CompanyApiCredential.STATUS_INACTIVE,
        client_ip: Optional[str] = None,
    ):
        return False, 'platform-managed model credentials only'
        ok, company_or_msg = CompanyService._get_company_for_admin(cid, user, client_ip)
        if not ok:
            return False, company_or_msg
        company = company_or_msg

        catalog = get_openclaw_model_catalog()
        if provider_id not in catalog.provider_ids:
            return False, '当前 OpenClaw 版本不支持该模型提供方'
        provider = CompanyService._get_or_create_catalog_provider(provider_id, version=catalog.version)

        credential = CompanyApiCredential.objects.filter(
            company=company,
            provider=provider,
            credentialName=credential_name,
        ).first()
        if credential is None:
            credential = CompanyApiCredential(
                company=company,
                provider=provider,
                credentialName=credential_name,
                created_at=str(time.time())[:20],
            )

        if api_key:
            credential.secretEncrypted = CompanyService._encode_secret(api_key)
            credential.keyMasked = CompanyService._mask_secret(api_key)
        credential.status = status
        credential.updated_at = str(time.time())[:20]
        credential.save()
        if (
            credential.status == CompanyApiCredential.STATUS_ACTIVE
            and not CompanyModelPolicy.objects.filter(
                company=company,
                status=CompanyModelPolicy.STATUS_ACTIVE,
            ).exists()
        ):
            CompanyService._sync_company_model_policy_from_active_credential(
                company=company,
                provider=provider,
                credential_name=credential.credentialName,
                version=catalog.version,
            )
        return True, credential.get_info()

    @staticmethod
    def get_company_session_memory_policy(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(cid, user, client_ip)
        if not ok:
            return False, company_or_msg

        company = company_or_msg
        policy = getattr(company, 'sessionMemoryPolicy', None)
        if not policy:
            return True, {
                'cid': company.cid,
                'mode': CompanySessionMemoryPolicy.MODE_SESSION,
                'status': CompanySessionMemoryPolicy.STATUS_ACTIVE,
                'enabled': True,
                'recentMessageWindow': 12,
                'maxTokens': 4000,
                'summaryTriggerTokens': 2800,
                'summaryMaxTokens': 800,
                'summaryStyle': CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE,
                'sessionTtlDays': 30,
                'redactPii': False,
                'created_at': '',
                'updated_at': '',
            }
        return True, policy.get_info()

    @staticmethod
    @transaction.atomic
    def upsert_company_session_memory_policy(
        cid: str,
        user: User,
        mode: str = CompanySessionMemoryPolicy.MODE_SESSION,
        status: str = CompanySessionMemoryPolicy.STATUS_ACTIVE,
        enabled: bool = True,
        recent_message_window: int = 12,
        max_tokens: int = 4000,
        summary_trigger_tokens: int = 2800,
        summary_max_tokens: int = 800,
        summary_style: str = CompanySessionMemoryPolicy.SUMMARY_STYLE_CONCISE,
        session_ttl_days: int = 30,
        redact_pii: bool = False,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(cid, user, client_ip)
        if not ok:
            return False, company_or_msg
        company = company_or_msg

        policy, _ = CompanySessionMemoryPolicy.objects.get_or_create(
            company=company,
            defaults={
                'created_at': str(time.time())[:20],
                'updated_at': str(time.time())[:20],
            },
        )
        policy.mode = mode
        policy.status = status
        policy.enabled = bool(enabled)
        policy.recentMessageWindow = recent_message_window
        policy.maxTokens = max_tokens
        policy.summaryTriggerTokens = summary_trigger_tokens
        policy.summaryMaxTokens = summary_max_tokens
        policy.summaryStyle = summary_style
        policy.sessionTtlDays = session_ttl_days
        policy.redactPii = bool(redact_pii)
        if not policy.created_at:
            policy.created_at = str(time.time())[:20]
        policy.updated_at = str(time.time())[:20]
        policy.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=company,
            operator_id=user.uid,
            notes="memory policy upsert",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[memory-policy-sync] runtime sync failed cid={cid} operator={user.uid}{_ip_suffix(client_ip)} "
                f"detail={sync_payload}"
            )
            return False, f"Memory 策略已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = policy.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def list_transparent_company_agent_memory_bindings(
        cid: str,
        user: User,
        agent_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        status: Optional[str] = None,
        client_ip: Optional[str] = None,
    ):
        access_resolver = (
            CompanyService._get_transparent_company_for_agent_configuration
            if agent_id
            else CompanyService._get_transparent_company_for_admin
        )
        access_args = (cid, agent_id, user, client_ip) if agent_id else (cid, user, client_ip)
        ok, transparent_company_or_msg = access_resolver(*access_args)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        qs = TransparentCompanyAgentMemoryBinding.objects.filter(
            transparentCompany=transparent_company
        ).select_related('agent').order_by('agent__sortOrder', 'id')
        if agent_id:
            qs = qs.filter(agent__agentId=agent_id)
        if enabled is not None:
            qs = qs.filter(enabled=enabled)
        if status:
            qs = qs.filter(status=status)
        return True, [binding.get_info() for binding in qs]

    @staticmethod
    @transaction.atomic
    def upsert_transparent_company_agent_memory_binding(
        cid: str,
        agent_id: str,
        user: User,
        memory_scope: str = TransparentCompanyAgentMemoryBinding.SCOPE_SESSION,
        enabled: bool = False,
        inherit_company_policy: bool = True,
        max_tokens_override: Optional[int] = None,
        summary_style_override: str = '',
        note: str = '',
        status: str = TransparentCompanyAgentMemoryBinding.STATUS_INACTIVE,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_agent_configuration(
            cid, agent_id, user, client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg
        transparent_company = transparent_company_or_msg

        try:
            agent = TransparentCompanyAgentDefinition.objects.get(
                transparentCompany=transparent_company,
                agentId=agent_id,
            )
        except TransparentCompanyAgentDefinition.DoesNotExist:
            return False, 'Agent 不存在'

        binding = TransparentCompanyAgentMemoryBinding.objects.filter(
            transparentCompany=transparent_company,
            agent=agent,
        ).first()
        if binding is None:
            binding = TransparentCompanyAgentMemoryBinding(
                transparentCompany=transparent_company,
                agent=agent,
                created_at=str(time.time())[:20],
            )

        binding.memoryScope = memory_scope
        binding.enabled = bool(enabled)
        binding.inheritCompanyPolicy = bool(inherit_company_policy)
        binding.maxTokensOverride = max_tokens_override
        binding.summaryStyleOverride = summary_style_override or None
        binding.note = note or ''
        binding.status = status
        binding.updated_at = str(time.time())[:20]
        if not binding.created_at:
            binding.created_at = str(time.time())[:20]
        binding.save()
        CompanyService._sync_agent_automatic_memory_permissions(
            transparent_company=transparent_company,
            agent=agent,
            enabled=(
                binding.enabled
                and binding.status == TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE
                and binding.memoryScope != TransparentCompanyAgentMemoryBinding.SCOPE_DISABLED
            ),
        )
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent memory binding upsert:{agent.agentId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[agent-memory-binding-sync] runtime sync failed cid={cid} agent={agent.agentId} detail={sync_payload}"
            )
            return False, f"Agent memory 绑定已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = binding.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    @transaction.atomic
    def toggle_transparent_company_agent_memory_binding(
        cid: str,
        agent_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_agent_configuration(
            cid, agent_id, user, client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg
        transparent_company = transparent_company_or_msg

        try:
            binding = TransparentCompanyAgentMemoryBinding.objects.select_related('agent').get(
                transparentCompany=transparent_company,
                agent__agentId=agent_id,
            )
        except TransparentCompanyAgentMemoryBinding.DoesNotExist:
            return False, 'Agent memory 绑定不存在'

        binding.enabled = not binding.enabled
        binding.updated_at = str(time.time())[:20]
        binding.save()
        CompanyService._sync_agent_automatic_memory_permissions(
            transparent_company=transparent_company,
            agent=binding.agent,
            enabled=(
                binding.enabled
                and binding.status == TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE
                and binding.memoryScope != TransparentCompanyAgentMemoryBinding.SCOPE_DISABLED
            ),
        )
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent memory binding toggle:{agent_id}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[agent-memory-binding-toggle-sync] runtime sync failed cid={cid} agent={agent_id} detail={sync_payload}"
            )
            return False, f"Agent memory 绑定状态已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = binding.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def _sync_agent_automatic_memory_permissions(
        *,
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
        enabled: bool,
    ) -> TransparentCompanyAgentPermissionBinding:
        """Synchronize default memory grants with the frontend memory switch."""
        now = str(time.time())[:20]
        permission = TransparentCompanyAgentPermissionBinding.objects.filter(
            transparentCompany=transparent_company,
            agent=agent,
        ).first()
        if permission is None:
            permission = TransparentCompanyAgentPermissionBinding(
                transparentCompany=transparent_company,
                agent=agent,
                created_at=now,
            )
        permission.memoryRead = bool(enabled)
        permission.memoryWrite = bool(enabled)
        permission.memoryUpdate = bool(enabled)
        permission.memoryDelete = False
        permission.enabled = True
        permission.status = TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE
        permission.updated_at = now
        if not permission.created_at:
            permission.created_at = now
        permission.save()
        return permission

    @staticmethod
    def list_transparent_company_permission_templates(
        cid: str,
        user: User,
        status: Optional[str] = None,
        enabled: Optional[bool] = None,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        qs = TransparentCompanyPermissionTemplate.objects.filter(
            transparentCompany=transparent_company
        ).order_by('name', 'id')
        if status:
            qs = qs.filter(status=status)
        if enabled is not None:
            qs = qs.filter(enabled=enabled)
        return True, [item.get_info() for item in qs]

    @staticmethod
    @transaction.atomic
    def upsert_transparent_company_permission_template(
        cid: str,
        user: User,
        name: str,
        template_id: str = '',
        description: str = '',
        agent_type: str = '',
        skills_include: Optional[list[str]] = None,
        skills_exclude: Optional[list[str]] = None,
        knowledge_read: Optional[list[str]] = None,
        knowledge_write: Optional[list[str]] = None,
        knowledge_delete: Optional[list[str]] = None,
        knowledge_deny: Optional[list[str]] = None,
        knowledge_read_deny: Optional[list[str]] = None,
        knowledge_write_deny: Optional[list[str]] = None,
        knowledge_delete_deny: Optional[list[str]] = None,
        knowledge_fields_provided: Optional[set[str]] = None,
        memory_read: Optional[bool] = None,
        memory_write: Optional[bool] = None,
        memory_update: Optional[bool] = None,
        memory_delete: Optional[bool] = None,
        memory_fields_provided: Optional[set[str]] = None,
        enabled: Optional[bool] = None,
        status: str = TransparentCompanyPermissionTemplate.STATUS_ACTIVE,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        template = None
        if template_id:
            template = TransparentCompanyPermissionTemplate.objects.filter(
                transparentCompany=transparent_company,
                templateId=template_id,
            ).first()
        is_new_template = template is None
        if is_new_template:
            template = TransparentCompanyPermissionTemplate(
                transparentCompany=transparent_company,
                templateId=template_id or f"{cid}-perm-{int(time.time() * 1000)}",
                created_at=str(time.time())[:20],
            )

        template.name = name
        template.description = description or ''
        template.agentType = agent_type or ''
        template.skillsInclude = skills_include or []
        template.skillsExclude = skills_exclude or []
        knowledge_updates = {
            'knowledge_read': ('knowledgeRead', knowledge_read),
            'knowledge_write': ('knowledgeWrite', knowledge_write),
            'knowledge_delete': ('knowledgeDelete', knowledge_delete),
            'knowledge_deny': ('knowledgeDeny', knowledge_deny),
            'knowledge_read_deny': ('knowledgeReadDeny', knowledge_read_deny),
            'knowledge_write_deny': ('knowledgeWriteDeny', knowledge_write_deny),
            'knowledge_delete_deny': ('knowledgeDeleteDeny', knowledge_delete_deny),
        }
        provided_knowledge_fields = (
            knowledge_fields_provided
            if knowledge_fields_provided is not None
            else {key for key, (_, value) in knowledge_updates.items() if value is not None}
        )
        for api_field, (model_field, value) in knowledge_updates.items():
            if api_field in provided_knowledge_fields:
                setattr(template, model_field, list(value or []))
            elif is_new_template:
                setattr(template, model_field, [])
        # Only provided keys change so partial edits do not erase other actions.
        memory_updates = {
            'memory_read': ('memoryRead', memory_read),
            'memory_write': ('memoryWrite', memory_write),
            'memory_update': ('memoryUpdate', memory_update),
            'memory_delete': ('memoryDelete', memory_delete),
        }
        provided_memory_fields = (
            memory_fields_provided
            if memory_fields_provided is not None
            else {key for key, (_, value) in memory_updates.items() if value is not None}
        )
        for api_field, (model_field, value) in memory_updates.items():
            if api_field in provided_memory_fields:
                setattr(template, model_field, value)
        template.enabled = enabled if enabled is not None else template.enabled
        template.status = status
        template.updated_at = str(time.time())[:20]
        template.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"permission template upsert:{template.templateId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[permission-template-sync] runtime sync failed cid={cid} template={template.templateId} detail={sync_payload}"
            )
            return False, f"权限模板已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = template.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def list_transparent_company_agent_permission_bindings(
        cid: str,
        user: User,
        agent_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        status: Optional[str] = None,
        client_ip: Optional[str] = None,
    ):
        access_resolver = (
            CompanyService._get_transparent_company_for_agent_configuration
            if agent_id
            else CompanyService._get_transparent_company_for_admin
        )
        access_args = (cid, agent_id, user, client_ip) if agent_id else (cid, user, client_ip)
        ok, transparent_company_or_msg = access_resolver(*access_args)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        qs = TransparentCompanyAgentPermissionBinding.objects.filter(
            transparentCompany=transparent_company
        ).select_related('agent', 'template').order_by('agent__sortOrder', 'agent__id')
        if agent_id:
            qs = qs.filter(agent__agentId=agent_id)
        if enabled is not None:
            qs = qs.filter(enabled=enabled)
        if status:
            qs = qs.filter(status=status)
        return True, [item.get_info() for item in qs]

    @staticmethod
    @transaction.atomic
    def upsert_transparent_company_agent_permission_binding(
        cid: str,
        agent_id: str,
        user: User,
        create_only: bool = False,
        template_id: str = '',
        skills_include: Optional[list[str]] = None,
        skills_exclude: Optional[list[str]] = None,
        knowledge_read: Optional[list[str]] = None,
        knowledge_write: Optional[list[str]] = None,
        knowledge_delete: Optional[list[str]] = None,
        knowledge_deny: Optional[list[str]] = None,
        knowledge_read_deny: Optional[list[str]] = None,
        knowledge_write_deny: Optional[list[str]] = None,
        knowledge_delete_deny: Optional[list[str]] = None,
        knowledge_fields_provided: Optional[set[str]] = None,
        memory_read: Optional[bool] = None,
        memory_write: Optional[bool] = None,
        memory_update: Optional[bool] = None,
        memory_delete: Optional[bool] = None,
        memory_fields_provided: Optional[set[str]] = None,
        enabled: Optional[bool] = None,
        status: str = TransparentCompanyAgentPermissionBinding.STATUS_INACTIVE,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_agent_configuration(
            cid, agent_id, user, client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg
        transparent_company = transparent_company_or_msg

        try:
            agent = TransparentCompanyAgentDefinition.objects.get(
                transparentCompany=transparent_company,
                agentId=agent_id,
            )
        except TransparentCompanyAgentDefinition.DoesNotExist:
            return False, 'Agent 不存在'

        template = None
        if template_id:
            template = TransparentCompanyPermissionTemplate.objects.filter(
                transparentCompany=transparent_company,
                templateId=template_id,
            ).first()
            if template is None:
                return False, '权限模板不存在'

        binding = TransparentCompanyAgentPermissionBinding.objects.filter(
            transparentCompany=transparent_company,
            agent=agent,
        ).first()
        if binding is not None and create_only:
            return False, '该 Agent 已存在权限绑定，请使用编辑入口修改现有配置'
        is_new_binding = binding is None
        if binding is None:
            binding = TransparentCompanyAgentPermissionBinding(
                transparentCompany=transparent_company,
                agent=agent,
                created_at=str(time.time())[:20],
            )

        if template:
            binding.template = template
            binding.skillsInclude = list(template.skillsInclude or [])
            binding.skillsExclude = list(template.skillsExclude or [])
            binding.knowledgeRead = list(template.knowledgeRead or [])
            binding.knowledgeWrite = list(template.knowledgeWrite or [])
            binding.knowledgeDelete = list(template.knowledgeDelete or [])
            binding.knowledgeDeny = list(template.knowledgeDeny or [])
            binding.knowledgeReadDeny = list(template.knowledgeReadDeny or [])
            binding.knowledgeWriteDeny = list(template.knowledgeWriteDeny or [])
            binding.knowledgeDeleteDeny = list(template.knowledgeDeleteDeny or [])
        else:
            binding.template = None
            binding.skillsInclude = skills_include or []
            binding.skillsExclude = skills_exclude or []
            knowledge_updates = {
                'knowledge_read': ('knowledgeRead', knowledge_read),
                'knowledge_write': ('knowledgeWrite', knowledge_write),
                'knowledge_delete': ('knowledgeDelete', knowledge_delete),
                'knowledge_deny': ('knowledgeDeny', knowledge_deny),
                'knowledge_read_deny': ('knowledgeReadDeny', knowledge_read_deny),
                'knowledge_write_deny': ('knowledgeWriteDeny', knowledge_write_deny),
                'knowledge_delete_deny': ('knowledgeDeleteDeny', knowledge_delete_deny),
            }
            provided_knowledge_fields = (
                knowledge_fields_provided
                if knowledge_fields_provided is not None
                else {key for key, (_, value) in knowledge_updates.items() if value is not None}
            )
            for api_field, (model_field, value) in knowledge_updates.items():
                if api_field in provided_knowledge_fields:
                    setattr(binding, model_field, list(value or []))
                elif is_new_binding:
                    setattr(binding, model_field, [])
        # `None` explicitly clears an override to inherit; omitted values preserve
        # existing bindings. New custom bindings default to full own-space CRUD.
        memory_updates = {
            'memory_read': ('memoryRead', memory_read),
            'memory_write': ('memoryWrite', memory_write),
            'memory_update': ('memoryUpdate', memory_update),
            'memory_delete': ('memoryDelete', memory_delete),
        }
        provided_memory_fields = (
            memory_fields_provided
            if memory_fields_provided is not None
            else {key for key, (_, value) in memory_updates.items() if value is not None}
        )
        for api_field, (model_field, value) in memory_updates.items():
            if api_field in provided_memory_fields:
                setattr(binding, model_field, value)
            elif is_new_binding:
                setattr(binding, model_field, None if template else True)

        binding.enabled = enabled if enabled is not None else binding.enabled
        binding.status = status
        binding.updated_at = str(time.time())[:20]
        binding.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent permission binding upsert:{agent.agentId}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[agent-permission-binding-sync] runtime sync failed cid={cid} agent={agent.agentId} detail={sync_payload}"
            )
            return False, f"Agent 权限绑定已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = binding.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    @transaction.atomic
    def toggle_transparent_company_agent_permission_binding(
        cid: str,
        agent_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_agent_configuration(
            cid, agent_id, user, client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg
        transparent_company = transparent_company_or_msg
        try:
            binding = TransparentCompanyAgentPermissionBinding.objects.select_related('agent', 'template').get(
                transparentCompany=transparent_company,
                agent__agentId=agent_id,
            )
        except TransparentCompanyAgentPermissionBinding.DoesNotExist:
            return False, 'Agent 权限配置不存在'

        binding.enabled = not binding.enabled
        binding.status = (
            TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE
            if binding.enabled
            else TransparentCompanyAgentPermissionBinding.STATUS_INACTIVE
        )
        binding.updated_at = str(time.time())[:20]
        binding.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=transparent_company.company,
            operator_id=user.uid,
            notes=f"agent permission binding toggle:{agent_id}",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[agent-permission-binding-toggle-sync] runtime sync failed cid={cid} agent={agent_id} detail={sync_payload}"
            )
            return False, f"Agent 权限绑定状态已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = binding.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def get_company_permission_config(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg
        config = getattr(company, 'permissionConfig', None)
        if config:
            return True, config.get_info()
        return True, {
            'cid': cid,
            'configJson': {
                'permissions': {},
            },
            'enabled': True,
            'updated_at': '',
        }

    @staticmethod
    def _normalize_permission_workspace_path(value: Any) -> str:
        from apps.runtime.security import validate_workspace_logical_path

        raw = str(value or "").strip().replace("\\", "/")
        if raw in {"", "*", ".", "/", "workspace"}:
            return ""

        if re.match(r"^[A-Za-z]:/", raw) or raw.startswith("/"):
            if "/workspace/" not in raw:
                return ""
            raw = raw.split("/workspace/", 1)[1]
        elif raw.startswith("workspace/"):
            raw = raw[len("workspace/"):]

        raw = raw.lstrip("/")
        if not raw:
            return ""

        raw_segments = [segment for segment in raw.split("/") if segment and segment != "."]
        if any(segment == ".." for segment in raw_segments):
            return ""

        try:
            return validate_workspace_logical_path(raw, "path")
        except ValueError:
            return ""

    @staticmethod
    def _permission_workspace_path_in_scope(logical_path: str, base_path: str) -> bool:
        if not logical_path:
            return False
        if not base_path:
            return True
        return logical_path == base_path or logical_path.startswith(f"{base_path}/")

    @staticmethod
    def _permission_workspace_parent_path(logical_path: str) -> str:
        normalized = CompanyService._normalize_permission_workspace_path(logical_path)
        if not normalized or "/" not in normalized:
            return ""
        return normalized.rsplit("/", 1)[0]

    @staticmethod
    def _permission_workspace_ancestors(logical_path: str, base_path: str) -> list[str]:
        normalized = CompanyService._normalize_permission_workspace_path(logical_path)
        if not normalized:
            return []

        ancestors: list[str] = []
        current = CompanyService._permission_workspace_parent_path(normalized)
        while current and current != base_path:
            ancestors.append(current)
            current = CompanyService._permission_workspace_parent_path(current)
        if base_path and normalized != base_path:
            return list(reversed([path for path in ancestors if path != base_path]))
        return list(reversed(ancestors))

    @staticmethod
    def _permission_workspace_item_name(logical_path: str) -> str:
        normalized = CompanyService._normalize_permission_workspace_path(logical_path)
        if not normalized:
            return ""
        return normalized.rsplit("/", 1)[-1]

    @staticmethod
    def _permission_workspace_ensure_node(
        nodes: dict[str, dict[str, Any]],
        logical_path: str,
        *,
        kind: str,
        runtime_visible: Optional[bool] = None,
        sync_status: Optional[str] = None,
    ) -> dict[str, Any] | None:
        normalized = CompanyService._normalize_permission_workspace_path(logical_path)
        if not normalized:
            return None

        node = nodes.get(normalized)
        if node is None:
            node = {
                "name": CompanyService._permission_workspace_item_name(normalized),
                "logical_path": normalized,
                "kind": "directory" if kind == "directory" else "file",
                "runtime_visible": bool(runtime_visible),
                "children": [],
            }
            nodes[normalized] = node
        else:
            if kind == "directory":
                node["kind"] = "directory"
            if runtime_visible is True:
                node["runtime_visible"] = True

        if sync_status:
            node["sync_status"] = sync_status
        return node

    @staticmethod
    def _sanitize_runtime_workspace_tree_items(
        payload: Any,
        *,
        base_path: str,
        recursive: bool,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            return {}

        items_by_path: dict[str, dict[str, Any]] = {}
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            return {}

        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            logical_path = CompanyService._normalize_permission_workspace_path(
                raw_item.get("logical_path") or raw_item.get("path") or ""
            )
            kind = str(raw_item.get("kind") or "").strip().lower()
            if not logical_path or kind not in {"file", "directory"}:
                continue
            if not CompanyService._permission_workspace_path_in_scope(logical_path, base_path):
                continue
            items_by_path[logical_path] = {
                "logical_path": logical_path,
                "kind": kind,
                "runtime_visible": True,
                "size": raw_item.get("size"),
                "updated_at": raw_item.get("updated_at") or raw_item.get("modified_at") or raw_item.get("mtime"),
            }
        return items_by_path

    @staticmethod
    def _build_company_permission_workspace_tree(
        *,
        company: Company,
        base_path: str,
        recursive: bool,
        runtime_items: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        from apps.agent_runtime.models import KnowledgeFolder
        from apps.runtime.models import WorkspaceFile

        nodes: dict[str, dict[str, Any]] = {}

        for runtime_path, runtime_item in runtime_items.items():
            node = CompanyService._permission_workspace_ensure_node(
                nodes,
                runtime_path,
                kind=runtime_item.get("kind") or "file",
                runtime_visible=True,
            )
            if node is not None:
                node["size"] = runtime_item.get("size")
                node["updated_at"] = runtime_item.get("updated_at")
            for ancestor in CompanyService._permission_workspace_ancestors(runtime_path, base_path):
                CompanyService._permission_workspace_ensure_node(
                    nodes,
                    ancestor,
                    kind="directory",
                    runtime_visible=True,
                )

        folder_queryset = KnowledgeFolder.objects.filter(company=company).order_by(
            "logical_path",
            "id",
        )
        if base_path:
            folder_queryset = folder_queryset.filter(logical_path__startswith=base_path)

        for knowledge_folder in folder_queryset:
            logical_path = CompanyService._normalize_permission_workspace_path(
                knowledge_folder.logical_path
            )
            if not logical_path:
                continue
            if not CompanyService._permission_workspace_path_in_scope(logical_path, base_path):
                continue
            for ancestor in CompanyService._permission_workspace_ancestors(logical_path, base_path):
                CompanyService._permission_workspace_ensure_node(
                    nodes,
                    ancestor,
                    kind="directory",
                    runtime_visible=True,
                )
            if (
                not recursive
                and CompanyService._permission_workspace_parent_path(logical_path) != base_path
            ):
                continue
            node = CompanyService._permission_workspace_ensure_node(
                nodes,
                logical_path,
                kind="directory",
                runtime_visible=True,
            )
            if node is not None and knowledge_folder.updated_at:
                node["updated_at"] = knowledge_folder.updated_at.isoformat()

        queryset = WorkspaceFile.objects.filter(company=company).order_by("logical_path", "id")
        if base_path:
            queryset = queryset.filter(logical_path__startswith=base_path)

        for workspace_file in queryset:
            logical_path = CompanyService._normalize_permission_workspace_path(workspace_file.logical_path)
            if not logical_path:
                continue
            if not CompanyService._permission_workspace_path_in_scope(logical_path, base_path):
                continue
            runtime_item = runtime_items.get(logical_path) or {}
            for ancestor in CompanyService._permission_workspace_ancestors(logical_path, base_path):
                CompanyService._permission_workspace_ensure_node(
                    nodes,
                    ancestor,
                    kind="directory",
                    runtime_visible=True,
                )
            if not recursive and CompanyService._permission_workspace_parent_path(logical_path) != base_path:
                continue
            runtime_visible = True
            CompanyService._permission_workspace_ensure_node(
                nodes,
                logical_path,
                kind="file",
                runtime_visible=runtime_visible,
                sync_status=workspace_file.sync_status or "",
            )
            node = nodes.get(logical_path)
            if node is not None:
                node["size"] = (
                    runtime_item.get("size")
                    if runtime_item.get("size") is not None
                    else workspace_file.size
                )
                node["updated_at"] = (
                    runtime_item.get("updated_at") or workspace_file.updated_at
                )

        def _sort_key(item: dict[str, Any]) -> tuple[int, str]:
            return (0 if item.get("kind") == "directory" else 1, str(item.get("name") or ""))

        if recursive:
            for node in nodes.values():
                node["children"] = []
            for logical_path, node in list(nodes.items()):
                parent_path = CompanyService._permission_workspace_parent_path(logical_path)
                if parent_path in nodes and CompanyService._permission_workspace_path_in_scope(parent_path, base_path):
                    nodes[parent_path]["children"].append(node)
            for node in nodes.values():
                node["children"].sort(key=_sort_key)

        visible_items: list[dict[str, Any]] = []
        for logical_path, node in nodes.items():
            if CompanyService._permission_workspace_parent_path(logical_path) != base_path:
                continue
            item = {
                "name": node["name"],
                "logical_path": node["logical_path"],
                "kind": node["kind"],
                "runtime_visible": bool(node.get("runtime_visible")),
            }
            if node.get("sync_status"):
                item["sync_status"] = node["sync_status"]
            if node.get("size") is not None:
                item["size"] = node["size"]
            if node.get("updated_at"):
                item["updated_at"] = node["updated_at"]
            if recursive and node.get("children"):
                item["children"] = [
                    CompanyService._prune_permission_workspace_tree_item(child)
                    for child in node["children"]
                ]
            visible_items.append(item)

        visible_items.sort(key=_sort_key)
        return {
            "cid": company.cid,
            "logical_path": base_path,
            "recursive": bool(recursive),
            "items": visible_items,
        }

    @staticmethod
    def _prune_permission_workspace_tree_item(node: dict[str, Any]) -> dict[str, Any]:
        item = {
            "name": str(node.get("name") or ""),
            "logical_path": str(node.get("logical_path") or ""),
            "kind": str(node.get("kind") or "file"),
            "runtime_visible": bool(node.get("runtime_visible")),
        }
        if node.get("sync_status"):
            item["sync_status"] = node["sync_status"]
        if node.get("size") is not None:
            item["size"] = node["size"]
        if node.get("updated_at"):
            item["updated_at"] = node["updated_at"]
        children = node.get("children") or []
        if isinstance(children, list) and children:
            item["children"] = [
                CompanyService._prune_permission_workspace_tree_item(child)
                for child in children
                if isinstance(child, dict)
            ]
        return item

    @staticmethod
    def list_company_permission_workspace_tree(
        cid: str,
        user: User,
        path: str = "",
        recursive: bool = False,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, {
                "status": 403,
                "message": str(company_or_msg),
            }

        company = company_or_msg
        base_path = CompanyService._normalize_permission_workspace_path(path)
        runtime_items: dict[str, dict[str, Any]] = {}

        payload = CompanyService._build_company_permission_workspace_tree(
            company=company,
            base_path=base_path,
            recursive=recursive,
            runtime_items=runtime_items,
        )
        payload["runtime_visible"] = True
        payload["runtime_source"] = "langgraph_native"
        return True, payload

    @staticmethod
    @transaction.atomic
    def upsert_company_permission_config(
        cid: str,
        user: User,
        config_json: Optional[dict] = None,
        enabled: Optional[bool] = None,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg
        config, _ = CompanyPermissionConfig.objects.get_or_create(
            company=company,
            defaults={
                'configJson': {'permissions': {}},
                'enabled': True,
                'updated_at': str(time.time())[:20],
            },
        )
        if config_json is not None:
            config.configJson = config_json
        if enabled is not None:
            config.enabled = enabled
        config.updated_at = str(time.time())[:20]
        config.save()
        sync_ok, sync_payload = CompanyService._sync_transparent_company_runtime_config(
            company=company,
            operator_id=user.uid,
            notes="permission config upsert",
        )
        if not sync_ok:
            transaction.set_rollback(True)
            logger.error(
                f"[permission-config-sync] runtime sync failed cid={cid} operator={user.uid}{_ip_suffix(client_ip)} "
                f"detail={sync_payload}"
            )
            return False, f"权限配置已保存，但同步到 OpenClaw 失败: {sync_payload}"
        payload = config.get_info()
        payload["runtimeSync"] = sync_payload
        return True, payload

    @staticmethod
    def list_company_conversations(
        cid: str,
        user: User,
        status: str = "",
        agent_id: str = "",
        source: str = "all",
        limit: int = 50,
        offset: int = 0,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        normalized_source = str(source or '').strip()
        queryset = (
            TransparentCompanyAgentTestSession.objects.filter(
                transparentCompany=transparent_company,
                source__in=[
                    TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
                    TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
                ],
            )
            .select_related('agent', 'operator')
            .order_by('-id')
        )
        if normalized_source and normalized_source != 'all':
            queryset = queryset.filter(source=normalized_source)
        if status:
            queryset = queryset.filter(status=status)
        if agent_id:
            queryset = queryset.filter(agent__agentId=agent_id)
        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return True, {
            'items': [item.get_info() for item in items],
            'total': total,
            'hasMore': offset + limit < total,
        }

    @staticmethod
    def get_company_conversation_messages(
        cid: str,
        conversation_id: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        session = (
            TransparentCompanyAgentTestSession.objects.filter(
                transparentCompany=transparent_company,
                conversationId=conversation_id,
                source__in=[
                    TransparentCompanyAgentTestSession.SOURCE_AGENT_TEST,
                    TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
                ],
            )
            .select_related('agent', 'operator')
            .first()
        )
        if session is None:
            return False, '\u5de5\u4f5c\u4f1a\u8bdd\u4e0d\u5b58\u5728'
        operator_display_name = ''
        if session.operator:
            operator_display_name = (
                getattr(session.operator, 'username', '')
                or getattr(session.operator, 'name', '')
                or getattr(session.operator, 'phone', '')
                or getattr(session.operator, 'email', '')
                or session.operator.uid
            )
        return True, {
            'conversation_id': session.conversationId,
            'title': session.title,
            'status': session.status,
            'session_key': session.sessionKey,
            'source': session.source,
            'user_id': session.operator.uid if session.operator else '',
            'user_name': operator_display_name,
            'user_display_name': operator_display_name,
            'user': session.operator.get_info() if session.operator else None,
            'messages': [item.get_info() for item in session.messages.order_by('id')],
        }

    @staticmethod
    def list_transparent_company_client_entry_conversations(
        cid: str,
        user: User,
        *,
        agent_id: str = '',
        limit: int = 20,
        offset: int = 0,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_client_entry(cid)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        queryset = (
            TransparentCompanyAgentTestSession.objects.filter(
                transparentCompany=transparent_company,
                operator=user,
                source=TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY,
            )
            .select_related('agent', 'operator')
            .order_by('-id')
        )
        if agent_id:
            queryset = queryset.filter(agent__agentId=agent_id)

        total = queryset.count()
        items = list(queryset[offset: offset + limit])
        return True, {
            'items': [item.get_info() for item in items],
            'total': total,
            'hasMore': offset + limit < total,
        }

    @staticmethod
    def get_transparent_company_client_entry_messages(
        cid: str,
        conversation_id: str,
        user: User,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_client_entry(cid)
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        session = CompanyService._get_public_client_entry_session(
            transparent_company,
            conversation_id,
            user,
        )
        if session is None:
            return False, '浼氳瘽涓嶅瓨鍦ㄦ垨鏃犳潈璁块棶'

        return True, {
            'conversation_id': session.conversationId,
            'title': session.title,
            'status': session.status,
            'session_key': session.sessionKey,
            'source': session.source,
            'agent_id': session.agent.agentId if session.agent else '',
            'agent_name': session.agent.name if session.agent else '',
            'messages': [item.get_info() for item in session.messages.order_by('id')],
        }

    @staticmethod
    def get_company_control_overview(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, transparent_company_or_msg = CompanyService._get_transparent_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, transparent_company_or_msg

        transparent_company = transparent_company_or_msg
        company = transparent_company.company
        CompanyService.ensure_default_transparent_agent(transparent_company)

        from apps.runtime.models import (
            OpenClawAuditLog,
            OpenClawInstance,
            OpenClawProvisionTask,
            RuntimeAlertPolicy,
            RuntimeDriftEvent,
            RuntimeRolloutPolicy,
            WorkspaceTransferTask,
        )

        instance = OpenClawInstance.objects.filter(company=company).order_by('-id').first()
        latest_task = OpenClawProvisionTask.objects.filter(company=company).order_by('-id').first()
        latest_audit = OpenClawAuditLog.objects.filter(company=company).order_by('-id').first()
        latest_transfer = WorkspaceTransferTask.objects.filter(company=company).order_by('-id').first()
        rollout_policy = RuntimeRolloutPolicy.objects.filter(company=company).first()
        alert_policy = RuntimeAlertPolicy.objects.filter(company=company).first()
        memory_policy = CompanySessionMemoryPolicy.objects.filter(company=company).first()
        model_policy = CompanyModelPolicy.objects.filter(company=company).first()

        agents_qs = TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany=transparent_company
        )
        skills_qs = TransparentCompanySkill.objects.filter(
            transparentCompany=transparent_company
        )
        agent_skill_bindings_qs = TransparentCompanyAgentSkillBinding.objects.filter(
            transparentCompany=transparent_company
        )
        agent_memory_bindings_qs = TransparentCompanyAgentMemoryBinding.objects.filter(
            transparentCompany=transparent_company
        )
        open_drift_qs = RuntimeDriftEvent.objects.filter(
            company=company,
            status=RuntimeDriftEvent.STATUS_OPEN,
        )
        public_agents_qs = agents_qs.filter(
            exposedToClient=True,
            enabled=True,
        ).order_by('exposureOrder', 'sortOrder', 'id')
        client_entry_payload = CompanyService._build_client_entry_payload(transparent_company)

        return True, {
            'company': company.get_info(),
            'transparent_company': transparent_company.get_info(),
            'runtime': instance.get_info() if instance else None,
            'summary': {
                'agent_total': agents_qs.count(),
                'agent_enabled': agents_qs.filter(enabled=True).count(),
                'agent_public': len(client_entry_payload.get('visible_agents', [])),
                'agent_core': agents_qs.filter(isCoreAgent=True).count(),
                'skill_total': skills_qs.count(),
                'skill_enabled': skills_qs.filter(enabled=True).count(),
                'skill_system': skills_qs.filter(isSystemSkill=True).count(),
                'agent_skill_binding_total': agent_skill_bindings_qs.count(),
                'agent_skill_binding_enabled': agent_skill_bindings_qs.filter(enabled=True).count(),
                'agent_memory_binding_total': agent_memory_bindings_qs.count(),
                'agent_memory_binding_enabled': agent_memory_bindings_qs.filter(enabled=True).count(),
                'open_drift_count': open_drift_qs.count(),
                'model_credential_count': CompanyApiCredential.objects.filter(company=company).count(),
            },
            'client_entry': client_entry_payload,
            'governance': {
                'rollout_policy': rollout_policy.get_info() if rollout_policy else None,
                'alert_policy': alert_policy.get_info() if alert_policy else None,
                'open_drift_events': [event.get_info() for event in open_drift_qs.order_by('-id')[:5]],
            },
            'memory_policy': memory_policy.get_info() if memory_policy else None,
            'model_policy': model_policy.get_info() if model_policy else None,
            'latest_task': latest_task.get_info() if latest_task else None,
            'latest_audit': latest_audit.get_info() if latest_audit else None,
            'latest_transfer': latest_transfer.get_info() if latest_transfer else None,
        }

    @staticmethod
    def list_company_members(
        cid: str,
        user: User,
        status: str = "",
        role: str = "",
        keyword: str = "",
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_member_manager(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg

        qs = CompanyMember.objects.filter(company=company).select_related('user').order_by('id')
        if status:
            qs = qs.filter(status=status)
        if role:
            qs = qs.filter(role=role)
        if keyword:
            keyword_l = keyword.lower()
            qs = [
                item
                for item in qs
                if keyword_l in (item.name or '').lower()
                or keyword_l in (item.email or '').lower()
                or keyword_l in (item.role or '').lower()
            ]
            return True, [item.get_info() for item in qs]
        return True, [item.get_info() for item in qs]

    @staticmethod
    def add_company_member_by_public_id(
        cid: str,
        user: User,
        public_id: str,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_member_manager(
            cid=cid, user=user, client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg
        target = User.objects.filter(public_id=public_id.strip().upper()).first()
        if not target:
            return False, '用户 ID 不存在'
        if CompanyMember.objects.filter(company=company, user=target).exists():
            return False, '该用户已加入企业；如已停用，请在成员列表中恢复'
        now = str(time.time())[:20]
        try:
            with transaction.atomic():
                member = CompanyMember.objects.create(
                    company=company, user=target, memberUid=target.uid,
                    name=target.username, email=target.email or '',
                    role='member', status=CompanyMember.STATUS_ACTIVE,
                    invitedByUid=user.uid, created_at=now, updated_at=now,
                )
                sync_organization_tenant_member(company, member)
        except IntegrityError:
            return False, '该用户已加入企业'
        return True, member.get_info()

    @staticmethod
    def add_company_member_by_phone(
        cid: str,
        user: User,
        phone: str,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_member_manager(
            cid=cid, user=user, client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg
        matches = User.objects.filter(phone=phone.strip()).order_by('id')
        match_count = matches.count()
        if match_count == 0:
            return False, '手机号对应的用户不存在'
        if match_count > 1:
            return False, '手机号对应多个账号，请改用用户 ID 添加'
        target = matches.first()
        if CompanyMember.objects.filter(company=company, user=target).exists():
            return False, '该用户已加入企业；如已停用，请在成员列表中恢复'
        now = str(time.time())[:20]
        try:
            with transaction.atomic():
                member = CompanyMember.objects.create(
                    company=company, user=target, memberUid=target.uid,
                    name=target.username, email=target.email or '',
                    role='member', status=CompanyMember.STATUS_ACTIVE,
                    invitedByUid=user.uid, created_at=now, updated_at=now,
                )
                sync_organization_tenant_member(company, member)
        except IntegrityError:
            return False, '该用户已加入企业'
        return True, member.get_info()

    @staticmethod
    def upsert_company_member(
        cid: str,
        user: User,
        name: str,
        role: str,
        email: str = "",
        member_id: Optional[int] = None,
        member_uid: str = "",
        status: str = CompanyMember.STATUS_INVITED,
        last_active_at: str = "",
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_member_manager(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg

        email = (email or '').strip().lower()
        target_user = None
        resolved_uid = member_uid or ''
        if member_uid:
            target_user = User.objects.filter(uid=member_uid).first()
            if target_user:
                resolved_uid = target_user.uid
                if not email:
                    email = target_user.email or ''
                if not name:
                    name = target_user.username or name
        elif email:
            target_user = User.objects.filter(email__iexact=email).first()
            if target_user:
                resolved_uid = target_user.uid

        member = None
        if member_id:
            member = CompanyMember.objects.filter(company=company, id=member_id).first()
            if member is None:
                return False, '成员不存在'
        elif target_user:
            member = CompanyMember.objects.filter(company=company, user=target_user).first()
        elif email:
            member = CompanyMember.objects.filter(
                company=company,
                user__isnull=True,
                email=email,
            ).first()

        if target_user and CompanyMember.objects.filter(
            company=company,
            user=target_user,
        ).exclude(pk=getattr(member, 'pk', None)).exists():
            return False, '成员已存在'

        if target_user is None and not email:
            return False, '未绑定成员必须提供邮箱'

        if member is None:
            member = CompanyMember(
                company=company,
                created_at=str(time.time())[:20],
                invitedByUid=user.uid,
            )

        member.user = target_user
        member.memberUid = resolved_uid
        member.name = name
        member.email = email
        member.role = role
        member.status = status
        member.lastActiveAt = last_active_at
        member.updated_at = str(time.time())[:20]
        try:
            with transaction.atomic():
                member.save()
                sync_organization_tenant_member(company, member)
        except IntegrityError:
            return False, '成员已存在'
        return True, member.get_info()

    @staticmethod
    def toggle_company_member_status(
        cid: str,
        user: User,
        member_id: int,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_member_manager(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg

        member = CompanyMember.objects.filter(company=company, id=member_id).first()
        if member is None:
            return False, '成员不存在'
        if member.user and has_company_iam_permission(company, member.user, Grant.PERM_ADMIN):
            return False, '企业管理员不能通过员工停用操作撤销管理权限'

        if member.status == CompanyMember.STATUS_DISABLED:
            member.status = CompanyMember.STATUS_ACTIVE
        else:
            member.status = CompanyMember.STATUS_DISABLED
        member.updated_at = str(time.time())[:20]
        member.save()
        sync_organization_tenant_member(company, member)
        return True, member.get_info()

    @staticmethod
    def get_company_system_setting(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg
        setting = getattr(company, 'systemSetting', None)
        if setting:
            return True, setting.get_info()
        return True, {
            'cid': cid,
            'displayName': company.company_name,
            'systemDescription': company.description or '',
            'enablePublicEntry': True,
            'showMonitoringCards': True,
            'requireDiffBeforePublish': True,
            'alertOnDrift': True,
            'alertWebhook': '',
            'allowBrowserDirectCosUpload': True,
            'workspacePrefix': f'/workspace/{company.cid}',
            'syncTimeoutSeconds': 30,
            'defaultEntryAgentId': '',
            'defaultModelId': '',
            'saveAgentTestSessions': True,
            'notifyPublishSuccess': True,
            'notifyAuditTasks': True,
            'notifyDriftDetected': True,
            'enabled': True,
            'created_at': '',
            'updated_at': '',
        }

    @staticmethod
    def upsert_company_system_setting(
        cid: str,
        user: User,
        client_ip: Optional[str] = None,
        **kwargs,
    ):
        ok, company_or_msg = CompanyService._get_company_for_admin(
            cid=cid,
            user=user,
            client_ip=client_ip,
        )
        if not ok:
            return False, company_or_msg
        company = company_or_msg

        setting, _ = CompanySystemSetting.objects.get_or_create(
            company=company,
            defaults={
                'displayName': company.company_name,
                'systemDescription': company.description or '',
                'enablePublicEntry': True,
                'showMonitoringCards': True,
                'requireDiffBeforePublish': True,
                'alertOnDrift': True,
                'alertWebhook': '',
                'allowBrowserDirectCosUpload': True,
                'workspacePrefix': f'/workspace/{company.cid}',
                'syncTimeoutSeconds': 30,
                'defaultEntryAgentId': '',
                'defaultModelId': '',
                'saveAgentTestSessions': True,
                'notifyPublishSuccess': True,
                'notifyAuditTasks': True,
                'notifyDriftDetected': True,
                'enabled': True,
                'created_at': str(time.time())[:20],
                'updated_at': str(time.time())[:20],
            },
        )

        field_map = {
            'display_name': 'displayName',
            'system_description': 'systemDescription',
            'enable_public_entry': 'enablePublicEntry',
            'show_monitoring_cards': 'showMonitoringCards',
            'require_diff_before_publish': 'requireDiffBeforePublish',
            'alert_on_drift': 'alertOnDrift',
            'alert_webhook': 'alertWebhook',
            'allow_browser_direct_cos_upload': 'allowBrowserDirectCosUpload',
            'workspace_prefix': 'workspacePrefix',
            'sync_timeout_seconds': 'syncTimeoutSeconds',
            'default_entry_agent_id': 'defaultEntryAgentId',
            'default_model_id': 'defaultModelId',
            'save_agent_test_sessions': 'saveAgentTestSessions',
            'notify_publish_success': 'notifyPublishSuccess',
            'notify_audit_tasks': 'notifyAuditTasks',
            'notify_drift_detected': 'notifyDriftDetected',
            'enabled': 'enabled',
        }
        for payload_key, model_key in field_map.items():
            if payload_key in kwargs and kwargs[payload_key] is not None:
                setattr(setting, model_key, kwargs[payload_key])
        setting.updated_at = str(time.time())[:20]
        setting.save()
        return True, setting.get_info()
