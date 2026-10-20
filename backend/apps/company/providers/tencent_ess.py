from __future__ import annotations

import logging

from django.conf import settings
from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.essbasic.v20210526 import essbasic_client, models

from apps.company.providers.base import (
    ConsoleUrlInput,
    ConsoleUrlResult,
    EnterpriseVerificationProvider,
    OrganizationResult,
    VerificationProviderConfigurationError,
    VerificationProviderProtocolError,
    VerificationProviderUnavailableError,
)


logger = logging.getLogger('touming')


class TencentEssVerificationProvider(EnterpriseVerificationProvider):
    """Tencent ESS third-party application adapter.

    The rest of the application only receives domain dataclasses from this
    adapter and never imports Tencent response models.
    """

    def __init__(self):
        self._application_id = str(
            getattr(settings, 'TENCENT_ESS_APP_ID', '') or ''
        ).strip()
        secret_id = str(getattr(settings, 'TENCENT_ESS_SECRET_ID', '') or '').strip()
        secret_key = str(getattr(settings, 'TENCENT_ESS_SECRET_KEY', '') or '').strip()
        if (
            not self._application_id
            or len(self._application_id) > 64
            or not secret_id
            or not secret_key
        ):
            raise VerificationProviderConfigurationError(
                'Tencent ESS credentials are incomplete',
                code='provider_not_configured',
            )

        endpoint = str(
            getattr(settings, 'TENCENT_ESS_ENDPOINT', 'essbasic.tencentcloudapi.com')
            or 'essbasic.tencentcloudapi.com'
        ).strip()
        region = str(
            getattr(settings, 'TENCENT_ESS_REGION', 'ap-guangzhou') or ''
        ).strip()
        http_profile = HttpProfile()
        http_profile.endpoint = endpoint
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        self._client = essbasic_client.EssbasicClient(
            credential.Credential(secret_id, secret_key),
            region,
            client_profile,
        )

    @property
    def application_id(self) -> str:
        return self._application_id

    def _agent(self, organization_open_id: str, operator_open_id: str):
        operator = models.UserInfo()
        operator.OpenId = operator_open_id
        agent = models.Agent()
        agent.AppId = self.application_id
        agent.ProxyOrganizationOpenId = organization_open_id
        agent.ProxyOperator = operator
        return agent

    @staticmethod
    def _provider_exception(exc: TencentCloudSDKException):
        return VerificationProviderUnavailableError(
            'Tencent ESS request failed',
            code=str(getattr(exc, 'code', '') or ''),
            request_id=str(getattr(exc, 'requestId', '') or ''),
        )

    def create_console_url(self, request: ConsoleUrlInput) -> ConsoleUrlResult:
        sdk_request = models.CreateConsoleLoginUrlRequest()
        sdk_request.Agent = self._agent(
            request.proxy_organization_open_id,
            request.proxy_operator_open_id,
        )
        sdk_request.ProxyOrganizationName = request.organization_name
        sdk_request.UniformSocialCreditCode = request.unified_social_credit_code
        sdk_request.Endpoint = str(
            getattr(settings, 'TENCENT_ESS_CONSOLE_ENDPOINT', 'H5') or 'H5'
        ).strip()

        options = models.OrganizationAuthorizationOptions()
        options.OrganizationNameSame = True
        options.UniformSocialCreditCodeSame = True
        sdk_request.OrganizationAuthorizationOptions = options

        authorization_types = list(
            getattr(settings, 'TENCENT_ESS_AUTHORIZATION_TYPES', []) or []
        )
        if authorization_types:
            sdk_request.AuthorizationTypes = authorization_types

        # TODO(phase4-external): validate Endpoint, AuthorizationTypes and the
        # locking options against the provisioned Tencent ESS sandbox app.
        try:
            response = self._client.CreateConsoleLoginUrl(sdk_request)
        except TencentCloudSDKException as exc:
            raise self._provider_exception(exc) from exc
        except Exception as exc:
            raise VerificationProviderUnavailableError(
                'Tencent ESS request failed',
                code=type(exc).__name__,
            ) from exc

        verification_url = str(getattr(response, 'ConsoleUrl', '') or '').strip()
        request_id = str(getattr(response, 'RequestId', '') or '').strip()
        if not verification_url:
            raise VerificationProviderProtocolError(
                'Tencent ESS returned no ConsoleUrl',
                code='missing_console_url',
                request_id=request_id,
            )
        return ConsoleUrlResult(
            verification_url=verification_url,
            request_id=request_id,
        )

    def describe_organization(
        self,
        *,
        proxy_organization_open_id: str,
        proxy_operator_open_id: str,
    ) -> OrganizationResult:
        sdk_request = models.DescribeChannelOrganizationsRequest()
        sdk_request.Agent = self._agent(
            proxy_organization_open_id,
            proxy_operator_open_id,
        )
        sdk_request.OrganizationOpenId = proxy_organization_open_id
        sdk_request.Limit = 1
        sdk_request.Offset = 0

        # TODO(phase4-external): run this query with a real in-progress sandbox
        # organization and confirm the app's operator permission assignment.
        try:
            response = self._client.DescribeChannelOrganizations(sdk_request)
        except TencentCloudSDKException as exc:
            raise self._provider_exception(exc) from exc
        except Exception as exc:
            raise VerificationProviderUnavailableError(
                'Tencent ESS request failed',
                code=type(exc).__name__,
            ) from exc

        request_id = str(getattr(response, 'RequestId', '') or '').strip()
        organizations = list(
            getattr(response, 'ChannelOrganizationInfos', None) or []
        )
        if not organizations:
            return OrganizationResult(
                provider_status='UNVERIFIED',
                organization_open_id=proxy_organization_open_id,
                organization_name='',
                unified_social_credit_code='',
                request_id=request_id,
            )
        if len(organizations) != 1:
            raise VerificationProviderProtocolError(
                'Tencent ESS returned multiple organizations for an exact OpenId',
                code='ambiguous_organization',
                request_id=request_id,
            )

        organization = organizations[0]
        return OrganizationResult(
            provider_status=str(
                getattr(organization, 'AuthorizationStatus', '') or ''
            ).strip(),
            organization_open_id=str(
                getattr(organization, 'OrganizationOpenId', '') or ''
            ).strip(),
            organization_name=str(
                getattr(organization, 'OrganizationName', '') or ''
            ).strip(),
            unified_social_credit_code=str(
                getattr(organization, 'UnifiedSocialCreditCode', '') or ''
            ).strip(),
            request_id=request_id,
        )
