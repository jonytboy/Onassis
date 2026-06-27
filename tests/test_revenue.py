"""Tests for the Revenue Intelligence Engine (calculations + rollups)."""

from __future__ import annotations

import pytest

from onassis.connectors import ManualConnector
from onassis.revenue import RevenueEngine, compute_order_metrics


# --- Pure calculations (the heart of the engine) --------------------

def test_compute_order_metrics_full():
    order = {
        "sale_price": 25, "quantity": 2,
        "ai_cost": 1, "advertising_cost": 4, "production_cost": 8,
        "marketplace_fees": 5, "payment_fees": 2, "other_costs": 0,
    }
    m = compute_order_metrics(order)
    assert m["gross_revenue"] == 50       # 25 * 2
    assert m["total_cost"] == 20          # 1+4+8+5+2+0
    assert m["gross_profit"] == 42        # 50 - production(8)
    assert m["net_profit"] == 30          # 50 - 20
    assert m["profit_margin"] == pytest.approx(0.6)   # 30/50
    assert m["roi"] == pytest.approx(1.5)             # 30/20


def test_compute_metrics_zero_revenue_is_safe():
    m = compute_order_metrics({"sale_price": 0, "quantity": 1, "ai_cost": 5})
    assert m["gross_revenue"] == 0
    assert m["net_profit"] == -5
    assert m["profit_margin"] == 0.0   # no divide-by-zero
    assert m["roi"] == pytest.approx(-1.0)


def test_compute_metrics_zero_cost_roi_is_safe():
    m = compute_order_metrics({"sale_price": 10, "quantity": 1})
    assert m["total_cost"] == 0
    assert m["roi"] == 0.0             # no divide-by-zero
    assert m["net_profit"] == 10


def test_quantity_defaults_to_one():
    assert compute_order_metrics({"sale_price": 30})["gross_revenue"] == 30


# --- Recording & persistence ----------------------------------------

@pytest.fixture
def revenue(config, db):
    return RevenueEngine(config, db)


def _order(**kw):
    base = dict(
        order_ref="ETSY-1", occurred_at="2026-06-26T10:00:00+00:00",
        product_id="SKU1", campaign_id=1, platform="etsy",
        sale_price=25, currency="GBP", quantity=2,
        ai_cost=1, advertising_cost=4, production_cost=8,
        marketplace_fees=5, payment_fees=2, other_costs=0,
    )
    base.update(kw)
    return base


def test_record_order_persists_with_metrics(revenue, db):
    stored = revenue.record_order(_order())
    assert "id" in stored
    assert stored["net_profit"] == 30
    fetched = db.get_order(stored["id"])
    assert fetched["gross_revenue"] == 50
    assert fetched["net_profit"] == 30
    assert fetched["sale_date"] == "2026-06-26"


def test_record_order_mirrors_into_ledger(revenue, db):
    revenue.record_order(_order())
    # Revenue + each non-zero cost component should appear in the ledger.
    assert db.total_revenue() == 50
    assert db.cost_by_category("ai") == 1
    assert db.cost_by_category("advertising") == 4
    assert db.cost_by_category("cogs") == 8
    assert db.cost_by_category("marketplace") == 5
    assert db.cost_by_category("payment") == 2
    assert db.total_cost() == 20


def test_order_lifts_company_cash_by_net_profit(revenue):
    before = revenue.profit.cash_balance()
    revenue.record_order(_order())  # net profit 30
    assert revenue.profit.cash_balance() == pytest.approx(before + 30)


# --- Rollups --------------------------------------------------------

def test_revenue_today(revenue):
    today = "2026-06-26"
    revenue.record_order(_order(occurred_at=f"{today}T09:00:00+00:00"))
    revenue.record_order(_order(occurred_at=f"{today}T18:00:00+00:00"))
    revenue.record_order(_order(occurred_at="2026-06-25T18:00:00+00:00"))  # yesterday

    summary = revenue.revenue_today(today)
    assert summary["orders"] == 2
    assert summary["gross_revenue"] == 100   # 2 * 50
    assert summary["net_profit"] == 60       # 2 * 30


def test_revenue_month(revenue):
    revenue.record_order(_order(occurred_at="2026-06-01T09:00:00+00:00"))
    revenue.record_order(_order(occurred_at="2026-06-30T09:00:00+00:00"))
    revenue.record_order(_order(occurred_at="2026-07-01T09:00:00+00:00"))  # next month

    summary = revenue.revenue_month("2026-06")
    assert summary["orders"] == 2
    assert summary["net_profit"] == 60


def test_company_profit_includes_non_order_costs(revenue):
    revenue.record_order(_order())            # net +30
    revenue.profit.record_campaign_ai_cost(1, 5)  # a non-order cost
    company = revenue.company_profit()
    assert company["gross_revenue"] == 50
    assert company["total_cost"] == 25        # 20 order costs + 5 campaign AI
    assert company["net_profit"] == 25
    assert company["orders"] == 1


# --- Products -------------------------------------------------------

def test_register_and_list_products(revenue, db):
    p = revenue.register_product({"sku": "SKU1", "name": "Linen Throw", "production_cost": 8})
    assert p["sku"] == "SKU1"
    assert db.get_product_by_sku("SKU1")["name"] == "Linen Throw"
    assert len(revenue.list_products()) == 1


# --- Connector ingestion (plug-in architecture) ---------------------

def test_ingest_via_connector(revenue, db):
    connector = ManualConnector(
        [_order(order_ref="A"), _order(order_ref="B", platform="pinterest")]
    )
    count = revenue.ingest(connector)
    assert count == 2
    assert len(db.list_orders()) == 2
    platforms = {o["platform"] for o in db.list_orders()}
    assert platforms == {"etsy", "pinterest"}
