"""Durable OpenViking account/user provisioning for LangGraph companies."""

from __future__ import annotations

from contextlib import suppress
from datetime import timedelta
import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any
from urllib.parse import quote

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
import requests

from apps.company.models import (
    CompanyVikingBinding,
    OpenVikingCluster,
    TransparentCompanyAgentVikingSpace,
)


class ProvisioningError(RuntimeError):
    pass


def _safe_ref_path(ref: str) -> Path:
    if not str(ref or '').startswith('file:'):
        raise ProvisioningError('Unsupported credential reference.')
    relative = PurePosixPath(str(ref)[5:])
    if relative.is_absolute() or not relative.parts or any(
        part in {'', '.', '..'} for part in relative.parts
    ):
        raise ProvisioningError('Unsafe credential reference.')
    base = Path(settings.OPENVIKING_CREDENTIALS_DIR).resolve()
    target = (base / Path(*relative.parts)).resolve()
    if base != target and base not in target.parents:
        raise ProvisioningError('Credential reference escaped its store.')
    return target


def read_credential(ref: str) -> str:
    path = _safe_ref_path(ref)
    try:
        stat = path.stat()
        if stat.st_mode & 0o077:
            raise ProvisioningError('Credential file permissions are too open.')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        try:
            value = os.read(fd, 8192).decode('utf-8').strip()
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise ProvisioningError('Credential reference is unavailable.') from exc
    if not value:
        raise ProvisioningError('Credential reference is empty.')
    return value


def write_credential(ref: str, value: str) -> None:
    target = _safe_ref_path(ref)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd, temp_path = tempfile.mkstemp(prefix='.credential-', dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, str(value).strip().encode('utf-8'))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_path)


class OpenVikingAdminClient:
    def __init__(self, endpoint: str, timeout: int):
        self.endpoint = str(endpoint).rstrip('/')
        self.timeout = max(int(timeout), 1)

    def create_account(self, root_key: str, account_id: str, admin_user_id: str) -> str:
        result = self._request(
            'POST',
            '/api/v1/admin/accounts',
            root_key,
            json={'account_id': account_id, 'admin_user_id': admin_user_id},
        )
        return self._required_key(result)

    def create_user(self, admin_key: str, account_id: str, user_id: str) -> str:
        path = f'/api/v1/admin/accounts/{quote(account_id, safe="")}/users'
        try:
            result = self._request(
                'POST', path, admin_key, json={'user_id': user_id, 'role': 'user'}
            )
        except ProvisioningError as exc:
            if 'status=409' not in str(exc):
                raise
            result = self._request(
                'POST',
                f'{path}/{quote(user_id, safe="")}/key',
                admin_key,
                json={},
            )
        return self._required_key(result)

    def write(self, key: str, uri: str, content: str) -> None:
        payload = {
            'uri': uri,
            'content': content,
            'mode': 'create',
            'wait': True,
            'timeout': self.timeout,
        }
        try:
            self._request('POST', '/api/v1/content/write', key, json=payload)
        except ProvisioningError as exc:
            if 'status=409' not in str(exc):
                raise
            payload['mode'] = 'replace'
            self._request('POST', '/api/v1/content/write', key, json=payload)

    def read(self, key: str, uri: str, *, allow_missing: bool = False) -> Any:
        return self._request(
            'GET', '/api/v1/content/read', key, params={'uri': uri}, allow_missing=allow_missing
        )

    def search(self, key: str, uri: str, query: str) -> list[dict]:
        result = self._request(
            'POST',
            '/api/v1/search/search',
            key,
            json={
                'query': query,
                'context_type': 'resource',
                'target_uri': uri.rsplit('/', 1)[0] + '/',
                'limit': 10,
            },
        )
        resources = result.get('resources', []) if isinstance(result, dict) else []
        return [item for item in resources if isinstance(item, dict)]

    def delete(self, key: str, uri: str, *, allow_missing: bool = False) -> Any:
        return self._request(
            'DELETE',
            '/api/v1/fs',
            key,
            params={'uri': uri, 'wait': 'true', 'timeout': self.timeout},
            allow_missing=allow_missing,
        )

    def _request(self, method, path, key, *, allow_missing=False, **kwargs):
        try:
            response = requests.request(
                method,
                f'{self.endpoint}{path}',
                headers={'X-API-Key': key},
                timeout=self.timeout,
                **kwargs,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ProvisioningError('OpenViking is unavailable.') from exc
        if allow_missing and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ProvisioningError(f'OpenViking request failed: status={response.status_code} path={path}')
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProvisioningError('OpenViking returned invalid JSON.') from exc
        if not isinstance(payload, dict) or payload.get('status') == 'error':
            raise ProvisioningError(f'OpenViking rejected request: path={path}')
        return payload.get('result')

    @staticmethod
    def _required_key(result) -> str:
        value = result.get('user_key') if isinstance(result, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ProvisioningError('OpenViking did not return a user credential.')
        return value.strip()


def enqueue_company_provisioning(binding_id: int, *, agent_id=None) -> None:
    provision_company_viking_binding.delay(binding_id, agent_id=agent_id)


@shared_task(bind=True, max_retries=5, name='apps.agent_runtime.provision_company_viking_binding')
def provision_company_viking_binding(self, binding_id: int, agent_id=None):
    preserve_active_binding = False
    with transaction.atomic():
        binding = CompanyVikingBinding.objects.select_for_update().select_related('cluster').get(
            pk=binding_id
        )
        if binding.status == CompanyVikingBinding.STATUS_ACTIVE and agent_id is None:
            return {'binding_id': binding_id, 'status': binding.status}
        if binding.cluster.status != OpenVikingCluster.STATUS_ACTIVE:
            raise ProvisioningError('OpenViking cluster is not active.')
        active_count = CompanyVikingBinding.objects.filter(
            cluster=binding.cluster,
            status=CompanyVikingBinding.STATUS_ACTIVE,
        ).exclude(pk=binding.pk).count()
        if active_count >= binding.cluster.capacity:
            raise ProvisioningError('OpenViking cluster company capacity is exhausted.')
        preserve_active_binding = bool(
            agent_id is not None and binding.status == CompanyVikingBinding.STATUS_ACTIVE
        )
        if not preserve_active_binding:
            binding.status = CompanyVikingBinding.STATUS_PROVISIONING
        binding.attempts = F('attempts') + 1
        binding.nextRetryAt = None
        binding.lastError = ''
        binding.save(update_fields=['status', 'attempts', 'nextRetryAt', 'lastError', 'updated_at'])

    try:
        _provision(binding_id, agent_id=agent_id)
    except Exception as exc:
        message = str(exc)[:2000] or exc.__class__.__name__
        retry_at = timezone.now() + timedelta(seconds=min(30 * (2 ** self.request.retries), 900))
        CompanyVikingBinding.objects.filter(pk=binding_id).update(
            status=(
                CompanyVikingBinding.STATUS_ACTIVE
                if preserve_active_binding
                else CompanyVikingBinding.STATUS_FAILED
            ),
            lastError=message,
            nextRetryAt=retry_at,
        )
        TransparentCompanyAgentVikingSpace.objects.filter(
            companyBinding_id=binding_id,
            **({'agent_id': agent_id} if agent_id else {}),
        ).exclude(status=TransparentCompanyAgentVikingSpace.STATUS_ARCHIVED).update(
            status=TransparentCompanyAgentVikingSpace.STATUS_FAILED,
            lastError=message,
        )
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=min(30 * (2 ** self.request.retries), 900))
    return {'binding_id': binding_id, 'status': CompanyVikingBinding.STATUS_ACTIVE}


@shared_task(name='apps.agent_runtime.retry_failed_viking_bindings')
def retry_failed_viking_bindings(limit: int = 50):
    now = timezone.now()
    stale_before = now - timedelta(
        seconds=max(int(settings.OPENVIKING_TIMEOUT) * 6, 1800)
    )
    ids = list(
        CompanyVikingBinding.objects.filter(
            Q(
                status__in=[
                    CompanyVikingBinding.STATUS_PENDING,
                    CompanyVikingBinding.STATUS_FAILED,
                ],
            )
            & (Q(nextRetryAt__isnull=True) | Q(nextRetryAt__lte=now))
            | Q(
                status=CompanyVikingBinding.STATUS_PROVISIONING,
                updated_at__lte=stale_before,
            )
        )
        .order_by('created_at').values_list('pk', flat=True)[:limit]
    )
    for binding_id in ids:
        provision_company_viking_binding.delay(binding_id)
    return {'queued': len(ids)}


def _provision(binding_id: int, *, agent_id=None) -> None:
    binding = CompanyVikingBinding.objects.select_related('cluster').get(pk=binding_id)
    client = OpenVikingAdminClient(binding.cluster.endpoint, settings.OPENVIKING_TIMEOUT)

    if not binding.adminCredentialRef:
        root_key = read_credential(binding.cluster.rootCredentialRef)
        admin_key = client.create_account(root_key, binding.accountId, binding.adminUserId)
        admin_ref = f'file:companies/{binding.accountId}/admin.key'
        write_credential(admin_ref, admin_key)
        CompanyVikingBinding.objects.filter(pk=binding.pk).update(adminCredentialRef=admin_ref)
        binding.adminCredentialRef = admin_ref
    admin_key = read_credential(binding.adminCredentialRef)

    if not binding.runtimeCredentialRef:
        runtime_key = client.create_user(admin_key, binding.accountId, binding.runtimeUserId)
        runtime_ref = f'file:companies/{binding.accountId}/runtime.key'
        write_credential(runtime_ref, runtime_key)
        CompanyVikingBinding.objects.filter(pk=binding.pk).update(runtimeCredentialRef=runtime_ref)
        binding.runtimeCredentialRef = runtime_ref
    runtime_key = read_credential(binding.runtimeCredentialRef)

    spaces = TransparentCompanyAgentVikingSpace.objects.filter(companyBinding=binding).exclude(
        status=TransparentCompanyAgentVikingSpace.STATUS_ARCHIVED
    )
    if agent_id is not None:
        spaces = spaces.filter(agent_id=agent_id)
    for space in spaces:
        TransparentCompanyAgentVikingSpace.objects.filter(pk=space.pk).update(
            status=TransparentCompanyAgentVikingSpace.STATUS_PROVISIONING,
            lastError='',
        )
        credential_ref = space.credentialRef
        if not credential_ref:
            user_key = client.create_user(admin_key, binding.accountId, space.userId)
            credential_ref = f'file:companies/{binding.accountId}/agents/{space.userId}.key'
            write_credential(credential_ref, user_key)
        else:
            read_credential(credential_ref)
        TransparentCompanyAgentVikingSpace.objects.filter(pk=space.pk).update(
            credentialRef=credential_ref,
            status=TransparentCompanyAgentVikingSpace.STATUS_ACTIVE,
            lastError='',
        )

    _validate_isolation(client, binding, runtime_key)
    CompanyVikingBinding.objects.filter(pk=binding.pk).update(
        status=CompanyVikingBinding.STATUS_ACTIVE,
        provisionedAt=timezone.now(),
        nextRetryAt=None,
        lastError='',
    )


def _validate_isolation(client, binding, runtime_key: str) -> None:
    nonce = hashlib.sha256(f'{binding.accountId}:{binding.attempts}'.encode()).hexdigest()[:24]
    uri = f'viking://resources/touming-isolation-tests/{binding.accountId}/{nonce}.txt'
    content = f'touming isolation canary {nonce}'
    client.write(runtime_key, uri, content)
    if content not in str(client.read(runtime_key, uri)):
        raise ProvisioningError('OpenViking same-tenant read acceptance failed.')
    matches = client.search(runtime_key, uri, content)
    expected_search_prefix = uri.rsplit('/', 1)[0].rstrip('/') + '/'
    if not any(
        str(item.get('uri', '')).startswith(expected_search_prefix)
        for item in matches
    ):
        raise ProvisioningError('OpenViking same-tenant search acceptance failed.')

    sentinel_key = read_credential(settings.OPENVIKING_ISOLATION_CREDENTIAL_REF)
    if client.read(sentinel_key, uri, allow_missing=True) is not None:
        raise ProvisioningError('OpenViking cross-tenant read isolation failed.')
    if client.search(sentinel_key, uri, content):
        raise ProvisioningError('OpenViking cross-tenant search isolation failed.')
    client.delete(sentinel_key, uri, allow_missing=True)
    if content not in str(client.read(runtime_key, uri)):
        raise ProvisioningError('OpenViking cross-tenant delete isolation failed.')
    client.delete(runtime_key, uri)
    if client.read(runtime_key, uri, allow_missing=True) is not None:
        raise ProvisioningError('OpenViking same-tenant delete acceptance failed.')
