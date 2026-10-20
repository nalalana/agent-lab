"""Export or compare the Phase 0 agent runtime data baseline."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.agent_runtime.baseline import build_data_baseline, compare_baseline_counts


class Command(BaseCommand):
    help = "Export migration state, model counts, and OpenViking namespace mappings."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default", help="Django database alias")
        parser.add_argument("--output", help="Write the JSON report atomically to this path")
        parser.add_argument("--compare", help="Compare counts with a previous baseline JSON")
        parser.add_argument(
            "--memory-company-cid",
            action="append",
            default=[],
            help="Company cid whose memory sample metadata may be included; repeatable",
        )
        parser.add_argument(
            "--include-memory-content",
            action="store_true",
            help="Include raw memory content; requires at least one --memory-company-cid",
        )
        parser.add_argument("--memory-sample-limit", type=int, default=20)
        parser.add_argument(
            "--fail-on-pending-migrations",
            action="store_true",
            help="Fail when unapplied or conflicting migrations are found",
        )

    def handle(self, *args, **options):
        company_cids = options["memory_company_cid"]
        if options["include_memory_content"] and not company_cids:
            raise CommandError(
                "--include-memory-content requires at least one --memory-company-cid"
            )
        if options["memory_sample_limit"] < 0:
            raise CommandError("--memory-sample-limit must be non-negative")

        report = build_data_baseline(
            using=options["database"],
            memory_company_cids=company_cids,
            include_memory_content=options["include_memory_content"],
            memory_sample_limit=options["memory_sample_limit"],
        )
        migrations = report["migrations"]
        if options["fail_on_pending_migrations"] and (
            migrations["pending"] or migrations["conflicts"]
        ):
            raise CommandError(
                "Migration check failed: "
                f"pending={migrations['pending']}, conflicts={migrations['conflicts']}"
            )

        serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        output_path = options.get("output")
        if output_path:
            target = Path(output_path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            self.stdout.write(self.style.SUCCESS(f"Baseline written to {target}"))
        else:
            self.stdout.write(serialized, ending="")

        compare_path = options.get("compare")
        if compare_path:
            expected_path = Path(compare_path).expanduser().resolve()
            try:
                expected = json.loads(expected_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CommandError(f"Cannot read baseline {expected_path}: {exc}") from exc
            mismatches = compare_baseline_counts(report, expected)
            if mismatches:
                raise CommandError("Baseline count mismatch:\n- " + "\n- ".join(mismatches))
            self.stdout.write(self.style.SUCCESS("Baseline counts match"))
