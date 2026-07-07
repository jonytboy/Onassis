"""Tests for the Deployment Service (Sprint 40.1) — offline, fake Git runner."""

from __future__ import annotations

import pytest

from onassis.deployment import DeploymentService


class FakeGit:
    """A scriptable stand-in for the git command runner.

    Tracks a local HEAD and a remote tip so deploy/reset/rollback can be
    observed without touching a real repository. ``fail`` injects failures for
    named subcommands (e.g. {'fetch'} or {'reset'})."""

    def __init__(self, *, head="a1a1a1a1a1a1", remote="b2b2b2b2b2b2", branch="main",
                 clean=True, behind=0, notes=None, fail=None, deps_changed=False,
                 diff_files=None):
        self.head = head
        self.remote = remote
        self.branch = branch
        self.clean = clean
        self.behind = behind
        self.notes = notes or []
        self.fail = set(fail or ())
        self.deps_changed = deps_changed
        # Files reported by `git diff --name-only` (for preview/deps detection).
        self.diff_files = list(diff_files) if diff_files is not None else (
            ["requirements.txt"] if deps_changed else [])
        self.calls: list[list] = []

    def __call__(self, cmd, timeout=120):
        self.calls.append(cmd)
        if cmd[0] != "git":            # pip / systemctl etc. — succeed quietly
            return (0, "", "")
        args = cmd[3:]                 # strip ['git','-C',root]
        sub = args[0]
        if sub in self.fail:
            return (1, "", f"{sub} failed")
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return (0, "true", "")
        if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return (0, self.branch, "")
        if args[:2] == ["rev-parse", "--short"]:
            ref = args[2]
            return (0, (self.remote if ref.startswith("origin/") else self.head)[:8], "")
        if sub == "rev-parse":                 # rev-parse HEAD | origin/<branch> | <commit>
            ref = args[1]
            return (0, self.remote if ref.startswith("origin/") else self.head, "")
        if sub == "status":
            return (0, "" if self.clean else " M onassis/x.py", "")
        if sub == "fetch":
            return (0, "", "")
        if sub == "rev-list":
            return (0, f"0\t{self.behind}", "")
        if sub == "log":
            return (0, "\n".join(f"{n['sha']}\x1f{n['subject']}" for n in self.notes), "")
        if sub == "diff":
            return (0, "\n".join(self.diff_files), "")
        if sub == "reset":                     # reset --hard <target>
            target = args[2]
            self.head = self.remote if target.startswith("origin/") else target
            self.clean = True
            return (0, "", "")
        return (0, "", "")


@pytest.fixture
def svc(config, db, tmp_path):
    s = DeploymentService(config, db, runner=FakeGit(branch="main"),
                          root=tmp_path)  # root only used for git -C arg + disk check
    s.branch = "main"
    s._run.branch = "main"  # keep FakeGit branch in sync
    s.backup_dir = tmp_path / "backups"
    return s


def _fresh(config, db, tmp_path, **git):
    g = FakeGit(**git)
    s = DeploymentService(config, db, runner=g, root=tmp_path)
    s.branch = g.branch
    s.backup_dir = tmp_path / "backups"
    return s, g


# --- Read models -----------------------------------------------------

def test_check_updates_reports_behind(config, db, tmp_path):
    s, g = _fresh(config, db, tmp_path, behind=2,
                  notes=[{"sha": "c1", "subject": "Fix"}, {"sha": "c2", "subject": "Feature"}])
    r = s.check_updates()
    assert r["update_available"] is True
    assert r["behind"] == 2
    assert [n["subject"] for n in r["release_notes"]] == ["Fix", "Feature"]


def test_check_updates_up_to_date(config, db, tmp_path):
    s, _ = _fresh(config, db, tmp_path, behind=0)
    assert s.check_updates()["update_available"] is False


# --- Validation (Objective 10) ---------------------------------------

def test_validate_passes_on_clean_correct_branch(svc):
    v = svc.validate()
    assert v["ok"] is True
    names = {c["name"]: c["ok"] for c in v["checks"]}
    assert names["Correct branch"] and names["No modified tracked files"]
    assert names["Database accessible"] and names["Backup location writable"]


def test_validate_fails_on_dirty_tree(config, db, tmp_path):
    s, _ = _fresh(config, db, tmp_path, clean=False)
    v = s.validate()
    assert v["ok"] is False
    assert any(c["name"] == "No modified tracked files" and not c["ok"] for c in v["checks"])


def test_validate_allow_dirty_downgrades(config, db, tmp_path):
    s, _ = _fresh(config, db, tmp_path, clean=False)
    assert s.validate(allow_dirty=True)["ok"] is True


# --- One-click deploy (Objective 6) ----------------------------------

def test_deploy_success_records_history(config, db, tmp_path):
    s, g = _fresh(config, db, tmp_path, head="old000000000", remote="new111111111", behind=1)
    result = s.deploy(operator="jony", notes="ship it")
    assert result["ok"] and result["status"] == "success"
    assert result["from_commit"] == "old000000000"[:12]
    assert result["to_commit"].startswith("new1")
    assert g.head == "new111111111"                       # tree moved forward
    steps = {st["step"]: st["status"] for st in result["steps"]}
    assert steps["Fetch latest changes"] == "ok"
    assert steps["Deploy application"] == "ok"
    assert steps["Health check"] == "ok"
    assert steps["Confirm success"] == "ok"
    hist = s.history()
    assert hist[0]["status"] == "success" and hist[0]["operator"] == "jony"


def test_deploy_refuses_on_validation_failure(config, db, tmp_path):
    s, g = _fresh(config, db, tmp_path, clean=False, behind=1)  # dirty tree
    result = s.deploy()
    assert result["ok"] is False and result["status"] == "failed"
    assert "validation failed" in result["error"]
    # It never reset the tree — nothing was deployed.
    assert not any(c[3:4] == ["reset"] for c in g.calls if c[0] == "git")
    assert g.head == "a1a1a1a1a1a1"


def test_deploy_auto_rollback_on_health_failure(config, db, tmp_path):
    bad_health = lambda: {"ok": False, "detail": "db down"}  # noqa: E731
    g = FakeGit(head="old000000000", remote="new111111111", behind=1)
    s = DeploymentService(config, db, runner=g, root=tmp_path, health_check=bad_health)
    s.branch = "main"; s.backup_dir = tmp_path / "backups"
    result = s.deploy()
    assert result["status"] == "failed" and result["rollback_performed"] is True
    assert "health check failed" in result["error"]
    # Auto-rollback restored the previous commit.
    assert g.head == "old000000000"
    steps = {st["step"]: st["status"] for st in result["steps"]}
    assert steps["Automatic rollback"] == "ok"


def test_deploy_installs_deps_when_requirements_change(config, db, tmp_path):
    g = FakeGit(head="old000000000", remote="new111111111", behind=1, deps_changed=True)
    s = DeploymentService(config, db, runner=g, root=tmp_path)
    s.branch = "main"; s.backup_dir = tmp_path / "backups"
    result = s.deploy()
    steps = {st["step"]: st for st in result["steps"]}
    assert steps["Install dependencies"]["status"] == "ok"
    assert "unchanged" not in steps["Install dependencies"]["detail"]


# --- Manual rollback (Objective 7) -----------------------------------

def test_rollback_to_previous_deployment(config, db, tmp_path):
    s, g = _fresh(config, db, tmp_path, head="old000000000", remote="new111111111", behind=1)
    s.deploy(operator="a")                    # records from_commit=old, head->new
    r = s.rollback(operator="b")
    assert r["ok"] and r["status"] == "rolled_back"
    assert g.head == "old000000000"           # back to the recorded previous version
    assert s.history()[0]["action"] == "rollback"


def test_rollback_without_history_is_reported(config, db, tmp_path):
    s, _ = _fresh(config, db, tmp_path)
    r = s.rollback()
    assert r["ok"] is False and "previous version" in r["error"].lower()


# --- Health dashboard (Objective 9) ----------------------------------

def test_health_dashboard_has_all_fields(svc):
    h = svc.health()
    for k in ("application", "api", "database", "disk", "memory", "cpu", "service",
              "git", "repository_clean", "pending_updates", "python_version",
              "operating_system", "restart_required"):
        assert k in h
    assert h["database"]["ok"] in (True, False)


def test_status_combines_version_git_health(svc):
    s = svc.status(check_remote=False)
    assert "current_version" in s and "git" in s and "health" in s
    assert "updates" in s and "last_deployment" in s
    assert "environment" in s and "versions" in s


# --- Sprint 40.2: safe ladder, preview, environment, lock -----------

def test_environment_awareness(svc):
    e = svc.environment()
    assert e["branch"] == "main" and e["git_status"] == "clean"
    assert e["database_version"] == 44
    assert "application_version" in e


def test_download_fetches_but_does_not_apply(config, db, tmp_path):
    s, g = _fresh(config, db, tmp_path, head="old000000000", remote="new111111111", behind=1)
    r = s.download()
    assert r["fetched"] is True and r["behind"] == 1
    assert g.head == "old000000000"       # nothing applied — tree unchanged
    assert not any(c[3:4] == ["reset"] for c in g.calls if c[0] == "git")


def test_preview_reports_migration_and_estimate(config, db, tmp_path):
    s, g = _fresh(config, db, tmp_path, head="a", remote="b", behind=1,
                  diff_files=["onassis/database.py", "requirements.txt", "onassis/api.py"])
    p = s.preview()
    assert p["files_changed"] == 3
    assert p["database_migration"] is True and p["requirements_changed"] is True
    assert p["restart_required"] is True and "minute" in p["estimated_deployment"]


def test_simulate_changes_nothing(config, db, tmp_path):
    s, g = _fresh(config, db, tmp_path, behind=1, diff_files=["onassis/api.py"])
    sim = s.simulate()
    assert sim["ok"] is True and sim["would_deploy"] is True
    assert "preview" in sim and "validate" in sim
    assert not any(c[3:4] == ["reset"] for c in g.calls if c[0] == "git")


def test_validate_gives_self_healing_message_when_dirty(config, db, tmp_path):
    s, _ = _fresh(config, db, tmp_path, clean=False)
    v = s.validate()
    assert v["ok"] is False
    assert "local code modifications" in v["remediation"]
    assert "stash or commit" in v["remediation"]


def test_deployment_lock_refuses_concurrent_deploy(svc):
    # Hold the lock as if a deploy were mid-flight.
    svc._begin_run("jony")
    assert svc._deploy_lock.acquire(blocking=False)
    try:
        r = svc.deploy(operator="someone-else")
        assert r["status"] == "busy" and r["ok"] is False
        assert r["operator"] == "jony"
    finally:
        svc._deploy_lock.release()


def test_deploy_records_live_progress(config, db, tmp_path):
    s, _ = _fresh(config, db, tmp_path, head="old000000000", remote="new111111111", behind=1)
    result = s.deploy(operator="jony")
    run = s.deploy_status()
    assert run["status"] == "success" and run["operator"] == "jony"
    assert run["log"] and any("Confirm success" in e["line"] for e in run["log"])
    assert result["ok"] is True


def test_available_versions_lists_recorded_commits(config, db, tmp_path):
    s, _ = _fresh(config, db, tmp_path, head="old000000000", remote="new111111111", behind=1)
    s.deploy(operator="a")
    versions = s.available_versions()
    assert versions and versions[0]["commit"]
    assert all("commit" in v for v in versions)


# --- Sprint 40.3: privilege-separated restart bridge ----------------

def test_restart_bridge_writes_request_not_shell(config, db, tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("ONASSIS_RESTART_SPOOL", str(spool))
    g = FakeGit()
    s = DeploymentService(config, db, runner=g, root=tmp_path)
    r = s.restart()
    assert r["pending"] is True and "systemd bridge" in r["detail"]
    # The web app only dropped a request file — it never called systemctl.
    assert (spool / "restart.request").exists()
    assert not any("systemctl" in " ".join(c) for c in g.calls)


def test_restart_bridge_result_clears_marker(config, db, tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("ONASSIS_RESTART_SPOOL", str(spool))
    s = DeploymentService(config, db, runner=FakeGit(), root=tmp_path)
    token = s._bridge.request("deploy")
    # Simulate the privileged worker: consume request, write a healthy result.
    (spool / "restart.request").unlink()
    (spool / "restart.result").write_text(
        f"token={token}\nok=true\nat=2026-07-06T10:00:00Z\n", encoding="utf-8")
    state = s.restart_state()
    assert state["bridge"] is True and state["pending"] is False
    assert state["last"]["ok"] is True


def test_environment_exposes_platform_and_schema(svc):
    e = svc.environment()
    assert e["platform_version"] == svc.version()
    assert e["schema_version"] == 44 and e["database_version"] == 44


def test_deploy_restart_uses_bridge_when_configured(config, db, tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("ONASSIS_RESTART_SPOOL", str(spool))
    g = FakeGit(head="old000000000", remote="new111111111", behind=1)
    s = DeploymentService(config, db, runner=g, root=tmp_path)
    s.backup_dir = tmp_path / "backups"
    result = s.deploy(operator="jony")
    assert result["ok"] is True and result["status"] == "success"
    # Restart went through the bridge — a request file exists, no systemctl call.
    assert (spool / "restart.request").exists()
    assert not any("systemctl" in " ".join(c) for c in g.calls)
