"""Database contract tests for the Phase 2-C SandboxExecution model."""

from dataclasses import replace
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.agent_runtime.models import AgentRun, SandboxExecution
from apps.agent_runtime.providers.sandbox.errors import SandboxProviderError
from apps.agent_runtime.providers.sandbox.policy import ResolvedSandboxPolicy
from apps.agent_runtime.providers.sandbox.repository import (
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_TERMINATED,
    DjangoSandboxExecutionRepository,
)
from apps.agent_runtime.tests.memory_fixtures import create_memory_fixture
from apps.company.models import TransparentCompanyAgentTestMessage


class SandboxExecutionModelTests(TestCase):
    def setUp(self):
        fixture = create_memory_fixture(prefix='phase2-sandbox-model')
        self.company = fixture['company_a']
        self.other_company = fixture['company_b']
        self.agent = fixture['agent_a1']
        self.session = fixture['session_a1']
        self.input_message = TransparentCompanyAgentTestMessage.objects.create(
            session=self.session,
            role=TransparentCompanyAgentTestMessage.ROLE_USER,
            content='execute code',
            created_at='2026-08-13',
            updated_at='2026-08-13',
        )
        self.run = AgentRun.objects.create(
            request_id='phase2-sandbox-model-run',
            company=self.company,
            agent=self.agent,
            user=fixture['owner'],
            session=self.session,
            source=AgentRun.SOURCE_AGENT_TEST,
            engine=AgentRun.ENGINE_UNIFIED,
            input_message=self.input_message,
        )
        self.policy = ResolvedSandboxPolicy(
            company_id=self.company.pk,
            template='touming-code-v1',
            cpu_cores=2,
            memory_mib=2048,
            ttl_seconds=300,
            network_policy='isolated',
            max_concurrent_runs=2,
        )
        self.repository = DjangoSandboxExecutionRepository()

    def test_repository_persists_and_transitions_real_model(self):
        expires_at = timezone.now() + timedelta(minutes=5)

        record, created = self.repository.reserve(
            run_id=str(self.run.pk),
            policy=self.policy,
            expires_at=expires_at,
        )
        duplicate, duplicate_created = self.repository.reserve(
            run_id=str(self.run.pk),
            policy=self.policy,
            expires_at=expires_at,
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, record.id)
        instance = SandboxExecution.objects.get(pk=record.id)
        self.assertEqual(instance.run, self.run)
        self.assertEqual(instance.company, self.company)
        self.assertEqual(instance.provider, SandboxExecution.PROVIDER_TENCENT_AGSX)
        self.assertEqual(instance.status, SandboxExecution.STATUS_CREATING)
        self.assertEqual(instance.cpu_cores, 2)
        self.assertEqual(instance.memory_mib, 2048)

        creating = self.repository.mark_provider_instance(
            record.id,
            external_id='agsx-instance-1',
        )
        self.assertEqual(creating.status, SandboxExecution.STATUS_CREATING)
        self.assertEqual(creating.external_id, 'agsx-instance-1')

        ready_at = timezone.now()
        self.repository.mark_ready(
            record.id,
            external_id='agsx-instance-1',
            ready_at=ready_at,
            expires_at=expires_at,
        )
        self.repository.mark_running(record.id)
        self.assertEqual(SandboxExecution.objects.get(pk=record.id).status, STATUS_RUNNING)
        self.repository.mark_ready_after_execute(record.id)
        self.assertEqual(SandboxExecution.objects.get(pk=record.id).status, STATUS_READY)
        self.repository.mark_terminated(
            record.id,
            terminated_at=timezone.now(),
            billed_seconds=17,
        )
        instance.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(instance.status, STATUS_TERMINATED)
        self.assertEqual(instance.billed_seconds, 17)
        self.assertEqual(self.run.sandbox_seconds, 17)

        # A repeated provider cleanup remains safe and replaces, rather than
        # accumulates, the authoritative usage value.
        self.repository.mark_terminated(
            record.id,
            terminated_at=timezone.now(),
            billed_seconds=17,
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.sandbox_seconds, 17)

    def test_database_enforces_one_sandbox_per_run(self):
        values = {
            'run': self.run,
            'company': self.company,
            'template': 'touming-code-v1',
            'cpu_cores': 2,
            'memory_mib': 2048,
            'network_policy': 'isolated',
            'expires_at': timezone.now() + timedelta(minutes=5),
        }
        SandboxExecution.objects.create(**values)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SandboxExecution.objects.create(**values)

    def test_repository_rejects_company_mismatch(self):
        mismatched_policy = replace(self.policy, company_id=self.other_company.pk)

        with self.assertRaisesMessage(
            SandboxProviderError,
            'sandbox Run belongs to another company',
        ):
            self.repository.reserve(
                run_id=str(self.run.pk),
                policy=mismatched_policy,
                expires_at=timezone.now() + timedelta(minutes=5),
            )

        self.assertFalse(SandboxExecution.objects.exists())

    def test_reaper_query_indexes_are_declared(self):
        index_names = {index.name for index in SandboxExecution._meta.indexes}

        self.assertEqual(SandboxExecution._meta.db_table, 'sandbox_execution')
        self.assertIn('sandbox_status_expiry_idx', index_names)
        self.assertIn('sandbox_company_created_idx', index_names)
