from .test import *  # noqa


ROOT_URLCONF = "apps.company.tests.urls"
MIDDLEWARE = [mw for mw in MIDDLEWARE if mw != "common.middleware.DevelopmentDatabaseRecoveryMiddleware"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(BASE_DIR / "test_security.sqlite3"),
        "TEST": {
            "NAME": str(BASE_DIR / "test_security.sqlite3"),
        },
    }
}


class DisableMigrations(dict):
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = DisableMigrations()
