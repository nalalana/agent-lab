"""Create or verify the final Phase 4 legacy runtime archive."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.runtime.legacy_archive import LegacyArchiveError, LegacyRuntimeArchive


class Command(BaseCommand):
    help = "Archive retired OpenClaw records and verify database immutability."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default="")
        parser.add_argument("--verify-manifest", default="")
        parser.add_argument("--cid", default="")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--report-only", action="store_true")

    def handle(self, *args, **options):
        archive = LegacyRuntimeArchive()
        try:
            payload = self._execute(archive, options)
        except LegacyArchiveError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if options["verify_manifest"] and not payload["valid"]:
            raise CommandError("Legacy archive verification failed.")

    @staticmethod
    def _execute(archive, options):
        if options["report_only"]:
            return archive.report(cid=options["cid"])
        if options["verify_manifest"]:
            return archive.verify(options["verify_manifest"])
        if not options["output_dir"]:
            raise LegacyArchiveError("--output-dir is required when creating an archive.")
        return archive.create(
            options["output_dir"],
            cid=options["cid"],
            force=options["force"],
        )
