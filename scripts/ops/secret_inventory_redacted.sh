#!/usr/bin/env bash
set -euo pipefail

echo "=== secret key inventory, values redacted ==="

scan_file() {
  local file="$1"
  [[ -f "${file}" ]] || return 0
  grep -InE '(TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY|SECRET_ID|SECRET_KEY|credential|auth)' "${file}" 2>/dev/null \
    | sed -E 's/(=|:).*/\1 [REDACTED]/I' || true
}

for file in \
  /home/ubuntu/project/touming/deploy/touming.prod.env \
  /home/ubuntu/project/touming/secrets/generated.env \
  /etc/tsds/shared-host-bridge.env \
  /etc/systemd/system/tsds-shared-host-bridge.service.d/*.conf
do
  for resolved in ${file}; do
    [[ -f "${resolved}" ]] || continue
    echo "--- ${resolved} ---"
    if [[ "${resolved}" == /etc/* ]]; then
      sudo sh -c "grep -InE '(TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY|SECRET_ID|SECRET_KEY|credential|auth)' '${resolved}' 2>/dev/null" \
        | sed -E 's/(=|:).*/\1 [REDACTED]/I' || true
    else
      scan_file "${resolved}"
    fi
  done
done

if [[ -d /data/tsds/tenants ]]; then
  echo "--- /data/tsds/tenants/*/config/openclaw.json ---"
  sudo grep -RInE '(token|secret|password|api_key|credential|auth)' /data/tsds/tenants/*/config/openclaw.json 2>/dev/null \
    | sed -E 's/(=|:).*/\1 [REDACTED]/I' || true
fi
