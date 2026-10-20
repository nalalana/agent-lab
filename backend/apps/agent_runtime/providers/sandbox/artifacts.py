"""Tenant-bound COS storage for Run inputs, outputs, and overflow logs."""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import quote, unquote
from uuid import UUID, uuid4

from django.conf import settings
from qcloud_cos import CosConfig, CosS3Client

from .errors import SandboxProviderError


ARTIFACT_SCHEME = "artifact://v1/"


class ArtifactAccessDenied(SandboxProviderError):
    pass


class ArtifactLimitExceeded(SandboxProviderError):
    pass


@dataclass(frozen=True)
class ArtifactPayload:
    ref: str
    name: str
    content: bytes
    content_type: str


class CosRunArtifactStore:
    """Map opaque-ish artifact refs to one company and one Run COS prefix."""

    def __init__(
        self,
        *,
        client,
        bucket: str,
        prefix: str,
        max_artifact_bytes: int,
    ) -> None:
        self.client = client
        self.bucket = str(bucket).strip()
        self.prefix = str(prefix).strip("/") or "agent-runtime/v1"
        self.max_artifact_bytes = max(int(max_artifact_bytes), 1)
        if not self.bucket:
            raise SandboxProviderError("artifact COS bucket is not configured")

    @classmethod
    def from_settings(cls) -> "CosRunArtifactStore":
        required = {
            "bucket": str(getattr(settings, "AGENT_RUNTIME_ARTIFACT_COS_BUCKET", "")).strip(),
            "region": str(getattr(settings, "AGENT_RUNTIME_ARTIFACT_COS_REGION", "")).strip(),
            "secret_id": str(
                getattr(settings, "AGENT_RUNTIME_ARTIFACT_COS_SECRET_ID", "")
            ).strip(),
            "secret_key": str(
                getattr(settings, "AGENT_RUNTIME_ARTIFACT_COS_SECRET_KEY", "")
            ).strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SandboxProviderError(
                f"artifact COS configuration is incomplete: {', '.join(sorted(missing))}"
            )
        config = CosConfig(
            Region=required["region"],
            SecretId=required["secret_id"],
            SecretKey=required["secret_key"],
            Token=str(getattr(settings, "AGENT_RUNTIME_ARTIFACT_COS_TOKEN", "")).strip() or None,
            Scheme="https",
        )
        return cls(
            client=CosS3Client(config),
            bucket=required["bucket"],
            prefix=getattr(settings, "AGENT_RUNTIME_ARTIFACT_COS_PREFIX", "agent-runtime/v1"),
            max_artifact_bytes=getattr(
                settings,
                "AGENT_RUNTIME_SANDBOX_MAX_ARTIFACT_BYTES",
                10 * 1024 * 1024,
            ),
        )

    def put(
        self,
        *,
        company_id: object,
        run_id: str,
        kind: str,
        name: str,
        content: bytes,
        allow_overflow: bool = False,
    ) -> str:
        normalized_kind = self._kind(kind)
        data = bytes(content)
        overflow_limit = int(
            getattr(settings, "AGENT_RUNTIME_SANDBOX_MAX_OVERFLOW_BYTES", 5 * 1024 * 1024)
        )
        limit = overflow_limit if allow_overflow else self.max_artifact_bytes
        if len(data) > max(limit, 1):
            raise ArtifactLimitExceeded("sandbox artifact exceeds the configured size limit")

        ref = self._build_ref(company_id, run_id, normalized_kind, name)
        key = self._key_for_ref(ref)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=BytesIO(data),
            ContentType=content_type,
        )
        return ref

    def get(self, ref: str, *, company_id: object, run_id: str) -> ArtifactPayload:
        parts = self._parse_ref(ref)
        expected_company = self._company_scope(company_id)
        expected_run = self._run_scope(run_id)
        if parts[0] != expected_company or parts[1] != expected_run:
            raise ArtifactAccessDenied("artifact does not belong to the trusted Run context")

        response = self.client.get_object(Bucket=self.bucket, Key=self._key_for_ref(ref))
        declared_size = int(response.get("Content-Length", -1))
        if declared_size > self.max_artifact_bytes:
            raise ArtifactLimitExceeded("sandbox input artifact exceeds the size limit")
        stream = response["Body"].get_raw_stream()
        content = stream.read(self.max_artifact_bytes + 1)
        if len(content) > self.max_artifact_bytes:
            raise ArtifactLimitExceeded("sandbox input artifact exceeds the size limit")
        name = self._safe_name(unquote(parts[-1]))
        return ArtifactPayload(
            ref=ref,
            name=name,
            content=bytes(content),
            content_type=str(response.get("Content-Type") or "application/octet-stream"),
        )

    def _build_ref(self, company_id: object, run_id: str, kind: str, name: str) -> str:
        safe_name = self._safe_name(name)
        return (
            f"{ARTIFACT_SCHEME}{self._company_scope(company_id)}/"
            f"{self._run_scope(run_id)}/{kind}/{uuid4().hex}/{quote(safe_name)}"
        )

    def _key_for_ref(self, ref: str) -> str:
        parts = self._parse_ref(ref)
        return posixpath.join(self.prefix, *parts)

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, ...]:
        value = str(ref or "")
        if not value.startswith(ARTIFACT_SCHEME):
            raise ArtifactAccessDenied("unsupported artifact reference")
        parts = tuple(value.removeprefix(ARTIFACT_SCHEME).split("/"))
        if len(parts) != 5 or any(not part or part in {".", ".."} for part in parts):
            raise ArtifactAccessDenied("invalid artifact reference")
        if parts[2] not in {"input", "output", "overflow"}:
            raise ArtifactAccessDenied("invalid artifact kind")
        return parts

    @staticmethod
    def _company_scope(company_id: object) -> str:
        return hashlib.sha256(str(company_id).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _run_scope(run_id: str) -> str:
        try:
            return str(UUID(str(run_id)))
        except (TypeError, ValueError) as exc:
            raise ArtifactAccessDenied("artifact Run ID must be a UUID") from exc

    @staticmethod
    def _kind(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"input", "output", "overflow"}:
            raise ArtifactAccessDenied("invalid artifact kind")
        return normalized

    @staticmethod
    def _safe_name(value: str) -> str:
        basename = unicodedata.normalize("NFC", str(value))
        if (
            not basename
            or basename != basename.strip()
            or basename in {".", ".."}
            or len(basename) > 128
            or "/" in basename
            or "\\" in basename
            or any(unicodedata.category(character).startswith("C") for character in basename)
        ):
            raise ArtifactAccessDenied("artifact name is invalid")
        return basename


def truncate_utf8(value: str, limit_bytes: int) -> tuple[str, bool]:
    """Truncate without returning an invalid UTF-8 sequence."""
    encoded = str(value or "").encode("utf-8", errors="replace")
    if len(encoded) <= max(int(limit_bytes), 0):
        return encoded.decode("utf-8"), False
    truncated = encoded[: max(int(limit_bytes), 0)].decode("utf-8", errors="ignore")
    return truncated, True
