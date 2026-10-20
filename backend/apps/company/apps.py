from django.apps import AppConfig


class CompanyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.company"

    def ready(self):
        from apps.company import checks  # noqa: F401
