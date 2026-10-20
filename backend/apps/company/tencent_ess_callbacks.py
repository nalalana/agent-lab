from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.company.models import CompanyVerification, TencentEssEvent
from apps.company.providers.base import EnterpriseVerificationProvider
from apps.company.verification import VerificationFlowError, refresh_company_verification


logger = logging.getLogger('touming')


class CallbackValidationError(Exception):
    def __init__(self, code: str, *, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class CallbackProcessResult:
    duplicate: bool
    event_id: Optional[int]


def verify_content_signature(raw_body: bytes, signature: str, verify_token: str) -> bool:
    if not verify_token or not signature:
        return False
    provided = str(signature).strip()
    if provided.lower().startswith('sha256='):
        provided = provided.split('=', 1)[1]
    expected = hmac.new(
        verify_token.encode('utf-8'),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


def _decrypt_callback(ciphertext: str, key_text: str) -> bytes:
    key = key_text.encode('utf-8')
    if len(key) != 32:
        raise CallbackValidationError('invalid_encryption_key', status_code=503)
    try:
        encrypted = base64.b64decode(ciphertext, validate=True)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except CallbackValidationError:
        raise
    except Exception as exc:
        raise CallbackValidationError('invalid_encrypted_payload') from exc


def decode_callback_payload(raw_body: bytes, encryption_key: str = '') -> dict:
    try:
        envelope = json.loads(raw_body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CallbackValidationError('invalid_json') from exc
    if not isinstance(envelope, dict):
        raise CallbackValidationError('invalid_payload')

    encrypted_value = envelope.get('encrypt') or envelope.get('Encrypt')
    if encrypted_value:
        if not encryption_key:
            raise CallbackValidationError('encryption_not_configured', status_code=503)
        # TODO(phase4-external): confirm whether the provisioned CallbackUrlKey
        # is delivered as 32 UTF-8 bytes or another encoded representation.
        decrypted = _decrypt_callback(str(encrypted_value), encryption_key)
        try:
            payload = json.loads(decrypted.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CallbackValidationError('invalid_decrypted_payload') from exc
        if not isinstance(payload, dict):
            raise CallbackValidationError('invalid_decrypted_payload')
        return payload

    if encryption_key:
        raise CallbackValidationError('unencrypted_payload_rejected')
    return envelope


def _lookup(payload: Any, *names: str):
    wanted = {name.lower() for name in names}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in wanted and value not in (None, ''):
                return value
        for value in payload.values():
            found = _lookup(value, *names)
            if found not in (None, ''):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _lookup(value, *names)
            if found not in (None, ''):
                return found
    return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'success'}


def _pending_verification(organization_open_id: str, application_id: str):
    return CompanyVerification.objects.select_related('company').filter(
        provider=CompanyVerification.PROVIDER_TENCENT_ESS,
        proxy_organization_open_id=organization_open_id,
        application_id=application_id,
        status=CompanyVerification.STATUS_PENDING,
    ).order_by('-id').first()


def _mark_event(event: TencentEssEvent, status: str, failure_reason: str = ''):
    event.process_status = status
    event.failure_reason = str(failure_reason or '')[:255]
    event.processed_at = timezone.now()
    event.save(update_fields=['process_status', 'failure_reason', 'processed_at'])


def _process_event(
    event: TencentEssEvent,
    payload: dict,
    provider: Optional[EnterpriseVerificationProvider],
):
    msg_type = event.msg_type
    if msg_type == 'OrgAuth':
        _mark_event(event, TencentEssEvent.PROCESS_STATUS_PROCESSED)
        return

    if msg_type == 'OrganizationAuthorization':
        # TODO(phase4-external): validate the exact operation key and nesting
        # against signed Create and Invalid callback samples from Tencent ESS.
        operation = str(
            _lookup(payload, 'Operate', 'Operation', 'Action', 'Event') or ''
        ).strip().lower()
        verification = _pending_verification(
            event.proxy_organization_open_id,
            event.application_id,
        )
        if not verification:
            _mark_event(event, TencentEssEvent.PROCESS_STATUS_IGNORED, 'pending_not_found')
            return
        if operation == 'create':
            if event.authorization_id:
                CompanyVerification.objects.filter(
                    pk=verification.pk,
                    status=CompanyVerification.STATUS_PENDING,
                ).update(authorization_id=event.authorization_id)
            _mark_event(event, TencentEssEvent.PROCESS_STATUS_PROCESSED)
            return
        if operation == 'invalid':
            if (
                verification.authorization_id
                and event.authorization_id != verification.authorization_id
            ):
                _mark_event(
                    event,
                    TencentEssEvent.PROCESS_STATUS_IGNORED,
                    'authorization_id_mismatch',
                )
                return
            now = timezone.now()
            CompanyVerification.objects.filter(
                pk=verification.pk,
                status=CompanyVerification.STATUS_PENDING,
            ).update(
                status=CompanyVerification.STATUS_EXPIRED,
                failure_reason='authorization_invalid',
                finished_at=now,
            )
            _mark_event(event, TencentEssEvent.PROCESS_STATUS_PROCESSED)
            return
        _mark_event(event, TencentEssEvent.PROCESS_STATUS_IGNORED, 'unknown_operation')
        return

    if msg_type == 'OrgOpenTsignBiz' and _as_bool(_lookup(payload, 'OpenSuccess')):
        verification = _pending_verification(
            event.proxy_organization_open_id,
            event.application_id,
        )
        if not verification:
            _mark_event(event, TencentEssEvent.PROCESS_STATUS_IGNORED, 'pending_not_found')
            return
        try:
            refresh_company_verification(
                company=verification.company,
                verification=verification,
                provider=provider,
            )
        except VerificationFlowError as exc:
            _mark_event(event, TencentEssEvent.PROCESS_STATUS_FAILED, exc.code)
            return
        _mark_event(event, TencentEssEvent.PROCESS_STATUS_PROCESSED)
        return

    _mark_event(event, TencentEssEvent.PROCESS_STATUS_IGNORED)


def process_tencent_ess_callback(
    *,
    raw_body: bytes,
    content_signature: str,
    provider: Optional[EnterpriseVerificationProvider] = None,
) -> CallbackProcessResult:
    verify_token = str(
        getattr(settings, 'TENCENT_ESS_CALLBACK_VERIFY_TOKEN', '') or ''
    )
    if not verify_token:
        raise CallbackValidationError('callback_not_configured', status_code=503)
    if not verify_content_signature(raw_body, content_signature, verify_token):
        raise CallbackValidationError('invalid_signature', status_code=401)

    encryption_key = str(
        getattr(settings, 'TENCENT_ESS_CALLBACK_ENCRYPT_KEY', '') or ''
    )
    payload = decode_callback_payload(raw_body, encryption_key)
    application_id = str(_lookup(payload, 'ApplicationId') or '').strip()
    configured_application_id = str(
        getattr(settings, 'TENCENT_ESS_APP_ID', '') or ''
    ).strip()
    if not application_id or application_id != configured_application_id:
        raise CallbackValidationError('application_id_mismatch', status_code=403)

    msg_id = str(_lookup(payload, 'MsgId') or '').strip()
    msg_type = str(_lookup(payload, 'MsgType') or '').strip()
    if not msg_id or not msg_type:
        raise CallbackValidationError('missing_message_identity')

    event_defaults = {
        'msg_type': msg_type,
        'msg_version': str(_lookup(payload, 'MsgVersion') or '')[:64],
        'proxy_organization_open_id': str(
            _lookup(payload, 'ProxyOrganizationOpenId') or ''
        )[:255],
        'proxy_operator_open_id': (
            str(_lookup(payload, 'ProxyOperatorOpenId') or '')[:255] or None
        ),
        'authorization_id': (
            str(_lookup(payload, 'AuthorizationId') or '')[:255] or None
        ),
        'auth_success': (
            _as_bool(_lookup(payload, 'AuthSuccess'))
            if _lookup(payload, 'AuthSuccess') is not None
            else None
        ),
        'payload_digest': hashlib.sha256(raw_body).hexdigest(),
    }
    with transaction.atomic():
        event, created = TencentEssEvent.objects.get_or_create(
            application_id=application_id,
            msg_id=msg_id,
            defaults=event_defaults,
        )
    if not created:
        return CallbackProcessResult(duplicate=True, event_id=event.id)

    logger.info(
        '[tencent-ess-callback] application_id=%s msg_id=%s msg_type=%s '
        'authorization_id=%s',
        application_id,
        msg_id,
        msg_type,
        event.authorization_id or '',
    )
    _process_event(event, payload, provider)
    return CallbackProcessResult(duplicate=False, event_id=event.id)
