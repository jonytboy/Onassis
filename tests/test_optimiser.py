"""Tests for the Autonomous Product Optimiser (deterministic, no LLM)."""

from __future__ import annotations

import pytest

from onassis.optimiser import ACTIONS, ProductOptimiser


@pytest.fixture
def opt(config, db):
    config.optimiser = {
        "min_views": 100, "good_conversion": 0.02, "potential_profit_floor": 5.0,
    }
    return ProductOptimiser(config, db)


def _product(db, sku, **kw):
    pid = db.insert_product({"sku": sku, "name": kw.get("name", f"Product {sku}"),
                             "active": kw.get("active", True),
                             "marketplace": kw.get("marketplace", "etsy")})
    return db.get_product(pid)


def _order(db, sku, *, price, qty=1, net_costs=0.0, sale_date="2026-06-20", campaign_id=None):
    from onassis.revenue import compute_order_metrics
    o = {
        "occurred_at": f"{sale_date}T10:00:00+00:00", "sale_date": sale_date,
        "order_ref": f"{sku}-{sale_date}-{price}-{qty}-{net_costs}",
        "product_id": sku, "campaign_id": campaign_id, "platform": "etsy",
        "sale_price": price, "currency": "GBP", "quantity": qty,
        "production_cost": net_costs,
    }
    o.update(compute_order_metrics(o))
    db.insert_order(o)


def _listing(db, listing_id, *, views, favourites, orders=0, revenue=0.0,
             stat_date="2026-06-26", conversion=None):
    db.upsert_etsy_listing({"listing_id": listing_id, "product_id": str(listing_id),
                            "views": views, "num_favorers": favourites})
    db.upsert_listing_stat({
        "listing_id": listing_id, "stat_date": stat_date, "views": views,
        "visits": views, "favourites": favourites, "orders": orders, "revenue": revenue,
        "conversion_rate": conversion if conversion is not None else (orders / views if views else 0),
    })


# --- Metrics --------------------------------------------------------

def test_metrics_combine_orders_and_stats(opt, db):
    p = _product(db, "9001")
    _order(db, "9001", price=48, net_costs=14)
    _listing(db, 9001, views=200, favourites=12, orders=1, revenue=48)

    m = opt.analyse_product(p)["metrics"]
    assert m["views"] == 200
    assert m["favourites"] == 12
    assert m["revenue"] == 48.0
    assert m["net_profit"] == 34.0          # 48 - 14
    assert m["orders"] == 1


# --- Decision branches (one action each) ----------------------------

def test_no_data_recommends_pinterest(opt, db):
    p = _product(db, "1")
    rec = opt.analyse_product(p)
    assert rec["action_key"] == "pinterest_campaign"


def test_unprofitable_low_traffic_archives(opt, db):
    p = _product(db, "2")
    _order(db, "2", price=10, net_costs=25)   # net negative
    _listing(db, 2, views=5, favourites=0, orders=1, revenue=10)
    assert opt.analyse_product(p)["action_key"] == "archive"


def test_unprofitable_with_traffic_rewrites_description(opt, db):
    p = _product(db, "3")
    _order(db, "3", price=10, net_costs=25)   # net negative
    _listing(db, 3, views=400, favourites=2, orders=1, revenue=10)
    assert opt.analyse_product(p)["action_key"] == "rewrite_description"


def test_profitable_low_traffic_recommends_pinterest(opt, db):
    p = _product(db, "4")
    _order(db, "4", price=48, net_costs=14)   # profitable
    _listing(db, 4, views=20, favourites=1, orders=1, revenue=48)
    assert opt.analyse_product(p)["action_key"] == "pinterest_campaign"


def test_profitable_low_conversion_high_favourites_improves_seo(opt, db):
    p = _product(db, "5")
    _order(db, "5", price=48, net_costs=14)
    # lots of views, lots of favourites, but tiny conversion
    _listing(db, 5, views=1000, favourites=80, orders=1, revenue=48, conversion=0.001)
    assert opt.analyse_product(p)["action_key"] == "improve_seo"


def test_profitable_low_conversion_low_favourites_fresh_images(opt, db):
    p = _product(db, "6")
    _order(db, "6", price=48, net_costs=14)
    _listing(db, 6, views=1000, favourites=2, orders=1, revenue=48, conversion=0.001)
    assert opt.analyse_product(p)["action_key"] == "fresh_images"


def test_healthy_product_left_unchanged(opt, db):
    p = _product(db, "7")
    _order(db, "7", price=48, net_costs=14)
    _listing(db, 7, views=300, favourites=20, orders=15, revenue=720, conversion=0.05)
    assert opt.analyse_product(p)["action_key"] == "leave_unchanged"


def test_healthy_but_declining_profit_gets_design_variation(opt, db):
    p = _product(db, "8")
    # Two orders, recent one less profitable -> profit trend down.
    _order(db, "8", price=60, net_costs=10, sale_date="2026-06-01")  # net 50
    _order(db, "8", price=30, net_costs=10, sale_date="2026-06-20")  # net 20
    _listing(db, 8, views=300, favourites=20, orders=2, revenue=90, conversion=0.05)
    rec = opt.analyse_product(p)
    assert rec["metrics"]["profit_trend"]["label"] == "down"
    assert rec["action_key"] == "design_variation"


# --- Recommendation shape & CEO + ranking ---------------------------

def test_recommendation_has_required_fields(opt, db):
    p = _product(db, "9")
    _order(db, "9", price=48, net_costs=14)
    _listing(db, 9, views=20, favourites=1, orders=1, revenue=48)
    rec = opt.analyse_product(p)
    for key in ("estimated_cost", "expected_increase_in_profit", "confidence", "reasoning"):
        assert key in rec
    assert rec["recommendation"] in ACTIONS.values()


def test_top_recommendation_runs_ceo(opt, db):
    p = _product(db, "10")
    _order(db, "10", price=48, net_costs=14)
    _listing(db, 10, views=20, favourites=1, orders=1, revenue=48)
    top = opt.top_recommendation()
    assert "ceo" in top
    assert top["ceo"]["verdict"] in ("APPROVE", "REJECT", "REQUEST_MORE_INFO")


def test_prefers_improving_profitable_product(opt, db):
    # An unprofitable product (would be archived) and a profitable one needing traffic.
    losing = _product(db, "20")
    _order(db, "20", price=10, net_costs=25)
    _listing(db, 20, views=5, favourites=0, orders=1, revenue=10)

    earner = _product(db, "21")
    _order(db, "21", price=48, net_costs=14)
    _listing(db, 21, views=20, favourites=2, orders=1, revenue=48)

    top = opt.top_recommendation()
    assert top["product"] == "21"        # the profitable product is prioritised


def test_archived_products_excluded(opt, db):
    _product(db, "30", active=False)
    assert opt.analyse_all() == []


def test_marketplace_agnostic(opt, db):
    # A non-Etsy product is analysed by the same logic.
    p = _product(db, "40", marketplace="gelato")
    _order(db, "40", price=48, net_costs=14)
    rec = opt.analyse_product(p)
    assert rec["marketplace"] == "gelato"
    assert rec["action_key"] in ACTIONS
