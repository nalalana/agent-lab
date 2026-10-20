"""Streaming, checksum-verifiable archives for retired OpenClaw records."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import BinaryIO

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from apps.agent_runtime.knowledge.integrity import MigrationIntegrityReporter
from apps.company.models import (
    Company,
    TransparentCompanyAgentDefinition,
    TransparentCompanyAgentVikingSpace,
)
from apps.runtime.models import (
    CompanyRuntimeBinding,
    OpenClawConfigVersion,
    OpenClawInstance,
    OpenClawProvisionTask,
    OpenClawRuntimeSnapshot,
)

from .legacy_control import legacy_runtime_is_read_only, legacy_runtime_write_mode


ARCHIVE_SCHEMA_VERSION = 1
LEGACY_MODELS = (
    OpenClawInstance,
    OpenClawProvisionTask,
    OpenClawRuntimeSnapshot,
    OpenClawConfigVersion,
    CompanyRuntimeBinding,
)


class LegacyArchiveError(ValueError):
    """Archive precondition or integrity verification failure."""


class LegacyRuntimeArchive:
    """Export stable JSONL files without loading legacy history into memory."""

    def create(self, output_dir, *, cid: str = "", force: bool = False) -> dict:
        if not legacy_runtime_is_read_only():
            raise LegacyArchiveError("Enable read_only mode before creating the final archive.")
        directory = self._prepare_directory(output_dir)
        self._preflight_targets(directory, force=force)
        model_reports = {}
        for model in LEGACY_MODELS:
            file_path = directory / f"{model._meta.db_table}.jsonl"
            model_reports[model._meta.label_lower] = self._write_model(
                model,
                file_path,
                cid=cid,
                force=force,
            )
        manifest = self._manifest(model_reports, cid=cid)
        self._write_json(directory / "manifest.json", manifest, force=force)
        return manifest

    def verify(self, manifest_path) -> dict:
        path = Path(manifest_path).expanduser().resolve()
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LegacyArchiveError(f"Cannot read archive manifest: {path}") from exc
        self._validate_manifest(manifest)
        cid = str(manifest.get("cid") or "")
        model_results = {
            model._meta.label_lower: self._verify_model(model, path.parent, manifest, cid)
            for model in LEGACY_MODELS
        }
        phase3 = self._phase3_integrity(cid)
        backfill = self._openviking_backfill_counts(cid)
        archived_backfill = manifest.get("openVikingBackfill") or {}
        backfill["unchanged"] = all(
            backfill.get(field) == archived_backfill.get(field)
            for field in ("agentCount", "spaceCount")
        )
        files_valid = all(item["archiveFileValid"] for item in model_results.values())
        database_unchanged = all(item["databaseUnchanged"] for item in model_results.values())
        return {
            "schemaVersion": ARCHIVE_SCHEMA_VERSION,
            "valid": bool(
                files_valid
                and database_unchanged
                and phase3["summary"]["valid"]
                and backfill["countMatches"]
                and backfill["unchanged"]
            ),
            "archiveFilesValid": files_valid,
            "databaseUnchanged": database_unchanged,
            "models": model_results,
            "openVikingBackfill": backfill,
            "phase3Integrity": phase3,
            "verifiedAt": timezone.now().isoformat(),
        }

    def report(self, *, cid: str = "") -> dict:
        models = {}
        for model in LEGACY_MODELS:
            digest, count = self._digest_queryset(self._queryset(model, cid))
            models[model._meta.label_lower] = {
                "table": model._meta.db_table,
                "count": count,
                "sha256": digest,
            }
        phase3 = self._phase3_integrity(cid)
        return {
            "schemaVersion": ARCHIVE_SCHEMA_VERSION,
            "legacyWriteMode": legacy_runtime_write_mode(),
            "cid": cid or None,
            "models": models,
            "openVikingBackfill": self._openviking_backfill_counts(cid),
            "phase3Integrity": phase3,
            "generatedAt": timezone.now().isoformat(),
        }

    def _write_model(self, model, path: Path, *, cid: str, force: bool) -> dict:
        with self._secure_binary_file(path, force=force) as output:
            digest, count = self._digest_queryset(self._queryset(model, cid), output)
        return {
            "table": model._meta.db_table,
            "file": path.name,
            "count": count,
            "sha256": digest,
        }

    def _verify_model(self, model, directory: Path, manifest: dict, cid: str) -> dict:
        archived = manifest["models"][model._meta.label_lower]
        file_name = str(archived.get("file") or "")
        if not file_name or Path(file_name).name != file_name:
            raise LegacyArchiveError("Archive manifest contains an unsafe file name.")
        archive_path = directory / file_name
        file_digest = self._file_digest(archive_path)
        current_digest, current_count = self._digest_queryset(self._queryset(model, cid))
        archive_file_valid = file_digest == archived["sha256"]
        database_unchanged = bool(
            current_count == archived["count"] and current_digest == archived["sha256"]
        )
        return {
            "archiveCount": archived["count"],
            "currentCount": current_count,
            "archiveFileValid": archive_file_valid,
            "databaseUnchanged": database_unchanged,
        }

    @staticmethod
    def _queryset(model, cid: str):
        queryset = model.objects.all().order_by("pk")
        return queryset.filter(company__cid=cid) if cid else queryset

    @staticmethod
    def _digest_queryset(queryset, output: BinaryIO | None = None) -> tuple[str, int]:
        digest = hashlib.sha256()
        count = 0
        for row in queryset.values().iterator(chunk_size=1000):
            line = json.dumps(
                row,
                cls=DjangoJSONEncoder,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            digest.update(line)
            if output is not None:
                output.write(line)
            count += 1
        return digest.hexdigest(), count

    def _manifest(self, model_reports, *, cid: str) -> dict:
        return {
            "schemaVersion": ARCHIVE_SCHEMA_VERSION,
            "archiveType": "openclaw_legacy_runtime",
            "restricted": True,
            "legacyWriteMode": legacy_runtime_write_mode(),
            "cid": cid or None,
            "models": model_reports,
            "openVikingBackfill": self._openviking_backfill_counts(cid),
            "phase3Integrity": self._phase3_integrity(cid),
            "generatedAt": timezone.now().isoformat(),
        }

    @staticmethod
    def _phase3_integrity(cid: str) -> dict:
        company = None
        if cid:
            try:
                company = Company.objects.get(cid=cid)
            except Company.DoesNotExist as exc:
                raise LegacyArchiveError(f"Company {cid!r} does not exist.") from exc
        return MigrationIntegrityReporter().build(company=company)

    @staticmethod
    def _openviking_backfill_counts(cid: str) -> dict:
        agents = TransparentCompanyAgentDefinition.objects.all()
        spaces = TransparentCompanyAgentVikingSpace.objects.all()
        if cid:
            agents = agents.filter(transparentCompany__company__cid=cid)
            spaces = spaces.filter(transparentCompany__company__cid=cid)
        agent_count = agents.count()
        space_count = spaces.count()
        return {
            "agentCount": agent_count,
            "spaceCount": space_count,
            "countMatches": agent_count == space_count,
        }

    @staticmethod
    def _prepare_directory(output_dir) -> Path:
        requested = Path(output_dir).expanduser()
        if requested.is_symlink():
            raise LegacyArchiveError("Archive output directory cannot be a symlink.")
        directory = requested.resolve()
        if directory.exists() and not directory.is_dir():
            raise LegacyArchiveError("Archive output path is not a directory.")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory

    @staticmethod
    def _preflight_targets(directory: Path, *, force: bool) -> None:
        if force:
            return
        targets = [directory / "manifest.json"]
        targets.extend(directory / f"{model._meta.db_table}.jsonl" for model in LEGACY_MODELS)
        existing = next((path for path in targets if path.exists()), None)
        if existing is not None:
            raise LegacyArchiveError(f"Archive file already exists: {existing}")

    @staticmethod
    def _secure_binary_file(path: Path, *, force: bool):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise LegacyArchiveError(f"Archive file already exists: {path}") from exc
        os.chmod(path, 0o600)
        return os.fdopen(descriptor, "wb")

    @classmethod
    def _write_json(cls, path: Path, payload: dict, *, force: bool) -> None:
        with cls._secure_binary_file(path, force=force) as output:
            output.write(
                (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise LegacyArchiveError(f"Cannot read archive file: {path}") from exc
        return digest.hexdigest()

    @staticmethod
    def _validate_manifest(manifest: dict) -> None:
        if manifest.get("schemaVersion") != ARCHIVE_SCHEMA_VERSION:
            raise LegacyArchiveError("Unsupported legacy archive schemaVersion.")
        expected = {model._meta.label_lower for model in LEGACY_MODELS}
        if set(manifest.get("models") or {}) != expected:
            raise LegacyArchiveError("Legacy archive manifest model set is incomplete.")
        if not isinstance(manifest.get("openVikingBackfill"), dict):
            raise LegacyArchiveError("Legacy archive has no OpenViking backfill counts.")
        for item in manifest["models"].values():
            if not isinstance(item, dict) or not isinstance(item.get("count"), int):
                raise LegacyArchiveError("Legacy archive model count is invalid.")
            checksum = str(item.get("sha256") or "")
            if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
                raise LegacyArchiveError("Legacy archive model checksum is invalid.")
