"""Minimal Django model registration; no production settings or credentials."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def setup(database=None):
    sys.path.insert(0, str(ROOT / "backend"))
    from django.conf import settings
    if database is not None and settings.configured:
        raise RuntimeError("Explicit database setup requires a fresh Python process")
    if not settings.configured:
        settings.configure(
            SECRET_KEY="offline-agent-lab", USE_TZ=True,
            INSTALLED_APPS=["apps.user", "apps.opengate", "apps.company", "apps.iam",
                            "apps.runtime", "apps.agent_runtime"],
            DATABASES={"default": database or {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        )
    import django
    django.setup()
