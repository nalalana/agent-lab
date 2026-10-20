import os

os.environ.setdefault('DJANGO_ENVIRONMENT', 'testing')

from .base import *  # noqa

DEBUG = False

# Production defaults to the retired read-only data plane. Legacy unit tests
# explicitly keep write mode so historical read/write behavior remains
# testable without re-enabling any Phase 4-C API or Celery entrypoint.
OPENCLAW_LEGACY_WRITE_MODE = 'read_write'

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "touming-test-cache",
    },
}

