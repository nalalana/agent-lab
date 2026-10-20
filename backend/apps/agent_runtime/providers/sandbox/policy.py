"""Trusted company policy resolution for sandbox creation.

The model may request a sandbox, but resource and network decisions always
come from server-side settings and ``CompanyAgentRuntimePolicy``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from apps.agent_runtime.contracts import SandboxSpec
from apps.agent_runtime.models import CompanyAgentRuntimePolicy

from .errors import SandboxCreateError


@dataclass(frozen=True)
class ResolvedSandboxPolicy:
    company_id: object
    template: str
    cpu_cores: int
    memory_mib: int
    ttl_seconds: int
    network_policy: str
    max_concurrent_runs: int


class DjangoSandboxPolicyResolver:
    """Resolve an effective policy without trusting client-supplied values."""

    def resolve(self, spec: SandboxSpec) -> ResolvedSandboxPolicy:
        try:
            policy = CompanyAgentRuntimePolicy.objects.get(company_id=spec.company_id)
        except CompanyAgentRuntimePolicy.DoesNotExist as exc:
            raise SandboxCreateError("company sandbox policy is not configured") from exc

        template = str(policy.sandbox_template or getattr(settings, "AGSX_TEMPLATE", "")).strip()
        if not template:
            raise SandboxCreateError("sandbox template is not configured")

        network_policy = str(policy.network_policy or "isolated").strip().lower()
        if network_policy != "isolated":
            # V1 deliberately rejects broader policies even if a caller asks for one.
            raise SandboxCreateError("only isolated sandbox networking is supported")

        configured_ttl = int(getattr(settings, "AGSX_DEFAULT_TTL_SECONDS", 300))
        requested_ttl = max(int(spec.ttl_seconds), 1)
        ttl_seconds = min(requested_ttl, int(policy.max_sandbox_seconds), configured_ttl)
        if ttl_seconds <= 0:
            raise SandboxCreateError("sandbox TTL must be positive")

        cpu_cores = int(getattr(settings, "AGSX_DEFAULT_CPU_CORES", 2))
        memory_mib = int(getattr(settings, "AGSX_DEFAULT_MEMORY_MIB", 2048))
        if cpu_cores <= 0 or memory_mib <= 0:
            raise SandboxCreateError("sandbox resource settings must be positive")

        return ResolvedSandboxPolicy(
            company_id=policy.company_id,
            template=template,
            cpu_cores=cpu_cores,
            memory_mib=memory_mib,
            ttl_seconds=ttl_seconds,
            network_policy=network_policy,
            max_concurrent_runs=max(int(policy.max_concurrent_runs), 1),
        )
