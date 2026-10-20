"""Authenticated B2B console endpoint for the company Agent inventory."""

from __future__ import annotations

from django.http import JsonResponse

from apps.company.models import (
    CompanyPermissionConfig,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentSkillBinding,
)
from apps.company.services import CompanyService
from apps.runtime.policy import build_effective_runtime_policy
from common.utils.Login_utils import login_checker
from common.utils.common_utils import get_user_from_request


def _agent_type(agent: TransparentCompanyAgentDefinition) -> str:
    if agent.isCoreAgent:
        return "core"
    if agent.exposedToClient:
        return "public"
    return "internal"


def _agent_status(agent: TransparentCompanyAgentDefinition) -> tuple[str, str]:
    if agent.status == TransparentCompanyAgentDefinition.STATUS_DEPRECATED:
        return "warning", "已废弃"
    if agent.enabled and agent.status == TransparentCompanyAgentDefinition.STATUS_ACTIVE:
        return "health", "运行中"
    if agent.status == TransparentCompanyAgentDefinition.STATUS_INACTIVE or not agent.enabled:
        return "idle", "未启用"
    return "error", "状态异常"


def _memory_policy_label(
    binding: TransparentCompanyAgentMemoryBinding | None,
) -> str:
    if binding and binding.enabled:
        if binding.memoryScope == TransparentCompanyAgentMemoryBinding.SCOPE_DISABLED:
            return "记忆已禁用"
        if binding.inheritCompanyPolicy:
            return "继承公司会话策略"
        return "Agent 会话策略"
    return "Agent 独立空间"


def _serialize_agent(
    agent: TransparentCompanyAgentDefinition,
    skill_bindings: list[TransparentCompanyAgentSkillBinding],
    memory_binding: TransparentCompanyAgentMemoryBinding | None,
    permission_binding: TransparentCompanyAgentPermissionBinding | None,
    permission_config: CompanyPermissionConfig | None,
) -> dict:
    template = permission_binding.template if permission_binding else None
    requires_explicit_binding = not (
        agent.isCoreAgent
        or agent.role == TransparentCompanyAgentDefinition.ROLE_MAIN_AGENT
    )
    policy = build_effective_runtime_policy(
        global_config=permission_config.get_info() if permission_config else {},
        template=template.get_info() if template else {},
        binding=permission_binding.get_info() if permission_binding else {},
        global_enabled=bool(permission_config and permission_config.enabled),
        template_enabled=bool(
            permission_binding
            and permission_binding.enabled
            and template
            and template.enabled
        ),
        binding_enabled=bool(permission_binding and permission_binding.enabled),
        agent_id=agent.agentId,
        require_explicit_binding=requires_explicit_binding,
        default_allow_skills=not requires_explicit_binding,
        default_allow_memory=True,
    )
    knowledge_permissions: list[str] = []
    if policy.knowledge_reads:
        knowledge_permissions.append("read")
    if policy.knowledge_writes:
        knowledge_permissions.append("write")
    if policy.knowledge_deletes:
        knowledge_permissions.append("delete")
    if policy.knowledge_denies or not (
        policy.knowledge_reads or policy.knowledge_writes or policy.knowledge_deletes
    ):
        knowledge_permissions.append("deny")

    skills = [
        binding.skill.name
        for binding in skill_bindings
        if binding.enabled
        and binding.skill.enabled
        and binding.skill.status == binding.skill.STATUS_ACTIVE
        and policy.allows_skill(binding.skill.skillId)
    ]
    status_level, status_label = _agent_status(agent)
    is_public = bool(agent.exposedToClient)
    return {
        "agent_id": agent.agentId,
        "name": agent.name,
        "type": _agent_type(agent),
        "is_public": is_public,
        "customer_portal": agent.routeKey if is_public else None,
        "knowledge_permissions": knowledge_permissions,
        "memory_policy": _memory_policy_label(memory_binding),
        "status_level": status_level,
        "status_label": status_label,
        "description": agent.description or "",
        "skills": skills,
        "updated_at": agent.updated_at,
    }


@login_checker
def console_agents(request):
    """Return every persisted Agent, including the bootstrapped CEO."""

    if request.method != "GET":
        return JsonResponse({"result": 0, "message": "仅支持GET请求"}, status=405)
    user = get_user_from_request(request)
    if not user:
        return JsonResponse({"result": 0, "message": "请先登录"}, status=401)
    cid = str(request.GET.get("cid") or "").strip()
    if not cid:
        return JsonResponse({"result": 0, "message": "cid不能为空"}, status=400)

    ok, transparent_company_or_message = CompanyService._get_transparent_company_for_admin(
        cid=cid,
        user=user,
        client_ip=request.META.get("REMOTE_ADDR"),
    )
    if not ok:
        return JsonResponse({"result": 0, "message": transparent_company_or_message})
    transparent_company = transparent_company_or_message

    # Repair older transparent companies that predate the automatic CEO bootstrap.
    CompanyService.ensure_default_transparent_agent(transparent_company)

    agents = list(
        TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany=transparent_company,
        )
        .select_related("transparentCompany__company")
        .order_by("-isCoreAgent", "exposureOrder", "sortOrder", "id")
    )
    skill_bindings_by_agent: dict[int, list[TransparentCompanyAgentSkillBinding]] = {}
    for binding in (
        TransparentCompanyAgentSkillBinding.objects.filter(
            transparentCompany=transparent_company,
        )
        .select_related("skill")
        .order_by("sortOrder", "id")
    ):
        skill_bindings_by_agent.setdefault(binding.agent_id, []).append(binding)
    memory_bindings = {
        binding.agent_id: binding
        for binding in TransparentCompanyAgentMemoryBinding.objects.filter(
            transparentCompany=transparent_company,
        )
    }
    permission_bindings = {
        binding.agent_id: binding
        for binding in TransparentCompanyAgentPermissionBinding.objects.filter(
            transparentCompany=transparent_company,
        ).select_related("template", "transparentCompany__company", "agent")
    }
    permission_config = CompanyPermissionConfig.objects.filter(
        company=transparent_company.company,
    ).first()
    return JsonResponse(
        {
            "result": 1,
            "message": "查询成功",
            "data": {
                "list": [
                    _serialize_agent(
                        agent,
                        skill_bindings_by_agent.get(agent.id, []),
                        memory_bindings.get(agent.id),
                        permission_bindings.get(agent.id),
                        permission_config,
                    )
                    for agent in agents
                ]
            },
        }
    )
