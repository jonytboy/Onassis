"""Tests for the Revenue Expansion Engine (product scoring + CEO launch + learning)."""

from __future__ import annotations

import pytest

from onassis.expansion import RevenueExpansionEngine
from onassis.revenue import RevenueEngine


@pytest.fixture
def engine(config, db):
    # Scoring/variety tests exercise the FULL catalogue; the shipped default is
    # now apparel-first (categories: [apparel]), so clear the allowlist here.
    config.expansion = {**config.expansion, "categories": []}
    return RevenueExpansionEngine(config, db)


# --- Catalogue ------------------------------------------------------

def test_catalogue_is_the_phase1_ten(engine):
    keys = [p["key"] for p in engine.catalogue()]
    assert len(keys) == 10
    assert set(keys) == {
        "premium_tshirt", "heavyweight_hoodie", "sweatshirt", "premium_poster",
        "framed_poster", "canvas", "ceramic_mug", "tote_bag",
        "hardcover_notebook", "greeting_card",
    }


def test_categories_allowlist_makes_an_apparel_only_store(config, db):
    """The shipped default (categories: [apparel]) builds ONLY clothing for new
    designs; clearing it restores the full catalogue."""
    apparel = RevenueExpansionEngine(config, db)   # config.yaml default
    keys = {p["key"] for p in apparel.catalogue()}
    assert keys == {"premium_tshirt", "heavyweight_hoodie", "sweatshirt"}
    config.expansion = {**config.expansion, "categories": []}
    assert len(RevenueExpansionEngine(config, db).catalogue()) == 10


def test_unavailable_product_is_skipped_gracefully(config, db):
    # Simulate a temporary Gelato outage for one product.
    cat = [dict(p) for p in config.expansion["catalogue"]]
    for p in cat:
        if p["key"] == "ceramic_mug":
            p["available"] = False
    config.expansion = {**config.expansion, "catalogue": cat, "categories": []}
    engine = RevenueExpansionEngine(config, db)

    keys = [p["key"] for p in engine.catalogue()]
    assert "ceramic_mug" not in keys and len(keys) == 9
    plan = engine.plan(1, store=False)
    assert "ceramic_mug" in plan["skipped_unavailable"]
    assert all(s["product_key"] != "ceramic_mug" for s in plan["scored"])


# --- Scoring --------------------------------------------------------

def test_score_design_ranks_and_clamps(engine):
    scored = engine.score_design()
    assert len(scored) == 10
    values = [s["composite_score"] for s in scored]
    assert values == sorted(values, reverse=True)          # best first
    for s in scored:
        for field in ("brand_fit", "commercial_suitability", "estimated_conversion",
                      "historical_performance", "composite_score"):
            assert 0 <= s[field] <= 100
        assert "expected_profit" in s and "production_cost" in s and "retail_price" in s


# --- Launch plan (CEO decides) --------------------------------------

def test_plan_launches_only_the_commercial_set(engine, db):
    plan = engine.plan(1)

    # Not everything launches — the objective isn't product count.
    assert 1 <= plan["products_launched"] < plan["products_scored"]
    launched_keys = {s["product_key"] for s in plan["launched"]}
    # The strongest Mediterranean products lead; the weakest never launch.
    assert "ceramic_mug" in launched_keys
    assert "hardcover_notebook" not in launched_keys
    # A product the CEO rejects is NEVER launched, whatever the mode.
    for s in plan["launched"]:
        assert s["ceo_verdict"] == "APPROVE"
    for s in plan["scored"]:
        if s["ceo_verdict"] != "APPROVE":
            assert not s["launched"]


def test_cold_start_guarantees_min_variants(engine, db):
    """Cold start: fewer than min_variants clear the threshold, so the engine
    tops up with the best CEO-approved products to reach the minimum."""
    plan = engine.plan(1)
    assert plan["selection_mode"] == "cold_start"
    assert engine.min_variants <= plan["products_launched"] <= engine.max_variants
    # The two threshold-clearers plus a top-up to reach min_variants (3).
    launched = plan["launched"]
    assert sum(s["composite_score"] >= engine.threshold for s in launched) >= 2
    # The top-up product is below threshold but still CEO-approved.
    below = [s for s in launched if s["composite_score"] < engine.threshold]
    assert below and all(s["ceo_verdict"] == "APPROVE" for s in below)


def test_build_mode_fills_category_gaps_first(config, db):
    """Sprint 45 — in build mode the Catalogue Manager steers the launch: an
    under-represented category leads even over a higher-scoring saturated one, so
    ONASSIS builds breadth instead of re-launching the same families."""
    # Targets: Mugs satisfied by one existing product; T-Shirts wide open.
    config.catalogue = {"targets": {"Mugs": 1, "T-Shirts": 25}}
    db.insert_product({"sku": "9-ceramic_mug", "name": "Ceramic Mug",
                       "campaign_id": 9, "product_key": "ceramic_mug"})
    engine = RevenueExpansionEngine(config, db)
    ceo_ok = [
        {"product_key": "ceramic_mug", "product_name": "Ceramic Mug", "composite_score": 95},
        {"product_key": "premium_tshirt", "product_name": "Premium T-Shirt",
         "composite_score": 70},
    ]
    ordered = engine._catalogue_reorder(list(ceo_ok))
    # The empty T-Shirts category leads despite the mug's higher score.
    assert ordered[0]["product_key"] == "premium_tshirt"
    assert ordered[-1]["product_key"] == "ceramic_mug"


def test_variants_capped_at_max(config, db):
    """Even with a very low threshold, no more than max_variants launch."""
    config.expansion = {**config.expansion, "score_threshold": 1, "max_variants": 4,
                        "categories": []}
    engine = RevenueExpansionEngine(config, db)
    plan = engine.plan(1)
    assert plan["selection_mode"] == "threshold"
    assert plan["products_launched"] == 4  # capped, not all ten CEO-approved


def test_cold_start_never_launches_a_ceo_rejection(config, db):
    """With cold start on but every product rejected, nothing launches."""
    from onassis.proposals import REJECT

    class _RejectAll:
        def evaluate(self, proposal, store=False):
            return {"verdict": REJECT, "reasoning": "policy: reject all"}

    engine = RevenueExpansionEngine(config, db)
    engine.ceo = _RejectAll()
    plan = engine.plan(1)
    assert plan["products_launched"] == 0
    assert all(not s["launched"] for s in plan["scored"])


def test_plan_records_scores_and_registers_products(engine, db):
    plan = engine.plan(1)
    # Every scored product is recorded.
    assert len(db.list_product_scores(1)) == plan["products_scored"]
    # Launched products become Product investments (with their product_key).
    products = {p["product_key"]: p for p in db.list_products()}
    for s in plan["launched"]:
        assert s["product_key"] in products
        assert products[s["product_key"]]["marketplace"] == "gelato"


def test_threshold_is_configurable(config, db):
    # Nothing qualifies and cold start is off -> a pure threshold launch of 0.
    config.expansion = {**config.expansion, "score_threshold": 101, "cold_start": False}
    engine = RevenueExpansionEngine(config, db)
    plan = engine.plan(1)
    assert plan["selection_mode"] == "threshold"
    assert plan["products_launched"] == 0
    assert all(not s["launched"] for s in plan["scored"])


# --- Learning from sales --------------------------------------------

def test_learning_raises_winners_and_lowers_losers(config, db):
    config.expansion = {**config.expansion, "categories": []}   # score full catalogue
    engine = RevenueExpansionEngine(config, db)
    revenue = RevenueEngine(config, db)

    # A mug that sells profitably; a notebook that loses money.
    db.insert_product({"sku": "mug1", "name": "Mug", "campaign_id": 1,
                       "product_key": "ceramic_mug", "production_cost": 7.5})
    db.insert_product({"sku": "nb1", "name": "Notebook", "campaign_id": 1,
                       "product_key": "hardcover_notebook", "production_cost": 8.5})
    for i in range(5):
        revenue.record_order({"order_ref": f"m{i}", "product_id": "mug1",
                              "platform": "etsy", "sale_price": 22, "quantity": 1,
                              "production_cost": 7.5})
    revenue.record_order({"order_ref": "n1", "product_id": "nb1", "platform": "etsy",
                          "sale_price": 24, "quantity": 1, "production_cost": 8.5,
                          "advertising_cost": 40})  # a loss

    result = engine.learn_from_sales()
    perf = {p["product_key"]: p for p in result["performance"]}
    assert perf["ceramic_mug"]["net_profit"] > 0
    assert perf["hardcover_notebook"]["net_profit"] < 0

    scored = {s["product_key"]: s for s in engine.score_design()}
    # The winner's historical score rose above neutral; the loser's fell below.
    assert scored["ceramic_mug"]["historical_performance"] > 50
    assert scored["hardcover_notebook"]["historical_performance"] < 50


def test_learning_is_idempotent(config, db):
    engine = RevenueExpansionEngine(config, db)
    revenue = RevenueEngine(config, db)
    db.insert_product({"sku": "mug1", "name": "Mug", "campaign_id": 1,
                       "product_key": "ceramic_mug", "production_cost": 7.5})
    revenue.record_order({"order_ref": "m1", "product_id": "mug1", "platform": "etsy",
                          "sale_price": 22, "quantity": 2, "production_cost": 7.5})
    def _data(perf):
        return [{k: v for k, v in p.items() if k != "updated_at"} for p in perf]

    first = engine.learn_from_sales()["performance"]
    second = engine.learn_from_sales()["performance"]
    assert _data(first) == _data(second)  # recomputed, not double-counted
    assert second[0]["units_sold"] == 2   # not 4
