#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_DIR="$ROOT_DIR/config/docker"
COMPOSE_FILE="$COMPOSE_DIR/compose.dev.yml"
RUNTIME_DIR="/home/ubuntu/docker-data/touming/postgres_dev"
BACKUP_ROOT="/home/ubuntu/backups/touming/postgres"
BASELINE_ROOT="$BACKUP_ROOT/baseline"
DUMP_ROOT="$BACKUP_ROOT/dumps"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BASELINE_DIR="$BASELINE_ROOT/pgdata-$TIMESTAMP"
GLOBAL_SQL="$DUMP_ROOT/global-$TIMESTAMP.sql"
DB_DUMP="$DUMP_ROOT/touming_dev-$TIMESTAMP.dump"

mkdir -p "$BASELINE_ROOT" "$DUMP_ROOT"
cd "$COMPOSE_DIR"

echo "[1/5] stopping writers"
docker compose -f "$COMPOSE_FILE" stop backend celery-worker >/dev/null || true

echo "[2/5] stopping postgres"
docker compose -f "$COMPOSE_FILE" stop postgres >/dev/null

echo "[3/5] snapshotting pgdata to $BASELINE_DIR"
sudo mkdir -p "$BASELINE_DIR"
sudo rsync -a --delete "$RUNTIME_DIR/" "$BASELINE_DIR/"

echo "[4/5] starting postgres"
docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null
sleep 8

echo "[5/5] exporting logical dumps"
docker exec touming-postgres-dev bash -lc "pg_dumpall -U postgres -g" > "$GLOBAL_SQL"
docker exec touming-postgres-dev bash -lc "pg_dump -U postgres -Fc touming_dev" > "$DB_DUMP"
docker compose -f "$COMPOSE_FILE" up -d backend celery-worker >/dev/null || true

echo "baseline=$BASELINE_DIR"
echo "global_sql=$GLOBAL_SQL"
echo "db_dump=$DB_DUMP"
