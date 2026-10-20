#!/usr/bin/env bash
set -euo pipefail

if [[ "${AGENT_RUNTIME_BACKUP_QUIESCED:-}" != "1" ]]; then
  echo "Refusing an inconsistent backup. Stop application writes and set AGENT_RUNTIME_BACKUP_QUIESCED=1." >&2
  exit 2
fi

: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${BACKEND_CONTAINER:=touming-backend-dev}"

command -v pg_dump >/dev/null
command -v sha256sum >/dev/null
command -v docker >/dev/null

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output_root="${1:-${project_root}/data/backups/agent-runtime}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${output_root%/}/${timestamp}"
mkdir -p "${backup_dir}"

baseline_path="${backup_dir}/baseline.json"
dump_path="${backup_dir}/${POSTGRES_DB}.dump"

container_baseline="/tmp/agent-runtime-baseline-${timestamp}.json"
docker exec "${BACKEND_CONTAINER}" \
  python manage.py agent_runtime_data_baseline \
  --fail-on-pending-migrations \
  --output "${container_baseline}"
docker cp "${BACKEND_CONTAINER}:${container_baseline}" "${baseline_path}" >/dev/null
docker exec "${BACKEND_CONTAINER}" rm -f "${container_baseline}"

baseline_database="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["database"]["name"])' "${baseline_path}")"
if [[ "${baseline_database}" != "${POSTGRES_DB}" ]]; then
  echo "Baseline database ${baseline_database} does not match POSTGRES_DB=${POSTGRES_DB}" >&2
  exit 2
fi

PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
  --host "${POSTGRES_HOST}" \
  --port "${POSTGRES_PORT}" \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --format custom \
  --no-owner \
  --no-privileges \
  --file "${dump_path}"

sha256sum "${dump_path}" > "${dump_path}.sha256"
printf '%s\n' \
  "created_at=${timestamp}" \
  "database=${POSTGRES_DB}" \
  "baseline=$(basename "${baseline_path}")" \
  "dump=$(basename "${dump_path}")" \
  > "${backup_dir}/manifest.txt"

echo "Backup completed: ${backup_dir}"
