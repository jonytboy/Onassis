"""Tests for the CEO Agent — the deterministic policy decision engine."""

from __future__ import annotations

import pytest

from onassis.ceo import CEOAgent
from onassis.proposals import APPROVE, REJECT, REQUEST_MORE_INFO, Proposal

# A policy with round numbers so the maths in assertions is obvious.
_POLICY = {
    "available_cash": 10000,
    "cash_reserve": 5000,
    "min_profit_margin": 0.30,
    "max_ai_spend": 50,
    "max_experiment_budget": 200,
    "min_confidence": 50,
    "brand_consistency_min": 70,
    "daily_ai_budget": 100,
    "min_roi": 0.50,
    "risk_weights": {"low": 1.0, "medium": 0.7, "high": 0.4},
}


@pytest.fixture
def ceo(config, db):
    config.policy = dict(_POLICY)
    return CEOAgent(config, db)


def _proposal(**kw) -> Proposal:
    base = dict(
        agent_name="ContentCreator",
        requested_action="Generate a campaign",
        estimated_cost=20,
        expected_benefit=100,  # margin 80%
        confidence=80,
        risks=["minor"],
        reasoning="Worth it.",
    )
    base.update(kw)
    return Proposal(**base)


def _decide(ceo, db, proposal, **kw) -> dict:
    """Store the proposal (FK target) then have the CEO decide on it."""
    pid = db.insert_proposal(proposal.to_dict())
    return ceo.evaluate(proposal, proposal_id=pid, **kw)


def test_approves_a_sound_proposal(ceo, db):
    d = _decide(ceo, db, _proposal())
    assert d["verdict"] == APPROVE
    assert "APPROVED" in d["reasoning"]
    assert d["authority"] == "CEO"


def test_rejects_when_over_max_ai_spend(ceo, db):
    d = _decide(ceo, db, _proposal(estimated_cost=80, expected_benefit=1000))
    assert d["verdict"] == REJECT
    assert "max_ai_spend" in d["reasoning"]


def test_rejects_when_breaching_cash_reserve(ceo, db):
    # Raise the spend caps and daily budget so cash reserve is the binding limit.
    ceo.policy["max_ai_spend"] = 100000
    ceo.policy["max_experiment_budget"] = 100000
    ceo.policy["daily_ai_budget"] = 1000000
    d = _decide(ceo, db, _proposal(estimated_cost=6000, expected_benefit=100000))
    assert d["verdict"] == REJECT
    assert "cash_reserve" in d["reasoning"]


def test_rejects_when_margin_too_low(ceo, db):
    # cost 40, benefit 50 -> margin 20% < 30%
    d = _decide(ceo, db, _proposal(estimated_cost=40, expected_benefit=50))
    assert d["verdict"] == REJECT
    assert "min_profit_margin" in d["reasoning"]


def test_requests_more_info_on_low_confidence(ceo, db):
    d = _decide(ceo, db, _proposal(confidence=20))
    assert d["verdict"] == REQUEST_MORE_INFO
    assert "INFORMATION" in d["reasoning"].upper()


def test_rejects_on_low_brand_consistency(ceo, db):
    d = _decide(ceo, db, _proposal(), brand_consistency_score=40)
    assert d["verdict"] == REJECT
    assert "brand_consistency" in d["reasoning"]


def test_approves_with_good_brand_consistency(ceo, db):
    d = _decide(ceo, db, _proposal(), brand_consistency_score=90)
    assert d["verdict"] == APPROVE


def test_decision_is_stored_with_reasoning(ceo, db):
    pid = db.insert_proposal(_proposal().to_dict())
    ceo.evaluate(_proposal(), proposal_id=pid)
    stored = db.get_decisions_for_proposal(pid)
    assert len(stored) == 1
    assert stored[0]["authority"] == "CEO"
    assert stored[0]["verdict"] == APPROVE
    assert stored[0]["reasoning"]  # non-empty written reasoning


def test_policy_checks_are_transparent(ceo, db):
    d = _decide(ceo, db, _proposal())
    names = {c["name"] for c in d["policy_checks"]}
    assert {"confidence", "max_ai_spend", "cash_reserve", "min_profit_margin"} <= names


def test_does_not_store_without_proposal_id(ceo, db):
    d = ceo.evaluate(_proposal(), store=True)  # no proposal_id
    assert "id" not in d
    assert db.list_decisions() == []


# --- Profit Engine: investment framing, ROI hurdle, allocation ------

def test_rejects_below_roi_hurdle(ceo, db):
    # Profitable but a weak return: cost 30, revenue 36 -> ROI 0.20, well under hurdle.
    d = _decide(ceo, db, _proposal(estimated_cost=30, expected_benefit=36))
    assert d["verdict"] == REJECT
    assert "roi_hurdle" in d["reasoning"]


def test_rejects_when_not_profitable(ceo, db):
    d = _decide(ceo, db, _proposal(estimated_cost=40, expected_benefit=30))  # net -10
    assert d["verdict"] == REJECT
    assert "positive_net_profit" in d["reasoning"]


def test_rejects_when_over_remaining_daily_budget(ceo, db):
    pid = db.insert_proposal(_proposal(estimated_cost=40).to_dict())
    d = ceo.evaluate(_proposal(estimated_cost=40), proposal_id=pid, remaining_budget=10)
    assert d["verdict"] == REJECT
    assert "daily_ai_budget" in d["reasoning"]


def test_economics_in_decision(ceo, db):
    d = _decide(ceo, db, _proposal(estimated_cost=20, expected_benefit=100))
    econ = d["economics"]
    assert econ["expected_net_profit"] == 80
    assert econ["expected_roi"] == 4.0
    assert econ["risk_adjusted_roi"] == pytest.approx(4.0 * 0.8 * 0.7)


def test_rank_orders_by_risk_adjusted_roi(ceo):
    low = _proposal(estimated_cost=50, expected_benefit=100)   # ROI 1.0
    high = _proposal(estimated_cost=10, expected_benefit=100)  # ROI 9.0
    ranked = ceo.rank([low, high])
    assert ranked[0] is high and ranked[1] is low


def test_allocate_funds_best_first_until_budget_exhausted(ceo, db):
    # Daily budget only covers one of two equally-priced, equally-confident bids.
    ceo.policy["daily_ai_budget"] = 50
    ceo.policy["max_ai_spend"] = 100
    better = _proposal(estimated_cost=40, expected_benefit=400)  # ROI 9
    worse = _proposal(estimated_cost=40, expected_benefit=120)   # ROI 2
    candidates = [
        {"proposal": worse, "proposal_id": db.insert_proposal(worse.to_dict())},
        {"proposal": better, "proposal_id": db.insert_proposal(better.to_dict())},
    ]
    results = ceo.allocate(candidates)

    assert results[0]["rank"] == 1
    assert results[0]["verdict"] == APPROVE          # highest ROI funded
    assert results[1]["verdict"] == REJECT           # budget exhausted
    assert "daily_ai_budget" in results[1]["reasoning"]
