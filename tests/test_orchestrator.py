"""Tests for the Orchestrator — the content agents + marketing-content step.

There is one production workflow (the product-first Daily Cycle); the
orchestrator provides its **last** creative step, ``generate_marketing_content``,
which promotes an already-created product. (The full cycle is covered in
``tests/test_daily_cycle.py``.)
"""

from __future__ import annotations

from onassis.orchestrator import Orchestrator
from tests.conftest import FakeLLM, make_content_response


def _counts(config):
    t = config.content_targets
    return (
        t.get("pinterest_posts", 5),
        t.get("instagram_captions", 3),
        t.get("facebook_posts", 2),
        t.get("image_prompts", 3),
    )


def test_orchestrator_exposes_content_agents(config, db):
    orch = Orchestrator(config, db)
    for attr in ("director", "creator", "brain", "compliance", "campaigns",
                 "publisher", "analytics", "profit"):
        assert getattr(orch, attr) is not None


def test_generate_marketing_content_creates_and_persists(config, db, sample_brief):
    n_pin, n_ig, n_fb, n_img = _counts(config)
    expected = n_pin + n_ig + n_fb + n_img

    orch = Orchestrator(config, db)
    orch.creator._llm = FakeLLM(make_content_response(n_pin, n_ig, n_fb, n_img))
    sample_brief["id"] = db.insert_brief(sample_brief)

    result = orch.generate_marketing_content(sample_brief)

    assert len(result["items"]) == expected
    assert result["published"] == 0  # publisher is a placeholder
    # The content is persisted and belongs to the brief.
    assert len(db.get_content_for_brief(sample_brief["id"])) == expected


def test_no_content_first_pipeline_remains(config, db):
    # The divergent content-first entry point was removed — one workflow only.
    assert not hasattr(Orchestrator(config, db), "run_daily")
