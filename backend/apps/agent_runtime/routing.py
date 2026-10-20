"""Stable per-company and per-session runtime-engine routing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from apps.agent_runtime.models import CompanyAgentRuntimePolicy


@dataclass(frozen=True)
class EngineRoute:
    engine: str
    policy_mode: str
    canary_bucket: int | None
    config_version: int
    reason: str


class RuntimeEngineRouter:
    """Choose an engine once, using only trusted server-side identifiers."""

    def select(self, *, company_id: object, session_id: object) -> EngineRoute:
        policy = CompanyAgentRuntimePolicy.objects.filter(company_id=company_id).first()
        return self.select_policy(policy, company_id=company_id, session_id=session_id)

    def select_policy(
        self,
        policy,
        *,
        company_id: object,
        session_id: object,
    ) -> EngineRoute:
        if policy is None:
            return EngineRoute("openclaw", "legacy", None, 0, "missing_policy")
        mode = str(policy.mode or "legacy")
        version = int(policy.config_version or 0)
        if mode == CompanyAgentRuntimePolicy.MODE_UNIFIED:
            return EngineRoute("unified", mode, None, version, "explicit_unified")
        if mode != CompanyAgentRuntimePolicy.MODE_CANARY:
            return EngineRoute("openclaw", "legacy", None, version, "explicit_legacy")

        percentage = min(max(int(policy.canary_percentage or 0), 0), 100)
        bucket = self.stable_bucket(company_id=company_id, session_id=session_id)
        if bucket < percentage:
            return EngineRoute("unified", mode, bucket, version, "canary_selected")
        return EngineRoute("openclaw", mode, bucket, version, "canary_not_selected")

    @staticmethod
    def stable_bucket(*, company_id: object, session_id: object) -> int:
        material = f"{company_id}:{session_id}".encode("utf-8")
        digest = hashlib.sha256(material).digest()
        return int.from_bytes(digest[:8], "big", signed=False) % 100
