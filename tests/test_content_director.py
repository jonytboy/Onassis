"""Tests for the Content Director agent (LLM mocked)."""

from __future__ import annotations

from datetime import date

import pytest

from onassis.agents.content_director import ContentDirector, _season
from tests.conftest import FakeLLM


@pytest.fixture
def director(config, db, sample_brief):
    agent = ContentDirector(config, db)
    agent._llm = FakeLLM(sample_brief)  # inject the fake LLM
    return agent


def test_run_persists_brief_and_returns_id(director, db, sample_brief):
    brief = director.run(for_date=date(2026, 6, 26))

    assert "id" in brief
    assert brief["campaign_name"] == sample_brief["campaign_name"]
    assert brief["theme"] == sample_brief["theme"]
    assert brief["brand"] == "Local Celebrity"
    assert brief["season"] == "summer"

    stored = db.get_brief(brief["id"])
    assert stored is not None
    assert stored["theme"] == sample_brief["theme"]


def test_run_calls_llm_once_with_schema(director):
    director.run(for_date=date(2026, 6, 26))
    fake = director._llm
    assert len(fake.calls) == 1
    schema = fake.calls[0]["schema"]
    # The brief schema must require the core fields the agent reads back.
    for field in ("campaign_name", "theme", "tone", "objective", "keywords"):
        assert field in schema["required"]


def test_prompt_includes_brand_season_and_pillars(director):
    director.run(for_date=date(2026, 1, 15))  # winter
    prompt = director._llm.last_prompt
    assert "Local Celebrity" in prompt
    assert "winter" in prompt
    # at least one configured pillar should appear
    assert "Mediterranean" in prompt


def test_prompt_lists_previous_campaigns_to_avoid_repetition(config, db, sample_brief):
    # Seed a prior campaign.
    db.insert_brief(
        {
            "brief_date": "2026-06-25",
            "theme": "Lavender fields",
            "campaign_name": "Provence in Bloom",
            "keywords": [],
        }
    )
    agent = ContentDirector(config, db)
    agent._llm = FakeLLM(sample_brief)
    agent.run(for_date=date(2026, 6, 26))

    prompt = agent._llm.last_prompt
    assert "Provence in Bloom" in prompt
    assert "Lavender fields" in prompt


def test_prompt_handles_no_history(director):
    director.run(for_date=date(2026, 6, 26))
    assert "None yet" in director._llm.last_prompt


def test_run_defaults_to_today(director):
    brief = director.run()
    assert brief["brief_date"] == date.today().isoformat()


@pytest.mark.parametrize(
    "month, expected",
    [(1, "winter"), (2, "winter"), (3, "spring"), (5, "spring"),
     (6, "summer"), (8, "summer"), (9, "autumn"), (11, "autumn"), (12, "winter")],
)
def test_season_helper(month, expected):
    assert _season(date(2026, month, 15)) == expected
