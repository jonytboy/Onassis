"""Tests for the governance chain — Compliance veto over the CEO."""

from __future__ import annotations

import pytest

from onassis.governance import Governance
from onassis.proposals import APPROVE, REJECT, REQUEST_MORE_INFO, Proposal
from tests.conftest import FakeLLM, make_compliance_response

_POLICY = {
    "available_cash": 10000,
    "cash_reserve": 5000,
    "min_profit_margin": 0.30,
    "max_ai_spend": 50,
    "max_experiment_budget": 200,
    "min_confidence": 50,
    "brand_consistency_min": 70,
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


def test_compliance_request_info_overrules(config, db):
    gov = _gov(config, db, make_compliance_response(platform=55))  # medium risk
    result = gov.submit(_sound_proposal())
    assert result["final_verdict"] == REQUEST_MORE_INFO
    assert result["final_authority"] == "Compliance"
    assert db.get_proposal(result["proposal_id"])["status"] == "needs_info"


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
