import time

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class EV2MigrationTests(TransactionTestCase):
    migrate_from = [
        ("company", "0031_phase4_tencent_ess_verification"),
        ("iam", "0001_initial"),
        ("user", "0002_user_consent"),
    ]
    migrate_to = [
        ("company", "0032_ev2_manual_verification_domain"),
        ("iam", "0001_initial"),
        ("user", "0002_user_consent"),
    ]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        old_apps = self.executor.loader.project_state(self.migrate_from).apps

        User = old_apps.get_model("user", "User")
        Company = old_apps.get_model("company", "Company")
        CompanyVerification = old_apps.get_model("company", "CompanyVerification")
        Resource = old_apps.get_model("iam", "Resource")
        Principal = old_apps.get_model("iam", "Principal")
        Grant = old_apps.get_model("iam", "Grant")

        stamp = str(time.time())[:20]
        user = User.objects.create(
            uid="ev2-migration-user",
            username="ev2-migration-user",
            created_at=stamp,
            updated_at=stamp,
        )
        self.historical_cid = "ev2-historical-company"
        historical = Company.objects.create(
            account_id=user.id,
            cid=self.historical_cid,
            company_name="Historical Company",
            registration_no="91350211M000100Y46",
            created_at=stamp,
            updated_at=stamp,
        )
        self.draft_cid = "ev2-no-grant-draft"
        Company.objects.create(
            account_id=user.id,
            cid=self.draft_cid,
            company_name="No Grant Draft",
            registration_no="91350211M000101Y42",
            created_at=stamp,
            updated_at=stamp,
        )
        resource = Resource.objects.create(
            type="company",
            external_id=self.historical_cid,
        )
        principal = Principal.objects.create(
            type="user",
            external_id=user.uid,
        )
        Grant.objects.create(
            principal_id=principal.id,
            resource_id=resource.id,
            granter_id=user.id,
            permissions=4,
        )
        self.verification_pk = CompanyVerification.objects.create(
            company_id=historical.id,
            applicant_user_id=user.id,
            provider="tencent_ess",
            application_id="legacy-app",
            status="cancelled",
        ).id

        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_grant_backed_history_is_verified_without_changing_grants(self):
        Company = self.apps.get_model("company", "Company")
        Grant = self.apps.get_model("iam", "Grant")
        VerificationAuditEvent = self.apps.get_model(
            "company",
            "VerificationAuditEvent",
        )

        historical = Company.objects.get(cid=self.historical_cid)
        draft = Company.objects.get(cid=self.draft_cid)

        self.assertEqual(historical.verification_status, "verified")
        self.assertEqual(historical.verification_provider, "legacy_import")
        self.assertIsNotNone(historical.verified_at)
        self.assertIsNone(historical.verified_verification_id)
        self.assertEqual(draft.verification_status, "unverified")
        self.assertEqual(Grant.objects.count(), 1)
        self.assertEqual(Grant.objects.get().permissions, 4)
        audit = VerificationAuditEvent.objects.get(company_id=historical.id)
        self.assertEqual(audit.action, "legacy_verification_imported")
        self.assertEqual(
            audit.detail,
            {"basis": "preexisting_company_grant"},
        )

    def test_existing_verification_receives_unique_public_id(self):
        CompanyVerification = self.apps.get_model(
            "company",
            "CompanyVerification",
        )
        application = CompanyVerification.objects.get(pk=self.verification_pk)
        self.assertIsNotNone(application.public_id)
        self.assertEqual(application.provider_environment, "legacy")
        self.assertEqual(application.stage, "legacy_ess")
