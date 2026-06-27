"""Tests for the Profit Engine (ledger + dashboard)."""

from __future__ import annotations

import pytest

from onassis.profit import ProfitEngine


@pytest.fixture
def profit(config, db):
    # Known starting cash + daily budget for predictable maths.
    config.policy = {"available_cash": 10000, "daily_ai_budget": 100}
    return ProfitEngine(config, db)


def test_empty_dashboard_has_priority_order(profit):
    d = profit.dashboard()
    keys = list(d.keys())
    assert keys[:8] == [
        "net_profit", "roi", "cash_balance", "ai_cost", "advertising_cost",
        "active_products", "profit_per_product", "profit_per_campaign",
    ]
    assert d["net_profit"] == 0
    assert d["cash_balance"] == 10000  # starting cash, no activity


def test_costs_and_revenue_flow_to_net_profit_and_cash(profit):
    profit.record_cost(30, category="ai", campaign_id=1)
    profit.record_cost(20, category="advertising", campaign_id=1)
    profit.record_revenue(200, campaign_id=1, product_id="SKU1")

    d = profit.dashboard()
    assert d["net_profit"] == 150        # 200 - 50
    assert d["ai_cost"] == 30
    assert d["advertising_cost"] == 20
    assert d["cash_balance"] == 10150    # 10000 + 150
    assert d["roi"] == pytest.approx(150 / 50)


def test_profit_per_campaign_and_product(profit):
    profit.record_cost(10, category="ai", campaign_id=1)
    profit.record_revenue(60, campaign_id=1, product_id="SKU1")
    profit.record_revenue(15, campaign_id=2, product_id="SKU2")

    d = profit.dashboard()
    by_campaign = {r["campaign_id"]: r["net_profit"] for r in d["profit_per_campaign"]}
    assert by_campaign == {1: 50, 2: 15}
    assert d["active_products"] == 2


def test_daily_ai_budget_tracking(profit):
    assert profit.remaining_ai_budget("2026-06-26") == 100
    profit.record_cost(40, category="ai", entry_date="2026-06-26")
    assert profit.ai_spend_today("2026-06-26") == 40
    assert profit.remaining_ai_budget("2026-06-26") == 60
    # A different day is unaffected.
    assert profit.remaining_ai_budget("2026-06-27") == 100


def test_record_campaign_ai_cost(profit, db):
    profit.record_campaign_ai_cost(7, 0.5)
    assert db.cost_by_category("ai") == 0.5
    assert db.net_by_campaign() == [{"campaign_id": 7, "net_profit": -0.5}]
