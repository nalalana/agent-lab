from django.test import TestCase

from apps.company.models import Company, CompanyApiCredential, CompanyModelPolicy, ModelProvider
from apps.company.services import CompanyService
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, get_or_create_resource, grant_permissions
from apps.user.models import User


class CompanyModelPolicyCredentialSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(
            uid="model-policy-sync-user",
            username="model-policy-sync-user",
            email="sync@example.com",
        )
        self.company = Company.objects.create(
            account=self.user,
            cid="company-model-policy-sync",
            company_name="Model Policy Sync Inc",
            created_at="1",
            updated_at="1",
        )
        principal = get_or_create_principal(Principal.TYPE_USER, self.user.uid)
        resource = get_or_create_resource(Resource.TYPE_COMPANY, self.company.cid)
        grant_permissions(principal, resource, Grant.PERM_ADMIN, granter_user=self.user)

    def test_company_credentials_are_rejected(self):
        ok, message = CompanyService.upsert_company_api_credential(
            cid=self.company.cid,
            provider_id="minimax",
            credential_name="M2.5",
            user=self.user,
            api_key="must-not-be-stored",
            status=CompanyApiCredential.STATUS_ACTIVE,
        )

        self.assertFalse(ok)
        self.assertIn("platform-managed", message)
        self.assertFalse(CompanyApiCredential.objects.filter(company=self.company).exists())
        self.assertFalse(CompanyModelPolicy.objects.filter(company=self.company).exists())

    def test_catalog_provider_syncs_minimax_base_url(self):
        provider, _ = ModelProvider.objects.update_or_create(
            providerId="minimax",
            defaults={
                "name": "MiniMax",
                "slug": "minimax-existing",
                "baseUrl": "",
                "authMode": ModelProvider.AUTH_API_KEY,
                "status": ModelProvider.STATUS_ACTIVE,
                "created_at": "1",
                "updated_at": "1",
            },
        )

        resolved = CompanyService._get_or_create_catalog_provider("minimax", version="2026.4.8")

        provider.refresh_from_db()
        self.assertEqual(resolved.pk, provider.pk)
        self.assertEqual(provider.baseUrl, "https://api.minimaxi.com/v1")
