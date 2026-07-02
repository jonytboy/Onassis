"""Tests for the governance chain — Compliance veto over the CEO."""

from __future__ import annotations

import pytest

from onassis.governance import Governance
from onassis.proposals import APPROVE, APPROVE_WITH_CHANGES, REJECT, Proposal
from tests.conftest import FakeLLM, make_compliance_response

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


def _gov(config, db, compliance_response):
    config.policy = dict(_POLICY)
    config.compliance = {"high_risk_threshold": 70, "medium_risk_threshold": 40,
                         "brand_consistency_min": 70}
    gov = Governance(config, db)
    gov.compliance._llm = FakeLLM(compliance_response)
    return gov


def _sound_proposal(**kw) -> Proposal:
    base = dict(
        agent_name="ContentCreator",
        requested_action="Generate a campaign",
        estimated_cost=20,
        expected_benefit=100,
        confidence=80,
    )
    base.update(kw)
    return Proposal(**base)


def test_approved_when_both_approve(config, db):
    gov = _gov(config, db, make_compliance_response())  # low risk -> compliance APPROVE
    result = gov.submit(_sound_proposal())
    assert result["final_verdict"] == APPROVE
    assert result["final_authority"] == "CEO"
    # proposal status persisted
    assert db.get_proposal(result["proposal_id"])["status"] == "approved"


def test_compliance_overrules_ceo_approval(config, db):
    # CEO would approve (sound proposal) but compliance finds high copyright risk.
    gov = _gov(config, db, make_compliance_response(copyright=95))
    result = gov.submit(_sound_proposal())
    assert result["ceo"]["verdict"] == APPROVE        # CEO said yes...
    assert result["final_verdict"] == REJECT          # ...but compliance vetoes
    assert result["final_authority"] == "Compliance"
    assert db.get_proposal(result["proposal_id"])["status"] == "rejected"


def test_ceo_rejects_even_when_compliance_clears(config, db):
    # Compliance approves, but the proposal breaks budget policy.
    gov = _gov(config, db, make_compliance_response())
    result = gov.submit(_sound_proposal(estimated_cost=80, expected_benefit=1000))
    assert result["compliance"]["verdict"] == APPROVE
    assert result["final_verdict"] == REJECT
    assert result["final_authority"] == "CEO"


def test_medium_risk_clears_to_the_ceo_not_a_veto(config, db):
    # Medium risk is APPROVE_WITH_CHANGES now — it does NOT veto; it clears the
    # subject through to the CEO's capital decision (the pipeline never stalls).
    gov = _gov(config, db, make_compliance_response(platform=55))  # medium risk
    result = gov.submit(_sound_proposal())
    assert result["compliance"]["verdict"] == APPROVE_WITH_CHANGES
    assert result["final_authority"] == "CEO"
    assert result["final_verdict"] == APPROVE
    assert db.get_proposal(result["proposal_id"])["status"] == "approved"


def test_both_decisions_are_logged(config, db):
    gov = _gov(config, db, make_compliance_response())
    result = gov.submit(_sound_proposal())
    decisions = db.get_decisions_for_proposal(result["proposal_id"])
    authorities = {d["authority"] for d in decisions}
    assert authorities == {"Compliance", "CEO"}
    assert all(d["reasoning"] for d in decisions)  # every decision has reasoning


def test_invalid_proposal_rejected_before_decisions(config, db):
    gov = _gov(config, db, make_compliance_response())
    with pytest.raises(ValueError):
        gov.submit(Proposal(agent_name="", requested_action=""))
    assert db.list_proposals() == []  # nothing stored


def test_get_proposal_decisions_bundle(config, db):
    gov = _gov(config, db, make_compliance_response())
    pid = gov.submit(_sound_proposal())["proposal_id"]
    bundle = gov.get_proposal_decisions(pid)
    assert bundle["proposal"]["id"] == pid
    assert bundle["compliance"] is not None
    assert len(bundle["decisions"]) == 2


# --- Capital allocation (batch) -------------------------------------

def test_allocate_funds_highest_roi_within_budget(config, db):
    config.policy = dict(_POLICY, daily_ai_budget=50, max_ai_spend=100)
    config.compliance = {"high_risk_threshold": 70, "medium_risk_threshold": 40,
                         "brand_consistency_min": 70}
    gov = Governance(config, db)
    gov.compliance._llm = FakeLLM(make_compliance_response())  # all clear compliance

    better = _sound_proposal(estimated_cost=40, expected_revenue=400)  # ROI 9
    worse = _sound_proposal(estimated_cost=40, expected_revenue=120)   # ROI 2
    results = gov.allocate([worse, better])

    # Ranked best-first; only the top fits the £50 budget.
    ranked = sorted(results, key=lambda r: r["rank"])
    assert ranked[0]["final_verdict"] == APPROVE
    assert ranked[0]["final_authority"] == "CEO"
    assert ranked[1]["final_verdict"] == REJECT


def test_allocate_excludes_compliance_vetoed(config, db):
    config.policy = dict(_POLICY)
    config.compliance = {"high_risk_threshold": 70, "medium_risk_threshold": 40,
                         "brand_consistency_min": 70}
    gov = Governance(config, db)
    # Every proposal in this batch trips a copyright veto.
    gov.compliance._llm = FakeLLM(make_compliance_response(copyright=95))

    results = gov.allocate([_sound_proposal(), _sound_proposal()])
    assert all(r["final_verdict"] == REJECT for r in results)
    assert all(r["final_authority"] == "Compliance" for r in results)
    assert all(r["ceo"] is None for r in results)  # CEO never funds a vetoed bid
