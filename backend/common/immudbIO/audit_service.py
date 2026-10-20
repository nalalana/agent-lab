"""
Agent Prompt evidence writing for immudb.

The platform DB remains the source of current configuration. immudb stores
append-only evidence for Prompt versions that have successfully landed in the
runtime.
"""
import hashlib
import json
import os
import time
from typing import Any

from immudb import ImmudbClient, constants

from common.immudbIO.client import toumingClient
from properties import PASSWORD, USER, db_local_base_port, db_local_base_url


class AgentPromptEvidenceService:
    DATABASE = os.getenv("IMMUDB_AUDIT_DATABASE", "touming_audit")
    TABLE = "agent_prompt_versions"
    FIELDS = [
        "evidence_id VARCHAR(64) NOT NULL",
        "cid VARCHAR(100) NOT NULL",
        "agent_id VARCHAR(100) NOT NULL",
        "agent_name VARCHAR(255)",
        "runtime_version VARCHAR(100) NOT NULL",
        "config_version_id VARCHAR(100)",
        "prompt_sha256 VARCHAR(64) NOT NULL",
        "prompt_content VARCHAR(65535)",
        "prompt_size INTEGER",
        "operator_id VARCHAR(128)",
        "notes VARCHAR(1024)",
        "created_at VARCHAR(20)",
        "readback_digest VARCHAR(64)",
        "diff_verified BOOLEAN",
        "PRIMARY KEY (evidence_id)",
    ]

    def __init__(self):
        immudb_host = os.getenv("IMMUDB_HOST", db_local_base_url)
        immudb_port = os.getenv("IMMUDB_PORT", str(db_local_base_port))
        immudb_user = os.getenv("IMMUDB_USER", USER)
        immudb_password = os.getenv("IMMUDB_ADMIN_PASSWORD", PASSWORD)
        self.client = ImmudbClient(f"{immudb_host}:{immudb_port}")
        self.client.login(immudb_user, immudb_password)

    @staticmethod
    def _hash_text(value: Any) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _load_payload(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            payload = json.loads(str(raw))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _extract_agents(payload: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for key in ("agents", "agent_configs", "agentConfigs"):
            items = payload.get(key)
            if isinstance(items, list):
                result.extend([item for item in items if isinstance(item, dict)])
        return result

    @staticmethod
    def _agent_id(item: dict[str, Any]) -> str:
        return str(item.get("agentId") or item.get("agent_id") or item.get("id") or "").strip()

    @staticmethod
    def _agent_prompt(item: dict[str, Any]) -> str:
        return str(item.get("systemPrompt") or item.get("system_prompt") or item.get("prompt") or "").strip()

    def ensure_schema(self) -> tuple[bool, str]:
        try:
            self.client.useDatabase(self.DATABASE.encode("utf-8"))
        except Exception:
            try:
                self.client.createDatabase(self.DATABASE)
            except Exception as exc:
                if "exist" not in str(exc).lower() and "already" not in str(exc).lower():
                    return False, f"create audit database failed: {exc}"
            try:
                self.client.useDatabase(self.DATABASE.encode("utf-8"))
            except Exception as exc:
                return False, f"use audit database failed: {exc}"

        ok, message = toumingClient(self.client).createTestTable(self.TABLE, *self.FIELDS)
        if not ok:
            return False, message
        return True, "ok"

    def write_prompt_versions(
        self,
        *,
        company,
        config_version: dict[str, Any],
        operator_id: str,
        notes: str,
        readback_payload: dict[str, Any],
        diff_payload: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, message = self.ensure_schema()
        if not ok:
            return False, message

        config_payload = self._load_payload(config_version.get("config_payload"))
        agents = self._extract_agents(config_payload)
        runtime_version = str(config_version.get("version") or "").strip()
        config_version_id = str(config_version.get("id") or runtime_version).strip()
        created_at = str(config_version.get("published_at") or config_version.get("created_at") or time.time())[:20]
        readback_digest = self._hash_text(json.dumps(readback_payload or {}, sort_keys=True, ensure_ascii=False))
        diff_verified = bool(isinstance(diff_payload, dict) and diff_payload.get("is_in_sync"))

        tclient = toumingClient(self.client)
        inserted = 0
        skipped = 0

        for agent in agents:
            agent_id = self._agent_id(agent)
            prompt_content = self._agent_prompt(agent)
            if not agent_id or not prompt_content:
                skipped += 1
                continue
            prompt_sha256 = self._hash_text(prompt_content)
            evidence_id = self._hash_text(
                f"{company.cid}:{agent_id}:{runtime_version}:{prompt_sha256}"
            )
            existing = tclient.simpleSelect(
                self.TABLE,
                ["evidence_id"],
                {},
                f"evidence_id = '{evidence_id}'",
                columnNameMode=constants.COLUMN_NAME_MODE_FIELD,
            )
            if existing:
                skipped += 1
                continue

            ok, insert_message = tclient.insertToTable(
                self.TABLE,
                [
                    "evidence_id",
                    "cid",
                    "agent_id",
                    "agent_name",
                    "runtime_version",
                    "config_version_id",
                    "prompt_sha256",
                    "prompt_content",
                    "prompt_size",
                    "operator_id",
                    "notes",
                    "created_at",
                    "readback_digest",
                    "diff_verified",
                ],
                [
                    "@evidence_id",
                    "@cid",
                    "@agent_id",
                    "@agent_name",
                    "@runtime_version",
                    "@config_version_id",
                    "@prompt_sha256",
                    "@prompt_content",
                    "@prompt_size",
                    "@operator_id",
                    "@notes",
                    "@created_at",
                    "@readback_digest",
                    "@diff_verified",
                ],
                {
                    "evidence_id": evidence_id,
                    "cid": company.cid,
                    "agent_id": agent_id,
                    "agent_name": str(agent.get("name") or ""),
                    "runtime_version": runtime_version,
                    "config_version_id": config_version_id,
                    "prompt_sha256": prompt_sha256,
                    "prompt_content": prompt_content,
                    "prompt_size": len(prompt_content.encode("utf-8")),
                    "operator_id": operator_id or "",
                    "notes": str(notes or "")[:1024],
                    "created_at": created_at,
                    "readback_digest": readback_digest,
                    "diff_verified": diff_verified,
                },
            )
            if not ok:
                return False, insert_message
            inserted += 1

        return True, {
            "database": self.DATABASE,
            "table": self.TABLE,
            "runtime_version": runtime_version,
            "inserted": inserted,
            "skipped": skipped,
        }

    def list_prompt_versions(self, cid: str, agent_id: str, limit: int = 30) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, message = self.ensure_schema()
        if not ok:
            return False, message
        tclient = toumingClient(self.client)
        rows = tclient.simpleSelect(
            self.TABLE,
            [
                "evidence_id",
                "cid",
                "agent_id",
                "agent_name",
                "runtime_version",
                "config_version_id",
                "prompt_sha256",
                "prompt_content",
                "prompt_size",
                "operator_id",
                "notes",
                "created_at",
                "readback_digest",
                "diff_verified",
            ],
            {},
            f"cid = '{cid}' and agent_id = '{agent_id}'",
            columnNameMode=constants.COLUMN_NAME_MODE_FIELD,
        )
        if rows is False:
            return False, "query prompt evidence failed"
        normalized = [dict(item) for item in (rows or []) if isinstance(item, dict)]
        normalized.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return True, normalized[:limit]
