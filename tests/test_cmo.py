"""Tests for the CMO manager + campaign calendar (Sprint 42 Phase 3)."""

from __future__ import annotations

from datetime import date

from onassis.cmo import CMOManager
from onassis.distribution import ChannelDistributor


def _seed_asset(db, channel):
    return db.insert_marketing_asset({"campaign_id": 1, "product_key": "mug",
                                      "channel": channel, "payload": {"subject": "s",
                                                                     "body": "b"}})


# --- Content calendar (Obj 4) ----------------------------------------

def test_content_calendar_has_channel_per_day(config, db):
    cal = CMOManager(config, db).content_calendar("2026-07-06", days=7)  # Mon
    assert len(cal) == 7
    assert cal[0]["weekday"] == "Monday" and cal[0]["channel"] == "pinterest"
    assert cal[1]["channel"] == "facebook" and cal[3]["channel"] == "tiktok"
    assert cal[6]["channel"] == "review"


def test_schedule_pending_assigns_matching_days(config, db):
    fb = _seed_asset(db, "facebook")
    ig = _seed_asset(db, "instagram")
    cmo = CMOManager(config, db)
    r = cmo.schedule_pending("2026-07-06")            # week starting Monday
    assert r["scheduled"] == 2
    got = {a["channel"]: a["scheduled_date"] for a in db.list_marketing_assets()}
    # Facebook → a Tuesday, Instagram → a Wednesday.
    assert date.fromisoformat(got["facebook"]).weekday() == 1
    assert date.fromisoformat(got["instagram"]).weekday() == 2


def test_calendar_view_attaches_scheduled_assets(config, db):
    _seed_asset(db, "email")
    cmo = CMOManager(config, db)
    cmo.schedule_pending("2026-07-06")
    view = cmo.calendar_view("2026-07-06")
    assert any(day["assets"] for day in view)


# --- Distribution respects the schedule ------------------------------

def test_distribution_only_sends_due_assets(config, db):
    _seed_asset(db, "email")
    CMOManager(config, db).schedule_pending("2026-07-06")   # email → Friday 2026-07-10

    class FakeEmail:
        can_publish = True
        def send(self, subject, body, to=None): return {"ok": True, "ref": "e1"}
    dist = ChannelDistributor(config, db, email=FakeEmail())
    # Not due yet on Monday.
    assert dist.distribute(due_on="2026-07-06")["posted"] == 0
    # Due on/after Friday.
    assert dist.distribute(due_on="2026-07-10")["posted"] == 1


# --- Budget + strategy (Obj 2) ---------------------------------------

def test_budget_allocation_sums_and_covers_channels(config, db):
    alloc = CMOManager(config, db).budget_allocation(100.0)
    assert set(alloc) >= {"pinterest", "facebook", "instagram", "tiktok", "email", "blog"}
    assert abs(sum(alloc.values()) - 100.0) < 1.0     # splits the whole budget


def test_launch_plan_is_ordered(config, db):
    plan = CMOManager(config, db).launch_plan("mug")
    channels = [s["channel"] for s in plan["sequence"]]
    assert channels[0] == "blog" and "tiktok" in channels
    assert [s["day"] for s in plan["sequence"]] == list(range(1, len(channels) + 1))


def test_strategy_is_business_grounded(config, db):
    s = CMOManager(config, db).strategy()
    assert "focus_channel" in s and "budget_split" in s and s["recommendations"]
