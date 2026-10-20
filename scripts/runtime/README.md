# Retired OpenClaw Runtime Assets

The Shared Host Bridge, per-company OpenClaw Compose runtimes, Tencent
Lighthouse Provision, Bootstrap and Deploy Bridge workflows were retired by
Phase 4-C. They are not part of the active backend or deployment procedure.

The files in this directory are retained for one release cycle only so an
approved rollback can restore the previous release package. In particular:

- `shared_host_bridge.py` is a stopped legacy service artifact.
- `deploy_lighthouse_bridge.py` is a recovery-only deployment artifact.
- versioned `openclaw_tools_*` modules support only the retained legacy package.

Do not invoke these scripts during a normal deploy, do not add their variables
back to the active backend environment, and do not restart the Bridge outside
the rollback procedure. Active Agent execution uses the unified Django runtime,
OpenViking and Tencent AGSX.

Operational procedures, evidence requirements, stop commands and the approved
rollback order are documented in:

```text
docs/phase4-c-infrastructure-retirement.md
```

After one complete release cycle without rollback, remove these retained assets
through a separate reviewed cleanup change. Historical database tables remain
read-only until their own later physical-deletion maintenance window.
