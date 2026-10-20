# Dev PostgreSQL Baseline

## Runtime directory
- `/home/ubuntu/docker-data/touming/postgres_dev`

## Backup outputs
- Physical baseline: `/home/ubuntu/backups/touming/postgres/baseline/pgdata-YYYYMMDD-HHMMSS`
- Logical dumps: `/home/ubuntu/backups/touming/postgres/dumps/`

## Commands
- Create physical baseline + logical dumps:
  - `bash scripts/postgres/backup_baseline.sh`
- Create logical dumps only:
  - `bash scripts/postgres/backup_dump.sh`
- Restore latest physical baseline:
  - `bash scripts/postgres/restore_baseline.sh`
- Restore specific physical baseline:
  - `bash scripts/postgres/restore_baseline.sh /home/ubuntu/backups/touming/postgres/baseline/pgdata-YYYYMMDD-HHMMSS`

## Notes
- Physical restore replaces the whole runtime `pgdata`.
- Use physical restore for fast recovery.
- Use logical dumps for inspection or cross-checks.
