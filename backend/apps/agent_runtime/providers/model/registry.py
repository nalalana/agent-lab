"""Small explicit registry for model gateway implementations."""

from __future__ import annotations

from apps.agent_runtime.contracts import ModelGateway

from .errors import ModelConfigurationError


class ModelGatewayRegistry:
    def __init__(self) -> None:
        self._gateways: dict[str, ModelGateway] = {}

    def register(self, provider_id: str, gateway: ModelGateway) -> None:
        key = self._normalize(provider_id)
        if not key:
            raise ValueError("provider_id is required")
        self._gateways[key] = gateway

    def get(self, provider_id: str) -> ModelGateway:
        key = self._normalize(provider_id)
        gateway = self._gateways.get(key) or self._gateways.get("*")
        if gateway is None:
            raise ModelConfigurationError("模型提供方尚未注册")
        return gateway

    @staticmethod
    def _normalize(provider_id: str) -> str:
        return str(provider_id or "").strip().lower()
