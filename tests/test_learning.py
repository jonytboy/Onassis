"""Tests for the Learning Engine — what sold, what didn't, and why."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from onassis.learning import LearningEngine
from onassis.revenue import RevenueEngine


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _launch(db, sku, product_key, *, age_days=40, cost=7.5):
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


def test_learning_increases_winners_and_retires_losers(config, db):
    # A winner, a loss-maker, a save-but-no-buy.
    _launch(db, "mug1", "ceramic_mug")
    _traffic(db, 1, "mug1", views=300, favourites=20)
    _sell(db, config, "mug1", units=5, sale_price=22.0, cost=7.5)

    _launch(db, "tote1", "tote_bag")
    _traffic(db, 2, "tote1", views=200)
    _sell(db, config, "tote1", units=2, sale_price=20.0, cost=9.0, adv=15.0)  # loss

    _launch(db, "post1", "premium_poster")
    _traffic(db, 3, "post1", views=300, favourites=12)   # interest, no conversion

    out = LearningEngine(config, db).run()

    increase_keys = {a["product_key"] for a in out["actions"]["increase"]}
    assert "ceramic_mug" in increase_keys           # winner scaled up
    retired_keys = {r["product_key"] for r in out["actions"]["retire"]}
    assert "tote_bag" in retired_keys               # loser archived
    assert db.get_product_by_sku("tote1")["active"] == 0
    adjust_keys = {a["product_key"] for a in out["actions"]["adjust"]}
    assert "premium_poster" in adjust_keys          # adjusted, not killed


def test_digest_answers_what_sold_and_why(config, db):
    _launch(db, "mug1", "ceramic_mug")
    _traffic(db, 1, "mug1", views=300)
    _sell(db, config, "mug1", units=3, sale_price=22.0, cost=7.5)

    out = LearningEngine(config, db).run()
    assert any(p["product_key"] == "ceramic_mug" for p in out["what_sold"])
    assert out["why"]                                # a human-readable explanation
    assert "scale" in out["headline"] or "retire" in out["headline"]


def test_learning_is_safe_with_no_data(config, db):
    out = LearningEngine(config, db).run()
    assert out["what_sold"] == []
    assert out["actions"]["increase"] == []
    assert out["actions"]["retire"] == []
    assert out["headline"]
