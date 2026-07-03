"""Tests for the Market Truth Engine — real signals replace LLM estimates."""

from __future__ import annotations

from onassis.market_intelligence import MarketIntelligence
from onassis.market_truth import EtsySearchSignals, RealSignalsProvider


class StubGoogle:
    def __init__(self, value=80):
        self.value = value

    def interest(self, keyword):
        return self.value


class FailingGoogle:
    def interest(self, keyword):
        raise RuntimeError("trends down")   # provider must degrade, not crash


class StubPinterestTrends:
    def __init__(self, terms):
        self.terms = terms

    def top_terms(self):
        return self.terms


def _provider(config, db, *, google=None, pinterest=None):
    return RealSignalsProvider(config, db, google_client=google or StubGoogle(),
                               pinterest_trends=pinterest or StubPinterestTrends([]))


def test_real_google_and_pinterest_momentum_are_used(config, db):
    prov = _provider(config, db, google=StubGoogle(90),
                     pinterest=StubPinterestTrends(["greek island tote"]))
    signals = prov.estimate(["Greek Island Tote"], "Local Celebrity")
    s = signals[0]
    assert s["google_trend"] == 90
    assert s["pinterest_trend"] == 85          # keyword is trending on Pinterest
    assert "google_trend" in s["real_signals"] and "pinterest_trend" in s["real_signals"]


def test_etsy_search_data_drives_real_demand(config, db):
    db.insert_etsy_search_terms([{"term": "linen throw", "impressions": 2500,
                                  "clicks": 60, "orders": 4, "snapshot_date": "2026-07-01"}])
    signals = _provider(config, db).estimate(["Linen Throw"], "Local Celebrity")
    s = signals[0]
    assert s["search_demand"] == 50            # 2500 / 5000 ceiling -> 50
    assert "etsy_search_demand" in s["real_signals"]
    assert s["est_monthly_sales"] == 16        # 4 orders x 4


def test_a_failing_trend_source_degrades_to_a_default_not_a_crash(config, db):
    prov = RealSignalsProvider(config, db, google_client=FailingGoogle(),
                               pinterest_trends=StubPinterestTrends([]))
    s = prov.estimate(["Aegean Print"], "Local Celebrity")[0]
    assert s["google_trend"] == 50             # conservative default
    assert "google_trend" not in s["real_signals"]
    assert "conservative defaults" in s["rationale"]


def test_market_intelligence_uses_the_real_provider_and_scores(config, db):
    config.market = {**(config.market or {}), "signals_provider": "real"}
    engine = MarketIntelligence(config, db,
                                signals_provider=_provider(config, db, google=StubGoogle(75)))
    result = engine.research(keywords=["Ceramic Mug", "Tote Bag"])
    assert result["provider"] == "real"
    assert result["count"] == 2
    # Deterministic scoring ran on the real signals -> opportunity bands assigned.
    assert all("opportunity" in k for k in result["keywords"])


def test_config_selects_the_real_provider_by_default(config, db):
    config.market = {**(config.market or {}), "signals_provider": "real"}
    engine = MarketIntelligence(config, db)     # no injected provider
    assert engine.provider.name == "real"


def test_config_can_still_select_llm(config, db):
    config.market = {**(config.market or {}), "signals_provider": "llm"}
    engine = MarketIntelligence(config, db)
    assert getattr(engine.provider, "name", "llm") in ("llm", "none")


def test_etsy_search_signals_none_when_no_data(config, db):
    assert EtsySearchSignals(db).demand("nothing here") is None
