"""Derive OpenViking namespaces only from database-backed trusted context."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Optional

from apps.company.models import (
    Company,
    CompanyVikingBinding,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentTestSession,
    TransparentCompanyAgentVikingSpace,
)
from apps.user.models import User

from .errors import MemoryConfigurationError, MemoryPermissionDenied


@dataclass(frozen=True)
class TrustedMemoryContext:
    company: Company
    agent: TransparentCompanyAgentDefinition
    session: TransparentCompanyAgentTestSession
    user: Optional[User]
    account_id: str
    agent_user_id: str
    agent_credential_ref: str
    runtime_user_id: str
    runtime_credential_ref: str


@dataclass(frozen=True)
class MemoryNamespace:
    scope: str
    account_id: str
    user_id: str
    root_uri: str
    credential_ref: str = ''


class NamespaceResolver:
    """Validates entity ownership before calculating unguessable tenant paths."""

    SUPPORTED_SCOPES = frozenset({'company', 'agent', 'user', 'session'})

    def resolve_context(
        self,
        *,
        company_id,
        agent_id,
        user_id,
        session_id,
    ) -> TrustedMemoryContext:
        company = self._get_company(company_id)
        agent = self._get_agent(company, agent_id)
        session = self._get_session(company, agent, session_id)
        user = self._get_user(session, user_id)
        identity = self._get_agent_viking_identity(company, agent)
        return TrustedMemoryContext(
            company=company,
            agent=agent,
            session=session,
            user=user,
            account_id=identity['account_id'],
            agent_user_id=identity['agent_user_id'],
            agent_credential_ref=identity['agent_credential_ref'],
            runtime_user_id=identity['runtime_user_id'],
            runtime_credential_ref=identity['runtime_credential_ref'],
        )

    def for_scope(self, context: TrustedMemoryContext, scope: str) -> MemoryNamespace:
        if scope not in self.SUPPORTED_SCOPES:
            raise MemoryPermissionDenied(f'Unsupported memory scope: {scope!r}.')
        if scope == 'agent':
            user_id = context.agent_user_id
            credential_ref = context.agent_credential_ref
            root_uri = f'viking://user/{user_id}/memories/'
        else:
            user_id = context.runtime_user_id
            credential_ref = context.runtime_credential_ref
            suffix = self._scope_path(context, scope)
            root_uri = f'viking://user/{user_id}/memories/{suffix}/'
        return MemoryNamespace(
            scope=scope,
            account_id=context.account_id,
            user_id=user_id,
            root_uri=root_uri,
            credential_ref=credential_ref,
        )

    def for_agent(
        self,
        company: Company,
        agent: TransparentCompanyAgentDefinition,
    ) -> MemoryNamespace:
        """Resolve an Agent memory root without requiring an unrelated chat session."""
        if (
            agent.transparentCompany.company_id != company.id
            or not agent.enabled
            or agent.status != TransparentCompanyAgentDefinition.STATUS_ACTIVE
        ):
            raise MemoryPermissionDenied('Memory Agent does not belong to the company.')
        identity = self._get_agent_viking_identity(company, agent)
        return MemoryNamespace(
            scope='agent',
            account_id=identity['account_id'],
            user_id=identity['agent_user_id'],
            root_uri=f"viking://user/{identity['agent_user_id']}/memories/",
            credential_ref=identity['agent_credential_ref'],
        )

    @staticmethod
    def is_public_client_context(context: TrustedMemoryContext) -> bool:
        """Return whether memory belongs to an authenticated public visitor."""
        return (
            context.user is not None
            and context.session.source
            == TransparentCompanyAgentTestSession.SOURCE_PUBLIC_CLIENT_ENTRY
        )

    @classmethod
    def public_peer_id(cls, context: TrustedMemoryContext) -> str:
        """Build a stable, non-guessable OpenViking peer ID for one visitor."""
        if not cls.is_public_client_context(context):
            raise MemoryPermissionDenied('Public visitor memory requires a public session owner.')
        seed = f'{context.company.cid}:{context.agent.agentId}:{context.user.pk}'
        return f'visitor-{cls._digest(seed)}'

    @classmethod
    def for_public_peer(cls, context: TrustedMemoryContext) -> MemoryNamespace:
        """Resolve the peer-owned memory root of one public visitor."""
        peer_id = cls.public_peer_id(context)
        return MemoryNamespace(
            scope='user',
            account_id=context.account_id,
            user_id=context.agent_user_id,
            root_uri=(
                f'viking://user/{context.agent_user_id}/peers/{peer_id}/memories/'
            ),
            credential_ref=context.agent_credential_ref,
        )

    @staticmethod
    def _get_company(company_id) -> Company:
        try:
            return Company.objects.get(pk=company_id)
        except (Company.DoesNotExist, TypeError, ValueError) as exc:
            raise MemoryPermissionDenied('Memory company context is invalid.') from exc

    @staticmethod
    def _get_agent(company: Company, agent_id) -> TransparentCompanyAgentDefinition:
        try:
            return TransparentCompanyAgentDefinition.objects.select_related(
                'transparentCompany'
            ).get(
                pk=agent_id,
                transparentCompany__company_id=company.id,
                enabled=True,
                status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            )
        except (TransparentCompanyAgentDefinition.DoesNotExist, TypeError, ValueError) as exc:
            raise MemoryPermissionDenied('Memory Agent does not belong to the company.') from exc

    @staticmethod
    def _get_session(
        company: Company,
        agent: TransparentCompanyAgentDefinition,
        session_id,
    ) -> TransparentCompanyAgentTestSession:
        try:
            return TransparentCompanyAgentTestSession.objects.get(
                pk=session_id,
                transparentCompany__company_id=company.id,
                agent_id=agent.id,
            )
        except (TransparentCompanyAgentTestSession.DoesNotExist, TypeError, ValueError) as exc:
            raise MemoryPermissionDenied('Memory session does not belong to the Agent.') from exc

    @staticmethod
    def _get_user(session: TransparentCompanyAgentTestSession, user_id) -> Optional[User]:
        if user_id is None:
            return None
        try:
            user = User.objects.get(pk=user_id)
        except (User.DoesNotExist, TypeError, ValueError) as exc:
            raise MemoryPermissionDenied('Memory user context is invalid.') from exc
        if session.operator_id != user.id:
            raise MemoryPermissionDenied('Memory user does not own the session.')
        return user

    def _get_agent_viking_identity(self, company, agent) -> dict[str, str]:
        expected_account = f'tm-{self._digest(company.cid)}'
        expected_user = f'agent-{self._digest(f"{company.cid}:{agent.agentId}")}'
        try:
            space = TransparentCompanyAgentVikingSpace.objects.select_related(
                'companyBinding'
            ).get(
                transparentCompany_id=agent.transparentCompany_id,
                agent_id=agent.id,
                status=TransparentCompanyAgentVikingSpace.STATUS_ACTIVE,
            )
        except TransparentCompanyAgentVikingSpace.DoesNotExist as exc:
            raise MemoryConfigurationError('Active OpenViking Agent space is missing.') from exc
        binding = space.companyBinding
        if (
            binding is None
            or binding.company_id != company.id
            or binding.status != CompanyVikingBinding.STATUS_ACTIVE
            or not binding.runtimeCredentialRef
            or not space.credentialRef
        ):
            raise MemoryConfigurationError('Active OpenViking company binding is missing.')
        expected_root = f'viking://user/{expected_user}/memories/'
        expected_space_id = f'{expected_account}:{expected_user}'
        if (
            space.accountId != expected_account
            or space.userId != expected_user
            or space.rootUri != expected_root
            or space.vikingSpaceId != expected_space_id
        ):
            raise MemoryConfigurationError('OpenViking Agent space identity is inconsistent.')
        return {
            'account_id': expected_account,
            'agent_user_id': expected_user,
            'agent_credential_ref': space.credentialRef,
            'runtime_user_id': binding.runtimeUserId,
            'runtime_credential_ref': binding.runtimeCredentialRef,
        }

    def _scope_path(self, context: TrustedMemoryContext, scope: str) -> str:
        cid = context.company.cid
        if scope == 'company':
            return 'company'
        if scope == 'session':
            seed = f'{cid}:{context.agent.agentId}:session:{context.session.pk}'
            return f'sessions/{self._digest(seed)}'
        if context.user is None:
            raise MemoryPermissionDenied('User memory requires an authenticated user.')
        seed = f'{cid}:{context.user.pk}'
        return f'users/{self._digest(seed)}'

    @staticmethod
    def agent_user_namespace(context: TrustedMemoryContext) -> MemoryNamespace:
        """Return an Agent-specific user namespace for `agent_user` policy mode."""
        if context.user is None:
            raise MemoryPermissionDenied('User memory requires an authenticated user.')
        seed = f'{context.company.cid}:{context.agent.agentId}:{context.user.pk}'
        digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]
        user_id = context.agent_user_id
        return MemoryNamespace(
            scope='user',
            account_id=context.account_id,
            user_id=user_id,
            root_uri=f'viking://user/{user_id}/memories/users/{digest}/',
            credential_ref=context.agent_credential_ref,
        )

    @staticmethod
    def _digest(value) -> str:
        return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]
