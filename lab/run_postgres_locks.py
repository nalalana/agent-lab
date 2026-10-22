"""Actual PostgreSQL row locks, source-extracted services, SQLite remote substitute."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from bootstrap import ROOT, setup


def config(path=None):
    values = dict(os.environ)
    if path is not None:
        values.update(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    if values.get("AGENT_LAB_PG_ENABLED") != "1":
        raise ValueError("Set AGENT_LAB_PG_ENABLED=1 for an explicitly configured test database")
    password = values.get("AGENT_LAB_PG_PASSWORD")
    if not password:
        raise ValueError("Missing AGENT_LAB_PG_PASSWORD")
    port = int(values.get("AGENT_LAB_PG_PORT", "55432"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid AGENT_LAB_PG_PORT")
    database = values.get("AGENT_LAB_PG_DATABASE", "agent_lab_test")
    if database != "agent_lab_test" and not database.startswith("test_"):
        raise ValueError("An explicitly named test database is required")
    return {"dbname": database, "user": values.get("AGENT_LAB_PG_USER", "agent_lab_test"), "password": password,
            "host": values.get("AGENT_LAB_PG_HOST", "127.0.0.1"), "port": port,
            "connect_timeout": 5, "sslmode": "require"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run(output, config_path=None, *, remote_factory=None, extra_checks=None, manifest_updates=None):
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "unavailable", "checks_passed": 0, "model_calls": 0,
               "real_immudb": False, "postgresql_lock_behavior_tested": False}
    control = None
    schema = "agent_lab_" + uuid4().hex
    created = False
    try:
        try:
            cfg = config(config_path)
        except (ValueError, TypeError) as exc:
            summary["reason"] = str(exc)
            return 2
        import psycopg2
        from psycopg2 import sql
        try:
            control = psycopg2.connect(**cfg)
        except psycopg2.Error:
            summary["reason"] = "Configured test PostgreSQL connection unavailable; no fallback used"
            return 2
        control.autocommit = True
        with control.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        setup({"ENGINE": "django.db.backends.postgresql", "NAME": cfg["dbname"],
            "USER": cfg["user"], "PASSWORD": cfg["password"], "HOST": cfg["host"], "PORT": cfg["port"],
            "OPTIONS": {"connect_timeout": 5, "sslmode": cfg["sslmode"],
                "options": f"-c search_path={schema} -c lock_timeout=5000 -c statement_timeout=10000"}})
        from django.db import connection, connections
        from apps.opengate.models import Gates, GatesTable, APIKey, TableAuditEvent
        from apps.user.models import User
        from run_table_boundary import RemoteFixture, load_writer, SOURCE
        with connection.schema_editor() as editor:
            for model in (User, Gates, GatesTable, APIKey, TableAuditEvent):
                editor.create_model(model)
        user = User.objects.create(uid="lock-user", username="Fixture", created_at="1", updated_at="1")
        gate = Gates.objects.create(gid="test_" + uuid4().hex if remote_factory else "lock-project",
            owner=user, gatename="Fixture", created_at="1", updated_at="1")
        manifest = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
            "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(), "schema": schema,
            "substitutions": ["IAM allow fixture", "moderation allow fixture", "SQLite remote store"]}
        manifest.update(manifest_updates or {})
        summary["real_immudb"] = manifest.get("real_immudb", False)
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        trials = []
        for first in ("write", "clear"):
            table = GatesTable.objects.create(tid="t" + uuid4().hex if remote_factory else "lock-" + first,
                gate=gate, name=first,
                num_columns=2, created_at="1", updated_at="1")
            entered, release = threading.Event(), threading.Event()
            target = (remote_factory(gate, table) if remote_factory else
                      RemoteFixture(output / (first + "-remote.sqlite")))
            class Remote:
                armed = False
                def pause(self):
                    if self.armed:
                        self.armed = False
                        entered.set()
                        require(release.wait(5), "Holding writer was not released")
                def upsert_data(self, *args):
                    self.pause()
                    return target.upsert_data(*args)
                def clear_cells(self, *args):
                    self.pause()
                    return target.clear_cells(*args)
                def values(self, tid):
                    return target.values(tid)
                @property
                def calls(self):
                    return target.calls
            remote = Remote()
            writer = load_writer(remote)
            ok, receipt = writer.upsert_table_cells(user, table, [{"r": 0, "c": 0, "value": 100}])
            require(ok, "Seed write failed")
            version = receipt["table_version"]
            remote.armed = True
            def invoke(kind, name):
                try:
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SET application_name = %s", [name])
                    if kind == "write":
                        return writer.upsert_table_cells(user, table, [{"r": 0, "c": 0, "value": 200}],
                            expected_table_version=version)
                    return writer.clear_table_cells(user, table, [{"r": 0, "c": 0}], expected_table_version=version)
                except Exception as exc:
                    return {"code": getattr(exc, "code", type(exc).__name__)}
                finally:
                    connections.close_all()
            waiter_name = schema + "_waiter"
            with ThreadPoolExecutor(max_workers=2) as pool:
                winner = pool.submit(invoke, first, schema + "_winner")
                try:
                    require(entered.wait(3), "First operation never reached remote while holding table lock")
                    loser = pool.submit(invoke, "clear" if first == "write" else "write", waiter_name)
                    waiting = False
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with control.cursor() as cursor:
                            cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock')", [waiter_name])
                            waiting = cursor.fetchone()[0]
                        if waiting:
                            break
                        time.sleep(0.02)
                    require(waiting and not loser.done(), "PostgreSQL did not expose a blocked competing operation")
                finally:
                    release.set()
                require(winner.result(timeout=6)[0] is True, "First operation did not commit")
                require(loser.result(timeout=6) == {"code": "TABLE_WRITE_CONFLICT"}, "Waiting operation did not reject stale version")
            require(remote.calls == 2, "Stale operation reached remote store")
            expected = "200" if first == "write" else ""
            require(remote.values(table.tid)[0]["value"] == expected, "Intervening value overwritten")
            trials.append({"first": first, "postgres_lock_wait_observed": True, "stale_operation_rejected": True})
        if extra_checks:
            trials.extend(extra_checks(user, gate))
        (output / "trials.json").write_text(json.dumps(trials, indent=2), encoding="utf-8")
        summary.update(status="passed", checks_passed=len(trials), postgresql_lock_behavior_tested=True)
        return 0
    except Exception as exc:
        # Avoid printing database exceptions: they may contain connection information.
        summary.update(status="failed", error_type=type(exc).__name__)
        return 1
    finally:
        if created:
            try:
                from django.conf import settings
                if settings.configured:
                    from django.db import connections
                    connections.close_all()
                with control.cursor() as cursor:
                    cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                summary["schema_removed"] = True
            except Exception:
                summary.update(status="failed", schema_removed=False)
        if control is not None:
            control.close()
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary))
        if summary.get("schema_removed") is False:
            return 1  # Cleanup failure overrides otherwise successful checks.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="Explicit local JSON configuration; never committed")
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve(), args.config))
