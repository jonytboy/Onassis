"""Tests for the ONASSIS Brain and the knowledge persistence layer."""

from __future__ import annotations

import pytest

from onassis.brain import OnassisBrain, _clamp_confidence
from onassis.campaign_manager import CampaignManager
from tests.conftest import FakeLLM

_PREDICTION = {
    "hypothesis": "Slow Mediterranean content beats yacht aspiration because it feels authentic.",
    "variables": ["lifestyle", "dining", "golden hour", "natural light"],
    "predicted_outcome": "Higher saves and shares than the rolling average.",
    "confidence": 74,
    "success_metrics": ["Pinterest saves", "outbound clicks", "Instagram shares"],
    "recommendation": "If Pinterest saves beat the average by 20%, make three more slow-dining campaigns.",
}


def _campaign(db) -> dict:
    """Create a brief + campaign and return the campaign dict."""
    brief = {
        "brief_date": "2026-06-26",
        "theme": "Slow coastal afternoons",
        "campaign_name": "The Hour After Lunch",
        "concept": "The unhurried hour when no one leaves the table.",
        "keywords": ["slow", "dining"],
    }
    brief["id"] = db.insert_brief(brief)
    return CampaignManager(None, db).create_from_brief(brief)


@pytest.fixture
def brain(config, db) -> OnassisBrain:
    agent = OnassisBrain(config, db)
    agent._llm = FakeLLM(_PREDICTION)
    return agent


# --- Database-layer knowledge methods -------------------------------

def test_insert_and_get_knowledge_roundtrip(db):
    campaign = _campaign(db)
    kid = db.insert_knowledge({**_PREDICTION, "campaign_id": campaign["id"]})
    stored = db.get_knowledge(kid)
    assert stored["hypothesis"] == _PREDICTION["hypothesis"]
    assert stored["variables"] == _PREDICTION["variables"]  # JSON decoded
    assert stored["success_metrics"] == _PREDICTION["success_metrics"]
    assert stored["confidence"] == 74
    assert stored["status"] == "predicted"
    assert stored["observed_metrics"] == {}


def test_get_knowledge_for_campaign(db):
    campaign = _campaign(db)
    db.insert_knowledge({**_PREDICTION, "campaign_id": campaign["id"]})
    found = db.get_knowledge_for_campaign(campaign["id"])
    assert found is not None
    assert found["campaign_id"] == campaign["id"]


def test_update_knowledge_whitelist_and_json(db):
    campaign = _campaign(db)
    kid = db.insert_knowledge({**_PREDICTION, "campaign_id": campaign["id"]})

    changed = db.update_knowledge(
        kid,
        {
            "status": "validated",
            "actual_outcome": "Saves up 31%.",
            "observed_metrics": {"pinterest_saves": 412},
            "confidence": 88,
            "not_a_column": "ignored",
        },
    )
    assert changed is True
    updated = db.get_knowledge(kid)
    assert updated["status"] == "validated"
    assert updated["actual_outcome"] == "Saves up 31%."
    assert updated["observed_metrics"] == {"pinterest_saves": 412}
    assert updated["confidence"] == 88
    assert "not_a_column" not in updated


def test_update_knowledge_no_valid_fields_returns_false(db):
    campaign = _campaign(db)
    kid = db.insert_knowledge({**_PREDICTION, "campaign_id": campaign["id"]})
    assert db.update_knowledge(kid, {"bogus": 1}) is False


def test_get_campaigns_without_knowledge(db):
    c1 = _campaign(db)
    _campaign(db)  # second campaign, no knowledge
    db.insert_knowledge({**_PREDICTION, "campaign_id": c1["id"]})
    pending = db.get_campaigns_without_knowledge()
    assert len(pending) == 1
    assert pending[0]["id"] != c1["id"]


# --- Brain.generate_for_campaign ------------------------------------

def test_generate_for_campaign_persists_prediction(brain, db):
    campaign = _campaign(db)
    knowledge = brain.generate_for_campaign(campaign)

    assert knowledge["campaign_id"] == campaign["id"]
    assert knowledge["hypothesis"] == _PREDICTION["hypothesis"]
    assert knowledge["confidence"] == 74
    assert knowledge["status"] == "predicted"
    assert db.get_knowledge_for_campaign(campaign["id"]) is not None


def test_generate_for_campaign_is_idempotent(brain, db):
    campaign = _campaign(db)
    first = brain.generate_for_campaign(campaign)
    second = brain.generate_for_campaign(campaign)
    assert first["id"] == second["id"]
    assert len(db.list_knowledge()) == 1


def test_prompt_includes_campaign_context(brain, db):
    campaign = _campaign(db)
    brain.generate_for_campaign(campaign)
    prompt = brain._llm.last_prompt
    assert "The Hour After Lunch" in prompt
    assert "Slow coastal afternoons" in prompt


def test_confidence_is_clamped(config, db):
    campaign = _campaign(db)
    b = OnassisBrain(config, db)
    b._llm = FakeLLM({**_PREDICTION, "confidence": 250})
    knowledge = b.generate_for_campaign(campaign)
    assert knowledge["confidence"] == 100


@pytest.mark.parametrize(
    "value, expected", [(-5, 0), (0, 0), (50, 50), (100, 100), (150, 100), ("x", 50), (None, 50)]
)
def test_clamp_confidence(value, expected):
    assert _clamp_confidence(value) == expected


def test_generate_missing_backfills(brain, db):
    _campaign(db)
    _campaign(db)
    created = brain.generate_missing()
    assert created == 2
    assert len(db.list_knowledge()) == 2


# --- Future-analytics hook ------------------------------------------

def test_record_outcome_updates_prediction(brain, db):
    campaign = _campaign(db)
    brain.generate_for_campaign(campaign)

    updated = brain.record_outcome(
        campaign["id"],
        actual_outcome="Pinterest saves up 31% vs. average.",
        observed_metrics={"pinterest_saves": 412, "shares": 88},
        revised_confidence=90,
        status="validated",
    )
    assert updated["status"] == "validated"
    assert updated["actual_outcome"].startswith("Pinterest saves up 31%")
    assert updated["observed_metrics"]["pinterest_saves"] == 412
    assert updated["confidence"] == 90
    # updated_at should advance past created_at semantics (both present)
    assert updated["updated_at"] >= updated["created_at"]


def test_record_outcome_unknown_campaign_raises(brain):
    with pytest.raises(ValueError, match="No knowledge"):
        brain.record_outcome(999, actual_outcome="x")


# --- Read views -----------------------------------------------------

def test_get_for_campaign_is_read_only(config, db):
    campaign = _campaign(db)
    b = OnassisBrain(config, db)  # no _llm injected
    # No knowledge yet, and reading must NOT trigger generation (no LLM call).
    assert b.get_for_campaign(campaign["id"]) is None
