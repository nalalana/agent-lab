"""Small deterministic database fixture shared by Phase 2 memory tests."""

import hashlib

from apps.company.models import (
    Company,
    CompanySessionMemoryPolicy,
    CompanyUserMemoryPolicy,
    CompanyVikingBinding,
    OpenVikingCluster,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentTestSession,
    TransparentCompanyAgentUserMemoryBinding,
    TransparentCompanyAgentVikingSpace,
)
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User


def create_memory_fixture(prefix='phase2'):
    owner = _user(f'{prefix}-owner')
    second_user = _user(f'{prefix}-user-2')
    company_a = _company(owner, f'{prefix}-company-a')
    company_b = _company(owner, f'{prefix}-company-b')
    principal = get_or_create_principal(Principal.TYPE_USER, owner.uid)
    for company in (company_a, company_b):
        resource = get_or_create_resource(Resource.TYPE_COMPANY, company.cid)
        grant_permissions(principal, resource, Grant.PERM_SUPER_ADMIN, granter_user=owner)
    transparent_a = TransparentCompany.objects.create(company=company_a, prompts='A')
    transparent_b = TransparentCompany.objects.create(company=company_b, prompts='B')
    cluster = OpenVikingCluster.objects.create(
        name=f'{prefix}-cluster',
        endpoint='http://openviking:1933',
        rootCredentialRef='file:root.key',
        isDefault=True,
    )
    binding_a = _binding(company_a, cluster)
    binding_b = _binding(company_b, cluster)
    agent_a1 = _agent(transparent_a, 'agent-a1')
    agent_a2 = _agent(transparent_a, 'agent-a2')
    agent_b1 = _agent(transparent_b, 'agent-b1')
    session_a1 = _session(transparent_a, agent_a1, owner, f'{prefix}-session-a1')
    session_a2 = _session(transparent_a, agent_a2, owner, f'{prefix}-session-a2')
    session_b1 = _session(transparent_b, agent_b1, owner, f'{prefix}-session-b1')
    for company, transparent, agent in (
        (company_a, transparent_a, agent_a1),
        (company_a, transparent_a, agent_a2),
        (company_b, transparent_b, agent_b1),
    ):
        binding = binding_a if company == company_a else binding_b
        _viking_space(company, transparent, agent, binding)
        _permission(transparent, agent)
        _session_binding(transparent, agent)
        _user_binding(transparent, agent)
    _memory_policies(company_a)
    _memory_policies(company_b)
    return {
        'owner': owner,
        'second_user': second_user,
        'company_a': company_a,
        'company_b': company_b,
        'agent_a1': agent_a1,
        'agent_a2': agent_a2,
        'agent_b1': agent_b1,
        'session_a1': session_a1,
        'session_a2': session_a2,
        'session_b1': session_b1,
    }


def _user(uid):
    return User.objects.create(
        uid=uid,
        username=uid,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _company(owner, cid):
    return Company.objects.create(
        account=owner,
        cid=cid,
        company_name=cid,
        verification_status=Company.VERIFICATION_STATUS_VERIFIED,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _agent(transparent, agent_id):
    return TransparentCompanyAgentDefinition.objects.create(
        transparentCompany=transparent,
        agentId=agent_id,
        name=agent_id,
        enabled=True,
        status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _session(transparent, agent, operator, conversation_id):
    return TransparentCompanyAgentTestSession.objects.create(
        transparentCompany=transparent,
        agent=agent,
        operator=operator,
        conversationId=conversation_id,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _binding(company, cluster):
    account = f'tm-{_digest(company.cid)}'
    return CompanyVikingBinding.objects.create(
        company=company,
        cluster=cluster,
        accountId=account,
        adminUserId='touming-admin',
        runtimeUserId='touming-runtime',
        adminCredentialRef=f'file:companies/{account}/admin.key',
        runtimeCredentialRef=f'file:companies/{account}/runtime.key',
        status=CompanyVikingBinding.STATUS_ACTIVE,
    )


def _viking_space(company, transparent, agent, binding):
    account = _digest(company.cid)
    user = _digest(f'{company.cid}:{agent.agentId}')
    TransparentCompanyAgentVikingSpace.objects.create(
        transparentCompany=transparent,
        agent=agent,
        companyBinding=binding,
        vikingSpaceId=f'tm-{account}:agent-{user}',
        accountId=f'tm-{account}',
        userId=f'agent-{user}',
        rootUri=f'viking://user/agent-{user}/memories/',
        credentialRef=f'file:companies/{binding.accountId}/agents/agent-{user}.key',
        status=TransparentCompanyAgentVikingSpace.STATUS_ACTIVE,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _permission(transparent, agent):
    TransparentCompanyAgentPermissionBinding.objects.create(
        transparentCompany=transparent,
        agent=agent,
        enabled=True,
        status=TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
        memoryRead=True,
        memoryWrite=True,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _session_binding(transparent, agent):
    TransparentCompanyAgentMemoryBinding.objects.create(
        transparentCompany=transparent,
        agent=agent,
        enabled=True,
        status=TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE,
        memoryScope=TransparentCompanyAgentMemoryBinding.SCOPE_SESSION,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _user_binding(transparent, agent):
    TransparentCompanyAgentUserMemoryBinding.objects.create(
        transparentCompany=transparent,
        agent=agent,
        enabled=True,
        status=TransparentCompanyAgentUserMemoryBinding.STATUS_ACTIVE,
        memoryScope=TransparentCompanyAgentUserMemoryBinding.SCOPE_COMPANY_USER,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _memory_policies(company):
    CompanySessionMemoryPolicy.objects.create(
        company=company,
        enabled=True,
        status=CompanySessionMemoryPolicy.STATUS_ACTIVE,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )
    CompanyUserMemoryPolicy.objects.create(
        company=company,
        enabled=True,
        status=CompanyUserMemoryPolicy.STATUS_ACTIVE,
        writebackEnabled=True,
        writebackMode=CompanyUserMemoryPolicy.WRITEBACK_MODE_SHADOW,
        created_at='2026-08-12',
        updated_at='2026-08-12',
    )


def _digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]
