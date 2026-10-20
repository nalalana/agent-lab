"""Create immutable, deterministic, and secret-free Agent config snapshots."""

import hashlib
import json
from typing import Any, Dict, Iterable, Mapping

from django.db import transaction

from apps.agent_runtime.models import AgentConfigSnapshot

from .prompt_builder import PromptBuilder
from .types import ResolvedAgentConfig


class SnapshotSecurityError(ValueError):
    """Raised before sensitive configuration can be persisted."""


class SnapshotService:
    """Turns resolved config into the immutable record referenced by AgentRun."""

    SENSITIVE_KEY_PARTS = ('apikey', 'authorization', 'credential', 'password', 'secret')
    SENSITIVE_TOKEN_KEYS = {'token', 'access_token', 'auth_token', 'bearer_token', 'refresh_token'}

    def __init__(self, prompt_builder: PromptBuilder = None):
        self.prompt_builder = prompt_builder or PromptBuilder()

    @transaction.atomic
    def create(self, config: ResolvedAgentConfig) -> AgentConfigSnapshot:
        payload = self.build_payload(config)
        self.assert_secret_free(payload)
        return AgentConfigSnapshot.objects.create(
            company=config.company,
            agent=config.agent,
            company_prompt_version=config.company_prompt_version,
            agent_prompt_version=config.agent_prompt_version,
            skill_versions=payload['skillVersions'],
            model_config=payload['modelConfig'],
            tool_policy=payload['toolPolicy'],
            memory_policy=payload['memoryPolicy'],
            # Plaintext compiled prompts are intentionally never stored.
            compiled_prompt_encrypted='',
            checksum=self.calculate_checksum(payload),
        )

    def build_payload(self, config: ResolvedAgentConfig) -> Dict[str, Any]:
        return {
            'companyId': config.company.id,
            'agentId': config.agent.id,
            'companyPromptVersionId': self._id_or_none(config.company_prompt_version),
            'agentPromptVersionId': self._id_or_none(config.agent_prompt_version),
            'skillVersions': {skill.skill_id: skill.version for skill in config.skills},
            'modelConfig': dict(config.model_config),
            'toolPolicy': dict(config.tool_policy),
            'memoryPolicy': dict(config.memory_policy),
            'compiledPromptChecksum': self._text_checksum(
                self.prompt_builder.build_system_prompt(config)
            ),
        }

    @classmethod
    def calculate_checksum(cls, payload: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(serialized).hexdigest()

    @classmethod
    def assert_secret_free(cls, value: Any, path: Iterable[str] = ()) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).lower().replace('-', '_')
                compact = normalized.replace('_', '')
                contains_secret_part = any(part in compact for part in cls.SENSITIVE_KEY_PARTS)
                if contains_secret_part or normalized in cls.SENSITIVE_TOKEN_KEYS:
                    location = '.'.join((*path, str(key)))
                    raise SnapshotSecurityError(f'Sensitive snapshot field is forbidden: {location}')
                cls.assert_secret_free(child, (*path, str(key)))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                cls.assert_secret_free(child, (*path, str(index)))

    @staticmethod
    def _id_or_none(instance):
        return instance.pk if instance else None

    @staticmethod
    def _text_checksum(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
