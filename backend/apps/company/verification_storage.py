"""Private, application-scoped object storage for verification evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.conf import settings
from qcloud_cos import CosConfig, CosS3Client


SAFE_KEY = re.compile(r"^[A-Za-z0-9/_\-.]+$")


class VerificationStorageError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__("Verification document storage is unavailable")


@dataclass(frozen=True)
class StoredVerificationObject:
    key: str
    version: str


def validate_verification_storage_key(key: str, *, environment: str) -> str:
    value = str(key or "").strip()
    prefix = f"enterprise-verification/{environment}/"
    if (
        not value.startswith(prefix)
        or not SAFE_KEY.fullmatch(value)
        or ".." in value.split("/")
        or "//" in value
        or "\\" in value
    ):
        raise VerificationStorageError("invalid_storage_key")
    return value


class PrivateCosVerificationStorage:
    def __init__(self, *, client=None):
        self.environment = str(
            getattr(settings, "COMPANY_VERIFICATION_PROVIDER_ENVIRONMENT", "production")
        ).strip().lower()
        self.bucket = str(getattr(settings, "VERIFICATION_PRIVATE_COS_BUCKET", "")).strip()
        self.region = str(getattr(settings, "VERIFICATION_PRIVATE_COS_REGION", "")).strip()
        if not self.bucket or not self.region:
            raise VerificationStorageError("storage_not_configured")
        if client is not None:
            self.client = client
            return
        secret_id = str(getattr(settings, "VERIFICATION_PRIVATE_COS_SECRET_ID", "")).strip()
        secret_key = str(getattr(settings, "VERIFICATION_PRIVATE_COS_SECRET_KEY", "")).strip()
        token = str(getattr(settings, "VERIFICATION_PRIVATE_COS_TOKEN", "")).strip() or None
        if not secret_id or not secret_key:
            raise VerificationStorageError("storage_credentials_unavailable")
        config = CosConfig(
            Region=self.region,
            SecretId=secret_id,
            SecretKey=secret_key,
            Token=token,
            Scheme="https",
        )
        self.client = CosS3Client(config)

    def put(self, key: str, content: bytes, *, content_type: str) -> StoredVerificationObject:
        key = validate_verification_storage_key(key, environment=self.environment)
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                ServerSideEncryption="AES256",
            )
        except Exception as exc:
            raise VerificationStorageError("storage_write_failed") from exc
        version = str(response.get("VersionId") or response.get("ETag") or "").strip('"')
        if not version:
            raise VerificationStorageError("storage_version_missing")
        return StoredVerificationObject(key=key, version=version)

    def get(self, key: str, *, max_bytes: int) -> bytes:
        key = validate_verification_storage_key(key, environment=self.environment)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            declared = int(response.get("Content-Length", -1))
            if declared > max_bytes:
                raise VerificationStorageError("storage_object_too_large")
            body = response["Body"]
            stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else body
            chunks, total = [], 0
            while True:
                chunk = stream.read(min(1024 * 1024, max_bytes + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise VerificationStorageError("storage_object_too_large")
                chunks.append(chunk)
            return b"".join(chunks)
        except VerificationStorageError:
            raise
        except Exception as exc:
            raise VerificationStorageError("storage_read_failed") from exc

    def delete(self, key: str):
        key = validate_verification_storage_key(key, environment=self.environment)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise VerificationStorageError("storage_delete_failed") from exc


def get_verification_storage() -> PrivateCosVerificationStorage:
    return PrivateCosVerificationStorage()
