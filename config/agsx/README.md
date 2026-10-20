# Phase4-C AGSX production deployment

This directory defines the production AGSX custom code sandbox. The V1 Tool is
non-persistent, has a five-minute timeout, uses 2 CPU / 2 GiB and `SANDBOX`
network mode. Runtime package installation is disabled; interpreter dependencies
are validated while the image is built.

## Configuration boundary

No Phase4-C deployment value is stored under `config/env`.

The only operator-managed AGSX configuration file is:

```text
/etc/touming/agsx/deploy.env
```

It contains Tencent Cloud API credentials, registry credentials, Tool/image
parameters, E2B runtime settings, quota/cost settings and the dedicated artifact
COS configuration. The tracked schema is `config/agsx/deploy.env.example`.

The deployment script parses `deploy.env` as plain `KEY=VALUE` data. It never
uses `source` or `eval`, never passes secrets as command arguments and never
copies secrets into release JSON. Tencent credentials are visible only to `agr`
child processes, and the registry password is passed to
`docker login --password-stdin` with a temporary Docker configuration.

After a Tool is successfully verified or created, the script generates:

```text
/etc/touming/agsx/runtime.env
```

This second file is generated automatically and must not be edited by an
operator. It contains only the explicit Backend/Celery runtime allowlist and
excludes Tencent deployment credentials and registry credentials. Production
Compose mounts it read-only as `/app/.env.agsx`.

## Relevant files

- `Dockerfile`: extends the approved Tencent image pinned by digest and keeps
  the `/init` entrypoint.
- `tool-2c2g.example.json`: secret-free AGSX Tool request structure.
- `deploy.env.example`: schema for the single operator-managed production file.
- `scripts/ops/deploy_agsx_tool.py`: validates, builds, pushes, creates/reuses
  and polls the Tool, then generates the runtime-only environment.
- `scripts/ops/deploy_agsx_production.sh`: one-command production entry point.
- `backend/config/agsx_runtime_env.py`: fail-closed runtime allowlist loader.

## One-time host prerequisites

Tencent Cloud account-level resources are assumed to be already deployed. The
deployment host still requires Python 3, Git, Docker and Tencent `agr` CLI.

```bash
python3 --version
git --version
docker version
agr version -o json --non-interactive
```

The entry script adds `$HOME/.local/bin` to `PATH`, covering the default
unprivileged `agr` installation location. The deployment account must be able
to use Docker and write `/etc/touming/agsx` and
`/var/lib/touming/agsx/releases`.

## Production deployment

The production checkout is expected at:

```text
/home/ubuntu/project/touming/touming-backend
```

### 1. Pull the approved code

```bash
cd /home/ubuntu/project/touming/touming-backend
git fetch origin
git switch develop
git pull --ff-only origin develop
git status --short --branch
git rev-parse HEAD
```

### 2. Install the single operator-managed configuration

Create protected directories:

```bash
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0700 \
  /etc/touming/agsx
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0750 \
  /var/lib/touming/agsx/releases
```

For the first deployment only:

```bash
cd /home/ubuntu/project/touming/touming-backend
test ! -e /etc/touming/agsx/deploy.env
install -m 0600 \
  config/agsx/deploy.env.example \
  /etc/touming/agsx/deploy.env
${EDITOR:-vi} /etc/touming/agsx/deploy.env
```

For later releases, edit the existing file in place; do not copy the example
over real credentials.

Fill every `<...>` placeholder. Important relationships:

1. `AGSX_TEMPLATE` must exactly equal `AGSX_TOOL_NAME`.
2. Use a new Tool name and image tag whenever image content or Tool parameters
   change.
3. `AGSX_BASE_IMAGE` must use an approved `@sha256:` digest.
4. Cost rate and both daily budgets must be positive for the requested full
   rollout.
5. `AGSX_RUNTIME_ENV_PATH` should remain
   `/etc/touming/agsx/runtime.env` because production Compose mounts that path.

Generate non-secret release identifiers:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
git rev-parse --short=12 HEAD
date -u +%Y%m%d
```

Use the UUID for `AGSX_CLIENT_TOKEN` and a date/commit tag for
`AGSX_IMAGE_TAG`.

### 3. Validate without changing Tencent Cloud

Run both checks as the deployment account that owns the protected file:

```bash
./scripts/ops/deploy_agsx_production.sh --check-config
./scripts/ops/deploy_agsx_production.sh --dry-run
```

`--dry-run` writes a secret-free `tool-request.json` below
`AGSX_RELEASE_ROOT` and performs no Docker push or Tencent Cloud call. Review
the request for `SANDBOX`, 2 CPU, 2 GiB, five minutes, `Persistent: false`, the
approved runtime role and immutable image tag.

### 4. Execute the one-click deployment

```bash
./scripts/ops/deploy_agsx_production.sh
```

The script performs:

1. protected-file ownership/mode and full configuration validation;
2. `agr status` and `agr doctor` with credentials injected from `deploy.env`;
3. existing Tool lookup and safe reuse of a matching ACTIVE Tool;
4. only for a missing immutable Tool: registry login, `linux/amd64` build,
   `/init` verification, digest-pinned push, creation and ACTIVE polling;
5. atomic mode-0600 generation of `/etc/touming/agsx/runtime.env`;
6. secret-free deployment results under `AGSX_RELEASE_ROOT`.

A same-name Tool with a different image tag, network or resources is rejected.

### 5. Apply the application release

Run the project's normal production Compose rollout after the AGSX script.
Backend, Celery Worker and Celery Beat mount the generated runtime file. Django
loads it after the legacy `.prod` source, so the separate AGSX file wins without
modifying anything under `config/env`.

Phase0 smoke-test COS credentials are intentionally not part of `deploy.env`.
When that legacy smoke test is needed, inject its credentials only for the smoke
command through a controlled, temporary environment.

Then verify:

```bash
cd /home/ubuntu/project/touming/touming-backend/backend
.venv/bin/python manage.py check --deploy
.venv/bin/python manage.py agent_runtime_phase4c_preflight
```

Complete the Phase 0 smoke test and Phase 2 acceptance matrix before serving
production traffic.

## Alternate path for controlled testing

The production path is fixed by convention. A test host can override only the
input path for one process:

```bash
AGSX_CONFIG_FILE=/absolute/test/path/deploy.env \
  ./scripts/ops/deploy_agsx_production.sh --dry-run
```

If `AGSX_RUNTIME_ENV_PATH` is also changed, that generated file is not consumed
by the production Compose definition.
