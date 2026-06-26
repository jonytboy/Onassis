"""Tests for the daily pipeline orchestration (all LLMs mocked)."""

from __future__ import annotations

from datetime import date

from onassis.orchestrator import Orchestrator
from tests.conftest import FakeLLM, make_content_response

_PREDICTION = {
    "hypothesis": "Authentic slow-living content outperforms aspirational yacht content.",
    "variables": ["lifestyle", "dining", "golden hour"],
    "predicted_outcome": "Saves and shares above the rolling average.",
    "confidence": 74,
    "success_metrics": ["Pinterest saves", "Instagram shares"],
    "recommendation": "If saves beat the average by 20%, make three more slow-dining campaigns.",
}


def _counts(config):
    t = config.content_targets
    return (
        t.get("pinterest_posts", 5),
        t.get("instagram_captions", 3),
        t.get("facebook_posts", 2),
        t.get("image_prompts", 3),
    )


def _wired(config, db, brief):
    """Build an orchestrator with the Director, Creator, and Brain LLMs faked."""
    n_pin, n_ig, n_fb, n_img = _counts(config)
    orch = Orchestrator(config, db)
    orch.director._llm = FakeLLM(brief)
    orch.creator._llm = FakeLLM(make_content_response(n_pin, n_ig, n_fb, n_img))
    orch.brain._llm = FakeLLM(_PREDICTION)
    return orch


def test_run_daily_wires_agents_end_to_end(config, db, sample_brief):
    n_pin, n_ig, n_fb, n_img = _counts(config)
    expected = n_pin + n_ig + n_fb + n_img

    orch = _wired(config, db, sample_brief)
    summary = orch.run_daily(for_date=date(2026, 6, 26))

    assert summary["theme"] == sample_brief["theme"]
    assert summary["items_created"] == expected
    assert summary["published"] == 0  # publisher is a placeholder
    assert summary["analytics"]["items_this_brief"] == expected

    stored = db.get_content_for_brief(summary["brief_id"])
    assert len(stored) == expected


def test_run_daily_persists_one_brief_per_run(config, db, sample_brief):
    orch = _wired(config, db, sample_brief)
    orch.run_daily(for_date=date(2026, 6, 26))
    orch.run_daily(for_date=date(2026, 6, 27))
    assert len(db.get_recent_briefs()) == 2


def test_run_daily_creates_campaign_with_content(config, db, sample_brief):
    n_pin, n_ig, n_fb, n_img = _counts(config)
    expected = n_pin + n_ig + n_fb + n_img

    orch = _wired(config, db, sample_brief)
    summary = orch.run_daily(for_date=date(2026, 6, 26))

    assert "campaign_id" in summary
    view = orch.campaigns.get_campaign(summary["campaign_id"])
    assert view is not None
    assert view["name"] == sample_brief["campaign_name"]
    assert view["status"] == "Draft"
    assert len(view["content_ids"]) == expected  # all content belongs to the campaign


def test_run_daily_generates_knowledge_for_campaign(config, db, sample_brief):
    orch = _wired(config, db, sample_brief)
    summary = orch.run_daily(for_date=date(2026, 6, 26))

    assert "knowledge_id" in summary
    assert summary["confidence"] == _PREDICTION["confidence"]

    knowledge = orch.brain.get_for_campaign(summary["campaign_id"])
    assert knowledge is not None
    assert knowledge["hypothesis"] == _PREDICTION["hypothesis"]
    assert knowledge["status"] == "predicted"
