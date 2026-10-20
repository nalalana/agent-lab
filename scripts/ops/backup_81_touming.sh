#!/usr/bin/env bash
set -euo pipefail

# 81 server production backup scope:
# - immudb primary data is mandatory.
# - immudb replica data is optional evidence / rebuild aid.
# - Tencent Cloud PostgreSQL / Redis are not backed up by this local script.
#
# Optional environment:
#   BACKUP_ROOT=/home/ubuntu/backups/immudb
#   RETENTION_DAYS=7
#   IMMUDB_PRIMARY_VOLUME=...
#   IMMUDB_REPLICA_VOLUME=...
#   IMMUDB_CONSISTENCY_MODE=online|stop
#   IMMUDB_STOP_COMMAND='docker service scale touming_immudb-primary=0 touming_immudb-replicator=0'
#   IMMUDB_START_COMMAND='docker service scale touming_immudb-primary=1 touming_immudb-replicator=1'
#   ENCRYPTION_PASSPHRASE_FILE=/path/root-only/passphrase
#   ALLOW_UNENCRYPTED_BACKUP=false
#   REMOTE_RSYNC_TARGET=user@host:/backup/path
#   COS_BUCKET_URI=cos://bucket/prefix
#   COS_UPLOAD_CMD='custom-upload "$BACKUP_ARTIFACT" "$BACKUP_CHECKSUM"'

BACKUP_ROOT="${BACKUP_ROOT:-/home/ubuntu/backups/immudb}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${BACKUP_ROOT}/${STAMP}"
ARCHIVE_NAME="touming-immudb-${STAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_ROOT}/${ARCHIVE_NAME}"
ENCRYPTED_PATH="${ARCHIVE_PATH}.enc"
MANIFEST="${OUT_DIR}/MANIFEST.txt"
CONSISTENCY_MODE="${IMMUDB_CONSISTENCY_MODE:-}"

log() {
  echo "[backup][immudb] $*" >&2
}

fail() {
  echo "[backup][immudb][error] $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

validate_backup_root() {
  [[ -n "${BACKUP_ROOT}" ]] || fail "BACKUP_ROOT must not be empty"
  [[ "${BACKUP_ROOT}" == /* ]] || fail "BACKUP_ROOT must be an absolute path"

  case "${BACKUP_ROOT%/}" in
    ""|/|/home|/data|/opt|/etc|/var|/tmp|/usr|/root)
      fail "BACKUP_ROOT is too broad or unsafe: ${BACKUP_ROOT}"
      ;;
  esac
}

validate_consistency_mode() {
  if [[ -z "${CONSISTENCY_MODE}" ]]; then
    fail "IMMUDB_CONSISTENCY_MODE must be set explicitly to online or stop; current production policy is online because immudb backup must not stop services"
  fi
  if [[ "${CONSISTENCY_MODE}" != "stop" && "${CONSISTENCY_MODE}" != "online" ]]; then
    fail "unsupported IMMUDB_CONSISTENCY_MODE=${CONSISTENCY_MODE}; use stop or online"
  fi
}

validate_remote_encryption_policy() {
  if [[ -n "${REMOTE_RSYNC_TARGET:-}" || -n "${COS_BUCKET_URI:-}" || -n "${COS_UPLOAD_CMD:-}" ]]; then
    if [[ -z "${ENCRYPTION_PASSPHRASE_FILE:-}" && "${ALLOW_UNENCRYPTED_BACKUP:-false}" != "true" ]]; then
      fail "remote upload requires ENCRYPTION_PASSPHRASE_FILE unless ALLOW_UNENCRYPTED_BACKUP=true"
    fi
  fi
}

run_optional_command() {
  local label="$1"
  local command_text="$2"
  if [[ -z "${command_text}" ]]; then
    fail "${label} command is required when IMMUDB_CONSISTENCY_MODE=stop"
  fi
  log "${label}"
  bash -lc "${command_text}"
}

resolve_volume() {
  local explicit="$1"
  shift
  if [[ -n "${explicit}" ]]; then
    docker volume inspect "${explicit}" >/dev/null 2>&1 || fail "configured volume not found: ${explicit}"
    echo "${explicit}"
    return 0
  fi

  local candidate
  for candidate in "$@"; do
    if docker volume inspect "${candidate}" >/dev/null 2>&1; then
      echo "${candidate}"
      return 0
    fi
  done

  return 1
}

archive_volume() {
  local volume="$1"
  local output_name="$2"
  log "archiving volume ${volume}"
  docker run --rm \
    -v "${volume}:/data:ro" \
    -v "${OUT_DIR}:/backup" \
    alpine:3.20 \
    tar -czf "/backup/${output_name}" -C /data .
}

encrypt_archive_if_configured() {
  if [[ -z "${ENCRYPTION_PASSPHRASE_FILE:-}" ]]; then
    log "encryption skipped; ENCRYPTION_PASSPHRASE_FILE is not set"
    echo "${ARCHIVE_PATH}"
    return 0
  fi

  [[ -r "${ENCRYPTION_PASSPHRASE_FILE}" ]] || fail "passphrase file is not readable: ${ENCRYPTION_PASSPHRASE_FILE}"
  require_cmd openssl
  log "encrypting archive with openssl aes-256-cbc"
  openssl enc -aes-256-cbc -salt -pbkdf2 \
    -in "${ARCHIVE_PATH}" \
    -out "${ENCRYPTED_PATH}" \
    -pass "file:${ENCRYPTION_PASSPHRASE_FILE}"
  chmod go-rwx "${ENCRYPTED_PATH}"
  echo "${ENCRYPTED_PATH}"
}

upload_if_configured() {
  local artifact="$1"
  local checksum="${artifact}.sha256"
  if [[ -n "${REMOTE_RSYNC_TARGET:-}" ]]; then
    require_cmd rsync
    log "uploading to remote rsync target"
    rsync -a --chmod=F600,D700 "${artifact}" "${checksum}" "${REMOTE_RSYNC_TARGET}/"
  fi

  if [[ -n "${COS_BUCKET_URI:-}" ]]; then
    require_cmd coscli
    log "uploading to COS bucket URI"
    coscli cp "${artifact}" "${COS_BUCKET_URI}/$(basename "${artifact}")"
    coscli cp "${checksum}" "${COS_BUCKET_URI}/$(basename "${checksum}")"
  fi

  if [[ -n "${COS_UPLOAD_CMD:-}" ]]; then
    log "uploading with custom COS_UPLOAD_CMD"
    BACKUP_ARTIFACT="${artifact}" BACKUP_CHECKSUM="${checksum}" bash -lc "${COS_UPLOAD_CMD}"
  fi
}

cleanup_old_local_backups() {
  validate_backup_root
  if [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]]; then
    log "removing local backups older than ${RETENTION_DAYS} days"
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime +"${RETENTION_DAYS}" -name "20*" -exec rm -rf {} +
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type f -mtime +"${RETENTION_DAYS}" -name "touming-immudb-*.tar.gz*" -delete
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type f -mtime +"${RETENTION_DAYS}" -name "touming-immudb-*.sha256" -delete
  else
    log "retention cleanup skipped; RETENTION_DAYS is not numeric"
  fi
}

require_cmd docker
require_cmd tar
require_cmd sha256sum

validate_backup_root
validate_consistency_mode
validate_remote_encryption_policy

mkdir -p "${OUT_DIR}"
chmod 700 "${BACKUP_ROOT}" "${OUT_DIR}"

PRIMARY_VOLUME="$(
  resolve_volume "${IMMUDB_PRIMARY_VOLUME:-}" \
    touming_immudb_primary_data \
    immudb_primary_data \
    touming-ops-feature-backup-immudb-dr_immudb_primary_data
)" || fail "immudb primary volume not found; set IMMUDB_PRIMARY_VOLUME"

REPLICA_VOLUME="$(
  resolve_volume "${IMMUDB_REPLICA_VOLUME:-}" \
    touming_immudb_replica1_data \
    immudb_replica1_data \
    touming-ops-feature-backup-immudb-dr_immudb_replica1_data || true
)"

log "output=${OUT_DIR}"
{
  echo "created_at=$(date -Is)"
  echo "host=$(hostname)"
  echo "scope=immudb-only"
  echo "postgres=managed-by-tencent-cloud-not-backed-up-here"
  echo "redis=managed-by-tencent-cloud-not-backed-up-here"
  echo "consistency_mode=${CONSISTENCY_MODE}"
  echo "primary_volume=${PRIMARY_VOLUME}"
  echo "replica_volume=${REPLICA_VOLUME:-not-found}"
} > "${MANIFEST}"

if [[ "${CONSISTENCY_MODE}" == "stop" ]]; then
  run_optional_command "stopping immudb writers/services" "${IMMUDB_STOP_COMMAND:-}"
  trap 'run_optional_command "starting immudb writers/services" "${IMMUDB_START_COMMAND:-}"' EXIT
fi

archive_volume "${PRIMARY_VOLUME}" "immudb-primary-data.tar.gz"

if [[ -n "${REPLICA_VOLUME:-}" ]]; then
  archive_volume "${REPLICA_VOLUME}" "immudb-replica1-data.tar.gz"
else
  log "replica volume not found; record rebuild steps in restore drill"
fi

docker volume inspect "${PRIMARY_VOLUME}" > "${OUT_DIR}/immudb-primary-volume.inspect.json"
if [[ -n "${REPLICA_VOLUME:-}" ]]; then
  docker volume inspect "${REPLICA_VOLUME}" > "${OUT_DIR}/immudb-replica1-volume.inspect.json"
fi

find "${OUT_DIR}" -maxdepth 1 -type f ! -name SHA256SUMS -exec sha256sum {} + > "${OUT_DIR}/SHA256SUMS"
chmod -R go-rwx "${OUT_DIR}"

log "packing backup set"
tar -czf "${ARCHIVE_PATH}" -C "${BACKUP_ROOT}" "${STAMP}"
sha256sum "${ARCHIVE_PATH}" > "${ARCHIVE_PATH}.sha256"
chmod go-rwx "${ARCHIVE_PATH}" "${ARCHIVE_PATH}.sha256"

ARTIFACT="$(encrypt_archive_if_configured)"
sha256sum "${ARTIFACT}" > "${ARTIFACT}.sha256"
chmod go-rwx "${ARTIFACT}.sha256"

upload_if_configured "${ARTIFACT}"
cleanup_old_local_backups

log "done artifact=${ARTIFACT}"
