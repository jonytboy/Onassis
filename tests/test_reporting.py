"""Tests for the Daily Report — Revenue / Profit / Best / Worst / Recommendation."""

from __future__ import annotations

from onassis.reporting import EXPAND, HOLD, KILL, DailyReport
from onassis.revenue import RevenueEngine


def _sell(db, config, sku, product_key, *, units, sale_price, cost, adv=0.0):
    """Register a product and record `units` profitable/unprofitable orders."""
    db.insert_product({"sku": sku, "name": product_key.title(), "campaign_id": 1,
                       "product_key": product_key, "production_cost": cost})
    rev = RevenueEngine(config, db)
    for i in range(units):
        rev.record_order({"order_ref": f"{sku}-{i}", "product_id": sku, "platform": "etsy",
                          "sale_price": sale_price, "quantity": 1, "production_cost": cost,
                          "marketplace_fees": sale_price * 0.09, "payment_fees": sale_price * 0.04,
                          "advertising_cost": adv})


def test_expand_hold_kill_recommendations(config, db):
    # A winner (sells profitably), a dud (traffic, no sales), a newcomer (no data).
    _sell(db, config, "mug1", "ceramic_mug", units=4, sale_price=22.0, cost=7.5)
    db.insert_product({"sku": "post1", "name": "Poster", "campaign_id": 1,
                       "product_key": "premium_poster", "production_cost": 8.0})
    db.insert_product({"sku": "note1", "name": "Notebook", "campaign_id": 1,
                       "product_key": "hardcover_notebook", "production_cost": 8.5})
    # Poster has had lots of traffic and zero sales; notebook is brand new.
    db.upsert_etsy_listing({"listing_id": 1, "product_id": "post1", "views": 400})
    db.upsert_etsy_listing({"listing_id": 2, "product_id": "note1", "views": 3})

    from onassis.expansion import RevenueExpansionEngine
    RevenueExpansionEngine(config, db).learn_from_sales()

    report = DailyReport(config, db).build()
    recs = {p["product_key"]: p["recommendation"] for p in report["products"]}
    assert recs["ceramic_mug"] == EXPAND        # sells profitably -> do more
    assert recs["premium_poster"] == KILL       # real traffic, no sales -> kill
    assert recs["hardcover_notebook"] == HOLD   # not enough signal yet


def test_kill_when_selling_at_a_loss(config, db):
    # Sells, but heavy ad spend makes every order a loss.
    _sell(db, config, "loss1", "tote_bag", units=2, sale_price=20.0, cost=9.0, adv=15.0)
    from onassis.expansion import RevenueExpansionEngine
    RevenueExpansionEngine(config, db).learn_from_sales()

    report = DailyReport(config, db).build()
    tote = next(p for p in report["products"] if p["product_key"] == "tote_bag")
    assert tote["net_profit"] < 0
    assert tote["recommendation"] == KILL


def test_best_and_worst_seller_and_revenue(config, db):
    _sell(db, config, "mug1", "ceramic_mug", units=5, sale_price=22.0, cost=7.5)
    _sell(db, config, "card1", "greeting_card", units=1, sale_price=6.0, cost=2.5, adv=20.0)
    from onassis.expansion import RevenueExpansionEngine
    RevenueExpansionEngine(config, db).learn_from_sales()

    report = DailyReport(config, db).build()
    assert report["best_seller"]["product_key"] == "ceramic_mug"
    assert report["worst_seller"]["product_key"] == "greeting_card"   # the loss-maker
    assert report["revenue"]["company"] > 0
    # The report carries all five required fields.
    assert set(report["recommendations"]) == {"expand", "hold", "kill"}
    assert "headline" in report and report["profit"]["company_net"] != 0


def test_empty_report_is_safe(config, db):
    report = DailyReport(config, db).build()
    assert report["best_seller"] is None and report["worst_seller"] is None
    assert report["revenue"]["company"] == 0
    assert "No sales yet" in report["headline"]
