"""Tests for the Analytics Collector (append-only history + trends)."""

from __future__ import annotations

import pytest

from onassis.analytics import AnalyticsEngine, EtsyAnalyticsSource, _trend


class _Source:
    """A simple metrics source for controlled tests."""

    name = "stub"

    def __init__(self, rows):
        self._rows = rows

    def fetch_metrics(self):
        return list(self._rows)


def _rows(product_id, *, date, views, orders, revenue, net_profit, conversion, campaign_id=1):
    return [
        {"platform": "etsy", "product_id": product_id, "campaign_id": campaign_id,
         "metric": m, "value": v, "snapshot_date": date}
        for m, v in (("views", views), ("orders", orders), ("revenue", revenue),
                     ("net_profit", net_profit), ("conversion", conversion))
    ]


@pytest.fixture
def engine(config, db):
    return AnalyticsEngine(config, db)


# --- Trend maths ----------------------------------------------------

def test_trend_up_down_flat():
    assert _trend([("d1", 10), ("d2", 20)])["label"] == "up"
    assert _trend([("d1", 20), ("d2", 5)])["label"] == "down"
    assert _trend([("d1", 5), ("d2", 5)])["label"] == "flat"
    assert _trend([("d1", 5)])["label"] == "flat"  # insufficient history


# --- Collection is append-only --------------------------------------

def test_collect_appends_and_never_overwrites(engine, db):
    src = _Source(_rows("P1", date="2026-06-25", views=100, orders=2, revenue=80,
                        net_profit=40, conversion=0.02))
    engine.collect([src])
    count_after_first = db.count_metric_snapshots()
    assert count_after_first == 5

    # Collecting again (same day, same source) appends — history is preserved.
    engine.collect([src])
    assert db.count_metric_snapshots() == 10


def test_etsy_source_derives_metrics(engine, db):
    db.upsert_etsy_listing({"listing_id": 9001, "product_id": "9001", "campaign_id": 1,
                            "views": 200, "num_favorers": 12})
    from onassis.revenue import RevenueEngine
    RevenueEngine(engine.config, db).record_order({
        "occurred_at": "2026-06-26T10:00:00+00:00", "sale_date": "2026-06-26",
        "order_ref": "x1", "product_id": "9001", "campaign_id": 1, "platform": "etsy",
        "sale_price": 48, "quantity": 1, "production_cost": 14,
    })
    rows = EtsyAnalyticsSource(db).fetch_metrics()
    metrics = {r["metric"]: r["value"] for r in rows if r["product_id"] == "9001"}
    assert metrics["views"] == 200
    assert metrics["favourites"] == 12
    assert metrics["orders"] == 1
    assert metrics["revenue"] == 48


# --- Trends over history --------------------------------------------

def test_product_trends_use_history(engine, db):
    engine.collect([_Source(_rows("P1", date="2026-06-20", views=100, orders=1,
                                  revenue=40, net_profit=20, conversion=0.01))])
    engine.collect([_Source(_rows("P1", date="2026-06-27", views=250, orders=5,
                                  revenue=200, net_profit=120, conversion=0.02))])
    a = engine.product_analytics("P1")
    assert a["history_points"] == 10
    assert a["trends"]["traffic_trend"]["label"] == "up"     # 100 -> 250
    assert a["trends"]["revenue_trend"]["label"] == "up"     # 40 -> 200
    assert a["trends"]["profit_trend"]["label"] == "up"
    assert a["latest"]["views"] == 250                       # latest value


def test_campaign_analytics_aggregates(engine, db):
    engine.collect([_Source(
        _rows("P1", date="2026-06-20", views=100, orders=1, revenue=40, net_profit=20,
              conversion=0.01, campaign_id=7)
        + _rows("P2", date="2026-06-20", views=50, orders=1, revenue=30, net_profit=10,
                conversion=0.02, campaign_id=7))])
    engine.collect([_Source(
        _rows("P1", date="2026-06-27", views=200, orders=2, revenue=90, net_profit=50,
              conversion=0.01, campaign_id=7))])
    a = engine.campaign_analytics(7)
    assert a["trends"]["revenue_trend"]["label"] == "up"


def test_product_trends_none_without_two_days(engine, db):
    engine.collect([_Source(_rows("P1", date="2026-06-20", views=100, orders=1,
                                  revenue=40, net_profit=20, conversion=0.01))])
    assert engine.product_trends("P1") is None  # only one day of history


# --- Optimiser consumes historical trends ---------------------------

def test_optimiser_uses_historical_profit_trend(config, db):
    from onassis.optimiser import ProductOptimiser
    from onassis.revenue import RevenueEngine

    sku = "9001"  # numeric so the optimiser reads current listing stats
    db.insert_product({"sku": sku, "name": "Throw", "production_cost": 14})
    RevenueEngine(config, db).record_order({
        "occurred_at": "2026-06-26T10:00:00+00:00", "sale_date": "2026-06-26",
        "order_ref": "o1", "product_id": sku, "platform": "etsy",
        "sale_price": 48, "quantity": 1, "production_cost": 14,
    })
    # Healthy CURRENT metrics: plenty of traffic and good conversion.
    db.upsert_etsy_listing({"listing_id": int(sku), "product_id": sku, "views": 300,
                            "num_favorers": 20})
    db.upsert_listing_stat({"listing_id": int(sku), "stat_date": "2026-06-27", "views": 300,
                            "visits": 300, "favourites": 20, "orders": 15, "revenue": 720,
                            "conversion_rate": 0.05})

    # ...but a DECLINING profit history in analytics.
    eng = AnalyticsEngine(config, db)
    eng.collect([_Source(_rows(sku, date="2026-06-10", views=300, orders=15, revenue=720,
                               net_profit=300, conversion=0.05))])
    eng.collect([_Source(_rows(sku, date="2026-06-27", views=300, orders=15, revenue=720,
                               net_profit=120, conversion=0.05))])

    opt = ProductOptimiser(config, db)
    rec = opt.analyse_product(db.get_product_by_sku(sku))
    assert rec["metrics"]["profit_trend"]["label"] == "down"   # from analytics history
    # A declining profitable product with healthy metrics -> design variation.
    assert rec["action_key"] == "design_variation"
