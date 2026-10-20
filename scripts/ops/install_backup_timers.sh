#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SYSTEMD_DIR="${ROOT_DIR}/scripts/ops/systemd"
INSTALL_DIR="${INSTALL_DIR:-/opt/touming-ops}"
TARGET="${1:-}"

install_units() {
  local service="$1"
  local timer="$2"
  local backup_script="$3"

  sudo install -d -m 0750 "${INSTALL_DIR}"
  sudo install -m 0750 "${ROOT_DIR}/scripts/ops/${backup_script}" "${INSTALL_DIR}/${backup_script}"
  if [[ -f "${ROOT_DIR}/scripts/ops/cos_upload.py" ]]; then
    sudo install -m 0750 "${ROOT_DIR}/scripts/ops/cos_upload.py" "${INSTALL_DIR}/cos_upload.py"
  fi
  sudo install -m 0644 "${SYSTEMD_DIR}/${service}" "/etc/systemd/system/${service}"
  sudo install -m 0644 "${SYSTEMD_DIR}/${timer}" "/etc/systemd/system/${timer}"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${timer}"
  sudo systemctl list-timers --all "${timer}"
}

case "${TARGET}" in
  81|immudb)
    install_units touming-immudb-backup.service touming-immudb-backup.timer backup_81_touming.sh
    ;;
  232|openclaw)
    install_units openclaw-runtime-backup.service openclaw-runtime-backup.timer backup_232_openclaw.sh
    ;;
  *)
    echo "usage: $0 81|immudb|232|openclaw" >&2
    exit 2
    ;;
esac
