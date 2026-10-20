"""Phase 1-B acceptance tests for config models, prompts, and snapshots."""

import hashlib
from importlib import import_module
from unittest.mock import Mock, patch

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from apps.agent_runtime.config import (
    ConfigResolver,
    PromptBuilder,
    SnapshotSecurityError,
    SnapshotService,
)
from apps.agent_runtime.models import AgentConfigSnapshot, AgentRun, CompanyAgentRuntimePolicy
from apps.agent_runtime.memory.access import MemoryAccessResolver
from apps.company.models import (
    Company,
    CompanyModelPolicy,
    CompanySessionMemoryPolicy,
    CompanyVikingBinding,
    OpenVikingCluster,
    TransparentCompany,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentMemoryBinding,
    TransparentCompanyAgentPermissionBinding,
    TransparentCompanyAgentPromptVersion,
    TransparentCompanyAgentSkillBinding,
    TransparentCompanyPromptVersion,
    TransparentCompanySkill,
)
from apps.company.services import CompanyService
from apps.user.models import User


class Phase1TableAndPromptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create(
            uid='phase1-user',
            username='phase1-user',
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )
        cls.company = Company.objects.create(
            account=cls.user,
            cid='phase1-company',
            company_name='Phase 1 Company',
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )
        cluster = OpenVikingCluster.objects.create(
            name='phase1-cluster',
            endpoint='http://openviking:1933',
            rootCredentialRef='file:root.key',
            isDefault=True,
        )
        CompanyVikingBinding.objects.create(
            company=cls.company,
            cluster=cluster,
            accountId='tm-phase1-company',
            adminUserId='touming-admin',
            runtimeUserId='touming-runtime',
            adminCredentialRef='file:companies/tm-phase1-company/admin.key',
            runtimeCredentialRef='file:companies/tm-phase1-company/runtime.key',
            status=CompanyVikingBinding.STATUS_ACTIVE,
        )
        cls.transparent_company = TransparentCompany.objects.create(
            company=cls.company,
            prompts='legacy company prompt',
        )
        cls.agent = TransparentCompanyAgentDefinition.objects.create(
            transparentCompany=cls.transparent_company,
            agentId='phase1-agent',
            name='Phase 1 Agent',
            systemPrompt='legacy agent prompt',
            enabled=True,
            status=TransparentCompanyAgentDefinition.STATUS_ACTIVE,
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )
        cls.company_prompt = TransparentCompanyPromptVersion.objects.create(
            company=cls.transparent_company,
            version=1,
            content='active company prompt',
            checksum=hashlib.sha256(b'active company prompt').hexdigest(),
            status=TransparentCompanyPromptVersion.STATUS_ACTIVE,
            created_by=cls.user,
        )
        cls.agent_prompt = TransparentCompanyAgentPromptVersion.objects.create(
            agent=cls.agent,
            versionLabel='1.0.0',
            content='active agent prompt',
            isCurrent=True,
            created_at='2026-08-11',
        )
        cls.skill = TransparentCompanySkill.objects.create(
            transparentCompany=cls.transparent_company,
            skillId='phase1-skill',
            name='Phase 1 Skill',
            version='1.2.3',
            content='use the phase 1 skill',
            enabled=True,
            status=TransparentCompanySkill.STATUS_ACTIVE,
            updated_at='2026-08-11',
        )
        TransparentCompanyAgentSkillBinding.objects.create(
            transparentCompany=cls.transparent_company,
            agent=cls.agent,
            skill=cls.skill,
            enabled=True,
            sortOrder=10,
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )

    def test_runtime_policy_enforces_canary_percentage(self):
        CompanyAgentRuntimePolicy.objects.create(company=self.company, canary_percentage=50)
        other_company = Company.objects.create(
            account=self.user,
            cid='phase1-invalid-policy',
            company_name='Invalid Policy',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyAgentRuntimePolicy.objects.create(
                company=other_company,
                canary_percentage=101,
            )

    def test_resolver_prefers_active_prompt_versions_and_enabled_skills(self):
        other_company = Company.objects.create(
            account=self.user,
            cid='phase1-other-company',
            company_name='Other Company',
        )
        other_transparent = TransparentCompany.objects.create(
            company=other_company,
            prompts='other company prompt',
        )
        other_skill = TransparentCompanySkill.objects.create(
            transparentCompany=other_transparent,
            skillId='cross-company-skill',
            name='Cross-company Skill',
            enabled=True,
            status=TransparentCompanySkill.STATUS_ACTIVE,
            updated_at='2026-08-11',
        )
        # Even malformed legacy data must not cross the company boundary.
        TransparentCompanyAgentSkillBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            skill=other_skill,
            enabled=True,
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )
        config = ConfigResolver().resolve(self.company, self.agent.pk)

        self.assertEqual(config.company_prompt, 'active company prompt')
        self.assertEqual(config.agent_prompt, 'active agent prompt')
        self.assertEqual([skill.skill_id for skill in config.skills], ['phase1-skill'])
        self.assertNotIn('credential', str(config.model_config).lower())

    @override_settings(
        AGENT_RUNTIME_PLATFORM_PROVIDER_ID='custom-qwen',
        AGENT_RUNTIME_PLATFORM_MODEL_ID='qwen3.8-27b',
    )
    def test_resolver_uses_platform_model_without_company_policy(self):
        self.assertFalse(CompanyModelPolicy.objects.filter(company=self.company).exists())

        config = ConfigResolver().resolve(self.company, self.agent.pk)

        self.assertEqual(config.model_config['providerId'], 'custom-qwen')
        self.assertEqual(config.model_config['modelId'], 'qwen3.8-27b')
        self.assertEqual(config.model_config['sourceMode'], CompanyModelPolicy.SOURCE_PLATFORM)

    def test_prompt_builder_preserves_contract_order(self):
        config = ConfigResolver().resolve(self.company, self.agent.pk)
        bundle = PromptBuilder().build(
            config,
            memory_context=['remember alpha'],
            session_summary='summary beta',
            recent_messages=[{'role': 'assistant', 'content': 'recent gamma'}],
            user_input='question delta',
        )

        ordered_markers = [
            '## Platform safety',
            '## Agent protocol',
            '## Company instructions',
            '## Agent instructions',
            '## Skills',
            '## Permission policy',
            '## Memory policy',
        ]
        positions = [bundle.system_prompt.index(marker) for marker in ordered_markers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(
            'Read workspace UTF-8 text files and PDFs directly with workspace.read.',
            bundle.system_prompt,
        )
        self.assertIn(
            'Never use workspace.checkout or python.execute merely to read a PDF',
            bundle.system_prompt,
        )
        self.assertEqual([message['role'] for message in bundle.messages], [
            'system', 'system', 'assistant', 'user'
        ])

    def test_automatic_memory_ignores_legacy_company_policy_and_hides_writeback(self):
        TransparentCompanyAgentPermissionBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            enabled=True,
            status=TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
            memoryRead=True,
            memoryWrite=True,
            memoryUpdate=True,
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )
        TransparentCompanyAgentMemoryBinding.objects.create(
            transparentCompany=self.transparent_company,
            agent=self.agent,
            enabled=True,
            status=TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE,
            memoryScope=TransparentCompanyAgentMemoryBinding.SCOPE_SESSION,
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )
        CompanySessionMemoryPolicy.objects.create(
            company=self.company,
            mode=CompanySessionMemoryPolicy.MODE_DISABLED,
            enabled=False,
            status=CompanySessionMemoryPolicy.STATUS_INACTIVE,
            created_at='2026-08-11',
            updated_at='2026-08-11',
        )

        config = ConfigResolver().resolve(self.company, self.agent.pk)
        prompt = PromptBuilder().build_system_prompt(config)

        self.assertTrue(config.memory_policy['automatic']['enabled'])
        self.assertTrue(config.memory_policy['session']['enabled'])
        self.assertNotIn('user', config.memory_policy)
        self.assertEqual(config.memory_policy['session']['recentMessageWindow'], 12)
        self.assertIn('"automaticLongTermMemory":{', prompt)
        self.assertIn('"durability":"cross_conversation"', prompt)
        self.assertIn('"preferencePersistence":"automatic_extraction"', prompt)
        self.assertIn('"operationMode":"runtime_managed"', prompt)
        self.assertNotIn('writebackMode', prompt)
        self.assertNotIn('"user"', prompt)
        context = Mock(company=self.company, agent=self.agent)
        self.assertEqual(
            MemoryAccessResolver._session_access(context, True, True),
            (True, True),
        )

    def test_frontend_memory_switch_atomically_syncs_default_permissions(self):
        with patch.object(
            CompanyService,
            '_get_transparent_company_for_admin',
            return_value=(True, self.transparent_company),
        ):
            ok, _ = CompanyService.upsert_transparent_company_agent_memory_binding(
                cid=self.company.cid,
                agent_id=self.agent.agentId,
                user=self.user,
                enabled=True,
                status=TransparentCompanyAgentMemoryBinding.STATUS_ACTIVE,
            )

        self.assertTrue(ok)
        permission = TransparentCompanyAgentPermissionBinding.objects.get(agent=self.agent)
        self.assertTrue(permission.memoryRead)
        self.assertTrue(permission.memoryWrite)
        self.assertTrue(permission.memoryUpdate)
        self.assertFalse(permission.memoryDelete)
        self.assertTrue(permission.enabled)
        self.assertEqual(
            permission.status,
            TransparentCompanyAgentPermissionBinding.STATUS_ACTIVE,
        )

        with patch.object(
            CompanyService,
            '_get_transparent_company_for_admin',
            return_value=(True, self.transparent_company),
        ):
            ok, _ = CompanyService.upsert_transparent_company_agent_memory_binding(
                cid=self.company.cid,
                agent_id=self.agent.agentId,
                user=self.user,
                enabled=False,
                status=TransparentCompanyAgentMemoryBinding.STATUS_INACTIVE,
            )

        self.assertTrue(ok)
        permission.refresh_from_db()
        self.assertFalse(permission.memoryRead)
        self.assertFalse(permission.memoryWrite)
        self.assertFalse(permission.memoryUpdate)
        self.assertFalse(permission.memoryDelete)

    def test_snapshot_is_deterministic_secret_free_and_immutable(self):
        config = ConfigResolver().resolve(self.company, self.agent.pk)
        service = SnapshotService()
        payload = service.build_payload(config)

        self.assertEqual(service.calculate_checksum(payload), service.calculate_checksum(payload))
        self.assertEqual(
            [tool['name'] for tool in payload['toolPolicy']['platformTools']],
            [
                'python.execute',
                'workspace.checkout',
                'workspace.delete',
                'workspace.list',
                'workspace.locate',
                'workspace.move',
                'workspace.publish',
                'workspace.read',
            ],
        )
        self.assertNotIn('platformTools', PromptBuilder().build_system_prompt(config))
        self.assertNotIn('active company prompt', str(payload))
        snapshot = service.create(config)
        self.assertEqual(len(snapshot.checksum), 64)
        snapshot.model_config = {'providerId': 'changed'}
        with self.assertRaises(ValidationError):
            snapshot.save()

        with self.assertRaises(SnapshotSecurityError):
            service.assert_secret_free({'nested': {'api_key': 'plaintext'}})

    def test_company_prompt_data_migration_is_idempotent(self):
        legacy_company = Company.objects.create(
            account=self.user,
            cid='phase1-legacy-company',
            company_name='Legacy Company',
        )
        transparent = TransparentCompany.objects.create(
            company=legacy_company,
            prompts='legacy prompt to version',
        )
        migration = import_module(
            'apps.company.migrations.0021_transparent_company_prompt_version'
        )

        migration.backfill_active_prompt_versions(apps, None)
        migration.backfill_active_prompt_versions(apps, None)

        versions = TransparentCompanyPromptVersion.objects.filter(company=transparent)
        self.assertEqual(versions.count(), 1)
        self.assertEqual(versions.get().checksum, hashlib.sha256(b'legacy prompt to version').hexdigest())

    def test_runtime_policy_data_migration_is_idempotent(self):
        CompanyAgentRuntimePolicy.objects.all().delete()
        migration = import_module(
            'apps.agent_runtime.migrations.0002_backfill_runtime_policy'
        )

        migration.create_legacy_runtime_policies(apps, None)
        migration.create_legacy_runtime_policies(apps, None)

        policies = CompanyAgentRuntimePolicy.objects.filter(company=self.company)
        self.assertEqual(policies.count(), 1)
        self.assertEqual(policies.get().mode, CompanyAgentRuntimePolicy.MODE_LEGACY)


class SnapshotKeyScannerTests(TestCase):
    def test_allows_non_sensitive_token_budget_key(self):
        # Budget metadata is not a bearer/authentication token.
        SnapshotService.assert_secret_free({'dailyTokenBudget': 1000})


class AgentRunSourceContractTests(SimpleTestCase):
    def test_model_sources_match_frozen_run_request_contract(self):
        self.assertEqual(
            tuple(value for value, _ in AgentRun.SOURCE_CHOICES),
            ('agent_test', 'public_client', 'cron'),
        )

    def test_data_migration_rewrites_legacy_source_values(self):
        migration = import_module(
            'apps.agent_runtime.migrations.0004_align_agent_run_source_contract'
        )
        historical_apps = Mock()
        historical_run = historical_apps.get_model.return_value
        test_rows = Mock()
        public_rows = Mock()
        historical_run.objects.filter.side_effect = [test_rows, public_rows]

        migration.align_agent_run_sources(historical_apps, None)

        test_rows.update.assert_called_once_with(source='agent_test')
        public_rows.update.assert_called_once_with(source='public_client')
