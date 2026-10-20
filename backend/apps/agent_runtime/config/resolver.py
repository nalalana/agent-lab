"""Resolve normalized, secret-free runtime configuration from company settings."""

from typing import Any, Dict, Iterable, Optional

from django.conf import settings

from apps.company.models import (
    Company,
    CompanyModelPolicy,
    CompanyVikingBinding,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentPromptVersion,
    TransparentCompanyAgentSkillBinding,
    TransparentCompanyPromptVersion,
)
from apps.agent_runtime.tools.registry import default_tool_registry
from apps.agent_runtime.tools.project_tables import agent_project_table_scopes

from .types import ResolvedAgentConfig, ResolvedSkill


class ConfigResolutionError(ValueError):
    """Raised when an active company/Agent configuration cannot be assembled."""


class ConfigResolver:
    """Builds the effective Agent configuration without reading credential values."""

    def resolve(self, company: Company, agent_id: object) -> ResolvedAgentConfig:
        transparent_company = self._get_transparent_company(company)
        agent = self._get_agent(transparent_company, agent_id)
        company_prompt_version = self._get_company_prompt_version(transparent_company)
        agent_prompt_version = self._get_agent_prompt_version(agent)
        tool_policy = self._resolve_tool_policy(transparent_company, agent)

        return ResolvedAgentConfig(
            company=company,
            transparent_company=transparent_company,
            agent=agent,
            company_prompt_version=company_prompt_version,
            agent_prompt_version=agent_prompt_version,
            company_prompt=(
                company_prompt_version.content
                if company_prompt_version
                else transparent_company.prompts
            ),
            agent_prompt=(agent_prompt_version.content if agent_prompt_version else agent.systemPrompt or ''),
            skills=tuple(self._resolve_skills(agent)),
            model_config=self._resolve_model_config(company),
            tool_policy=tool_policy,
            memory_policy=self._resolve_memory_policy(
                transparent_company,
                agent,
                tool_policy=tool_policy,
            ),
        )

    @staticmethod
    def _get_transparent_company(company: Company) -> TransparentCompany:
        if not CompanyVikingBinding.objects.filter(
            company_id=company.id,
            status=CompanyVikingBinding.STATUS_ACTIVE,
        ).exists():
            raise ConfigResolutionError(
                f'Company {company.id} OpenViking binding is not active.'
            )
        item = TransparentCompany.objects.filter(company_id=company.id).first()
        if item is None:
            raise ConfigResolutionError(f'Company {company.id} is not a transparent company.')
        return item

    @staticmethod
    def _get_agent(
        transparent_company: TransparentCompany, agent_id: object
    ) -> TransparentCompanyAgentDefinition:
        agent = TransparentCompanyAgentDefinition.objects.filter(
            transparentCompany_id=transparent_company.id,
            # RunRequest.agent_id is the existing Agent table primary key.
            # External business IDs are translated at the API boundary.
            pk=agent_id,
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
        ).first()
        if agent is None:
            raise ConfigResolutionError(f'Active Agent {agent_id!r} was not found for this company.')
        return agent

    @staticmethod
    def _get_company_prompt_version(
        transparent_company: TransparentCompany,
    ) -> Optional[TransparentCompanyPromptVersion]:
        return TransparentCompanyPromptVersion.objects.filter(
            company_id=transparent_company.id,
            status=TransparentCompanyPromptVersion.STATUS_ACTIVE,
        ).first()

    @staticmethod
    def _get_agent_prompt_version(
        agent: TransparentCompanyAgentDefinition,
    ) -> Optional[TransparentCompanyAgentPromptVersion]:
        return TransparentCompanyAgentPromptVersion.objects.filter(
            agent_id=agent.id,
            isCurrent=True,
        ).order_by('-id').first()

    @staticmethod
    def _resolve_skills(agent: TransparentCompanyAgentDefinition) -> Iterable[ResolvedSkill]:
        bindings = (
            TransparentCompanyAgentSkillBinding.objects.filter(
                agent_id=agent.id,
                transparentCompany_id=agent.transparentCompany_id,
                enabled=True,
                skill__enabled=True,
                skill__status='active',
                skill__transparentCompany_id=agent.transparentCompany_id,
            )
            .select_related('skill')
            .order_by('sortOrder', 'skill__priority', 'skill__id')
        )
        for binding in bindings:
            skill = binding.skill
            yield ResolvedSkill(
                skill_id=skill.skillId,
                version=skill.version,
                name=skill.name,
                content=skill.content or '',
            )

    @staticmethod
    def _resolve_model_config(company: Company) -> Dict[str, Any]:
        # Model identity is platform-managed.  A company policy is optional and
        # only contributes tenant-specific package and rate-limit metadata.
        policy = (
            CompanyModelPolicy.objects.filter(company_id=company.id, status='active')
            .select_related('package')
            .first()
        )
        return {
            'packageId': policy.package.packageId if policy and policy.package else '',
            'providerId': str(getattr(settings, 'AGENT_RUNTIME_PLATFORM_PROVIDER_ID', 'custom-qwen')),
            'modelId': str(getattr(settings, 'AGENT_RUNTIME_PLATFORM_MODEL_ID', 'qwen3.8-27b')),
            'sourceMode': CompanyModelPolicy.SOURCE_PLATFORM,
            'rateLimitRpm': policy.rateLimitRpm if policy else 60,
            'rateLimitTpm': policy.rateLimitTpm if policy else 120000,
            'fallbackToPlatform': True,
        }

    def _resolve_tool_policy(
        self,
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
    ) -> Dict[str, Any]:
        project_tables = agent_project_table_scopes(transparent_company.company, agent)
        binding = (
            TransparentCompanyAgentPermissionBinding.objects.filter(
                transparentCompany_id=transparent_company.id,
                agent_id=agent.id,
                enabled=True,
                status='active',
            )
            .select_related('template')
            .first()
        )
        if binding is None:
            policy = self._empty_tool_policy()
            policy['project_tables'] = project_tables
            return policy
        template = binding.template
        if not self._template_is_active_for_company(template, transparent_company):
            template = None
        return {
            'skills': self._merge_list_policy(template, binding, 'skillsInclude', 'skillsExclude'),
            'knowledge': {
                'read': self._merge_values(template, binding, 'knowledgeRead'),
                'write': self._merge_values(template, binding, 'knowledgeWrite'),
                'delete': self._merge_values(template, binding, 'knowledgeDelete'),
                'deny': self._merge_values(template, binding, 'knowledgeDeny'),
                'read_deny': self._merge_values(template, binding, 'knowledgeReadDeny'),
                'write_deny': self._merge_values(template, binding, 'knowledgeWriteDeny'),
                'delete_deny': self._merge_values(template, binding, 'knowledgeDeleteDeny'),
            },
            'project_tables': project_tables,
            'memory': {
                action: self._effective_flag(template, binding, f'memory{action.title()}')
                for action in ('read', 'write', 'update', 'delete')
            },
            'platformTools': default_tool_registry().snapshot_manifest(),
        }

    @staticmethod
    def _empty_tool_policy() -> Dict[str, Any]:
        return {
            'skills': {'allow': [], 'deny': []},
            'knowledge': {
                'read': [],
                'write': [],
                'delete': [],
                'deny': [],
                'read_deny': [],
                'write_deny': [],
                'delete_deny': [],
            },
            'memory': {'read': False, 'write': False, 'update': False, 'delete': False},
            'platformTools': default_tool_registry().snapshot_manifest(),
        }

    @staticmethod
    def _template_is_active_for_company(template, transparent_company) -> bool:
        return bool(
            template
            and template.enabled
            and template.status == 'active'
            and template.transparentCompany_id == transparent_company.id
        )

    @staticmethod
    def _merge_list_policy(template, binding, allow_field: str, deny_field: str) -> Dict[str, Any]:
        return {
            'allow': ConfigResolver._merge_values(template, binding, allow_field),
            'deny': ConfigResolver._merge_values(template, binding, deny_field),
        }

    @staticmethod
    def _merge_values(template, binding, field_name: str) -> list[str]:
        template_values = getattr(template, field_name, []) if template else []
        binding_values = getattr(binding, field_name, []) or []
        return sorted(set(template_values or []) | set(binding_values))

    @staticmethod
    def _effective_flag(template, binding, field_name: str) -> bool:
        binding_value = getattr(binding, field_name)
        if binding_value is not None:
            return binding_value
        template_value = getattr(template, field_name, None) if template else None
        return bool(template_value)

    def _resolve_memory_policy(
        self,
        transparent_company: TransparentCompany,
        agent: TransparentCompanyAgentDefinition,
        *,
        tool_policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        session_binding = TransparentCompanyAgentMemoryBinding.objects.filter(
            transparentCompany_id=transparent_company.id, agent_id=agent.id
        ).first()
        session = self._resolve_session_memory(session_binding)
        memory_permissions = dict(tool_policy.get('memory') or {})
        automatic_enabled = bool(
            session['enabled']
            and (memory_permissions.get('read') or memory_permissions.get('write'))
        )
        return {
            'automatic': {
                'enabled': automatic_enabled,
                'provider': 'openviking',
                'operationMode': 'runtime_managed',
                'read': bool(memory_permissions.get('read')),
                'write': bool(memory_permissions.get('write')),
                'update': bool(memory_permissions.get('update')),
            },
            'session': session,
        }

    @staticmethod
    def _resolve_session_memory(binding) -> Dict[str, Any]:
        """Resolve the current frontend switch without consulting legacy policies."""
        enabled = bool(
            binding and binding.enabled
            and binding.status == TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE
            and binding.memoryScope != TransparentCompanyAgentMemoryBinding.SCOPE_DISABLED
        )
        return {
            'enabled': enabled,
            'scope': binding.memoryScope if binding else 'disabled',
            'recentMessageWindow': 12 if enabled else 0,
            'maxTokens': (
                binding.maxTokensOverride
                if enabled and binding.maxTokensOverride is not None
                else (4000 if enabled else 0)
            ),
            'summaryStyle': (
                binding.summaryStyleOverride or 'concise'
            ) if enabled else '',
            'redactPii': True,
        }
