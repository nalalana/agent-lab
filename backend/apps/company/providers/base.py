from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class VerificationProviderError(Exception):
    """Safe provider failure carrying diagnostics that never enter the API contract."""

    def __init__(self, message: str, *, code: str = '', request_id: str = ''):
        super().__init__(message)
        self.code = str(code or '')
        self.request_id = str(request_id or '')


class VerificationProviderConfigurationError(VerificationProviderError):
    pass


class VerificationProviderUnavailableError(VerificationProviderError):
    pass


class VerificationProviderProtocolError(VerificationProviderError):
    pass


@dataclass(frozen=True)
class ConsoleUrlInput:
    proxy_organization_open_id: str
    proxy_operator_open_id: str
    organization_name: str
    unified_social_credit_code: str


@dataclass(frozen=True)
class ConsoleUrlResult:
    verification_url: str
    request_id: str = ''


@dataclass(frozen=True)
class OrganizationResult:
    provider_status: str
    organization_open_id: str
    organization_name: str
    unified_social_credit_code: str
    request_id: str = ''


class EnterpriseVerificationProvider(ABC):
    @property
    @abstractmethod
    def application_id(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def create_console_url(self, request: ConsoleUrlInput) -> ConsoleUrlResult:
        raise NotImplementedError

    @abstractmethod
    def describe_organization(
        self,
        *,
        proxy_organization_open_id: str,
        proxy_operator_open_id: str,
    ) -> OrganizationResult:
        raise NotImplementedError
