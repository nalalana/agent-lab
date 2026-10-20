#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="/home/ubuntu/backups/touming/postgres/dumps"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
GLOBAL_SQL="$BACKUP_ROOT/global-$TIMESTAMP.sql"
DB_DUMP="$BACKUP_ROOT/touming_dev-$TIMESTAMP.dump"

mkdir -p "$BACKUP_ROOT"

docker exec touming-postgres-dev bash -lc "pg_dumpall -U postgres -g" > "$GLOBAL_SQL"
docker exec touming-postgres-dev bash -lc "pg_dump -U postgres -Fc touming_dev" > "$DB_DUMP"

echo "global_sql=$GLOBAL_SQL"
echo "db_dump=$DB_DUMP"
