from django.db import migrations


def use_platform_qwen(apps, schema_editor):
    ModelProvider = apps.get_model("company", "ModelProvider")
    ModelDefinition = apps.get_model("company", "ModelDefinition")
    CompanyModelPolicy = apps.get_model("company", "CompanyModelPolicy")
    CompanyApiCredential = apps.get_model("company", "CompanyApiCredential")

    now = "0"
    provider, _ = ModelProvider.objects.update_or_create(
        providerId="custom-qwen",
        defaults={
            "name": "Platform Qwen3.8", "slug": "custom-qwen",
            "baseUrl": "http://112.95.75.67:9018/v1",
            "authMode": "platform_key", "status": "active",
            "isPlatformManaged": True, "created_at": now, "updated_at": now,
        },
    )
    model, _ = ModelDefinition.objects.update_or_create(
        provider=provider, modelId="qwen3.8-27b",
        defaults={
            "displayName": "Qwen3.8 27B", "category": "chat",
            "status": "active", "supportsTools": True,
            "contextWindow": 131072, "isPlatformDefault": True,
            "created_at": now, "updated_at": now,
        },
    )
    CompanyModelPolicy.objects.update(
        provider=provider, defaultModel=model, package=None,
        selectedCredentialName="", sourceMode="platform", fallbackToPlatform=True,
    )
    CompanyApiCredential.objects.update(
        status="inactive", keyMasked="", secretEncrypted="",
    )


class Migration(migrations.Migration):
    dependencies = [("company", "0022_agent_accent_color")]
    operations = [migrations.RunPython(use_platform_qwen, migrations.RunPython.noop)]
