import logging
import os
import threading
import time

import psycopg2
from psycopg2 import sql
from psycopg2.errors import DuplicateDatabase

from django.core.management import call_command
from django.db import connections


logger = logging.getLogger("touming")

_BOOTSTRAP_LOCK = threading.Lock()
_LAST_BOOTSTRAP_TS = 0.0
_BOOTSTRAP_CACHE_SECONDS = 30.0


def is_missing_database_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return "database" in message and "does not exist" in message


def should_attempt_runtime_db_bootstrap() -> bool:
    explicit = str(os.getenv("ALLOW_RUNTIME_DB_BOOTSTRAP", "")).strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    return os.getenv("DJANGO_ENVIRONMENT") == "development"


def ensure_runtime_database_ready() -> tuple[bool, str]:
    global _LAST_BOOTSTRAP_TS
    now = time.time()
    if now - _LAST_BOOTSTRAP_TS < _BOOTSTRAP_CACHE_SECONDS:
        return True, "recently_bootstrapped"

    with _BOOTSTRAP_LOCK:
        now = time.time()
        if now - _LAST_BOOTSTRAP_TS < _BOOTSTRAP_CACHE_SECONDS:
            return True, "recently_bootstrapped"

        host = os.environ.get("POSTGRES_HOST", "postgres")
        port = int(os.environ.get("POSTGRES_PORT", "5432"))
        user = os.environ.get("POSTGRES_USER", "postgres")
        password = os.environ.get("POSTGRES_PASSWORD", "postgres")
        dbname = os.environ.get("POSTGRES_DB", "touming_dev")

        try:
            admin_conn = psycopg2.connect(
                host=host,
                port=port,
                dbname="postgres",
                user=user,
                password=password,
                connect_timeout=5,
            )
            admin_conn.autocommit = True
            try:
                with admin_conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,))
                    if not cur.fetchone():
                        try:
                            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
                        except DuplicateDatabase:
                            pass
            finally:
                admin_conn.close()

            connections.close_all()
            call_command("migrate", interactive=False, verbosity=0)
            connections.close_all()
            _LAST_BOOTSTRAP_TS = time.time()
            logger.warning("[db-bootstrap] ensured missing database and applied migrations db=%s", dbname)
            return True, "bootstrapped"
        except Exception as exc:
            logger.exception("[db-bootstrap] failed to ensure runtime database db=%s err=%s", dbname, exc)
            connections.close_all()
            return False, str(exc)
