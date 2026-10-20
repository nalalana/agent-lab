#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

# Tencent's installer places agr here for an unprivileged deployment user.
# Explicitly add it so this entry point also works from SSH, CI and systemd.
export PATH="${HOME}/.local/bin:${PATH}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_FILE="${AGSX_CONFIG_FILE:-/etc/touming/agsx/deploy.env}"

# The Python entry point parses the file as KEY=VALUE data. Deliberately do not
# source it: real credentials must never become interactive-shell variables.
exec python3 "${REPO_ROOT}/scripts/ops/deploy_agsx_tool.py" \
  --config "${CONFIG_FILE}" \
  --confirm-cloud \
  "$@"
