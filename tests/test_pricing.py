"""Tests for the Commercial Pricing Engine (maximise expected profit)."""

from __future__ import annotations

import pytest

from onassis.pricing import PricingEngine


@pytest.fixture
def engine(config):
    config.pricing = {"optimise": True, "base_conversion": 0.025, "elasticity": 1.6,
                      "candidates": 13, "price_span": 0.45, "min_margin": 0.10,
                      "target_margin": 0.60, "default_monthly_visitors": 500}
    return PricingEngine(config)


def test_optimise_returns_the_peak_of_the_profit_curve(engine):
    r = engine.optimise(production_cost=7.5, reference_price=22.0)
    curve = r["curve"]
    best = max(curve, key=lambda c: c["expected_profit"])
    assert r["price"] == best["price"]
    assert r["expected_profit"] == best["expected_profit"]
    # It beats naive cost-plus.
    assert r["profit_uplift_vs_cost_plus"] >= 0
    assert r["expected_profit"] > 0


def test_never_recommends_a_loss_making_price(engine):
    r = engine.optimise(production_cost=20.0, reference_price=22.0)
    assert r["unit_margin"] > 0
    assert r["net_margin"] >= 0


def test_lower_elasticity_favours_a_higher_price(config):
    config.pricing = {"optimise": True, "elasticity": 1.6}
    elastic = PricingEngine(config).optimise(7.5, 22.0)["price"]
    config.pricing = {"optimise": True, "elasticity": 0.6}
    inelastic = PricingEngine(config).optimise(7.5, 22.0)["price"]
    assert inelastic > elastic          # less price-sensitive -> charge more


def test_expected_profit_is_conversion_times_volume_times_margin(engine):
    point = engine.optimise(7.5, 22.0)["curve"][5]
    assert point["expected_profit"] == pytest.approx(
        point["volume"] * point["unit_margin"], abs=0.02)


def test_market_signals_anchor_price_and_traffic(engine):
    r = engine.optimise(7.5, reference_price=None,
                        market={"avg_selling_price": 30.0, "est_monthly_sales": 50})
    assert r["reference_price"] == pytest.approx(30.0, abs=2.0)   # anchored on market
    assert r["monthly_visitors"] > 0


def test_vat_reduces_margin_and_price_floor(config):
    config.fees = {**(config.fees or {}), "vat_rate": 0.20}
    config.pricing = {"optimise": True}
    with_vat = PricingEngine(config).optimise(7.5, 22.0)["unit_margin"]
    config.fees = {**(config.fees or {}), "vat_rate": 0.0}
    without_vat = PricingEngine(config).optimise(7.5, 22.0)["unit_margin"]
    assert with_vat < without_vat        # VAT is a real cost that thins the margin
