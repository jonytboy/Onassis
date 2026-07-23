"""Tests for the Traffic Engine — schedule pins, distribute, log the funnel."""

from __future__ import annotations

from pathlib import Path

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


def _hero(config, tmp_path, product_key, campaign_id=1):
    """Write a real hero.jpg where the Traffic Engine expects it."""
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    images = tmp_path / "exports" / str(campaign_id) / product_key / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "hero.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")


def _seed_pins(config, db, product_key="linen_throw", listing_id="555", campaign_id=1,
               tmp_path=None):
    if tmp_path is not None:
        _hero(config, tmp_path, product_key, campaign_id)
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


def test_per_day_cap_holds_and_the_rest_roll_forward(config, db):
    config.traffic = {**(config.traffic or {}), "max_pins_per_day": 3,
                      "schedule_horizon_days": 7}
    # Two products => 10 candidate pins; 3/day cap => they fill a rolling calendar.
    _seed_pins(config, db, "linen_throw", "1", 1)
    _seed_pins(config, db, "ceramic_mug", "2", 1)
    out = TrafficEngine(config, db).schedule(today="2026-07-03")
    assert out["scheduled"] == 10                         # all placed across days
    assert db.count_pins_scheduled_on("2026-07-03") == 3  # never more than 3/day
    assert out["by_day"]["2026-07-03"] == 3
    assert max(out["by_day"].values()) == 3               # cap respected every day


def test_does_not_double_schedule_the_same_pin(config, db):
    _seed_pins(config, db)
    eng = TrafficEngine(config, db)
    eng.schedule(today="2026-07-03")
    # Next day, the same 5 pins are already queued -> nothing new to schedule.
    again = eng.schedule(today="2026-07-04")
    assert again["scheduled"] == 0


def test_pins_carry_the_product_hero_image(config, db, tmp_path):
    _seed_pins(config, db, tmp_path=tmp_path)
    TrafficEngine(config, db).schedule(today="2026-07-03")
    rows = db.list_pin_schedule()
    assert all(r["image_path"] and r["image_path"].endswith("hero.jpg") for r in rows)


def test_distribute_is_a_safe_noop_when_pinterest_unconfigured(config, db, tmp_path):
    _seed_pins(config, db, tmp_path=tmp_path)
    eng = TrafficEngine(config, db)
    eng.schedule(today="2026-07-03")
    out = eng.distribute(today="2026-07-03")
    assert out["posted"] == 0 and out["queued"] == 5    # queued, not lost
    assert db.list_pin_schedule(status="scheduled")     # still scheduled


def test_distribute_posts_live_pins_with_their_hero_image(config, db, tmp_path):
    _seed_pins(config, db, tmp_path=tmp_path)

    class FakePinterest:
        can_publish = True

        def __init__(self):
            self.posted_images = []

        def publish_pins(self, pins):
            self.posted_images.append(pins[0].get("image_path"))
            return {"posted": len(pins), "failed": 0, "skipped": 0,
                    "results": [{"status": "posted", "pin_id": "p1"}]}

    fake = FakePinterest()
    eng = TrafficEngine(config, db, pinterest=fake)
    eng.schedule(today="2026-07-03")
    out = eng.distribute(today="2026-07-03")
    assert out["posted"] == 5 and out["failed"] == 0
    assert all(img and img.endswith("hero.jpg") for img in fake.posted_images)
    assert len(db.list_pin_schedule(status="posted")) == 5


def test_publish_all_products_pins_every_product_with_a_hero(config, db, tmp_path):
    _hero(config, tmp_path, "linen_throw", 1)
    _hero(config, tmp_path, "ceramic_mug", 1)
    db.insert_product({"sku": "1-linen_throw", "name": "Linen Throw", "campaign_id": 1,
                       "product_key": "linen_throw"})
    db.insert_product({"sku": "1-ceramic_mug", "name": "Ceramic Mug", "campaign_id": 1,
                       "product_key": "ceramic_mug"})
    db.insert_product({"sku": "1-no_image", "name": "No Image", "campaign_id": 1,
                       "product_key": "no_image"})           # no hero on disk

    class FakePinterest:
        can_publish = True

        def __init__(self):
            self.calls = []

        def publish_pins(self, pins):
            self.calls.append(pins[0])
            return {"posted": 1, "failed": 0, "skipped": 0,
                    "results": [{"status": "posted", "pin_id": "p1"}]}

    fake = FakePinterest()
    out = TrafficEngine(config, db, pinterest=fake).publish_all_products()
    assert out["posted"] == 2 and out["no_image"] == 1 and out["total"] == 3
    assert all(c["image_path"].endswith("hero.jpg") for c in fake.calls)


def test_publish_all_products_safe_noop_when_pinterest_unconfigured(config, db):
    out = TrafficEngine(config, db).publish_all_products()
    assert out["posted"] == 0 and "not connected" in out["reason"].lower()


def test_distribute_leaves_imageless_pins_queued(config, db):
    _seed_pins(config, db)                                # no hero file on disk

    class FakePinterest:
        can_publish = True

        def publish_pins(self, pins):  # pragma: no cover - must not be called
            raise AssertionError("must not post an imageless pin")

    eng = TrafficEngine(config, db, pinterest=FakePinterest())
    eng.schedule(today="2026-07-03")
    out = eng.distribute(today="2026-07-03")
    assert out["posted"] == 0 and out["no_image"] == 5   # kept queued, never posted


def test_import_metrics_attributes_impressions_and_clicks_to_products(config, db, tmp_path):
    _seed_pins(config, db, tmp_path=tmp_path)

    class FakePinterest:
        can_publish = True
        can_read_analytics = True

        def publish_pins(self, pins):
            return {"posted": 1, "failed": 0, "skipped": 0,
                    "results": [{"status": "posted", "pin_id": "pin-x"}]}

        def pin_analytics(self, pin_id, **_):
            return {"impressions": 100, "clicks": 8}

    eng = TrafficEngine(config, db, pinterest=FakePinterest())
    eng.schedule(today="2026-07-03")
    eng.distribute(today="2026-07-03")
    out = eng.import_metrics(today="2026-07-03")
    assert out["impressions"] == 500 and out["clicks"] == 40   # 5 pins x (100/8)
    # Attributed to the product in the funnel.
    funnel = db.list_traffic_funnel(product_key="linen_throw")
    assert sum(r["impressions"] for r in funnel) == 500
    # A second import only books the increment (delta), not the lifetime total again.
    again = eng.import_metrics(today="2026-07-04")
    assert again["impressions"] == 0 and again["clicks"] == 0


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
