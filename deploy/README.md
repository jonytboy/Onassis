# ONASSIS deployment — the standard path

Routine operation of ONASSIS, **including software updates, is done entirely
from the Operations Centre** (System → Software Updates). Terminal access is
reserved for exceptional maintenance and disaster recovery, not normal business
operation.

## The release flow (no SSH)

```
Check  →  Download  →  Validate  →  Deploy  →  Restart  →  Health  →  Success
```

- **Check** — contacts GitHub, reports current/latest/commits-behind/release notes. Changes nothing.
- **Download** — `git fetch` only. Nothing is applied.
- **Validate** — simulates the deploy (pre-flight checks + release preview). Changes nothing.
- **Deploy** — the only privileged step: automatic snapshot (DB + config + commit) → migrate → hard-reset to `origin/<branch>` → **restart** → health, with automatic rollback on failure and a live log.

The web application talks to GitHub over HTTPS and manages its own working tree.
It never runs arbitrary shell commands.

## Privilege-separated restart (the last automated step)

The web app runs unprivileged and must not call `systemctl`. Instead, restart is
brokered through a tiny root helper:

```
Operations Centre → Deployment Service → restart.request (spool file)
                                              ↓
                        systemd path unit (onassis-restart.path)
                                              ↓
                     root oneshot (onassis-restart.service → restart-worker.sh)
                                              ↓
                          systemctl restart onassis → health check
                                              ↓
                                   restart.result (spool file)
```

The app can only *drop a request file*; the root oneshot performs the restart and
health-check and writes a result. The security boundary stays intact.

### Install

```sh
sudo cp deploy/systemd/onassis.service          /etc/systemd/system/
sudo cp deploy/systemd/onassis-restart.service  /etc/systemd/system/
sudo cp deploy/systemd/onassis-restart.path     /etc/systemd/system/
sudo chmod +x /opt/onassis/deploy/restart-worker.sh
sudo systemctl daemon-reload
sudo systemctl enable --now onassis.service onassis-restart.path
```

`RuntimeDirectory=onassis` makes systemd create `/run/onassis` (the request/result
spool) owned by the service user. Set `ONASSIS_RESTART_SPOOL=/run/onassis` for the
app so the Deployment Service uses the bridge automatically. Without the bridge
configured, the app defers the restart and flags "restart required" in the
Operations Centre instead.

## Runtime lives outside the checkout

Set `ONASSIS_RUNTIME_DIR` (e.g. `/var/lib/onassis`) so the database, logs,
backups and exports live outside the Git checkout. Application code is then
effectively read-only, and a deploy/rollback never touches business data.

## Git bundles are for emergencies only

Git bundles are **not** part of the normal release process. The platform checks
GitHub, downloads, validates and deploys on its own. A bundle is only a manual
disaster-recovery mechanism for when GitHub or the network is unavailable — apply
one by hand, then return to the Operations Centre flow.
