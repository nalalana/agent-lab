"""Tests for Phase 0-B baseline, namespace, and acceptance fixture tooling."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.agent_runtime.baseline import build_data_baseline, compare_baseline_counts
from apps.company.models import (
    Company,
    CompanyUserMemoryItem,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentVikingSpace,
)


class Phase0DataBaselineTests(TestCase):
    def _seed(self):
        call_command("agent_runtime_seed_acceptance_data", stdout=StringIO())

    def test_acceptance_fixture_is_idempotent_and_complete(self):
        self._seed()
        first_counts = {
            "companies": Company.objects.count(),
            "agents": TransparentCompanyAgentDefinition.objects.count(),
            "spaces": TransparentCompanyAgentVikingSpace.objects.count(),
            "memories": CompanyUserMemoryItem.objects.count(),
        }
        self._seed()
        second_counts = {
            "companies": Company.objects.count(),
            "agents": TransparentCompanyAgentDefinition.objects.count(),
            "spaces": TransparentCompanyAgentVikingSpace.objects.count(),
            "memories": CompanyUserMemoryItem.objects.count(),
        }

        self.assertEqual(first_counts, second_counts)
        self.assertEqual(first_counts, {"companies": 2, "agents": 4, "spaces": 4, "memories": 2})

    def test_fixture_namespaces_and_memory_are_isolated(self):
        self._seed()
        alpha = Company.objects.get(cid="phase0-company-alpha")
        beta = Company.objects.get(cid="phase0-company-beta")
        alpha_spaces = set(
            TransparentCompanyAgentVikingSpace.objects.filter(
                transparentCompany__company=alpha
            ).values_list("accountId", "userId", "rootUri")
        )
        beta_spaces = set(
            TransparentCompanyAgentVikingSpace.objects.filter(
                transparentCompany__company=beta
            ).values_list("accountId", "userId", "rootUri")
        )

        self.assertTrue(alpha_spaces.isdisjoint(beta_spaces))
        self.assertEqual(
            set(CompanyUserMemoryItem.objects.filter(company=alpha).values_list("content", flat=True)),
            {"ALPHA-ONLY-731"},
        )
        self.assertEqual(
            set(CompanyUserMemoryItem.objects.filter(company=beta).values_list("content", flat=True)),
            {"BETA-ONLY-942"},
        )

    def test_baseline_defaults_to_memory_hashes_without_raw_content(self):
        self._seed()
        report = build_data_baseline(
            memory_company_cids=["phase0-company-alpha", "phase0-company-beta"]
        )

        samples = report["openViking"]["memorySamples"]
        self.assertEqual(len(samples), 2)
        self.assertTrue(all("content" not in sample for sample in samples))
        self.assertTrue(all(len(sample["contentSha256"]) == 64 for sample in samples))
        self.assertEqual(report["counts"]["companies"]["count"], 2)
        self.assertEqual(report["counts"]["agents"]["count"], 4)
        self.assertEqual(report["openViking"]["namespaceCount"], 4)

    def test_count_comparison_reports_exact_mismatch(self):
        current = {
            "counts": {
                "companies": {"table": "company_company", "count": 2},
            }
        }
        expected = {
            "counts": {
                "companies": {"table": "company_company", "count": 3},
            }
        }

        self.assertEqual(
            compare_baseline_counts(current, expected),
            ["companies: expected 3, got 2"],
        )
