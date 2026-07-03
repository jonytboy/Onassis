"""Tests for the Financial Protection Engine — the commercial safeguard."""

from __future__ import annotations

import pytest

from onassis.protection import APPROVE, REJECT, FinancialProtectionEngine


@pytest.fixture
def guard(config, db):
    return FinancialProtectionEngine(config, db)


def test_healthy_launch_is_approved(guard):
    d = guard.guard_launch(26.0, production_cost=7.5, shipping_cost=3.0,
                           product_key="ceramic_mug")
    assert d["decision"] == APPROVE
    assert d["gross_margin"] >= 0.25
    assert d["contribution_margin"] >= 0.18
    assert d["protected_profit"] > 0


def test_thin_margin_launch_is_rejected(guard):
    d = guard.guard_launch(12.0, production_cost=8.0, shipping_cost=3.0)
    assert d["decision"] == REJECT
    assert "contribution margin" in d["reason"] or "protected profit" in d["reason"]
    # It still tells the caller the price that WOULD be safe (adjust up, not refuse).
    assert d["min_safe_price"] > 12.0


def test_unknown_costs_do_not_auto_block_but_raise_the_reserve(guard):
    known = guard.guard_launch(30.0, production_cost=7.5, shipping_cost=2.0, ai_cost=0.05,
                               product_key="ceramic_mug")
    unknown = guard.guard_launch(30.0, product_key="ceramic_mug")  # all costs unknown
    assert unknown["decision"] == APPROVE                 # estimated, not refused
    assert unknown["estimated_costs"] > 0                 # conservative defaults applied
    assert unknown["risk_reserve_percent"] > known["risk_reserve_percent"]  # more uncertainty


def test_dynamic_buffer_shrinks_as_sales_confidence_grows(config, db):
    # A proven product (lots of sales) earns a slimmer risk reserve than a new one.
    db.upsert_product_performance({"product_key": "proven", "units_sold": 40, "orders": 40,
                                   "gross_revenue": 1000, "net_profit": 300})
    guard = FinancialProtectionEngine(config, db)
    new = guard.guard_launch(26.0, production_cost=7.5, shipping_cost=3.0,
                             product_key="brand_new")
    proven = guard.guard_launch(26.0, production_cost=7.5, shipping_cost=3.0,
                                product_key="proven")
    assert proven["risk_reserve_percent"] < new["risk_reserve_percent"]
    assert proven["protected_profit"] > new["protected_profit"]
    assert proven["data_confidence"] > new["data_confidence"]


def test_price_change_beyond_the_cap_is_rejected(guard):
    d = guard.guard_price_change(30.0, reference_price=22.0, production_cost=7.5,
                                 shipping_cost=2.0, product_key="ceramic_mug")
    assert d["decision"] == REJECT
    assert "exceeds max single change" in d["reason"]


def test_small_price_change_within_the_cap_is_allowed(guard):
    d = guard.guard_price_change(24.0, reference_price=22.0, production_cost=7.5,
                                 shipping_cost=2.0, product_key="ceramic_mug")
    assert d["decision"] == APPROVE


def test_confidence_gates_changes_but_not_launches(guard):
    # Thin-but-positive economics: protected margin below the comfort band, so
    # commercial confidence is low. A launch is allowed (bigger buffer, still safe);
    # a change of an existing listing is not (only act when confident).
    kw = {"production_cost": 12.0, "shipping_cost": 4.0}
    launch = guard.guard_launch(26.0, **kw)
    change = guard.guard_price_change(26.0, reference_price=25.0, **kw)
    assert launch["confidence"] < guard.min_confidence
    assert launch["decision"] == APPROVE                 # launches are not confidence-gated
    assert change["decision"] == REJECT
    assert "commercial confidence" in change["reason"]


def test_advertising_that_erodes_protected_profit_is_rejected(guard):
    ok = guard.guard_advertising(26.0, ad_cost=1.0, production_cost=7.5, shipping_cost=2.0,
                                 product_key="ceramic_mug")
    heavy = guard.guard_advertising(26.0, ad_cost=12.0, production_cost=7.5,
                                    shipping_cost=2.0, product_key="ceramic_mug")
    assert ok["decision"] == APPROVE
    assert heavy["decision"] == REJECT                    # the ad spend kills the margin


def test_every_decision_is_audited_with_the_full_trail(guard, db):
    guard.guard_launch(26.0, production_cost=7.5, shipping_cost=3.0, product_key="mug")
    row = db.list_protection_decisions()[0]
    for field in ("action", "expected_revenue", "expected_costs", "estimated_costs",
                  "risk_reserve_percent", "risk_reserve_amount", "protected_profit",
                  "decision", "reason"):
        assert field in row
    assert row["action"] == "launch"


def test_rejections_are_surfaced_as_ceo_alerts(guard):
    guard.guard_launch(12.0, production_cost=8.0, shipping_cost=3.0)
    alerts = guard.alerts()
    assert len(alerts) == 1 and alerts[0]["decision"] == REJECT
