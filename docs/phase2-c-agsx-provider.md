# P2-C Tencent AGSX Provider implementation

## Delivered boundary

Phase 2-C adds a tenant-bound AGSX provider, company-scoped Redis leases, COS
Run artifacts, output limits, a Celery Reaper, structured metrics, deployment
checks, and Celery Beat scheduling. The public chat and Agent-test APIs remain
unchanged; the provider is called only by the P2-A `SandboxToolExecutor`.

`AGSX_ENABLED` defaults to `false`. Enabling it requires the included
`SandboxExecution` Migration to be applied and all AGSX/COS credentials.
Django's deploy check fails closed when these prerequisites are missing.

## Security invariants

- Company and Run ownership come from the trusted `AgentRun`/policy context.
- V1 forces isolated networking and passes an empty environment at creation.
- Executions accept only `LANG`, locale, timezone, and Python encoding variables.
- E2B, model, OpenViking, and COS management credentials never enter a sandbox.
- Commands are represented as an argument tuple and quoted with `shlex.join`.
- The working directory stays below `/tmp/touming/runs/<run UUID>/work`.
- Inputs and outputs use company-hashed, Run-bound `artifact://v1/...` refs.
- `external_id` is stored for cleanup but omitted from status/API payloads.
- stdout/stderr use UTF-8-safe byte limits; overflow and output files have
  per-file, total-size, and count limits.

## SandboxExecution schema

Phase 2-C includes this model in `agent_runtime/models.py` and Migration
`0005_sandbox_execution`:

| Field | Required shape |
|---|---|
| `id` | UUID primary key |
| `run` | OneToOne to `AgentRun` |
| `company` | FK to `company.Company` |
| `provider` | varchar, `tencent_agsx` |
| `external_id` | varchar, initially blank |
| `status` | creating/ready/running/terminated/failed |
| `template` | varchar |
| `cpu_cores` / `memory_mib` | positive integer |
| `network_policy` | varchar |
| `expires_at` | UTC datetime |
| `ready_at` / `terminated_at` | nullable UTC datetime |
| `billed_seconds` | positive integer |
| `error_code` | safe varchar |
| `created_at` | UTC datetime |

Indexes are required on `(status, expires_at)` and `(company, created_at)`.
The repository intentionally resolves this model lazily and raises
`SandboxSchemaUnavailable` if application code and database deployment become
inconsistent.

P2-A also needs its B-owned `AgentToolCall` model and Migration before the
tool loop can persist audit state.

## Lifecycle

1. Resolve the company policy and force isolated networking.
2. Acquire an expiring Redis semaphore member keyed by company and Run.
3. Reserve one `SandboxExecution` row; its OneToOne constraint prevents a
   second sandbox for the same Run.
4. Create AGSX with safe labels and no Backend environment.
5. Persist `external_id` immediately, before validating public-egress denial
   and observing the effective resources, so the Reaper can recover failed
   post-create cleanup.
6. Reconnect by `external_id` across workers and reuse within the Run.
7. Upload trusted input refs, execute in `work`, upload regular files from
   `output`, and return internal artifact refs.
8. Completed, failed, and cancelled Runs call idempotent `terminate()`.
9. Celery Beat runs the Reaper every minute as a crash-recovery fallback.

## Celery scheduling

The deployment now runs a separate `celery-beat` process. It schedules:

- failed OpenViking memory-job recovery every five minutes;
- expired/terminal/failed sandbox cleanup every minute.

Only one Beat scheduler should run per environment. Multiple workers are safe
because termination is idempotent, but the B-owned model should use the
documented OneToOne and indexed status fields.

## Metrics

Low-cardinality metrics are written as valid JSON Lines to
`logs/django/sandbox_metrics.jsonl` for log-agent collection. They cover
create/execute/terminate latency, billed seconds, status reconciliation,
cleanup outcomes, resource mismatches, truncated output, and artifact bytes.
Run IDs are restricted-log correlation fields and are not metric labels.

## Verification

Local/Fake verification:

```bash
cd backend
.venv/bin/python -m django check --settings=config.settings.security_tests
.venv/bin/python manage.py test \
  apps.agent_runtime.tests.test_phase0_sandbox \
  apps.agent_runtime.tests.test_phase2_sandbox_models \
  apps.agent_runtime.tests.test_phase2_sandbox_provider \
  apps.agent_runtime.tests.test_phase2_sandbox_artifacts \
  --settings=config.settings.security_tests
```

Cloud acceptance must cover create, `python -V`, input/output round-trip,
multiple calls in one Run, no-tool zero creation, permission denial zero
creation, timeout, cancellation, Worker termination followed by Reaper cleanup,
public-egress denial, cross-company artifact denial, and console confirmation
that no instance remains.

## Rollback

1. Set all companies to `legacy`, or set `AGSX_ENABLED=false`.
2. Keep Celery Worker and Beat running until the Reaper reports no active rows.
3. Confirm the AGSX console has no `service=touming` instances.
4. Roll back the application while retaining `SandboxExecution` audit rows.
5. Do not reverse the schema Migration until all rows are terminal and an
   audit backup has been taken.
