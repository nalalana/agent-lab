#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_DIR="$ROOT_DIR/config/docker"
COMPOSE_FILE="$COMPOSE_DIR/compose.dev.yml"
RUNTIME_DIR="/home/ubuntu/docker-data/touming/postgres_dev"
BASELINE_ROOT="/home/ubuntu/backups/touming/postgres/baseline"
TARGET="${1:-latest}"

if [[ "$TARGET" == "latest" ]]; then
  TARGET="$(find "$BASELINE_ROOT" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
fi

if [[ -z "$TARGET" || ! -d "$TARGET" ]]; then
  echo "baseline not found: $TARGET" >&2
  exit 1
fi

cd "$COMPOSE_DIR"

echo "restoring from $TARGET"
docker compose -f "$COMPOSE_FILE" stop backend celery-worker postgres >/dev/null || true
sudo mkdir -p "$RUNTIME_DIR"
sudo find "$RUNTIME_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
sudo rsync -a --delete "$TARGET/" "$RUNTIME_DIR/"
sudo chown -R 70:70 "$RUNTIME_DIR"
docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null
sleep 8
docker compose -f "$COMPOSE_FILE" up -d backend celery-worker >/dev/null || true

echo "restored=$TARGET"
