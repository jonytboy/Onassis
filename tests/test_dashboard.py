"""Tests for the CEO Dashboard — money, and nothing else."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from onassis.dashboard import CEODashboard
from onassis.revenue import RevenueEngine
from onassis.traffic import TrafficEngine

_MORNING = "2026-07-04"
_YESTERDAY = "2026-07-03"

_REQUIRED = ["revenue_yesterday", "profit_yesterday", "visitors", "conversion",
             "pinterest_clicks", "best_seller", "worst_seller", "products_retired",
             "products_launched", "cash_generated", "ai_cost", "roi"]


def _sell(db, config, sku, product_key, *, units, sale_price, cost, day=_YESTERDAY):
    db.insert_product({"sku": sku, "name": product_key.title(), "campaign_id": 1,
                       "product_key": product_key, "production_cost": cost})
    rev = RevenueEngine(config, db)
    for i in range(units):
        rev.record_order({"order_ref": f"{sku}-{i}", "occurred_at": f"{day}T10:00:00Z",
                          "sale_date": day, "product_id": sku, "platform": "etsy",
                          "sale_price": sale_price, "quantity": 1, "production_cost": cost,
                          "marketplace_fees": sale_price * 0.09,
                          "payment_fees": sale_price * 0.04})
    from onassis.expansion import RevenueExpansionEngine
    RevenueExpansionEngine(config, db).learn_from_sales()  # refresh perf (as the cycle does)


def test_dashboard_carries_every_money_metric(config, db):
    out = CEODashboard(config, db).build(today=_MORNING)
    for field in _REQUIRED:
        assert field in out
    assert out["date"] == _YESTERDAY          # the day that just closed
    assert "headline" in out


def test_revenue_and_profit_are_for_yesterday(config, db):
    _sell(db, config, "mug1", "ceramic_mug", units=4, sale_price=22.0, cost=7.5)
    out = CEODashboard(config, db).build(today=_MORNING)
    assert out["revenue_yesterday"] == pytest.approx(88.0)   # 4 * 22
    assert out["profit_yesterday"] > 0
    assert out["cash_generated"] == out["profit_yesterday"]
    assert out["best_seller"]["product_key"] == "ceramic_mug"


def test_visitors_conversion_and_pinterest_clicks_from_funnel(config, db):
    _sell(db, config, "mug1", "ceramic_mug", units=3, sale_price=22.0, cost=7.5)
    TrafficEngine(config, db).record_funnel(product_key="ceramic_mug", impressions=1000,
                                            clicks=90, visits=60, sales=3, today=_YESTERDAY)
    out = CEODashboard(config, db).build(today=_MORNING)
    assert out["visitors"] == 60
    assert out["pinterest_clicks"] == 90
    assert out["conversion"] == pytest.approx(0.05)   # 3 sales / 60 visits


def test_products_launched_and_retired_counts(config, db):
    # A product launched yesterday.
    yesterday_ts = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        microsecond=0)
    db.insert_product({"sku": "new1", "product_key": "tote_bag",
                       "launched_at": yesterday_ts.isoformat()})
    # A retirement recorded yesterday.
    db.insert_portfolio_review({"sku": "old1", "product_key": "greeting_card",
                                "decision": "RETIRE", "reason": "no sales"})
    db.set_product_active("new1", False)  # something archived overall
    out = CEODashboard(config, db).build()  # today defaults; yesterday derived
    # The launched-yesterday product is counted for yesterday.
    assert out["products_launched"]["yesterday"] >= 1
    assert out["products_retired"]["total"] >= 1


def test_ai_cost_and_roi_reported(config, db):
    _sell(db, config, "mug1", "ceramic_mug", units=2, sale_price=22.0, cost=7.5)
    db.insert_ledger_entry({"kind": "cost", "category": "ai",
                            "amount": 1.25, "entry_date": _YESTERDAY, "note": "gen"})
    out = CEODashboard(config, db).build(today=_MORNING)
    assert out["ai_cost"] == pytest.approx(1.25)
    assert out["roi"] > 0


def test_safe_with_no_activity(config, db):
    out = CEODashboard(config, db).build(today=_MORNING)
    assert out["revenue_yesterday"] == 0
    assert out["best_seller"] is None
    assert out["headline"]
