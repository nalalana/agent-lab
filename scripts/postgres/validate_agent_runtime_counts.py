#!/usr/bin/env python3
"""Validate restored PostgreSQL table counts against an exported baseline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql


SAFE_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--host", default=os.getenv("POSTGRES_HOST", "localhost"))
    parser.add_argument("--port", default=os.getenv("POSTGRES_PORT", "5432"))
    parser.add_argument("--database", default=os.getenv("POSTGRES_DB", ""))
    parser.add_argument("--user", default=os.getenv("POSTGRES_USER", ""))
    parser.add_argument("--password", default=os.getenv("POSTGRES_PASSWORD", ""))
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.database or not args.user:
        print("POSTGRES_DB and POSTGRES_USER are required", file=sys.stderr)
        return 2
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read baseline: {exc}", file=sys.stderr)
        return 2

    expected_counts = baseline.get("counts") or {}
    mismatches: list[str] = []
    connection = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
        password=args.password,
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            for key, expected in sorted(expected_counts.items()):
                table_name = str(expected.get("table") or "")
                if not SAFE_TABLE_NAME.fullmatch(table_name):
                    mismatches.append(f"{key}: unsafe or invalid table name {table_name!r}")
                    continue
                cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table_name))
                )
                actual_count = int(cursor.fetchone()[0])
                expected_count = int(expected.get("count", -1))
                if actual_count != expected_count:
                    mismatches.append(
                        f"{key} ({table_name}): expected {expected_count}, got {actual_count}"
                    )
    finally:
        connection.close()

    if mismatches:
        print("Count validation failed:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"- {mismatch}", file=sys.stderr)
        return 1
    print(f"Validated {len(expected_counts)} table counts against {args.baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
