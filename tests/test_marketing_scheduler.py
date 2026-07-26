"""Tests for the in-app marketing scheduler (UI-controlled cadence)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from onassis.marketing_scheduler import MarketingScheduler


class _FakeDaily:
    def __init__(self): self.runs = 0
    def run_marketing(self, today=None): self.runs += 1; return {"ok": True}


def _sched(config, db):
    return MarketingScheduler(config, db, daily=_FakeDaily())


def test_disabled_when_hours_zero(config, db):
    db.set_setting("business.marketing_every_hours", 0)
    s = _sched(config, db)
    assert s.status()["enabled"] is False
    assert s._due(datetime.now(timezone.utc)) is False


def test_due_when_never_run(config, db):
    db.set_setting("business.marketing_every_hours", 3)
    s = _sched(config, db)
    assert s._due(datetime.now(timezone.utc)) is True


def test_not_due_until_interval_elapses(config, db):
    db.set_setting("business.marketing_every_hours", 3)
    s = _sched(config, db)
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    s._mark_run(now)
    assert s._due(now + timedelta(hours=2)) is False   # too soon
    assert s._due(now + timedelta(hours=3)) is True     # cadence reached
    st = s.status()
    assert st["enabled"] and st["every_hours"] == 3 and st["next_run"]


def test_tick_fires_run_marketing_when_due_and_marks_it(config, db):
    db.set_setting("business.marketing_every_hours", 1)
    s = _sched(config, db)
    s._tick()
    assert s.daily.runs == 1                     # fired
    assert db.get_setting("system.last_marketing_run", None)  # recorded
    s._tick()                                    # immediately after → not due again
    assert s.daily.runs == 1
