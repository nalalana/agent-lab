"""Controlled lab tool: durable intent journal plus atomic business idempotency.

The caller binds scope/intent; neither is a model-selected tool argument.
This contract cannot be assumed for third-party tools.
"""
import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


@contextmanager
def transaction(path):
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


class DurableCounter:
    def __init__(self, directory, *, scope):
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("Trusted scope is required")
        self.scope = scope
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.journal, self.business = directory / "journal.sqlite", directory / "business.sqlite"
        with transaction(self.journal) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS intents (
                operation_id TEXT PRIMARY KEY, scope TEXT NOT NULL, intent_id TEXT NOT NULL,
                amount INTEGER NOT NULL, state TEXT NOT NULL, receipt TEXT,
                UNIQUE(scope, intent_id))""")
        with transaction(self.business) as db:
            db.execute("CREATE TABLE IF NOT EXISTS counters (scope TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS effects (
                operation_id TEXT PRIMARY KEY, scope TEXT NOT NULL, amount INTEGER NOT NULL,
                receipt TEXT NOT NULL)""")

    def prepare(self, intent_id, amount=1):
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("Trusted intent ID is required")
        if type(amount) is not int or amount <= 0:
            raise ValueError("Amount must be a positive integer")
        with transaction(self.journal) as db:
            db.execute("INSERT OR IGNORE INTO intents VALUES (?, ?, ?, ?, 'queued', NULL)",
                       (uuid4().hex, self.scope, intent_id, amount))
            row = db.execute("SELECT * FROM intents WHERE scope=? AND intent_id=?", (self.scope, intent_id)).fetchone()
            if row["amount"] != amount:
                raise ValueError("Intent payload conflict")
            return row["operation_id"]

    def _intent(self, db, operation_id):
        row = db.execute("SELECT * FROM intents WHERE operation_id=? AND scope=?", (operation_id, self.scope)).fetchone()
        if row is None:
            raise PermissionError("Operation is not in this trusted scope")
        return row

    def status(self, operation_id):
        """Authoritative, operation-specific business query; absence stays unknown."""
        with transaction(self.journal) as db:
            intent = dict(self._intent(db, operation_id))
        with transaction(self.business) as db:
            row = db.execute("SELECT * FROM effects WHERE operation_id=? AND scope=?", (operation_id, self.scope)).fetchone()
            if row is None:
                return {"operation_id": operation_id, "status": "unknown"}
            if row["amount"] != intent["amount"]:
                raise ValueError("Business payload conflict")
            return json.loads(row["receipt"])

    def reconcile(self, operation_id):
        # No supplied receipt argument: a model cannot assert reconciliation.
        receipt = self.status(operation_id)
        if receipt["status"] in {"committed", "rejected"}:
            with transaction(self.journal) as db:
                self._intent(db, operation_id)
                db.execute("UPDATE intents SET state=?, receipt=? WHERE operation_id=? AND scope=?",
                           (receipt["status"], json.dumps(receipt), operation_id, self.scope))
        return receipt

    def dispatch(self, operation_id, *, retry_same_key=False, crash_at=None):
        with transaction(self.journal) as db:
            row = dict(self._intent(db, operation_id))
            if row["state"] in {"committed", "rejected"}:
                return json.loads(row["receipt"])
            if row["state"] == "unknown" and not retry_same_key:
                raise RuntimeError("Unknown operation: reconcile before any retry")
            db.execute("UPDATE intents SET state='unknown' WHERE operation_id=? AND scope=?", (operation_id, self.scope))
        if crash_at == "before_dispatch":
            os._exit(70)
        self._apply(operation_id, row["amount"], crash_at=crash_at)
        if crash_at == "after_commit":
            os._exit(72)
        return self.reconcile(operation_id)

    def _apply(self, operation_id, amount, *, crash_at=None):
        # ponytail: one local transactional backend; remote tools need their own equivalent contract.
        with transaction(self.business) as db:
            existing = db.execute("SELECT * FROM effects WHERE operation_id=?", (operation_id,)).fetchone()
            if existing is not None:
                if existing["scope"] != self.scope or existing["amount"] != amount:
                    raise ValueError("Idempotency key binding conflict")
                return
            if amount > 5:
                receipt = {"operation_id": operation_id, "status": "rejected", "reason": "amount exceeds lab limit"}
            else:
                db.execute("INSERT OR IGNORE INTO counters VALUES (?, 0)", (self.scope,))
                db.execute("UPDATE counters SET value=value+? WHERE scope=?", (amount, self.scope))
                value = db.execute("SELECT value FROM counters WHERE scope=?", (self.scope,)).fetchone()["value"]
                receipt = {"operation_id": operation_id, "status": "committed", "delta": amount,
                           "counter_value_after_this_operation": value}
            db.execute("INSERT INTO effects VALUES (?, ?, ?, ?)", (operation_id, self.scope, amount, json.dumps(receipt)))
            if crash_at == "before_commit":
                os._exit(71)

    def snapshot(self, operation_id):
        with transaction(self.journal) as db:
            state = self._intent(db, operation_id)["state"]
        with transaction(self.business) as db:
            row = db.execute("SELECT value FROM counters WHERE scope=?", (self.scope,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM effects WHERE operation_id=? AND scope=?", (operation_id, self.scope)).fetchone()[0]
        return {"journal_state": state, "counter_value": row[0] if row else 0, "effect_records": count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--intent", default="increment-once")
    parser.add_argument("--amount", type=int, default=1)
    parser.add_argument("--action", choices=("prepare", "dispatch", "reconcile", "retry"), required=True)
    parser.add_argument("--crash-at", choices=("before_dispatch", "before_commit", "after_commit"))
    args = parser.parse_args()
    counter = DurableCounter(args.directory, scope=args.scope)
    op = counter.prepare(args.intent, args.amount)
    if args.action == "prepare":
        result = {"operation_id": op}
    elif args.action == "reconcile":
        result = counter.reconcile(op)
    else:
        result = counter.dispatch(op, retry_same_key=args.action == "retry", crash_at=args.crash_at)
    print(json.dumps({"result": result, **counter.snapshot(op)}))
