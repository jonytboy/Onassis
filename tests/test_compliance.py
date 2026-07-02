"""Tests for the Compliance Director (LLM mocked; deterministic verdicts)."""

from __future__ import annotations

import pytest

from onassis.compliance import ComplianceDirector
from onassis.proposals import APPROVE, APPROVE_WITH_CHANGES, REJECT, Proposal
from tests.conftest import FakeLLM, make_compliance_response


@pytest.fixture
def director(config, db):
    config.compliance = {
        "high_risk_threshold": 70,
        "medium_risk_threshold": 40,
        "brand_consistency_min": 70,
    }
    return ComplianceDirector(config, db)


def _proposal() -> Proposal:
    return Proposal(
        agent_name="ContentCreator",
        requested_action="Use the slogan 'Just Do It' on a product",
        reasoning="It's catchy.",
    )


# --- Verdict rules (deterministic from risk scores) -----------------

def test_approves_low_risk(director):
    director._llm = FakeLLM(make_compliance_response())
    report = director.review_proposal(_proposal(), proposal_id=1)
    assert report["verdict"] == APPROVE
    assert report["compliance_score"] >= 80


def test_rejects_high_trademark_risk(director):
    director._llm = FakeLLM(make_compliance_response(trademark=85))
    report = director.review_proposal(_proposal(), proposal_id=1)
    assert report["verdict"] == REJECT


def test_rejects_high_copyright_risk(director):
    director._llm = FakeLLM(make_compliance_response(copyright=90))
    report = director.review_proposal(_proposal(), proposal_id=1)
    assert report["verdict"] == REJECT


def test_rejects_low_brand_consistency(director):
    director._llm = FakeLLM(make_compliance_response(brand=40))
    report = director.review_proposal(_proposal(), proposal_id=1)
    assert report["verdict"] == REJECT


def test_medium_risk_is_approve_with_changes(director):
    # No human to answer "more info" — medium risk becomes APPROVE_WITH_CHANGES.
    director._llm = FakeLLM(make_compliance_response(platform=55))
    report = director.review_proposal(_proposal(), proposal_id=1)
    assert report["verdict"] == APPROVE_WITH_CHANGES


def test_never_emits_request_more_info(director):
    # Across the whole risk range, compliance only ever speaks the three
    # autonomous verdicts — never REQUEST_MORE_INFO.
    for risk in range(0, 101, 5):
        director._llm = FakeLLM(make_compliance_response(platform=risk))
        v = director.review_proposal(_proposal(), proposal_id=1)["verdict"]
        assert v in (APPROVE, APPROVE_WITH_CHANGES, REJECT)


# --- Autonomous remediation loop (resolve) --------------------------

def test_resolve_amends_then_approves(director):
    # First review needs changes, the amended version is clean -> approved.
    director._llm = FakeLLM([
        make_compliance_response(platform=55, corrections=["Drop the risky phrase"]),
        make_compliance_response(),  # clean second pass
    ])
    seen_corrections = []

    def produce(corrections):
        seen_corrections.append(corrections)
        return {"v": len(seen_corrections)}

    outcome = director.resolve(produce, lambda a: director.review_proposal(_proposal()))
    assert outcome["status"] == "approved"
    assert outcome["attempts"] == 2
    # First call had no corrections; the second received the amendments to apply.
    assert seen_corrections[0] is None
    assert seen_corrections[1] == ["Drop the risky phrase"]


def test_resolve_stops_immediately_on_reject(director):
    director._llm = FakeLLM(make_compliance_response(copyright=95))  # hard reject
    calls = []
    outcome = director.resolve(
        lambda c: calls.append(c) or {}, lambda a: director.review_proposal(_proposal()))
    assert outcome["status"] == "rejected"
    assert outcome["attempts"] == 1          # no pointless regeneration after a veto


def test_resolve_is_bounded_when_never_clean(director):
    # Every pass still needs changes -> stop after max_remediation_attempts.
    director.max_remediation_attempts = 3
    director._llm = FakeLLM(make_compliance_response(platform=55,
                                                     corrections=["fix it"]))
    attempts = []
    outcome = director.resolve(
        lambda c: attempts.append(c) or {}, lambda a: director.review_proposal(_proposal()))
    assert outcome["status"] == "exhausted"
    assert outcome["attempts"] == 3
    assert len(attempts) == 3                 # bounded — it never loops forever
    assert outcome["missing"] == ["fix it"]   # states precisely what's still wrong


def test_risk_scores_are_clamped(director):
    director._llm = FakeLLM(make_compliance_response(trademark=250, brand=-10))
    report = director.review_proposal(_proposal(), proposal_id=1)
    assert report["trademark_risk"] == 100
    assert report["brand_consistency_score"] == 0


# --- Storage --------------------------------------------------------

def test_report_is_stored(director, db):
    director._llm = FakeLLM(make_compliance_response(corrections=["Use an original phrase"]))
    director.review_proposal(_proposal(), proposal_id=5)
    stored = db.get_compliance_for_proposal(5)
    assert stored is not None
    assert stored["corrections"] == ["Use an original phrase"]
    assert stored["reasoning"]


def test_review_campaign_stores_with_campaign_id(director, db):
    brief = {"brief_date": "2026-06-26", "theme": "T", "keywords": []}
    brief_id = db.insert_brief(brief)
    cid = db.insert_campaign({"name": "C", "brief_id": brief_id})
    director._llm = FakeLLM(make_compliance_response())
    report = director.review_campaign({"id": cid, "brief_id": brief_id, "name": "C"}, content=[])
    assert report["campaign_id"] == cid
    assert db.get_compliance_for_campaign(cid) is not None


# --- Learning from past decisions -----------------------------------

def test_prompt_includes_past_rejections(director, db):
    # Seed a prior rejection.
    db.insert_compliance_report(
        {
            "subject": "Mickey Mouse mug",
            "verdict": REJECT,
            "reasoning": "Copyrighted character — never use.",
            "compliance_score": 10,
            "trademark_risk": 90,
            "copyright_risk": 95,
            "platform_risk": 50,
            "brand_consistency_score": 30,
        }
    )
    director._llm = FakeLLM(make_compliance_response())
    director.review_proposal(_proposal(), proposal_id=2)
    prompt = director._llm.last_prompt
    assert "PAST COMPLIANCE DECISIONS" in prompt
    assert "Mickey Mouse mug" in prompt


def test_get_recent_rejections_excludes_approvals(db):
    db.insert_compliance_report({"subject": "ok", "verdict": APPROVE, "reasoning": "fine"})
    db.insert_compliance_report({"subject": "bad", "verdict": REJECT, "reasoning": "no"})
    rejections = db.get_recent_compliance_rejections()
    assert [r["subject"] for r in rejections] == ["bad"]
