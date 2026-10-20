# Production Hardening Runbook

This runbook covers the three production gaps that must be closed before a
formal public launch: backups, dependency vulnerability remediation, and secret
governance. Do not store plaintext secrets in this document.

## 1. Backup Strategy

### 81 application server

Persistent data lives in Docker volumes. Container restart, image rebuild, and
Swarm rolling update should not erase these volumes, but host disk loss or
`docker volume rm` will.

Critical volumes:

```text
touming_postgres_data
touming_immudb_primary_data
touming_immudb_replica1_data
touming_redis_data
touming_backend_media
touming_backend_logs
```

Minimum backup policy:

- PostgreSQL: daily logical backup with `pg_dump`, retained for at least 14 days.
- immudb: daily backup of primary and replica data volumes, retained for at least 14 days.
- Redis: daily copy of the persistent data volume if Redis data is business-critical.
- Media/config: daily backup of `touming_backend_media`, deployment env files, and stack files.
- Snapshot: take a server snapshot before every production release.
- Restore drill: test restoring PostgreSQL and tenant/media data at least once before formal launch.

Suggested script:

```text
scripts/ops/backup_81_touming.sh
```

The script writes archives under `/home/ubuntu/backups/touming/YYYYmmdd-HHMMSS`.
Copy the directory to COS or another off-host storage after each run.

### 232 OpenClaw runtime server

OpenClaw container data is bind-mounted to the host, so stopping/removing a
container does not remove tenant data as long as the host directories remain.

Critical paths:

```text
/data/tsds/tenants
/opt/tsds/bridge
/etc/tsds/shared-host-bridge.env
```

Minimum backup policy:

- Tenant data: daily archive of `/data/tsds/tenants`.
- Bridge runtime: backup `/opt/tsds/bridge` after every bridge change.
- Bridge env: backup `/etc/tsds/shared-host-bridge.env` with root-only permissions.
- Snapshot: take a server snapshot before bridge/runtime upgrades.
- Restore drill: verify one tenant can be restored into a test namespace.

Suggested script:

```text
scripts/ops/backup_232_openclaw.sh
```

## 2. Dependency Vulnerability Remediation

Current release branches include dependency upgrades intended to clear known
Dependabot alerts in the active manifests:

Backend:

- Removed legacy `properties` and `dotenv` package pins.
- Upgraded vulnerable Python dependencies:
  - `requests>=2.33.0,<3`
  - `PyJWT>=2.12.0,<3`
  - `python-dotenv>=1.2.2,<2`
  - `PyMySQL>=1.1.1,<2`

Frontend:

- Removed stale `package-lock.json`; the project uses `pnpm-lock.yaml`.
- Upgraded Next.js package set.
- Added overrides for vulnerable transitive packages such as lodash,
  picomatch, and postcss.

Required verification before formal launch:

```bash
# backend
python -m pip_audit -r /tmp/touming-backend-requirements-audit.txt
python manage.py test apps.user.tests apps.company.tests.test_registration_no_validation

# frontend
pnpm install --frozen-lockfile
pnpm audit
pnpm build
```

Important GitHub note: Dependabot alerts are calculated from the default branch
and all tracked manifests. If an old monorepo `frontend/package-lock.json` or
`frontend/yarn.lock` remains on the default branch, GitHub may continue to show
alerts even after the deployed frontend repo is clean.

## 3. Secret Governance

### Current principles

- Do not print secrets in logs, shell history, documentation, or final reports.
- Keep production secret files root-only or deployment-user-only.
- Keep bridge token, Django secret, database passwords, Redis password, immudb
  password, COS keys, Tencent Cloud keys, SMS keys, and captcha keys out of Git.
- Rotate every secret that has appeared in chat, shell history, screenshots, or
  a non-secret file.

### Required controls

81 application server:

- `/home/ubuntu/project/touming/deploy/touming.prod.env`
- `/home/ubuntu/project/touming/secrets/generated.env`
- Docker stack env input files

232 runtime server:

- `/etc/tsds/shared-host-bridge.env`
- `/etc/systemd/system/tsds-shared-host-bridge.service.d/*.conf`
- tenant `config/openclaw.json` files, especially gateway auth tokens

Recommended file permissions:

```text
env files: 600
secret directories: 700
owner: deployment user or root, never world-readable
```

### Rotation order

1. Generate new value.
2. Put the new value in the dependent service env file.
3. Restart or roll the dependent service.
4. Verify service health.
5. Revoke or delete the old value.
6. Record the rotation date without recording the secret itself.

Suggested inventory script:

```text
scripts/ops/secret_inventory_redacted.sh
```

The script prints only secret key names and file locations with values redacted.

## 4. Production Launch Gate

Formal launch should wait until all items are true:

- [ ] 81 backup script has run successfully.
- [ ] 232 backup script has run successfully.
- [ ] At least one restore drill has been completed.
- [ ] `pip-audit` reports no known vulnerabilities for active backend requirements.
- [ ] `pnpm audit` reports no known vulnerabilities for active frontend lockfile.
- [ ] 232 bridge port `19080` is restricted to the 81 server source IP or equivalent network control.
- [ ] Secrets that appeared in chat or shell history have been rotated.
- [ ] Secret files have restrictive permissions.

## 5. Tencent Cloud Cold Cutover

Current production has no business data, so the Tencent Cloud PostgreSQL and
Redis rollout should be handled as a cold cutover, not a data migration.

Do not commit real connection values. Put the Tencent Cloud PostgreSQL and Redis
host, port, username, database name, and passwords only in `config/env/.prod`.
That file is ignored by Git.

Cutover steps:

1. Create `config/env/.prod` on the production server from `config/env/.prod.example`.
2. Fill in the Tencent Cloud PostgreSQL and Redis values in `config/env/.prod`.
3. Verify the 81 server can reach Tencent Cloud PostgreSQL and Redis over the private network.
4. Start production with `config/docker/compose.prod.yml`. By default, Docker PostgreSQL and Redis are not started.
5. Run `python manage.py migrate --noinput` through the backend container to initialize the empty Tencent Cloud PostgreSQL database.
6. Run `python manage.py collectstatic --noinput` if static assets were not collected during container startup.
7. Start or restart the Celery worker.
8. Run smoke tests for login, registration, company, project, table, transparency stats, Celery, WebSocket, and Redis-backed Django cache.

Rollback:

- Keep old Docker PostgreSQL and Redis volumes for the agreed retention window.
- To temporarily use Docker local state again, start Compose with `COMPOSE_PROFILES=local-state` and point `config/env/.prod` at the Docker service names.
- Do not delete old volumes until smoke tests and an agreed observation window have passed.
