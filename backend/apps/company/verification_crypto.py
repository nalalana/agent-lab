"""PII protection boundary for enterprise verification applications."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings


class VerificationCryptoError(Exception):
    """Raised when identity protection is unavailable or input is invalid."""


@dataclass(frozen=True)
class ProtectedIdentity:
    ciphertext: str
    masked: str
    digest: str
    key_version: str


def normalize_identity_number(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if not 4 <= len(normalized) <= 64:
        raise VerificationCryptoError("invalid_identity")
    return normalized


def mask_identity_number(value: str) -> str:
    normalized = normalize_identity_number(value)
    if len(normalized) <= 7:
        return normalized[:1] + "*" * (len(normalized) - 2) + normalized[-1:]
    return normalized[:3] + "*" * (len(normalized) - 7) + normalized[-4:]


class AESGCMIdentityProtector:
    """AES-GCM/HMAC protector backed by caller-supplied local key material."""

    def __init__(self, encryption_key: bytes, digest_key: bytes, key_version: str):
        if len(encryption_key) not in (16, 24, 32) or len(digest_key) < 32:
            raise VerificationCryptoError("invalid_key")
        self._encryption_key = encryption_key
        self._digest_key = digest_key
        self.key_version = str(key_version or "").strip()
        if not self.key_version:
            raise VerificationCryptoError("invalid_key_version")

    @classmethod
    def from_base64(cls, encryption_key: str, digest_key: str, key_version: str):
        try:
            encryption_bytes = base64.b64decode(encryption_key, validate=True)
            digest_bytes = base64.b64decode(digest_key, validate=True)
        except (TypeError, ValueError) as exc:
            raise VerificationCryptoError("invalid_key") from exc
        return cls(encryption_bytes, digest_bytes, key_version)

    def digest(self, identity_number: str) -> str:
        normalized = normalize_identity_number(identity_number)
        return hmac.new(
            self._digest_key,
            b"company-verification-identity:" + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def protect(self, identity_number: str, *, context: str) -> ProtectedIdentity:
        normalized = normalize_identity_number(identity_number)
        nonce = os.urandom(12)
        aad = str(context).encode("utf-8")
        ciphertext = AESGCM(self._encryption_key).encrypt(
            nonce,
            normalized.encode("utf-8"),
            aad,
        )
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return ProtectedIdentity(
            ciphertext=f"aesgcm.{self.key_version}.{encoded}",
            masked=mask_identity_number(normalized),
            digest=self.digest(normalized),
            key_version=self.key_version,
        )

    def protect_secret(self, value: str, *, context: str) -> str:
        plaintext = str(value or "")
        if not plaintext or len(plaintext.encode("utf-8")) > 8192:
            raise VerificationCryptoError("invalid_secret")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._encryption_key).encrypt(
            nonce, plaintext.encode("utf-8"), str(context).encode("utf-8")
        )
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"aesgcm.{self.key_version}.{encoded}"

    def digest_secret(self, value: str, *, purpose: str) -> str:
        plaintext = str(value or "")
        if not plaintext:
            raise VerificationCryptoError("invalid_secret")
        return hmac.new(
            self._digest_key,
            f"company-verification-{purpose}:".encode("utf-8") + plaintext.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def decrypt(self, protected: str, *, context: str) -> str:
        try:
            algorithm, key_version, encoded = str(protected).split(".", 2)
            if algorithm != "aesgcm" or key_version != self.key_version:
                raise VerificationCryptoError("key_version_unavailable")
            payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
            plaintext = AESGCM(self._encryption_key).decrypt(
                payload[:12],
                payload[12:],
                str(context).encode("utf-8"),
            )
            return plaintext.decode("utf-8")
        except VerificationCryptoError:
            raise
        except Exception as exc:
            raise VerificationCryptoError("decrypt_failed") from exc


def get_identity_protector() -> AESGCMIdentityProtector:
    """Return mock keys only in mock mode; otherwise require runtime-file keys."""
    environment = str(
        getattr(settings, "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT", "production")
    ).strip().lower()
    allow_mock = bool(getattr(settings, "COMPANY_VERIFICATION_ALLOW_MOCK", False))
    if environment == "mock":
        if not allow_mock:
            raise VerificationCryptoError("pii_protector_unavailable")
        encryption_key = str(getattr(settings, "COMPANY_VERIFICATION_TEST_ENCRYPTION_KEY", ""))
        hmac_key = str(getattr(settings, "COMPANY_VERIFICATION_TEST_HMAC_KEY", ""))
        key_version = str(getattr(settings, "COMPANY_VERIFICATION_TEST_KEY_VERSION", "test-v1"))
    else:
        encryption_key = str(getattr(settings, "COMPANY_VERIFICATION_PII_ENCRYPTION_KEY", ""))
        hmac_key = str(getattr(settings, "COMPANY_VERIFICATION_PII_HMAC_KEY", ""))
        key_version = str(getattr(settings, "COMPANY_VERIFICATION_PII_KEY_VERSION", ""))
    if not encryption_key.strip() or not hmac_key.strip() or not key_version.strip():
        raise VerificationCryptoError("pii_protector_unavailable")
    return AESGCMIdentityProtector.from_base64(
        encryption_key,
        hmac_key,
        key_version,
    )
