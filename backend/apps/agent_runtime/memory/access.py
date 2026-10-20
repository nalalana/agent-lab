"""Resolve effective memory permissions from company and Agent policies."""

from dataclasses import dataclass
from typing import Iterable

from apps.company.models import (
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
)

from .errors import MemoryPermissionDenied
from .namespaces import TrustedMemoryContext


@dataclass(frozen=True)
class EffectiveMemoryAccess:
    can_read: bool
    can_write: bool
    session_read: bool
    session_write: bool
    user_read: bool
    user_write: bool
    user_scope: str
    max_user_items: int


class MemoryAccessResolver:
    """Applies global memory grants before scope-specific policy switches."""

    def resolve(self, context: TrustedMemoryContext) -> EffectiveMemoryAccess:
        permission = self._permission_binding(context)
        can_read = self._effective_flag(permission, 'memoryRead')
        can_write = self._effective_flag(permission, 'memoryWrite')
        session_read, session_write = self._session_access(context, can_read, can_write)
        return EffectiveMemoryAccess(
            can_read=can_read,
            can_write=can_write,
            session_read=session_read,
            session_write=session_write,
            # The legacy user-writeback policy is retired. User/visitor
            # isolation is enforced by trusted OpenViking namespaces instead.
            user_read=False,
            user_write=False,
            user_scope='disabled',
            max_user_items=0,
        )

    def authorize_recall(
        self,
        context: TrustedMemoryContext,
        requested_scopes: Iterable[str],
    ) -> tuple[str, ...]:
        access = self.resolve(context)
        scopes = tuple(dict.fromkeys(str(scope) for scope in requested_scopes))
        if not scopes:
            return ()
        denied = [scope for scope in scopes if not self._can_read_scope(access, scope)]
        if denied:
            raise MemoryPermissionDenied(f'Memory read is denied for scopes: {", ".join(denied)}.')
        return scopes

    def commit_scopes(self, context: TrustedMemoryContext) -> tuple[str, ...]:
        """Persist chat memory only to Agent, session, and allowed user scopes.

        Company memory is curated knowledge and is never populated automatically
        from a conversation.
        """
        access = self.resolve(context)
        scopes = []
        if access.can_write:
            scopes.append('agent')
        if access.session_write:
            scopes.append('session')
        if access.user_write:
            scopes.append('user')
        if not scopes:
            raise MemoryPermissionDenied('Memory commit is not allowed for this Agent.')
        return tuple(scopes)

    @staticmethod
    def _permission_binding(context):
        return (
            TransparentCompanyAgentPermissionBinding.objects.filter(
                transparentCompany_id=context.agent.transparentCompany_id,
                agent_id=context.agent.id,
                enabled=True,
                status=TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            )
            .select_related('template')
            .first()
        )

    @staticmethod
    def _effective_flag(binding, field_name: str) -> bool:
        if binding is None:
            return False
        value = getattr(binding, field_name)
        if value is not None:
            return bool(value)
        template = binding.template
        template_active = bool(
            template
            and template.enabled
            and template.status == template.STATUS_ACTIVE
            and template.transparentCompany_id == binding.transparentCompany_id
        )
        return bool(getattr(template, field_name, False)) if template_active else False

    @staticmethod
    def _session_access(context, can_read: bool, can_write: bool) -> tuple[bool, bool]:
        binding = TransparentCompanyAgentMemoryBinding.objects.filter(
            transparentCompany_id=context.agent.transparentCompany_id,
            agent_id=context.agent.id,
            enabled=True,
            status=TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE,
        ).first()
        enabled = bool(
            binding
            and binding.memoryScope != TransparentCompanyAgentMemoryBinding.SCOPE_DISABLED
        )
        return can_read and enabled, can_write and enabled

    @staticmethod
    def _can_read_scope(access: EffectiveMemoryAccess, scope: str) -> bool:
        if scope in {'company', 'agent'}:
            return access.can_read
        if scope == 'session':
            return access.session_read
        if scope == 'user':
            return access.user_read
        return False
