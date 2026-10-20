"""Public model-provider API for the unified Agent runtime."""

from .credentials import (
    CompanyModelResolver,
    DjangoModelCredentialLoader,
    ResolvedCredential,
    ResolvedModelSelection,
)
from .errors import ModelConfigurationError, NormalizedModelError, normalize_model_error
from .openai_compatible import OpenAICompatibleModelGateway, ProviderHealth
from .registry import ModelGatewayRegistry

__all__ = [
    "CompanyModelResolver",
    "DjangoModelCredentialLoader",
    "ModelConfigurationError",
    "ModelGatewayRegistry",
    "NormalizedModelError",
    "OpenAICompatibleModelGateway",
    "ProviderHealth",
    "ResolvedCredential",
    "ResolvedModelSelection",
    "normalize_model_error",
]
