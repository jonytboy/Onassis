"""Tests for the Proposal investment economics."""

from __future__ import annotations

import pytest

from onassis.proposals import Proposal


def test_net_profit_and_roi_computed():
    p = Proposal(agent_name="a", requested_action="x", estimated_cost=20,
                 expected_revenue=100, confidence=100, risk_level="low")
    assert p.net_profit() == 80
    assert p.roi() == 4.0


def test_explicit_net_profit_and_roi_win():
    p = Proposal(agent_name="a", requested_action="x", estimated_cost=20,
                 expected_revenue=100, expected_net_profit=50, expected_roi=2.5)
    assert p.net_profit() == 50
    assert p.roi() == 2.5


def test_legacy_expected_benefit_used_as_revenue():
    p = Proposal(agent_name="a", requested_action="x", estimated_cost=10,
                 expected_benefit=40)
    assert p.revenue() == 40
    assert p.net_profit() == 30


def test_risk_adjusted_roi_discounts_by_confidence_and_risk():
    p = Proposal(agent_name="a", requested_action="x", estimated_cost=10,
                 expected_revenue=110, confidence=50, risk_level="high")
    # ROI 10 * 0.5 confidence * 0.4 high-risk weight = 2.0
    assert p.risk_adjusted_roi() == pytest.approx(2.0)


def test_zero_cost_positive_profit_is_strong_return():
    p = Proposal(agent_name="a", requested_action="x", estimated_cost=0,
                 expected_revenue=100, confidence=100, risk_level="low")
    assert p.roi() == 100  # no capital at risk -> profit treated as the return


def test_economics_view_is_serializable():
    p = Proposal(agent_name="a", requested_action="x", estimated_cost=20,
                 expected_revenue=100, confidence=80, risk_level="medium",
                 time_to_payback_days=30)
    econ = p.economics()
    assert econ["expected_net_profit"] == 80
    assert econ["risk_level"] == "medium"
    assert econ["time_to_payback_days"] == 30


def test_validate_rejects_bad_risk_level():
    p = Proposal(agent_name="a", requested_action="x", confidence=50, risk_level="extreme")
    with pytest.raises(ValueError, match="risk_level"):
        p.validate()


def test_from_dict_keeps_brand_and_marketplace():
    p = Proposal.from_dict(
        {"agent_name": "a", "requested_action": "x", "brand": "Local Celebrity",
         "marketplace": "Etsy", "unknown": "ignored"}
    )
    assert p.brand == "Local Celebrity"
    assert p.marketplace == "Etsy"
