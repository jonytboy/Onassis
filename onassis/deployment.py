"""The Deployment Service — ONASSIS manages its own software lifecycle.

Guiding principle:

    Routine operation of ONASSIS, including software updates, should be possible
    entirely from the Operations Centre. Terminal access should be reserved for
    exceptional maintenance and disaster recovery, not normal business operation.

Software deployment is a *privileged* operation, so the flow is deliberately
staged and safe rather than one-click:

    Check  → contact GitHub, report what's available (changes nothing)
    Download → fetch Git only (still nothing applied)
    Validate → simulate the deploy (pre-flight checks + release preview)
    Deploy  → the privileged step: backup → migrate → apply → restart → health,
              with an automatic snapshot before, a deployment lock so only one
              runs, a live progress log, and automatic rollback on failure.

The service exposes a **small, well-defined API** — check / download / preview /
validate / deploy / rollback / health / backup — and never runs arbitrary shell
commands on behalf of the web app. It is deterministic and dependency-injected
(Git runner, restart hook, health check all replaceable), so the whole flow runs
offline in tests and is safe on a live server. Every deploy/rollback is recorded
to the ``deployments`` table for a complete, auditable history.
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from onassis.config import ROOT_DIR, Config, runtime_root
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

CommandRunner = Callable[[list], tuple]

# Free disk required before a deploy is allowed (bytes).
MIN_FREE_BYTES = 500 * 1024 * 1024
RESTART_MARKER = ".onassis_restart_required"


def _default_runner(cmd: list, timeout: int = 120) -> tuple:
    """Run a command, returning ``(returncode, stdout, stderr)``."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as exc:  # missing binary etc.
        return 127, "", str(exc)


class RestartBridge:
    """Privilege-separated service restart.

    The (unprivileged) web application must never call ``systemctl``. Instead it
    drops a tiny request file into a spool directory; a **root** systemd
    ``path`` unit notices it and triggers a ``oneshot`` service that performs the
    actual restart and writes a result. The security boundary stays intact — the
    web app can only *ask* for a restart, never execute one.

    The request/result files are plain ``key=value`` so the shell worker can
    parse them without a JSON library.
    """

    def __init__(self, spool_dir: str | Path) -> None:
        self.spool = Path(spool_dir)
        self.request_file = self.spool / "restart.request"
        self.result_file = self.spool / "restart.result"

    def request(self, reason: str = "deploy") -> str:
        self.spool.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        try:
            self.result_file.unlink()          # clear any stale result
        except FileNotFoundError:
            pass
        self.request_file.write_text(
            f"token={token}\nreason={reason}\nrequested_at={_now_iso()}\n",
            encoding="utf-8")
        return token

    def read_result(self, token: str | None = None) -> dict | None:
        if not self.result_file.exists():
            return None
        data = _parse_kv(self.result_file.read_text(encoding="utf-8"))
        if token is not None and data.get("token") != token:
            return None
        return {"ok": data.get("ok") == "true", "at": data.get("at"),
                "token": data.get("token")}

    @property
    def pending(self) -> bool:
        return self.request_file.exists()


class DeploymentService:
    """Owns the application's deploy / rollback / health lifecycle."""

    def __init__(
        self, config: Config, db: Database, *, root: Path | str = ROOT_DIR,
        runner: CommandRunner | None = None,
        restart: Callable[[], dict] | None = None,
        health_check: Callable[[], dict] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.root = Path(root)
        self._run = runner or _default_runner
        self._restart_hook = restart
        self._health_hook = health_check
        self.backup_dir = runtime_root() / "backups"
        self.branch = os.environ.get("ONASSIS_BRANCH") or self._current_branch_raw() or "main"
        # Deployment lock + live run state (only one deploy at a time).
        self._deploy_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._deploy_run: dict | None = None
        # Privilege-separated restart bridge (systemd), when a spool is configured.
        spool = os.environ.get("ONASSIS_RESTART_SPOOL")
        self._bridge = RestartBridge(spool) if spool else None

    # --- Git primitives ---------------------------------------------

    def _git(self, *args: str, timeout: int = 120) -> tuple:
        return self._run(["git", "-C", str(self.root), *args], timeout)

    def _current_branch_raw(self) -> str:
        rc, out, _ = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return out if rc == 0 else ""

    def current_commit(self, short: bool = False) -> str:
        args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
        rc, out, _ = self._git(*args)
        return out if rc == 0 else ""

    def is_clean(self) -> bool:
        rc, out, _ = self._git("status", "--porcelain")
        return rc == 0 and out == ""

    def dirty_files(self) -> list:
        rc, out, _ = self._git("status", "--porcelain")
        return [ln[3:] for ln in out.splitlines()] if (rc == 0 and out) else []

    def _fetch(self) -> tuple:
        return self._git("fetch", "--prune", "origin", self.branch, timeout=180)

    def _ahead_behind(self) -> tuple:
        """(ahead, behind) vs origin/<branch>; (0, 0) if unknown."""
        rc, out, _ = self._git("rev-list", "--left-right", "--count",
                               f"HEAD...origin/{self.branch}")
        if rc != 0 or not out:
            return 0, 0
        try:
            ahead, behind = out.split()[:2]
            return int(ahead), int(behind)
        except (ValueError, IndexError):
            return 0, 0

    # --- Read models -------------------------------------------------

    def version(self) -> str:
        return getattr(self.config, "version", "0.0.0")

    def git_status(self) -> dict:
        branch = self._current_branch_raw()
        return {
            "commit": self.current_commit(),
            "short_commit": self.current_commit(short=True),
            "branch": branch,
            "expected_branch": self.branch,
            "on_correct_branch": branch == self.branch,
            "clean": self.is_clean(),
            "dirty_files": self.dirty_files(),
        }

    def check_updates(self, fetch: bool = True) -> dict:
        """Fetch and report whether newer code exists on origin/<branch>."""
        fetched = False
        if fetch:
            rc, _, err = self._fetch()
            fetched = rc == 0
            if rc != 0:
                log.warning("check_updates: fetch failed: %s", err)
        ahead, behind = self._ahead_behind()
        notes: list = []
        if behind:
            rc, out, _ = self._git("log", "--no-merges", "--pretty=format:%h\x1f%s",
                                   f"HEAD..origin/{self.branch}")
            if rc == 0 and out:
                for line in out.splitlines():
                    sha, _, subject = line.partition("\x1f")
                    notes.append({"sha": sha, "subject": subject})
        rc, latest, _ = self._git("rev-parse", "--short", f"origin/{self.branch}")
        return {
            "fetched": fetched,
            "update_available": behind > 0,
            "behind": behind,
            "ahead": ahead,
            "branch": self.branch,
            "current_commit": self.current_commit(short=True),
            "latest_commit": latest if rc == 0 else "",
            "release_notes": notes,
        }

    # --- Repository validation (Objective 10) -----------------------

    def validate(self, *, allow_dirty: bool = False) -> dict:
        """Pre-deploy safety checks. Deployment refuses to continue unless every
        critical check passes."""
        checks: list = []

        def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail,
                           "critical": critical})

        rc, _, _ = self._git("rev-parse", "--is-inside-work-tree")
        add("Git repository", rc == 0, str(self.root))

        gs = self.git_status()
        add("Correct branch", gs["on_correct_branch"],
            f"on {gs['branch']} (expected {self.branch})")
        add("No modified tracked files", gs["clean"] or allow_dirty,
            "clean" if gs["clean"] else f"{len(gs['dirty_files'])} modified",
            critical=not allow_dirty)

        missing = [m for m in ("fastapi", "yaml", "jinja2") if _missing(m)]
        add("Dependencies present", not missing,
            "all present" if not missing else f"missing: {', '.join(missing)}")

        try:
            db_ok = self.db.integrity_ok()
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            log.warning("validate: db check failed: %s", exc)
        add("Database accessible", db_ok, str(self.config.db_path))

        cfg_ok = bool(getattr(self.config, "version", None)) and bool(self.config.db_path)
        add("Configuration valid", cfg_ok, "config loaded")

        try:
            free = shutil.disk_usage(self.root).free
        except Exception:
            free = 0
        add("Sufficient disk space", free >= MIN_FREE_BYTES,
            f"{free // (1024**2)} MB free")

        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            probe = self.backup_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            backup_ok = True
        except Exception as exc:  # noqa: BLE001
            backup_ok = False
            log.warning("validate: backup dir not writable: %s", exc)
        add("Backup location writable", backup_ok, str(self.backup_dir))

        ok = all(c["ok"] for c in checks if c["critical"])
        # Repository self-healing: turn a raw Git error into a clear operational
        # message the operator can act on, rather than just "failed".
        remediation = ""
        if not gs["clean"] and not allow_dirty:
            files = ", ".join(gs["dirty_files"][:5]) or "tracked files"
            remediation = (
                "Deployment cannot continue because the repository has local code "
                f"modifications ({files}). Restore, stash or commit the changes on "
                "the server before retrying — production code must match Git.")
        return {"ok": ok, "checks": checks, "remediation": remediation}

    # --- Backup (Python, no shell) ----------------------------------

    def _backup(self) -> dict:
        """Copy the database + secrets + config into a timestamped snapshot
        under the runtime backup dir. Best-effort per item; a copy failure is
        fatal (the caller aborts the deploy)."""
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        dest = self.backup_dir / stamp
        dest.mkdir(parents=True, exist_ok=True)
        copied: list = []
        items = [Path(self.config.db_path), self.root / ".env", self.root / "config.yaml"]
        for src in items:
            if src.exists():
                target = dest / src.name
                if src.is_dir():
                    shutil.copytree(src, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, target)
                copied.append(src.name)
        (dest / "MANIFEST.txt").write_text(
            f"backup_timestamp: {stamp}\ncommit: {self.current_commit(short=True)}\n"
            f"branch: {self._current_branch_raw()}\ncontents: {', '.join(copied)}\n",
            encoding="utf-8")
        return {"ok": True, "path": str(dest), "items": copied}

    def backup(self) -> dict:
        """Take an on-demand backup snapshot (operator action)."""
        return self._backup()

    def restart(self) -> dict:
        """Restart the service (operator action; delegated to the service manager)."""
        return self._restart()

    def last_backup(self) -> dict | None:
        if not self.backup_dir.exists():
            return None
        snaps = sorted((p for p in self.backup_dir.iterdir() if p.is_dir()),
                       key=lambda p: p.name, reverse=True)
        if not snaps:
            return None
        return {"path": str(snaps[0]), "at": snaps[0].name}

    # --- Deploy / rollback hooks ------------------------------------

    def _restart(self) -> dict:
        if self._restart_hook is not None:
            return self._restart_hook()
        # Preferred: privilege-separated systemd bridge. The web app only *asks*;
        # a root oneshot performs the restart (which may terminate this process),
        # so we fire-and-forget rather than block on a result we might not see.
        if self._bridge is not None:
            token = self._bridge.request("deploy")
            self._set_restart_required(True)
            return {"ok": True, "restarted": False, "pending": True, "token": token,
                    "detail": "restart requested via systemd bridge"}
        # Fallback (simple single-host setups): opt-in via ONASSIS_ALLOW_RESTART.
        # Try a non-interactive systemctl first; if that isn't possible, re-exec
        # this very process so the button still restarts the running code.
        if os.environ.get("ONASSIS_ALLOW_RESTART"):
            service = self._service_name()
            if shutil.which("systemctl"):
                # ``sudo -n`` never prompts — it fails fast if we lack rights,
                # so we can fall through to a self-restart instead of hanging.
                rc, _, err = self._run(["sudo", "-n", "systemctl", "restart", service], 60)
                if rc == 0:
                    self._set_restart_required(False)
                    return {"ok": True, "restarted": True,
                            "detail": f"{service}: restarted via systemctl"}
            # No systemctl / no sudo rights → re-exec ourselves.
            return self._self_reexec()
        # Dev / no restart mechanism: defer and flag it.
        self._set_restart_required(True)
        return {"ok": True, "restarted": False, "pending": True,
                "detail": "restart deferred to service manager"}

    def _self_reexec(self) -> dict:
        """Restart by replacing this process image with a fresh one (no root,
        no service manager needed). Scheduled on a short timer so the HTTP
        response returns before the process is replaced. Any process manager
        with Restart= will also recover us if the re-exec ever fails to bind."""
        argv = [sys.executable, *sys.argv]

        def _go() -> None:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            try:
                os.execv(sys.executable, argv)
            except Exception:  # last resort — exit so a process manager restarts us
                os._exit(3)

        threading.Timer(1.0, _go).start()
        self._set_restart_required(False)
        return {"ok": True, "restarted": True, "self_reexec": True,
                "detail": "restarting in-process (re-exec) — reconnect in a few seconds"}

    def _health(self) -> dict:
        if self._health_hook is not None:
            return self._health_hook()
        try:
            db_ok = self.db.integrity_ok()
        except Exception:
            db_ok = False
        return {"ok": bool(db_ok), "detail": "database integrity ok" if db_ok
                else "database integrity FAILED"}

    def _service_name(self) -> str:
        sec = self.config.security if isinstance(self.config.security, dict) else {}
        return sec.get("service", "onassis")

    def _deps_changed(self, a: str, b: str) -> bool:
        rc, out, _ = self._git("diff", "--name-only", f"{a}..{b}")
        return rc == 0 and any(ln.strip() == "requirements.txt" for ln in out.splitlines())

    def _install_deps(self) -> dict:
        pip = self._pip_bin()
        if not pip:
            return {"ok": True, "detail": "no pip found — skipped", "ran": False}
        rc, _, err = self._run([*pip, "install", "-r", str(self.root / "requirements.txt")], 600)
        return {"ok": rc == 0, "detail": "dependencies updated" if rc == 0 else err,
                "ran": True}

    def _pip_bin(self) -> list | None:
        for cand in (self.root / "venv/bin/pip", self.root / ".venv/bin/pip"):
            if cand.exists():
                return [str(cand)]
        found = shutil.which("pip3") or shutil.which("pip")
        return [found] if found else [sys.executable, "-m", "pip"]

    def _migrate(self) -> dict:
        # The schema self-migrates (idempotent CREATE/ALTER) on open.
        try:
            Database(self.config.db_path)
            return {"ok": True, "detail": "schema migrations applied"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}

    def _set_restart_required(self, required: bool) -> None:
        marker = self.root / RESTART_MARKER
        try:
            if required:
                marker.write_text("1", encoding="utf-8")
            elif marker.exists():
                marker.unlink()
        except Exception:  # noqa: BLE001 — advisory only
            pass

    def restart_required(self) -> bool:
        return (self.root / RESTART_MARKER).exists()

    def _reset_to(self, commit: str) -> tuple:
        """Hard-reset the working tree to a commit — guarantees production code
        matches Git exactly (no drift)."""
        return self._git("reset", "--hard", commit)

    # --- Environment awareness --------------------------------------

    def environment(self) -> dict:
        """The always-on environment banner: which env / branch / commit is
        running, whether the tree is clean, and the DB vs app version."""
        gs = self.git_status()
        try:
            db_version = self.db.schema_version()
        except Exception:
            db_version = None
        return {
            "environment": getattr(self.config, "environment", "development"),
            "branch": gs["branch"],
            "expected_branch": self.branch,
            "commit": gs["short_commit"],
            "git_status": "clean" if gs["clean"] else "modified",
            "clean": gs["clean"],
            "database_version": db_version,       # schema version
            "schema_version": db_version,
            # The sprint-based platform version is more operationally meaningful
            # than a semantic version while we're moving fast.
            "platform_version": self.version(),
            "application_version": self.version(),
        }

    # --- Safe Mode ladder: Check → Download → Validate → Deploy ------

    def download(self) -> dict:
        """Safe Mode step 2 — fetch from origin ONLY. Nothing is applied to the
        working tree; this just makes the latest code available locally."""
        rc, _, err = self._fetch()
        ahead, behind = self._ahead_behind()
        return {"ok": rc == 0, "fetched": rc == 0, "behind": behind, "ahead": ahead,
                "branch": self.branch, "detail": "fetched origin/" + self.branch
                if rc == 0 else (err or "fetch failed")}

    def preview(self) -> dict:
        """Release Preview — what a deploy WOULD do (read-only). Reports files
        changed, whether migrations/requirements are involved, restart need, and
        an estimated duration, so the operator has confidence before deploying."""
        target = self._resolve(f"origin/{self.branch}")
        changed: list = []
        if target:
            rc, out, _ = self._git("diff", "--name-only", f"HEAD..{target}")
            if rc == 0 and out:
                changed = [ln.strip() for ln in out.splitlines() if ln.strip()]
        migration = any(f == "onassis/database.py" for f in changed)
        requirements = any(f == "requirements.txt" for f in changed)
        restart = len(changed) > 0
        est = 30 + (90 if requirements else 0) + (15 if migration else 0) + (
            10 if restart else 0)
        return {
            "files_changed": len(changed),
            "changed_files": changed[:50],
            "database_migration": migration,
            "requirements_changed": requirements,
            "restart_required": restart,
            "estimated_seconds": est,
            "estimated_deployment": _human_duration(est),
            "update_available": len(changed) > 0,
        }

    def simulate(self) -> dict:
        """Safe Mode step 3 — 'Validate': dry-run the deploy. Runs every
        pre-flight check and the release preview WITHOUT changing anything."""
        val = self.validate()
        return {"ok": val["ok"], "validate": val, "preview": self.preview(),
                "would_deploy": val["ok"]}

    def available_versions(self) -> list:
        """Versions available to roll back to — drawn from the deployment
        history (most recent first), each a real recorded commit."""
        seen: set = set()
        out: list = []
        for d in self.db.list_deployments(50):
            commit = d.get("to_commit") or d.get("from_commit")
            if not commit or commit in seen:
                continue
            seen.add(commit)
            out.append({"version": d.get("version"), "commit": commit,
                        "at": d.get("created_at"), "status": d.get("status"),
                        "action": d.get("action")})
        return out

    # --- Deployment lock + live progress ----------------------------

    @property
    def is_deploying(self) -> bool:
        with self._state_lock:
            return bool(self._deploy_run and self._deploy_run.get("status") == "running")

    def deploy_status(self) -> dict | None:
        """The current or most recent deployment run (live progress + log)."""
        with self._state_lock:
            return dict(self._deploy_run) if self._deploy_run else None

    def _begin_run(self, operator: str) -> None:
        with self._state_lock:
            self._deploy_run = {
                "status": "running", "operator": operator,
                "started_at": _now_iso(), "current_stage": "Starting",
                "steps": [], "log": [], "result": None}

    def _emit(self, stage: str, status: str = "info", detail: str = "") -> None:
        with self._state_lock:
            if not self._deploy_run:
                return
            self._deploy_run["current_stage"] = stage
            self._deploy_run["log"].append(
                {"ts": _now_hms(), "line": stage + (f" — {detail}" if detail else ""),
                 "level": "error" if status == "failed" else "info"})

    def _end_run(self, result: dict) -> None:
        with self._state_lock:
            if self._deploy_run:
                self._deploy_run["status"] = result.get("status", "failed")
                self._deploy_run["current_stage"] = "Done"
                self._deploy_run["result"] = result

    # --- Deploy (privileged, locked, live) --------------------------

    def deploy(self, operator: str = "operator", notes: str | None = None,
               allow_dirty: bool = False) -> dict:
        # Deployment lock — only one deploy at a time (Objective 4).
        if not self._deploy_lock.acquire(blocking=False):
            active = self.deploy_status() or {}
            return {"ok": False, "status": "busy",
                    "error": "Deployment already running.",
                    "started_at": active.get("started_at"),
                    "operator": active.get("operator"),
                    "current_stage": active.get("current_stage")}
        try:
            self._begin_run(operator)
            return self._deploy_locked(operator, notes, allow_dirty)
        finally:
            self._deploy_lock.release()

    def _deploy_locked(self, operator: str, notes: str | None,
                       allow_dirty: bool) -> dict:
        t0 = time.monotonic()
        steps: list = []
        from_commit = self.current_commit()

        def step(name: str, ok: bool, detail: str = "") -> bool:
            steps.append({"step": name, "status": "ok" if ok else "failed",
                          "detail": detail})
            self._emit(name, "ok" if ok else "failed", detail)  # live progress
            return ok

        def finish(status: str, error: str | None = None, to_commit: str | None = None,
                   rolled_back: bool = False) -> dict:
            dur = round(time.monotonic() - t0, 2)
            rec = {"action": "deploy", "version": self.version(),
                   "from_commit": from_commit[:12], "to_commit": (to_commit or "")[:12],
                   "branch": self.branch, "operator": operator, "duration_seconds": dur,
                   "status": status, "rollback_performed": rolled_back, "steps": steps,
                   "notes": notes, "error": error}
            rec["id"] = self.db.insert_deployment(rec)
            log.info("Deploy %s by %s (%s -> %s) in %ss", status, operator,
                     from_commit[:8], (to_commit or from_commit)[:8], dur)
            result = {**rec, "ok": status == "success"}
            self._end_run(result)
            return result

        # 1. Fetch — nothing has changed yet, so a failure just aborts cleanly.
        rc, _, err = self._fetch()
        if not step("Fetch latest changes", rc == 0, err or "fetched origin/" + self.branch):
            return finish("failed", f"git fetch failed: {err}")

        # 2. Validate — refuse to deploy a repo that fails safety checks.
        val = self.validate(allow_dirty=allow_dirty)
        failed = [c["name"] for c in val["checks"] if c["critical"] and not c["ok"]]
        if not step("Validate repository", val["ok"],
                    "all checks passed" if val["ok"] else "failed: " + ", ".join(failed)):
            return finish("failed", "repository validation failed: " + ", ".join(failed))

        # 3+4. Backup configuration + database (abort if it fails).
        try:
            bkp = self._backup()
            step("Backup configuration + database", True,
                 f"{bkp['path']} ({', '.join(bkp['items'])})")
        except Exception as exc:  # noqa: BLE001
            step("Backup configuration + database", False, str(exc))
            return finish("failed", f"backup failed: {exc}")

        target_commit = self._resolve(f"origin/{self.branch}")
        deps_needed = self._deps_changed(from_commit, target_commit) if target_commit else False

        # --- Everything below can leave the tree changed → rollback on failure.
        # 7. Deploy application (hard reset to origin → no code drift, ever).
        rc, _, err = self._reset_to(f"origin/{self.branch}")
        if not step("Deploy application", rc == 0, err or f"reset to origin/{self.branch}"):
            return finish("failed", f"git reset failed: {err}", from_commit)
        to_commit = self.current_commit()

        # 5. Install dependencies (only if requirements.txt changed).
        if deps_needed:
            dep = self._install_deps()
            if not step("Install dependencies", dep["ok"], dep["detail"]):
                return self._rollback_after_failure(from_commit, steps, finish,
                                                    f"dependency install failed: {dep['detail']}")
        else:
            step("Install dependencies", True, "requirements.txt unchanged — skipped")

        # 6. Run migrations.
        mig = self._migrate()
        if not step("Run migrations", mig["ok"], mig["detail"]):
            return self._rollback_after_failure(from_commit, steps, finish,
                                                f"migration failed: {mig['detail']}")

        # 8. Restart services.
        rst = self._restart()
        if not step("Restart services", rst["ok"], rst["detail"]):
            return self._rollback_after_failure(from_commit, steps, finish,
                                                f"restart failed: {rst['detail']}")

        # 9. Health check.
        hc = self._health()
        if not step("Health check", hc["ok"], hc["detail"]):
            return self._rollback_after_failure(from_commit, steps, finish,
                                                f"health check failed: {hc['detail']}")

        # 10. Confirm success.
        step("Confirm success", True, f"now at {to_commit[:8]}")
        return finish("success", to_commit=to_commit)

    def _resolve(self, ref: str) -> str:
        rc, out, _ = self._git("rev-parse", ref)
        return out if rc == 0 else ""

    def _rollback_after_failure(self, from_commit: str, steps: list,
                                finish: Callable, error: str) -> dict:
        """Automatic rollback (Objective 7): restore the previous version,
        restart, and report clearly."""
        rc, _, err = self._reset_to(from_commit)
        steps.append({"step": "Automatic rollback",
                      "status": "ok" if rc == 0 else "failed",
                      "detail": f"restored {from_commit[:8]}" if rc == 0 else err})
        rst = self._restart()
        steps.append({"step": "Restart after rollback",
                      "status": "ok" if rst["ok"] else "failed", "detail": rst["detail"]})
        return finish("failed", error=error, to_commit=from_commit, rolled_back=True)

    # --- Manual rollback --------------------------------------------

    def rollback(self, operator: str = "operator", to_commit: str | None = None,
                 notes: str | None = None) -> dict:
        t0 = time.monotonic()
        steps: list = []
        from_commit = self.current_commit()
        # Target: explicit commit, else the previous successful deploy's origin.
        if not to_commit:
            last = self.db.get_last_deployment(action="deploy")
            to_commit = (last or {}).get("from_commit")
        if not to_commit:
            return {"ok": False, "status": "failed",
                    "error": "No previous version recorded to roll back to."}

        rc, _, err = self._reset_to(to_commit)
        steps.append({"step": "Restore previous version",
                      "status": "ok" if rc == 0 else "failed",
                      "detail": f"reset to {to_commit[:8]}" if rc == 0 else err})
        if rc != 0:
            rec = {"action": "rollback", "version": self.version(),
                   "from_commit": from_commit[:12], "to_commit": to_commit[:12],
                   "branch": self.branch, "operator": operator,
                   "duration_seconds": round(time.monotonic() - t0, 2),
                   "status": "failed", "rollback_performed": True, "steps": steps,
                   "notes": notes, "error": err}
            rec["id"] = self.db.insert_deployment(rec)
            return {**rec, "ok": False}

        self._migrate()
        rst = self._restart()
        steps.append({"step": "Restart services", "status": "ok" if rst["ok"] else "failed",
                      "detail": rst["detail"]})
        hc = self._health()
        steps.append({"step": "Health check", "status": "ok" if hc["ok"] else "failed",
                      "detail": hc["detail"]})
        status = "rolled_back" if (rst["ok"] and hc["ok"]) else "failed"
        rec = {"action": "rollback", "version": self.version(),
               "from_commit": from_commit[:12], "to_commit": to_commit[:12],
               "branch": self.branch, "operator": operator,
               "duration_seconds": round(time.monotonic() - t0, 2),
               "status": status, "rollback_performed": True, "steps": steps,
               "notes": notes, "error": None if status == "rolled_back" else "post-rollback checks failed"}
        rec["id"] = self.db.insert_deployment(rec)
        log.info("Rollback %s by %s to %s", status, operator, to_commit[:8])
        return {**rec, "ok": status == "rolled_back"}

    # --- History + Health -------------------------------------------

    def history(self, limit: int = 25) -> list:
        return self.db.list_deployments(limit)

    def health(self) -> dict:
        """The expanded Health dashboard (Objective 9)."""
        gs = self.git_status()
        try:
            db_ok = self.db.integrity_ok()
        except Exception:
            db_ok = False
        try:
            total, used, free = shutil.disk_usage(self.root)
            disk = {"percent": round(used / total * 100), "free_gb": round(free / 1024**3, 1)}
        except Exception:
            disk = {"percent": None, "free_gb": None}
        mem, cpu = _memory(), _cpu()
        pending = self._ahead_behind()[1]
        return {
            "application": {"ok": True, "detail": "running"},
            "api": {"ok": True, "detail": "responding"},
            "database": {"ok": db_ok, "detail": "integrity ok" if db_ok else "integrity FAILED"},
            "disk": disk,
            "memory": mem,
            "cpu": cpu,
            "service": self._service_status(),
            "git": {"commit": gs["short_commit"], "branch": gs["branch"],
                    "on_correct_branch": gs["on_correct_branch"]},
            "repository_clean": gs["clean"],
            "pending_updates": pending,
            "python_version": platform.python_version(),
            "operating_system": f"{platform.system()} {platform.release()}",
            "restart_required": self.restart_required(),
        }

    def _service_status(self) -> dict:
        if not shutil.which("systemctl"):
            return {"ok": None, "detail": "systemctl n/a (dev)"}
        rc, out, err = self._run(["systemctl", "is-active", self._service_name()], 5)
        state = (out or err).strip()
        return {"ok": state == "active", "detail": f"{self._service_name()}: {state or 'unknown'}"}

    # --- Combined status for the Software Updates page ---------------

    def status(self, check_remote: bool = False) -> dict:
        updates = self.check_updates(fetch=check_remote)
        last_deploy = self.db.get_last_deployment(action="deploy")
        return {
            "current_version": self.version(),
            "latest_version": self.version() if not updates["update_available"]
                              else f"{self.version()}+{updates['behind']}",
            "environment": self.environment(),
            "git": self.git_status(),
            "updates": updates,
            "health": self.health(),
            "versions": self.available_versions(),
            "last_deployment": last_deploy,
            "last_backup": self.last_backup(),
            "restart_required": self.restart_required(),
            "restart": self.restart_state(),
            "deploying": self.is_deploying,
            "deploy_run": self.deploy_status(),
        }

    def restart_state(self) -> dict:
        """Whether an automated (privilege-separated) restart is available, and
        the status of the most recent restart request."""
        state = {"bridge": self._bridge is not None,
                 "required": self.restart_required(), "pending": False, "last": None}
        if self._bridge is not None:
            state["pending"] = self._bridge.pending
            state["last"] = self._bridge.read_result()
            # The bridge completed and cleared the request — drop the marker.
            if state["last"] and state["last"].get("ok") and not self._bridge.pending:
                self._set_restart_required(False)
                state["required"] = False
        return state


# --- Module helpers ---------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_hms() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return f"~{seconds} seconds"
    mins = round(seconds / 60)
    return f"~{mins} minute{'s' if mins != 1 else ''}"


def _parse_kv(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _missing(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is None


def _memory() -> dict:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used_pct = round((total - avail) / total * 100) if total else None
        return {"percent": used_pct, "available_mb": avail // 1024}
    except Exception:
        return {"percent": None, "available_mb": None}


def _cpu() -> dict:
    try:
        load1, load5, load15 = os.getloadavg()
        cores = os.cpu_count() or 1
        return {"load1": round(load1, 2), "percent": round(min(load1 / cores, 1.0) * 100),
                "cores": cores}
    except Exception:
        return {"load1": None, "percent": None, "cores": os.cpu_count()}
