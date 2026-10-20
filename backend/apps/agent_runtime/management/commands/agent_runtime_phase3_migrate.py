"""Backfill Phase 3 knowledge jobs and emit an integrity report."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.agent_runtime.knowledge.integrity import MigrationIntegrityReporter
from apps.agent_runtime.knowledge.services import KnowledgeIndexService
from apps.company.models import Company, TransparentCompany, TransparentCompanyAgentDefinition
from apps.company.services import CompanyService
from apps.runtime.models import WorkspaceFile


class Command(BaseCommand):
    help = "Backfill AgentVikingSpace/knowledge jobs and report migration integrity."

    def add_arguments(self, parser):
        parser.add_argument("--cid", default="", help="Limit work to one company CID.")
        parser.add_argument("--apply", action="store_true", help="Apply deterministic backfill changes.")
        parser.add_argument("--dispatch", action="store_true", help="Dispatch created index jobs to Celery.")
        parser.add_argument("--output", default="", help="Optional JSON report path.")

    def handle(self, *args, **options):
        company = self._company(options["cid"])
        if options["apply"]:
            self._backfill_spaces(company)
            self._backfill_knowledge(company, dispatch=options["dispatch"])
        report = MigrationIntegrityReporter().build(company=company)
        rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        if options["output"]:
            Path(options["output"]).write_text(rendered + "\n", encoding="utf-8")
        self.stdout.write(rendered)
        if not report["summary"]["valid"]:
            self.stderr.write(self.style.WARNING("Phase 3 integrity report contains issues."))

    @staticmethod
    def _company(cid):
        if not cid:
            return None
        return Company.objects.get(cid=cid)

    @staticmethod
    def _backfill_spaces(company) -> None:
        transparent_companies = TransparentCompany.objects.select_related("company")
        if company is not None:
            transparent_companies = transparent_companies.filter(company=company)
        for transparent_company in transparent_companies:
            agents = TransparentCompanyAgentDefinition.objects.filter(
                transparentCompany=transparent_company
            )
            for agent in agents:
                CompanyService._ensure_agent_viking_space(transparent_company, agent)

    @staticmethod
    def _backfill_knowledge(company, *, dispatch: bool) -> None:
        files = WorkspaceFile.objects.filter(logical_path__startswith="knowledge/").select_related("company")
        if company is not None:
            files = files.filter(company=company)
        dispatcher = None if dispatch else (lambda _job_id: None)
        service = KnowledgeIndexService(dispatcher=dispatcher)
        for workspace_file in files:
            if workspace_file.resource_sync_status == WorkspaceFile.RESOURCE_SYNC_COMPLETED:
                continue
            service.enqueue(workspace_file, force=True)
