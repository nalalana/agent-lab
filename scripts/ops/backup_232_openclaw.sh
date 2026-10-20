#!/usr/bin/env bash
set -euo pipefail

# 232 OpenClaw runtime backup.
#
# Optional environment:
#   BACKUP_ROOT=/home/bifnc/backups/openclaw
#   RETENTION_DAYS=7
#   TENANTS_DIR=/data/tsds/tenants
#   BRIDGE_DIR=/opt/tsds/bridge
#   BRIDGE_ENV_FILE=/etc/tsds/shared-host-bridge.env
#   BRIDGE_SERVICE_NAME=tsds-shared-host-bridge.service
#   ENCRYPTION_PASSPHRASE_FILE=/path/root-only/passphrase
#   ALLOW_UNENCRYPTED_BACKUP=false
#   REMOTE_RSYNC_TARGET=user@host:/backup/path
#   COS_BUCKET_URI=cos://bucket/prefix
#   COS_UPLOAD_CMD='custom-upload "$BACKUP_ARTIFACT" "$BACKUP_CHECKSUM"'

BACKUP_ROOT="${BACKUP_ROOT:-/home/bifnc/backups/openclaw}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TENANTS_DIR="${TENANTS_DIR:-/data/tsds/tenants}"
BRIDGE_DIR="${BRIDGE_DIR:-/opt/tsds/bridge}"
BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE:-/etc/tsds/shared-host-bridge.env}"
BRIDGE_SERVICE_NAME="${BRIDGE_SERVICE_NAME:-tsds-shared-host-bridge.service}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${BACKUP_ROOT}/${STAMP}"
ARCHIVE_NAME="openclaw-runtime-${STAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_ROOT}/${ARCHIVE_NAME}"
ENCRYPTED_PATH="${ARCHIVE_PATH}.enc"
REDACTED_ENV="${OUT_DIR}/shared-host-bridge.env.redacted"
BRIDGE_STAGING_DIR="${OUT_DIR}/bridge-whitelist"

log() {
  echo "[backup][openclaw] $*" >&2
}

fail() {
  echo "[backup][openclaw][error] $*" >&2
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

validate_remote_encryption_policy() {
  if [[ -n "${REMOTE_RSYNC_TARGET:-}" || -n "${COS_BUCKET_URI:-}" || -n "${COS_UPLOAD_CMD:-}" ]]; then
    if [[ -z "${ENCRYPTION_PASSPHRASE_FILE:-}" && "${ALLOW_UNENCRYPTED_BACKUP:-false}" != "true" ]]; then
      fail "remote upload requires ENCRYPTION_PASSPHRASE_FILE unless ALLOW_UNENCRYPTED_BACKUP=true"
    fi
  fi
}

redact_env_file() {
  local source="$1"
  local target="$2"
  if [[ ! -r "${source}" ]]; then
    echo "missing_or_unreadable=${source}" > "${target}"
    return 0
  fi

  sed -E \
    -e 's/^([A-Za-z_][A-Za-z0-9_]*=).*/\1<redacted>/' \
    -e 's/(token|secret|password|passwd|key)=([^[:space:]]+)/\1=<redacted>/Ig' \
    "${source}" > "${target}"
}

copy_bridge_whitelist() {
  mkdir -p "${BRIDGE_STAGING_DIR}"

  local found=false
  local filename
  local source
  for filename in \
    shared_host_bridge.py \
    runtime_security.py \
    runtime_policy.py \
    openclaw_tools_*.py
  do
    for source in "${BRIDGE_DIR}"/${filename}; do
      [[ -f "${source}" ]] || continue
      case "$(basename "${source}")" in
        *.bak|*.bak*|*.env|*.key|*.pem|*token*|*secret*|*password*)
          log "skipping sensitive or backup bridge file $(basename "${source}")"
          continue
          ;;
      esac
      sudo install -m 0640 -o "$(id -u)" -g "$(id -g)" "${source}" "${BRIDGE_STAGING_DIR}/$(basename "${source}")"
      found=true
    done
  done

  [[ "${found}" == "true" ]] || fail "no whitelisted bridge files found in ${BRIDGE_DIR}"
}

write_systemd_unit_inventory() {
  local target="${OUT_DIR}/${BRIDGE_SERVICE_NAME}.inventory"
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl_not_available=true" > "${target}"
    return 0
  fi

  systemctl show "${BRIDGE_SERVICE_NAME}" \
    -p Id \
    -p Names \
    -p LoadState \
    -p ActiveState \
    -p FragmentPath \
    -p DropInPaths > "${target}" 2>/dev/null || {
      echo "systemd_unit_unavailable=${BRIDGE_SERVICE_NAME}" > "${target}"
      return 0
    }

  {
    echo ""
    echo "# unit_file_sha256"
    systemctl show "${BRIDGE_SERVICE_NAME}" -p FragmentPath --value 2>/dev/null | while read -r unit_path; do
      [[ -n "${unit_path}" && -r "${unit_path}" ]] && sha256sum "${unit_path}" || true
    done
    systemctl show "${BRIDGE_SERVICE_NAME}" -p DropInPaths --value 2>/dev/null | tr ' ' '\n' | while read -r dropin_path; do
      [[ -n "${dropin_path}" && -r "${dropin_path}" ]] && sha256sum "${dropin_path}" || true
    done
  } >> "${target}"
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
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type f -mtime +"${RETENTION_DAYS}" -name "openclaw-runtime-*.tar.gz*" -delete
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type f -mtime +"${RETENTION_DAYS}" -name "openclaw-runtime-*.sha256" -delete
  else
    log "retention cleanup skipped; RETENTION_DAYS is not numeric"
  fi
}

require_cmd tar
require_cmd sha256sum
require_cmd sed

validate_backup_root
validate_remote_encryption_policy

[[ -d "${TENANTS_DIR}" ]] || fail "tenant directory not found: ${TENANTS_DIR}"
[[ -d "${BRIDGE_DIR}" ]] || fail "bridge directory not found: ${BRIDGE_DIR}"

mkdir -p "${OUT_DIR}"
chmod 700 "${BACKUP_ROOT}" "${OUT_DIR}"

log "output=${OUT_DIR}"

{
  echo "created_at=$(date -Is)"
  echo "host=$(hostname)"
  echo "scope=openclaw-runtime"
  echo "tenants_dir=${TENANTS_DIR}"
  echo "bridge_dir=${BRIDGE_DIR}"
  echo "bridge_env_file=${BRIDGE_ENV_FILE}"
  echo "bridge_service_name=${BRIDGE_SERVICE_NAME}"
} > "${OUT_DIR}/MANIFEST.txt"

log "archiving tenant data"
sudo tar -czf "${OUT_DIR}/tsds-tenants.tar.gz" -C "$(dirname "${TENANTS_DIR}")" "$(basename "${TENANTS_DIR}")"
sudo chown "$(id -u):$(id -g)" "${OUT_DIR}/tsds-tenants.tar.gz"

log "archiving bridge runtime"
copy_bridge_whitelist
tar -czf "${OUT_DIR}/tsds-bridge.tar.gz" -C "${BRIDGE_STAGING_DIR}" .
rm -rf "${BRIDGE_STAGING_DIR}"

log "writing redacted bridge env inventory"
redact_env_file "${BRIDGE_ENV_FILE}" "${REDACTED_ENV}"
chmod go-rwx "${REDACTED_ENV}"

log "capturing systemd unit"
write_systemd_unit_inventory

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
