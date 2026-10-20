"""Deployment checks for explicitly enabled AGSX execution."""

from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def check_agsx_configuration(app_configs, **kwargs):
    if not bool(getattr(settings, "AGSX_ENABLED", False)):
        return []

    errors = []
    domain = str(getattr(settings, "E2B_DOMAIN", "")).strip()
    if not domain or "://" in domain or "/" in domain:
        errors.append(
            Error(
                "E2B_DOMAIN must contain only the AGSX endpoint domain.",
                id="agent_runtime.E001",
            )
        )
    if not str(getattr(settings, "E2B_API_KEY", "")).strip():
        errors.append(Error("E2B_API_KEY is required when AGSX is enabled.", id="agent_runtime.E002"))
    if not str(getattr(settings, "AGSX_TEMPLATE", "")).strip():
        errors.append(Error("AGSX_TEMPLATE is required when AGSX is enabled.", id="agent_runtime.E003"))
    if str(getattr(settings, "AGSX_NETWORK_POLICY", "isolated")).lower() != "isolated":
        errors.append(Error("AGSX V1 network policy must be isolated.", id="agent_runtime.E004"))
    try:
        apps.get_model("agent_runtime", "SandboxExecution")
    except LookupError:
        errors.append(
            Error(
                "SandboxExecution model/Migration must be merged before AGSX is enabled.",
                id="agent_runtime.E005",
            )
        )

    artifact_names = (
        "AGENT_RUNTIME_ARTIFACT_COS_BUCKET",
        "AGENT_RUNTIME_ARTIFACT_COS_REGION",
        "AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID",
        "AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY",
    )
    missing = [name for name in artifact_names if not str(getattr(settings, name, "")).strip()]
    if missing:
        errors.append(
            Error(
                "Dedicated artifact COS configuration is incomplete.",
                hint=", ".join(missing),
                id="agent_runtime.E006",
            )
        )
    if int(getattr(settings, "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES", 0)) <= 0:
        errors.append(
            Error(
                "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES must be positive.",
                id="agent_runtime.E007",
            )
        )
    if not bool(getattr(settings, "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR", True)):
        errors.append(
            Error(
                "AGSX quota control must fail closed in an enabled deployment.",
                id="agent_runtime.E008",
            )
        )
    if int(
        getattr(settings, "AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G", 0)
    ) <= 0:
        errors.append(
            Error(
                "AGSX 2C2G estimated cost per second must be configured.",
                id="agent_runtime.E009",
            )
        )
    currency = str(getattr(settings, "AGSX_COST_CURRENCY", "")).strip()
    if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
        errors.append(
            Error(
                "AGSX_COST_CURRENCY must be a three-letter uppercase currency code.",
                id="agent_runtime.E010",
            )
        )
    budget_names = (
        "AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS",
        "AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS",
    )
    if any(int(getattr(settings, name, 0)) < 0 for name in budget_names):
        errors.append(
            Error(
                "AGSX daily cost budgets cannot be negative.",
                id="agent_runtime.E011",
            )
        )
    return errors
