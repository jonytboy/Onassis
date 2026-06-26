"""Tests for the daily pipeline orchestration (both LLMs mocked)."""

from __future__ import annotations

from datetime import date

from onassis.orchestrator import Orchestrator
from tests.conftest import FakeLLM, make_content_response


def test_run_daily_wires_agents_end_to_end(config, db, sample_brief):
    n_pin = config.content_targets.get("pinterest_posts", 5)
    n_ig = config.content_targets.get("instagram_captions", 3)
    n_fb = config.content_targets.get("facebook_posts", 2)
    n_img = config.content_targets.get("image_prompts", 3)
    expected = n_pin + n_ig + n_fb + n_img

    orch = Orchestrator(config, db)
    orch.director._llm = FakeLLM(sample_brief)
    orch.creator._llm = FakeLLM(make_content_response(n_pin, n_ig, n_fb, n_img))

    summary = orch.run_daily(for_date=date(2026, 6, 26))

    assert summary["theme"] == sample_brief["theme"]
    assert summary["items_created"] == expected
    assert summary["published"] == 0  # publisher is a placeholder
    assert summary["analytics"]["items_this_brief"] == expected

    # Everything persisted under the one brief.
    stored = db.get_content_for_brief(summary["brief_id"])
    assert len(stored) == expected


def test_run_daily_persists_one_brief_per_run(config, db, sample_brief):
    orch = Orchestrator(config, db)
    orch.director._llm = FakeLLM(sample_brief)
    orch.creator._llm = FakeLLM(make_content_response(1, 1, 1, 1))

    orch.run_daily(for_date=date(2026, 6, 26))
    orch.run_daily(for_date=date(2026, 6, 27))

    assert len(db.get_recent_briefs()) == 2


def test_run_daily_creates_campaign_with_content(config, db, sample_brief):
    n = (
        config.content_targets.get("pinterest_posts", 5)
        + config.content_targets.get("instagram_captions", 3)
        + config.content_targets.get("facebook_posts", 2)
        + config.content_targets.get("image_prompts", 3)
    )
    orch = Orchestrator(config, db)
    orch.director._llm = FakeLLM(sample_brief)
    orch.creator._llm = FakeLLM(
        make_content_response(
            config.content_targets.get("pinterest_posts", 5),
            config.content_targets.get("instagram_captions", 3),
            config.content_targets.get("facebook_posts", 2),
            config.content_targets.get("image_prompts", 3),
        )
    )

    summary = orch.run_daily(for_date=date(2026, 6, 26))

    assert "campaign_id" in summary
    view = orch.campaigns.get_campaign(summary["campaign_id"])
    assert view is not None
    assert view["name"] == sample_brief["campaign_name"]
    assert view["status"] == "Draft"
    assert len(view["content_ids"]) == n  # all content belongs to the campaign
