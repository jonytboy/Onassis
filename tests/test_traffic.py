"""Tests for the Traffic Engine — schedule pins, distribute, log the funnel."""

from __future__ import annotations

import pytest

from onassis.marketing import MarketingEngine
from onassis.traffic import TrafficEngine, season_for

_LISTING = {
    "title": "Linen Throw — Slow Mediterranean Mornings",
    "description": "A stonewashed linen throw for unhurried coastal mornings.",
    "tags": ["linen throw", "coastal blanket", "slow living"],
    "seo_keywords": ["linen throw", "coastal blanket", "stonewashed linen"],
    "theme": "coastal mornings", "product_key": "linen_throw",
}


def _seed_pins(config, db, product_key="linen_throw", listing_id="555", campaign_id=1):
    MarketingEngine(config, db).build({**_LISTING, "product_key": product_key},
                                      listing_id=listing_id, campaign_id=campaign_id,
                                      product_key=product_key)


def test_schedules_pins_across_boards_and_a_season(config, db):
    _seed_pins(config, db)
    out = TrafficEngine(config, db).schedule(today="2026-07-03")
    assert out["scheduled"] == 5            # the 5 pins for the one product
    assert out["season"] == "summer"        # July -> summer
    rows = db.list_pin_schedule(scheduled_date="2026-07-03")
    assert len(rows) == 5
    assert all(r["listing_url"] == "https://www.etsy.com/listing/555" for r in rows)


def test_never_exceeds_the_daily_cap(config, db):
    config.traffic = {**(config.traffic or {}), "max_pins_per_day": 3}
    # Two products => 10 candidate pins, but the cap is 3/day.
    _seed_pins(config, db, "linen_throw", "1", 1)
    _seed_pins(config, db, "ceramic_mug", "2", 1)
    out = TrafficEngine(config, db).schedule(today="2026-07-03")
    assert out["scheduled"] == 3
    assert db.count_pins_scheduled_on("2026-07-03") == 3


def test_does_not_double_schedule_the_same_pin(config, db):
    _seed_pins(config, db)
    eng = TrafficEngine(config, db)
    eng.schedule(today="2026-07-03")
    # Next day, the same 5 pins are already queued -> nothing new to schedule.
    again = eng.schedule(today="2026-07-04")
    assert again["scheduled"] == 0


def test_distribute_is_a_safe_noop_when_pinterest_unconfigured(config, db):
    _seed_pins(config, db)
    eng = TrafficEngine(config, db)
    eng.schedule(today="2026-07-03")
    out = eng.distribute(today="2026-07-03")
    assert out["posted"] == 0 and out["queued"] == 5    # queued, not lost
    assert db.list_pin_schedule(status="scheduled")     # still scheduled


def test_distribute_posts_when_pinterest_is_configured(config, db):
    _seed_pins(config, db)

    class FakePinterest:
        can_publish = True

        def publish_pins(self, pins):
            return {"posted": len(pins), "failed": 0, "skipped": 0,
                    "results": [{"status": "posted", "pin_id": "p1"}]}

    eng = TrafficEngine(config, db, pinterest=FakePinterest())
    eng.schedule(today="2026-07-03")
    out = eng.distribute(today="2026-07-03")
    assert out["posted"] == 5 and out["failed"] == 0
    assert len(db.list_pin_schedule(status="posted")) == 5


def test_funnel_records_impressions_clicks_visits_sales(config, db):
    eng = TrafficEngine(config, db)
    eng.record_funnel(product_key="linen_throw", impressions=1000, clicks=80,
                      visits=60, sales=3, today="2026-07-03")
    f = eng.funnel("2026-07-03")
    assert f["impressions"] == 1000 and f["clicks"] == 80
    assert f["visits"] == 60 and f["sales"] == 3
    assert f["click_through_rate"] == pytest.approx(0.08)
    assert f["conversion_rate"] == pytest.approx(0.05)


def test_snapshot_builds_the_funnel_from_collected_metrics(config, db):
    # Pinterest + Etsy metric snapshots for the day, plus a real order.
    db.insert_metric_snapshots([
        {"platform": "pinterest", "metric": "impressions", "value": 500,
         "snapshot_date": "2026-07-03"},
        {"platform": "pinterest", "metric": "clicks", "value": 40,
         "snapshot_date": "2026-07-03"},
        {"platform": "etsy", "metric": "visits", "value": 30,
         "snapshot_date": "2026-07-03"},
    ])
    from onassis.revenue import RevenueEngine
    RevenueEngine(config, db).record_order({
        "order_ref": "o1", "occurred_at": "2026-07-03T10:00:00Z",
        "sale_date": "2026-07-03", "product_id": "linen_throw", "platform": "etsy",
        "sale_price": 30, "quantity": 1, "production_cost": 8})
    totals = TrafficEngine(config, db).snapshot(today="2026-07-03")
    assert totals["impressions"] == 500 and totals["clicks"] == 40
    assert totals["visits"] == 30 and totals["sales"] == 1


def test_season_for():
    assert season_for("2026-01-15") == "winter"
    assert season_for("2026-07-03") == "summer"
    assert season_for("2026-10-01") == "autumn"
