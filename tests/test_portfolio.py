"""Tests for the Portfolio Manager — the 30-day KEEP / IMPROVE / RETIRE lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from onassis.portfolio import IMPROVE, KEEP, RETIRE, PortfolioManager
from onassis.revenue import RevenueEngine


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _launch(db, sku, product_key, *, age_days, cost=7.5):
    """Create a live product launched `age_days` ago."""
    db.insert_product({"sku": sku, "name": product_key.title(), "campaign_id": 1,
                       "product_key": product_key, "production_cost": cost,
                       "launched_at": _days_ago(age_days)})


def _traffic(db, listing_id, sku, *, views, favourites=0):
    db.upsert_etsy_listing({"listing_id": listing_id, "product_id": sku,
                            "views": views, "num_favorers": favourites})


def _sell(db, config, sku, *, units, sale_price, cost, adv=0.0):
    rev = RevenueEngine(config, db)
    for i in range(units):
        rev.record_order({"order_ref": f"{sku}-{i}", "product_id": sku, "platform": "etsy",
                          "sale_price": sale_price, "quantity": 1, "production_cost": cost,
                          "marketplace_fees": sale_price * 0.09, "payment_fees": sale_price * 0.04,
                          "advertising_cost": adv})


def test_a_young_listing_is_not_judged_yet(config, db):
    _launch(db, "mug1", "ceramic_mug", age_days=10)
    _traffic(db, 1, "mug1", views=500)
    out = PortfolioManager(config, db).review()
    assert out["reviewed"] == 0          # still on probation (< 30 days)


def test_profitable_seller_is_kept(config, db):
    _launch(db, "mug1", "ceramic_mug", age_days=40)
    _traffic(db, 1, "mug1", views=300, favourites=20)
    _sell(db, config, "mug1", units=5, sale_price=22.0, cost=7.5)
    out = PortfolioManager(config, db).review()
    rec = out["reviews"][0]
    assert rec["decision"] == KEEP
    assert db.get_product_by_sku("mug1")["active"] == 1


def test_loss_maker_is_retired_and_archived(config, db):
    _launch(db, "tote1", "tote_bag", age_days=40)
    _traffic(db, 1, "tote1", views=200)
    _sell(db, config, "tote1", units=2, sale_price=20.0, cost=9.0, adv=15.0)  # loss
    out = PortfolioManager(config, db).review()
    assert out["reviews"][0]["decision"] == RETIRE
    assert db.get_product_by_sku("tote1")["active"] == 0   # archived


def test_traffic_but_no_sales_and_no_interest_is_retired(config, db):
    _launch(db, "post1", "premium_poster", age_days=40)
    _traffic(db, 1, "post1", views=400, favourites=0)      # market saw it, said no
    out = PortfolioManager(config, db).review()
    assert out["reviews"][0]["decision"] == RETIRE
    assert db.get_product_by_sku("post1")["active"] == 0


def test_interest_but_no_conversion_is_improved(config, db):
    _launch(db, "post1", "premium_poster", age_days=40)
    _traffic(db, 1, "post1", views=300, favourites=12)     # people save it, don't buy
    out = PortfolioManager(config, db).review()
    rec = out["reviews"][0]
    assert rec["decision"] == IMPROVE
    assert "reprice" in rec["improvements"]
    assert db.get_product_by_sku("post1")["active"] == 1   # kept live to fix


def test_starved_of_traffic_is_improved_then_retired(config, db):
    _launch(db, "note1", "hardcover_notebook", age_days=40)
    _traffic(db, 1, "note1", views=5)                      # barely seen
    pm = PortfolioManager(config, db)
    first = pm.review()
    assert first["reviews"][0]["decision"] == IMPROVE      # drive traffic, refresh hero
    assert "drive_traffic" in first["reviews"][0]["improvements"]
    # A second window later, still starved -> retire (can't earn its slot).
    second = pm.review()
    assert second["reviews"][0]["decision"] == RETIRE
    assert db.get_product_by_sku("note1")["active"] == 0


def test_archived_product_is_never_reviewed_again(config, db):
    _launch(db, "tote1", "tote_bag", age_days=40)
    _traffic(db, 1, "tote1", views=400)
    pm = PortfolioManager(config, db)
    pm.review()
    assert db.get_product_by_sku("tote1")["active"] == 0
    again = pm.review()
    assert again["reviewed"] == 0            # archived listings are skipped


def test_reconsider_revives_a_type_on_a_turning_trend(config, db):
    _launch(db, "tote1", "tote_bag", age_days=40)
    _traffic(db, 1, "tote1", views=400)
    pm = PortfolioManager(config, db)
    pm.review()
    assert db.get_product_by_sku("tote1")["active"] == 0
    # Fresh market data now shows tote_bag as a VERY HIGH opportunity.
    db.insert_market_signal({
        "run_at": "2026-01-01T00:00:00Z", "brand": "Local Celebrity",
        "keyword": "Tote", "product_type": "tote_bag", "theme": "citrus",
        "demand": 95, "competition": 20, "opportunity_score": 90,
        "opportunity": "VERY HIGH", "avg_selling_price": 26, "est_monthly_sales": 60,
        "competitor_count": 100, "review_count": 200, "payload": {}})
    out = pm.reconsider_archived()
    assert any(r["product_key"] == "tote_bag" for r in out["reactivated"])
    assert db.get_product_by_sku("tote1")["active"] == 1     # back in play
