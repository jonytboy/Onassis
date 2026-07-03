"""Tests for the Market Intelligence Engine (research before invention)."""

from __future__ import annotations

import pytest

from onassis.market_intelligence import (
    HIGH, LOW, VERY_HIGH, MarketIntelligence, _band,
)
from tests.conftest import FakeSignals


@pytest.fixture
def engine(config, db):
    return MarketIntelligence(config, db, signals_provider=FakeSignals())


# --- Opportunity banding (matches the report spec) ------------------

def test_band_thresholds():
    assert _band(45) == VERY_HIGH
    assert _band(38) == HIGH
    assert _band(15) == "MEDIUM"
    assert _band(9) == LOW


def test_research_scores_and_ranks_by_opportunity(engine, db):
    result = engine.research()
    assert result["count"] == 3
    kws = result["keywords"]
    # Best opportunity first: the wide-open niche outranks the saturated one.
    assert kws[0]["keyword"] in ("Slow Living Kitchen Print", "Greek Island Tote")
    assert kws[-1]["keyword"] == "Lemon Tote"
    # The saturated, high-competition keyword is a LOW opportunity.
    lemon = next(k for k in kws if k["keyword"] == "Lemon Tote")
    assert lemon["competition"] > lemon["demand"]
    assert lemon["opportunity"] == LOW
    # Every row carries demand, competition, opportunity band and price signals.
    for k in kws:
        assert 0 <= k["demand"] <= 100 and 0 <= k["competition"] <= 100
        assert k["opportunity"] in (VERY_HIGH, HIGH, "MEDIUM", LOW)
        assert k["avg_selling_price"] > 0 and k["est_monthly_sales"] >= 0


def test_research_is_persisted_and_readable(engine, db):
    engine.research()
    latest = engine.latest()
    assert len(latest) == 3
    assert latest[0]["opportunity_score"] >= latest[-1]["opportunity_score"]


def test_top_filters_by_minimum_band(engine):
    engine.research()
    high = engine.top(5, min_band="HIGH")
    assert high and all(k["opportunity"] in (VERY_HIGH, HIGH) for k in high)
    assert all(k["keyword"] != "Lemon Tote" for k in high)   # LOW excluded


def test_latest_report_supersedes_older_ones(config, db):
    MarketIntelligence(config, db, signals_provider=FakeSignals([
        {"keyword": "Old Niche", "product_type": "print", "theme": "x",
         "search_demand": 50, "bestseller_frequency": 50, "seasonal_trend": 50,
         "pinterest_trend": 50, "google_trend": 50, "competition": 50, "saturation": 50,
         "keyword_difficulty": 50, "review_velocity": 50, "avg_selling_price": 20,
         "est_monthly_sales": 10, "competitor_count": 100, "review_count": 100,
         "rationale": "old"}])).research()
    MarketIntelligence(config, db, signals_provider=FakeSignals()).research()   # newer
    keywords = {k["keyword"] for k in MarketIntelligence(config, db).latest()}
    assert "Old Niche" not in keywords          # only the latest report is returned


def test_format_report_reads_like_the_spec(engine):
    engine.research()
    text = engine.format_report()
    assert "MARKET INTELLIGENCE REPORT" in text
    assert "Demand:" in text and "Competition:" in text and "Opportunity:" in text
