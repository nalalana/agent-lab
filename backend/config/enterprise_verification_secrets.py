"""Strict loader for the runtime-only enterprise-verification secret file."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


MAX_SECRET_FILE_BYTES = 16 * 1024
REQUIRED_KEYS = frozenset({
    "tencent_secret_id",
    "tencent_secret_key",
    "faceid_rule_id",
    "faceid_redirect_url",
    "pii_encryption_key_b64",
    "pii_hmac_key_b64",
    "pii_key_version",
})
KEY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class EnterpriseVerificationSecrets:
    values: dict[str, str]
    error: str = ""


def _failure(code: str) -> EnterpriseVerificationSecrets:
    return EnterpriseVerificationSecrets(values={}, error=code)


def load_enterprise_verification_secrets(path_value: str) -> EnterpriseVerificationSecrets:
    """Load secrets without leaking values or filesystem exception details.

    The file must be an absolute, regular, non-symlink file owned by root or the
    current process user. Group read access is allowed; group/world writes are not.
    """

    path_text = str(path_value or "").strip()
    if not path_text:
        return _failure("path_not_configured")
    path = Path(path_text)
    if not path.is_absolute():
        return _failure("path_not_absolute")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return _failure("file_not_found")
    except OSError:
        return _failure("file_inaccessible")

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return _failure("not_regular_file")
        permissions = stat.S_IMODE(metadata.st_mode)
        forbidden_permissions = (
            stat.S_IWGRP | stat.S_IXUSR | stat.S_IXGRP | stat.S_IRWXO
        )
        if not permissions & stat.S_IRUSR or permissions & forbidden_permissions:
            return _failure("insecure_permissions")
        allowed_owners = {0}
        if hasattr(os, "geteuid"):
            allowed_owners.add(os.geteuid())
        if metadata.st_uid not in allowed_owners:
            return _failure("untrusted_owner")
        if metadata.st_size <= 0 or metadata.st_size > MAX_SECRET_FILE_BYTES:
            return _failure("invalid_size")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            raw = stream.read(MAX_SECRET_FILE_BYTES + 1)
    except UnicodeError:
        return _failure("invalid_encoding")
    except OSError:
        return _failure("file_inaccessible")
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw.encode("utf-8")) > MAX_SECRET_FILE_BYTES:
        return _failure("invalid_size")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return _failure("invalid_json")
    if not isinstance(payload, dict):
        return _failure("invalid_shape")
    keys = set(payload)
    if keys - REQUIRED_KEYS:
        return _failure("unexpected_keys")
    if REQUIRED_KEYS - keys:
        return _failure("missing_keys")

    values: dict[str, str] = {}
    for key in REQUIRED_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return _failure("invalid_value")
        values[key] = value.strip()

    if not KEY_VERSION_PATTERN.fullmatch(values["pii_key_version"]):
        return _failure("invalid_key_version")
    redirect = urlsplit(values["faceid_redirect_url"])
    if (
        redirect.scheme not in {"http", "https"}
        or not redirect.netloc
        or redirect.username
        or redirect.password
        or redirect.fragment
    ):
        return _failure("invalid_redirect_url")

    decoded_keys = []
    for key in ("pii_encryption_key_b64", "pii_hmac_key_b64"):
        try:
            decoded = base64.b64decode(values[key], validate=True)
        except (TypeError, ValueError):
            return _failure("invalid_pii_key")
        if len(decoded) != 32:
            return _failure("invalid_pii_key")
        decoded_keys.append(decoded)
    if decoded_keys[0] == decoded_keys[1]:
        return _failure("duplicate_pii_keys")

    return EnterpriseVerificationSecrets(values=values)
