from django.apps import AppConfig


class AgentRuntimeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agent_runtime"
    verbose_name = "Agent Runtime"

    def ready(self):
        # Importing registers deploy-time checks without performing cloud calls.
        from apps.agent_runtime import checks  # noqa: F401
