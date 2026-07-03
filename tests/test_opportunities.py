"""Tests for the Product Opportunity Engine (discovery + ranked backlog).

The LLM is faked; scoring, dedup, ranking, and CEO selection are deterministic.
The engine must never produce images, mock-ups, or listings.
"""

from __future__ import annotations

from typing import Any

import pytest

from onassis.market_intelligence import MarketIntelligence
from onassis.opportunities import OpportunityEngine, dedupe_key
from onassis.proposals import APPROVE
from tests.conftest import FakeLLM, FakeSignals


def _item(**overrides: Any) -> dict[str, Any]:
    """A complete raw LLM opportunity item; high-scoring by default."""
    base = {
        "theme": "Slow coastal mornings",
        "target_customer": "design-loving travellers",
        "emotional_angle": "calm, unhurried luxury",
        "product_type": "linen tea towel",
        "search_intent": "mediterranean kitchen linen",
        "seasonal_relevance": "spring/summer",
        "commercial_score": 85,
        "originality_score": 80,
        "brand_fit_score": 90,
        "estimated_demand": 80,
        "estimated_competition": 30,
        "confidence": 80,
        "product_name": "Amalfi Morning Linen",
        "concept": "A hand-finished linen tea towel evoking slow Amalfi mornings.",
        "colour_palette": ["citrus yellow", "whitewash", "sea blue"],
        "typography_style": "elegant serif",
        "illustration_style": "soft watercolour",
        "photography_style": "natural morning light",
        "mockup_style": "sunlit terrace flatlay",
    }
    base.update(overrides)
    return base


def _engine(config, db, items: list[dict[str, Any]]) -> OpportunityEngine:
    eng = OpportunityEngine(config, db)
    eng._llm = FakeLLM({"opportunities": items})
    eng.market.provider = FakeSignals()
    return eng


def test_opportunities_are_drawn_from_the_market_report(config, db):
    # Research the market first; the opportunity prompt must be seeded with the
    # highest-opportunity keywords — never a blank-slate vacuum.
    MarketIntelligence(config, db, signals_provider=FakeSignals()).research()
    eng = _engine(config, db, [_item()])
    eng.generate(3)
    prompt = eng._llm.last_prompt
    assert "MARKET INTELLIGENCE" in prompt
    assert "Greek Island Tote" in prompt        # a HIGH-opportunity keyword
    assert "Slow Living Kitchen Print" in prompt
    assert "Lemon Tote" not in prompt           # LOW opportunity — excluded


def test_generation_without_a_report_still_works(config, db):
    # No market report yet -> the prompt simply omits the market block (the daily
    # cycle's Market Research stage supplies it in production).
    eng = _engine(config, db, [_item()])
    result = eng.generate(1)
    assert result["generated"] == 1
    assert "MARKET INTELLIGENCE" not in eng._llm.last_prompt


# --- Generation + storage -------------------------------------------

def test_generate_stores_all_fields(config, db):
    eng = _engine(config, db, [_item()])
    result = eng.generate(1)
    assert result["generated"] == 1
    opp = result["opportunities"][0]

    # Every required commercial + creative field is present.
    for field in (
        "opportunity_id", "brand", "theme", "target_customer", "emotional_angle",
        "product_type", "search_intent", "seasonal_relevance", "commercial_score",
        "originality_score", "brand_fit_score", "estimated_demand",
        "estimated_competition", "confidence", "product_name", "concept",
        "colour_palette", "typography_style", "illustration_style",
        "photography_style", "mockup_style", "expected_value",
    ):
        assert field in opp, f"missing {field}"
    assert opp["opportunity_id"].startswith("OPP-")
    assert opp["brand"] == config.brand.get("name")
    assert opp["status"] == "backlog"
    # Persisted and retrievable.
    assert db.get_opportunity(opp["opportunity_id"])["product_name"] == "Amalfi Morning Linen"


def test_no_images_mockups_or_listings_are_produced(config, db):
    eng = _engine(config, db, [_item()])
    opp = eng.generate(1)["opportunities"][0]
    # The engine only describes creative direction — it produces no assets.
    assert "images" not in opp and "image_order" not in opp
    assert "listing" not in opp and "manifest" not in opp
    assert isinstance(opp["mockup_style"], str)  # a style hint, not a mock-up


# --- Deduplication --------------------------------------------------

def test_duplicate_concepts_in_one_batch_are_skipped(config, db):
    dup = _item()  # identical concept fingerprint
    eng = _engine(config, db, [_item(), dup])
    result = eng.generate(2)
    assert result["generated"] == 1
    assert result["duplicates_skipped"] == 1


def test_duplicates_against_existing_backlog_are_skipped(config, db):
    _engine(config, db, [_item()]).generate(1)
    # A second run yielding the same concept must not create a duplicate.
    result = _engine(config, db, [_item()]).generate(1)
    assert result["generated"] == 0
    assert result["duplicates_skipped"] == 1
    assert db.count_opportunities() == 1


def test_dedupe_key_normalises():
    a = dedupe_key("Linen Tea-Towel", "Slow  Mornings", "Calm!")
    b = dedupe_key("linen tea towel", "slow mornings", "calm")
    assert a == b


# --- Scoring + ranking ----------------------------------------------

def test_expected_value_is_deterministic(config, db):
    eng = OpportunityEngine(config, db)
    opp = {
        "commercial_score": 80, "originality_score": 60, "brand_fit_score": 70,
        "estimated_demand": 50, "estimated_competition": 40, "confidence": 100,
    }
    # base = .35*80 + .15*60 + .20*70 + .20*50 + .10*(100-40) = 28+9+14+10+6 = 67
    # weights sum to 1.0; confidence 100% -> 67.0
    assert eng.expected_value(opp) == pytest.approx(67.0)


def test_backlog_is_ranked_by_expected_value(config, db):
    low = _item(product_type="ceramic mug", theme="evening",
                emotional_angle="cosy", commercial_score=30, originality_score=30,
                brand_fit_score=30, estimated_demand=30, estimated_competition=80,
                confidence=40, product_name="Low Idea")
    high = _item(product_name="High Idea")
    eng = _engine(config, db, [low, high])
    result = eng.generate(2)
    assert [o["product_name"] for o in result["opportunities"]] == ["High Idea", "Low Idea"]
    # top() returns the best first.
    assert eng.top(limit=1)[0]["product_name"] == "High Idea"


# --- Selection: Optimiser + CEO choose from the ranked queue --------

def test_select_next_high_value_is_approved_and_marked_selected(config, db):
    eng = _engine(config, db, [_item()])
    eng.generate(1)
    choice = eng.select_next(agent_name="ProductOptimiser")

    assert choice is not None
    assert choice["ceo"]["verdict"] == APPROVE
    assert choice["opportunity"]["status"] == "selected"
    assert choice["opportunity"]["selected_by"] == "ProductOptimiser"
    assert db.count_opportunities(status="selected") == 1


def test_select_next_low_value_stays_in_backlog(config, db):
    poor = _item(commercial_score=10, originality_score=10, brand_fit_score=10,
                 estimated_demand=10, estimated_competition=90, confidence=30,
                 product_name="Poor Idea")
    eng = _engine(config, db, [poor])
    eng.generate(1)
    choice = eng.select_next()
    assert choice["ceo"]["verdict"] != APPROVE
    assert choice["opportunity"]["status"] == "backlog"
    assert db.count_opportunities(status="selected") == 0


def test_select_next_empty_backlog_returns_none(config, db):
    assert OpportunityEngine(config, db).select_next() is None


def test_optimiser_delegates_to_the_queue(config, db):
    from onassis.optimiser import ProductOptimiser

    _engine(config, db, [_item()]).generate(1)
    choice = ProductOptimiser(config, db).next_opportunity()
    assert choice is not None
    assert choice["opportunity"]["opportunity_id"].startswith("OPP-")
    assert choice["proposal"]["agent_name"] == "ProductOptimiser"


def test_overview_counts(config, db):
    eng = _engine(config, db, [_item(), _item(product_type="poster", theme="coast",
                                              emotional_angle="bold")])
    eng.generate(2)
    ov = eng.overview()
    assert ov["total"] == 2 and ov["backlog"] == 2 and ov["selected"] == 0
