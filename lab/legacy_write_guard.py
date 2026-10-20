"""Lab guard for legacy table writes without backend idempotency support."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from durable_counter import transaction


class OutcomeUnknown(RuntimeError):
    def __init__(self, operation_id):
        self.operation_id = operation_id
        super().__init__("Write outcome is unknown; automatic retry is blocked")


class LegacyWriteGuard:
    def __init__(self, path, *, scope):
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("Trusted company/user/agent/task scope is required")
        self.path, self.scope = Path(path), scope
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with transaction(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS legacy_operations (
                operation_id TEXT PRIMARY KEY, scope TEXT NOT NULL, intent TEXT NOT NULL,
                payload_hash TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT,
                UNIQUE(scope, intent))""")

    @staticmethod
    def _authorize(authorize):
        if authorize() is not True:
            raise PermissionError("Current authorization is required, including receipt replay")

    def invoke(self, intent, tool_name, arguments, *, authorize, execute):
        self._authorize(authorize)
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("Trusted logical intent is required")
        if tool_name not in {"company.table.write", "company.table.clear_cells"}:
            raise ValueError("Unsupported mutation")
        digest = hashlib.sha256(json.dumps({"tool": tool_name, "arguments": arguments},
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        with transaction(self.path) as db:
            row = db.execute("SELECT * FROM legacy_operations WHERE scope=? AND intent=?", (self.scope, intent)).fetchone()
            if row is not None:
                if row["payload_hash"] != digest:
                    raise ValueError("Logical intent payload conflict")
                if row["state"] == "acknowledged":
                    return {"operation_id": row["operation_id"], "status": "acknowledged",
                            "replayed": True, "historical_receipt": json.loads(row["receipt"])}
                raise OutcomeUnknown(row["operation_id"])
            pending = db.execute("SELECT operation_id FROM legacy_operations WHERE scope=? AND state='unknown'", (self.scope,)).fetchone()
            if pending is not None:
                raise OutcomeUnknown(pending["operation_id"])
            operation_id = uuid4().hex
            db.execute("INSERT INTO legacy_operations VALUES (?, ?, ?, ?, 'unknown', NULL)",
                       (operation_id, self.scope, intent, digest))
        # The legacy remote write cannot join this local journal transaction.
        try:
            ok, receipt = execute()
            if ok is not True:
                raise OutcomeUnknown(operation_id)  # False can include partial remote effects.
            encoded = json.dumps(receipt, ensure_ascii=False, allow_nan=False)
            with transaction(self.path) as db:
                db.execute("UPDATE legacy_operations SET state='acknowledged', receipt=? WHERE operation_id=? AND scope=?",
                           (encoded, operation_id, self.scope))
        except Exception as exc:
            raise OutcomeUnknown(operation_id) from exc
        return {"operation_id": operation_id, "status": "acknowledged", "replayed": False,
                "historical_receipt": receipt}

    def status(self, intent, *, authorize):
        self._authorize(authorize)
        with transaction(self.path) as db:
            row = db.execute("SELECT operation_id, state FROM legacy_operations WHERE scope=? AND intent=?", (self.scope, intent)).fetchone()
            return dict(row) if row else None
