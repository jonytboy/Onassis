"""Tests for the Catalogue & Portfolio Manager (Sprint 44)."""

from __future__ import annotations

from onassis.catalogue import CatalogueManager, category_of


def _seed(db, key, name=None, campaign_id=1, i=0):
    db.insert_product({"sku": f"{campaign_id}-{key}-{i}", "name": name or key,
                       "campaign_id": campaign_id, "product_key": key})


def _campaign(db, name="Mediterranean Morning"):
    bid = db.insert_brief({"brief_date": "2026-07-01", "theme": "x", "keywords": []})
    return db.insert_campaign({"name": name, "brief_id": bid})


def test_category_mapping():
    assert category_of("ceramic_mug") == "Mugs"
    assert category_of("premium_tshirt") == "T-Shirts"
    assert category_of("heavyweight_hoodie") == "Hoodies"
    assert category_of("linen_tea_towel") == "Tea Towels"
    assert category_of("olive_wood_board") == "Olive Boards"
    assert category_of("premium_poster") == "Posters"
    assert category_of("canvas_tote") == "Tote Bags"


def test_gap_analysis_and_mode(config, db):
    cid = _campaign(db)
    for i in range(3):
        _seed(db, "ceramic_mug", campaign_id=cid, i=i)
    cm = CatalogueManager(config, db)
    ga = cm.gap_analysis()
    assert ga["mode"] == "build"                         # nothing near target
    cats = {r["category"]: r for r in ga["categories"]}
    assert cats["Mugs"]["current"] == 3 and cats["Mugs"]["remaining"] == 27
    assert cats["Tea Towels"]["status"] == "critical"    # zero made
    assert cats["Mugs"]["status"] in ("priority", "building")
    assert "Tea Towels" in ga["prioritise"]


def test_saturated_category_is_suspended(config, db):
    # Override targets to a tiny number so a category saturates.
    config.catalogue = {"targets": {"Mugs": 2, "T-Shirts": 5}}
    cid = _campaign(db)
    for i in range(3):
        _seed(db, "ceramic_mug", campaign_id=cid, i=i)     # 3 mugs, target 2
    cm = CatalogueManager(config, db)
    ga = cm.gap_analysis()
    assert "Mugs" in ga["suspend"]
    assert "Mugs" in cm.saturated_categories()
    m = {r["category"]: r for r in ga["categories"]}["Mugs"]
    assert m["status"] == "complete" and m["remaining"] == 0


def test_mode_switches_to_optimise_when_all_targets_met(config, db):
    config.catalogue = {"targets": {"Mugs": 1, "Posters": 1}}
    cid = _campaign(db)
    _seed(db, "ceramic_mug", campaign_id=cid, i=0)
    _seed(db, "premium_poster", campaign_id=cid, i=1)
    assert CatalogueManager(config, db).mode() == "optimise"


def test_health_score_shape(config, db):
    cid = _campaign(db)
    _seed(db, "ceramic_mug", campaign_id=cid, i=0)
    h = CatalogueManager(config, db).health()
    for k in ("category_balance", "diversity", "coverage",
              "collection_completeness", "commercial_readiness", "overall"):
        assert 0 <= h[k] <= 100


def test_collection_dashboard(config, db):
    cid = _campaign(db)
    for i, k in enumerate(["ceramic_mug", "premium_tshirt", "linen_apron"]):
        _seed(db, k, campaign_id=cid, i=i)
    cols = CatalogueManager(config, db).collection_dashboard()
    assert len(cols) == 1
    assert cols[0]["products"] == 3 and cols[0]["category_count"] == 3
    assert cols[0]["name"].endswith("Collection") and cols[0]["status"] == "building"


def test_build_ranking_prefers_gap_categories(config, db):
    config.catalogue = {"targets": {"Mugs": 1, "T-Shirts": 25}}
    cid = _campaign(db)
    _seed(db, "ceramic_mug", campaign_id=cid, i=0)        # Mugs already at target
    cm = CatalogueManager(config, db)
    candidates = [
        {"product_key": "ceramic_mug", "product_name": "Mug", "composite_score": 95},
        {"product_key": "premium_tshirt", "product_name": "Tee", "composite_score": 70},
    ]
    ranked = cm.rank_for_build(candidates)
    # The T-Shirt (a gap) outranks the higher-scoring but saturated Mug.
    assert ranked[0]["product_key"] == "premium_tshirt"
