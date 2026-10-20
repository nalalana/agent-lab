"""Resolve company model policy without exposing credential values.

Configuration resolution and secret loading are intentionally separate.  A
snapshot or ``ModelRequest`` receives only ``credential_ref``; plaintext exists
only for the short period in which an SDK client is constructed.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings

from apps.company.models import Company

from .errors import ModelConfigurationError


PROVIDER_KEY_ENVIRONMENTS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "qwen": ("QWEN_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "kimi": ("MOONSHOT_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "glm": ("GLM_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


@dataclass(frozen=True)
class ResolvedModelSelection:
    """Secret-free model selection safe for Run state and snapshots."""

    provider: str
    model: str
    credential_ref: str
    timeout_seconds: int
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ResolvedCredential:
    """Ephemeral SDK credential; never serialize or include in logs."""

    api_key: str
    base_url: str | None


class CompanyModelResolver:
    """Resolve the active company policy into a secret-free selection."""

    def resolve(self, company: Company) -> ResolvedModelSelection:
        # Platform configuration is authoritative. Catalog rows are optional
        # control-plane metadata and must never block a newly created company.
        provider_id = str(
            getattr(settings, "AGENT_RUNTIME_PLATFORM_PROVIDER_ID", "custom-qwen") or ""
        ).strip()
        model_id = str(
            getattr(settings, "AGENT_RUNTIME_PLATFORM_MODEL_ID", "qwen3.8-27b") or ""
        ).strip()
        if not provider_id or not model_id:
            raise ModelConfigurationError("平台统一模型尚未配置")

        credential_ref = self._credential_ref(provider_id)
        return ResolvedModelSelection(
            provider=provider_id,
            model=model_id,
            credential_ref=credential_ref,
            timeout_seconds=max(
                1,
                int(getattr(settings, "AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS", 30)),
            ),
        )

    @staticmethod
    def _credential_ref(provider_id: str) -> str:
        # BYOK is intentionally unsupported: every run uses the platform secret.
        return f"platform:{provider_id}"


class DjangoModelCredentialLoader:
    """Load and validate a credential referenced by ``ModelRequest``."""

    def load(self, credential_ref: str, provider_id: str) -> ResolvedCredential:
        parts = str(credential_ref or "").split(":")
        if not parts:
            raise ModelConfigurationError()
        if parts[0] == "platform" and len(parts) == 2:
            return self._load_platform(parts[1], provider_id)
        raise ModelConfigurationError("模型密钥引用无效")

    def _load_platform(self, ref_provider: str, provider_id: str) -> ResolvedCredential:
        if ref_provider != provider_id:
            raise ModelConfigurationError("模型密钥引用与提供方不匹配")
        api_key = self._platform_key(provider_id)
        if not api_key:
            raise ModelConfigurationError("平台模型密钥未配置")
        configured_provider = str(getattr(settings, "AGENT_RUNTIME_PLATFORM_PROVIDER_ID", "custom-qwen"))
        base_url = (
            str(getattr(settings, "AGENT_RUNTIME_PLATFORM_BASE_URL", "")).strip()
            if provider_id == configured_provider
            else ""
        )
        return ResolvedCredential(
            api_key=api_key,
            base_url=self._validated_base_url(base_url),
        )

    @staticmethod
    def _decode_legacy_secret(value: str) -> str:
        """Decode the existing storage format without ever logging its value."""

        if not value:
            return ""
        try:
            return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
        except (UnicodeError, ValueError, binascii.Error):
            return ""

    @staticmethod
    def _platform_key(provider_id: str) -> str:
        platform_provider = str(getattr(settings, "AGENT_RUNTIME_PLATFORM_PROVIDER_ID", "custom-qwen"))
        if provider_id == platform_provider:
            value = str(getattr(settings, "AGENT_RUNTIME_PLATFORM_API_KEY", "") or "").strip()
            if value:
                return value
        configured = getattr(settings, "AGENT_RUNTIME_MODEL_API_KEYS", {})
        if isinstance(configured, dict):
            value = str(configured.get(provider_id) or "").strip()
            if value:
                return value
        env_names = PROVIDER_KEY_ENVIRONMENTS.get(
            provider_id.lower(),
            (f"{provider_id.upper().replace('-', '_')}_API_KEY",),
        )
        for env_name in env_names:
            value = os.getenv(env_name, "").strip()
            if value:
                return value
        # Custom platform-managed providers may not have a stable provider-specific
        # environment variable. Keep the generic platform credential as a final
        # fallback while still preferring explicitly scoped provider keys above.
        value = os.getenv("AGENT_RUNTIME_PLATFORM_API_KEY", "").strip()
        if value:
            return value
        return ""

    @staticmethod
    def _validated_base_url(value: str | None) -> str | None:
        normalized = str(value or "").strip().rstrip("/")
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        allow_insecure = bool(
            getattr(settings, "AGENT_RUNTIME_ALLOW_INSECURE_MODEL_BASE_URL", False)
        )
        trusted_platform_url = str(
            getattr(settings, "AGENT_RUNTIME_PLATFORM_BASE_URL", "") or ""
        ).strip().rstrip("/")
        allow_platform_http = bool(trusted_platform_url and normalized == trusted_platform_url)
        allowed_schemes = {"https", "http"} if allow_insecure or allow_platform_http else {"https"}
        if parsed.scheme not in allowed_schemes:
            raise ModelConfigurationError("模型提供方地址必须使用 HTTPS")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelConfigurationError("模型提供方地址无效")
        host = parsed.hostname.lower()
        allowed_hosts = tuple(
            str(item).lower()
            for item in getattr(settings, "AGENT_RUNTIME_MODEL_ALLOWED_HOSTS", ())
            if str(item).strip()
        )
        if allowed_hosts and host not in allowed_hosts:
            raise ModelConfigurationError("模型提供方地址不在允许列表")
        if not allowed_hosts and DjangoModelCredentialLoader._is_non_public_host(host):
            raise ModelConfigurationError("模型提供方地址不能指向本机或私有网络")
        return normalized

    @staticmethod
    def _is_non_public_host(host: str) -> bool:
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return not address.is_global
