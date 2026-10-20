"""Small immutable values shared by config resolution, prompts, and snapshots."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from apps.company.models import (
    Company,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentPromptVersion,
    TransparentCompanyPromptVersion,
)


@dataclass(frozen=True)
class ResolvedSkill:
    skill_id: str
    version: str
    name: str
    content: str


@dataclass(frozen=True)
class ResolvedAgentConfig:
    company: Company
    transparent_company: TransparentCompany
    agent: TransparentCompanyAgentDefinition
    company_prompt_version: Optional[TransparentCompanyPromptVersion]
    agent_prompt_version: Optional[TransparentCompanyAgentPromptVersion]
    company_prompt: str
    agent_prompt: str
    skills: Tuple[ResolvedSkill, ...]
    model_config: Mapping[str, Any]
    tool_policy: Mapping[str, Any]
    memory_policy: Mapping[str, Any]


@dataclass(frozen=True)
class PromptBundle:
    """The stable system prompt plus ordered request-specific model messages."""

    system_prompt: str
    messages: Tuple[Mapping[str, str], ...]
