"""Tests for AI cost accounting + the CFO (Sprint 42.2)."""

from __future__ import annotations

import pytest

from onassis.ai_accounting import (COST_TARGETS, cost_context, rate_cost,
                                    record_image, record_llm, set_recorder)
from onassis.cfo import CFOManager


@pytest.fixture(autouse=True)
def recorder(db):
    set_recorder(db)
    yield
    set_recorder(None)          # reset process-wide recorder between tests


def test_llm_and_image_costs_are_recorded(db):
    with cost_context(stage="Market Research", product_id="1-mug", campaign_id=1):
        c = record_llm(provider="anthropic", model="claude-opus-4-8",
                       input_tokens=1_000_000, output_tokens=0, duration_ms=10)
    assert c == pytest.approx(15.0)             # $15 / 1M input tokens (Opus)
    with cost_context(stage="Generate Master Artwork", product_id="1-mug", campaign_id=1):
        ci = record_image(provider="openai", model="gpt-image-1", quality="high", images=1)
    assert ci == pytest.approx(0.167)
    assert db.ai_spend_total() == pytest.approx(15.167)
    rows = db.ai_cost_by_product("1-mug")
    assert len(rows) == 2 and {r["kind"] for r in rows} == {"llm", "image"}


def test_failed_call_is_recorded_at_zero_cost(db):
    with cost_context(stage="Generate Master Artwork", product_id="1-mug"):
        c = record_image(provider="openai", model="gpt-image-1", quality="high",
                         images=0, ok=False, detail="Billing hard limit")
    assert c == 0.0
    rows = db.ai_cost_by_product("1-mug")
    assert rows[0]["cost_usd"] == 0.0


def test_cost_targets_rating():
    assert rate_cost(0.50) == "excellent"
    assert rate_cost(1.00) == "good"
    assert rate_cost(1.75) == "acceptable"
    assert rate_cost(2.50) == "investigate"
    assert rate_cost(3.50) == "critical"
    assert COST_TARGETS[0] == ("excellent", 0.75)


def test_cfo_dashboard_and_optimisation(config, db):
    with cost_context(stage="Generate Master Artwork", product_id="1-mug", campaign_id=1):
        record_image(provider="openai", model="gpt-image-1", quality="high", images=3)
    with cost_context(stage="Market Research", product_id="1-mug", campaign_id=1):
        record_llm(provider="anthropic", model="claude-opus-4-8",
                   input_tokens=10_000, output_tokens=2_000, duration_ms=5)
    cfo = CFOManager(config, db)
    d = cfo.dashboard()
    assert d["ai_spend_today"] > 0 and d["products_costed"] == 1
    assert d["breakdown"]["artwork"] == pytest.approx(0.501)     # 3 × $0.167
    # $0.501 artwork + $0.30 LLM = $0.801 for one product → "good" (<$1.25).
    assert d["cost_per_product_rating"] == "good"
    report = cfo.optimisation_report()
    # Image generation dominates → a saving recommendation is produced.
    assert any(r["area"] == "artwork" for r in report["recommendations"])
    assert report["largest_cost"] == "artwork"
