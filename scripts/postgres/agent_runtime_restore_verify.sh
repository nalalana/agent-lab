#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 BACKUP.dump BASELINE.json" >&2
  exit 2
fi

: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${TARGET_POSTGRES_DB:?TARGET_POSTGRES_DB is required}"
: "${RESTORE_CONFIRM_DATABASE:?RESTORE_CONFIRM_DATABASE is required}"
: "${BACKEND_CONTAINER:=touming-backend-dev}"

if [[ "${TARGET_POSTGRES_DB}" != "${RESTORE_CONFIRM_DATABASE}" ]]; then
  echo "RESTORE_CONFIRM_DATABASE must exactly match TARGET_POSTGRES_DB" >&2
  exit 2
fi
if [[ "${TARGET_POSTGRES_DB}" != *_restore_verify && "${ALLOW_NONSTANDARD_RESTORE_TARGET:-}" != "1" ]]; then
  echo "Restore target must end with _restore_verify unless ALLOW_NONSTANDARD_RESTORE_TARGET=1" >&2
  exit 2
fi
if [[ "${DJANGO_ENVIRONMENT:-}" == "production" || "${DJANGO_ENVIRONMENT:-}" == "prod" ]]; then
  echo "Restore verification is forbidden when DJANGO_ENVIRONMENT is production" >&2
  exit 2
fi

dump_path="$1"
baseline_path="$2"
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

command -v pg_restore >/dev/null
command -v docker >/dev/null
if [[ -f "${dump_path}.sha256" ]]; then
  sha256sum --check "${dump_path}.sha256"
fi

PGPASSWORD="${POSTGRES_PASSWORD}" pg_restore \
  --host "${POSTGRES_HOST}" \
  --port "${POSTGRES_PORT}" \
  --username "${POSTGRES_USER}" \
  --dbname "${TARGET_POSTGRES_DB}" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  "${dump_path}"

container_validation_dir="/tmp/agent-runtime-restore-verify-$$"
cleanup_container_validation_dir() {
  docker exec "${BACKEND_CONTAINER}" rm -rf "${container_validation_dir}" >/dev/null 2>&1 || true
}
trap cleanup_container_validation_dir EXIT
docker exec "${BACKEND_CONTAINER}" mkdir -p "${container_validation_dir}"
docker cp \
  "${project_root}/scripts/postgres/validate_agent_runtime_counts.py" \
  "${BACKEND_CONTAINER}:${container_validation_dir}/validate_counts.py" >/dev/null
docker cp \
  "${baseline_path}" \
  "${BACKEND_CONTAINER}:${container_validation_dir}/baseline.json" >/dev/null
docker exec \
  -e POSTGRES_HOST="${POSTGRES_HOST}" \
  -e POSTGRES_PORT="${POSTGRES_PORT}" \
  -e POSTGRES_DB="${TARGET_POSTGRES_DB}" \
  -e POSTGRES_USER="${POSTGRES_USER}" \
  -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  "${BACKEND_CONTAINER}" \
  python "${container_validation_dir}/validate_counts.py" \
  "${container_validation_dir}/baseline.json"
cleanup_container_validation_dir
trap - EXIT

echo "Restore and count verification completed for ${TARGET_POSTGRES_DB}"
