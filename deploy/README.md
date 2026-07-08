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

### Install (one command)

```sh
sudo ONASSIS_DIR=/opt/onassis SERVICE=onassis deploy/install-restart-bridge.sh
```

This installs and enables `onassis-restart.path` + `onassis-restart.service`
(pinning your paths into the oneshot) so the **Restart ONASSIS** button and the
deploy flow's restart step actually restart the process. Verify with
`systemctl status onassis-restart.path` (should be *active (waiting)*).

`RuntimeDirectory=onassis` makes systemd create `/run/onassis` (the request/result
spool) owned by the service user, and the sample `onassis.service` sets
`ONASSIS_RESTART_SPOOL=/run/onassis` so the Deployment Service uses the bridge
automatically. If your hand-written main unit lacks those two lines, add them and
`systemctl daemon-reload && systemctl restart onassis`.

### If you can't install the bridge

Set **`ONASSIS_ALLOW_RESTART=1`** in the service environment. The Restart button
then restarts directly: it tries `sudo -n systemctl restart <service>` (add a
sudoers rule for that exact command to make it non-interactive) and, if that
isn't possible, **re-execs its own process** so the running code still reloads —
no root and no service manager required. Without either the bridge or this flag,
the app defers the restart and flags "restart required" in the Operations Centre.

> Why the button can look dead: without a restart mechanism the app can only
> *request* a restart. If nothing consumes the request, the code reloads never
> happen — the tell is the banner showing a new commit but an **old schema
> version** (files updated on disk, but the old process still serving).

## Runtime lives outside the checkout

Set `ONASSIS_RUNTIME_DIR` (e.g. `/var/lib/onassis`) so the database, logs,
backups and exports live outside the Git checkout. Application code is then
effectively read-only, and a deploy/rollback never touches business data.

## Git bundles are for emergencies only

Git bundles are **not** part of the normal release process. The platform checks
GitHub, downloads, validates and deploys on its own. A bundle is only a manual
disaster-recovery mechanism for when GitHub or the network is unavailable — apply
one by hand, then return to the Operations Centre flow.
