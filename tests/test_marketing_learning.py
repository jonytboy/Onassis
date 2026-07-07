"""Tests for the Marketing Learning Loop (Sprint 42 Phase 5)."""

from __future__ import annotations

from onassis.marketing_learning import MarketingLearning


def _seed_channel(db, source, clicks, sales, impressions=1000):
    db.insert_traffic_funnel({"funnel_date": "2026-07-01", "product_key": "mug",
                              "source": source, "impressions": impressions,
                              "clicks": clicks, "visits": clicks, "sales": sales})


def test_effectiveness_ranks_channels(config, db):
    _seed_channel(db, "pinterest", clicks=50, sales=4)
    _seed_channel(db, "facebook", clicks=10, sales=0)
    eff = MarketingLearning(config, db).effectiveness()
    assert eff[0]["channel"] == "pinterest"          # highest effectiveness first
    assert eff[0]["effectiveness"] > eff[1]["effectiveness"]


def test_records_and_compares_over_time(config, db):
    ml = MarketingLearning(config, db)
    _seed_channel(db, "pinterest", clicks=10, sales=0)
    ml.record("2026-07-01")                           # baseline
    # Pinterest improves the next period.
    _seed_channel(db, "pinterest", clicks=90, sales=6)
    d = ml.digest("2026-07-08")
    pin = next(c for c in d["channels"] if c["channel"] == "pinterest")
    assert pin["trend"] == "improving"
    assert d["best_channel"] == "pinterest"
    assert any("effective" in x for x in d["learnings"])


def test_recommended_budget_weights_to_performers(config, db):
    _seed_channel(db, "pinterest", clicks=100, sales=10)
    _seed_channel(db, "email", clicks=0, sales=0)
    d = MarketingLearning(config, db).digest("2026-07-01")
    assert d["recommended_budget"]["pinterest"] > d["recommended_budget"]["email"]
    assert abs(sum(d["recommended_budget"].values()) - 100.0) < 1.0


def test_empty_is_safe(config, db):
    d = MarketingLearning(config, db).digest("2026-07-01")
    assert d["best_channel"] is None and d["learnings"]
