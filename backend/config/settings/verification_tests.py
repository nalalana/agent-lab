"""PostgreSQL-only settings for enterprise-verification tests.

The module deliberately ignores the normal ``POSTGRES_*`` database target.
Every connection value must use the verification-specific environment
contract so an operator cannot accidentally run migrations or tests against a
development or production database inherited from the shell.
"""

import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


os.environ["DJANGO_ENVIRONMENT"] = "testing"

from .test import *  # noqa: E402,F401,F403


_DATABASE_NAME_PATTERN = re.compile(r"^touming_verification_[a-z0-9_]+$")
_FORBIDDEN_DATABASE_NAMES = {
    "postgres",
    "template0",
    "template1",
    "touming_dev",
    "touming_prod",
    "touming_production",
}
_FORBIDDEN_NAME_MARKERS = ("prod", "production", "live")
_STORAGE_PARENT = Path("/tmp").resolve()


def _required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} is required for verification tests")
    return value


def _validate_database_name(name: str, variable: str) -> str:
    normalized = name.strip().lower()
    if (
        normalized in _FORBIDDEN_DATABASE_NAMES
        or any(marker in normalized for marker in _FORBIDDEN_NAME_MARKERS)
        or not _DATABASE_NAME_PATTERN.fullmatch(normalized)
    ):
        raise ImproperlyConfigured(
            f"{variable} must name an isolated database with the "
            "touming_verification_ prefix"
        )
    return normalized


VERIFICATION_TEST_DB_NAME = _validate_database_name(
    _required_setting("VERIFICATION_TEST_DB_NAME"),
    "VERIFICATION_TEST_DB_NAME",
)
VERIFICATION_TEST_TARGET_DB_NAME = _validate_database_name(
    _required_setting("VERIFICATION_TEST_TARGET_DB_NAME"),
    "VERIFICATION_TEST_TARGET_DB_NAME",
)
if VERIFICATION_TEST_DB_NAME == VERIFICATION_TEST_TARGET_DB_NAME:
    raise ImproperlyConfigured(
        "verification control and Django test database names must differ"
    )

VERIFICATION_TEST_DB_USER = _required_setting("VERIFICATION_TEST_DB_USER")
if VERIFICATION_TEST_DB_USER.lower() in {"postgres", "root"}:
    raise ImproperlyConfigured(
        "verification tests must not use a database superuser account"
    )

VERIFICATION_TEST_DB_PASSWORD = _required_setting("VERIFICATION_TEST_DB_PASSWORD")
VERIFICATION_TEST_DB_HOST = _required_setting("VERIFICATION_TEST_DB_HOST")
try:
    VERIFICATION_TEST_DB_PORT = int(_required_setting("VERIFICATION_TEST_DB_PORT"))
except ValueError as exc:
    raise ImproperlyConfigured("VERIFICATION_TEST_DB_PORT must be an integer") from exc
if not 1 <= VERIFICATION_TEST_DB_PORT <= 65535:
    raise ImproperlyConfigured("VERIFICATION_TEST_DB_PORT is outside the valid range")

storage_root = Path(_required_setting("VERIFICATION_TEST_STORAGE_ROOT")).resolve()
if (
    not storage_root.is_relative_to(_STORAGE_PARENT)
    or not storage_root.name.startswith("touming-verification-")
):
    raise ImproperlyConfigured(
        "VERIFICATION_TEST_STORAGE_ROOT must be a dedicated "
        "/tmp/touming-verification-* directory"
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": VERIFICATION_TEST_DB_NAME,
        "USER": VERIFICATION_TEST_DB_USER,
        "PASSWORD": VERIFICATION_TEST_DB_PASSWORD,
        "HOST": VERIFICATION_TEST_DB_HOST,
        "PORT": VERIFICATION_TEST_DB_PORT,
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 5},
        "TEST": {"NAME": VERIFICATION_TEST_TARGET_DB_NAME},
    }
}
