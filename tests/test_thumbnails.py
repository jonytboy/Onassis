"""Tests for the Thumbnail Optimiser — generate 4 heroes, score, choose, learn."""

from __future__ import annotations

import pytest

from onassis.thumbnails import VARIANTS, ThumbnailOptimiser


@pytest.fixture
def optimiser(config, db):
    config.thumbnails = {**(config.thumbnails or {}), "optimise": True}
    return ThumbnailOptimiser(config, db)


@pytest.fixture
def design_package():
    return {"design_brief": {"theme": "Aegean citrus mornings",
                             "product_name": "Ceramic Mug", "brand": "Local Celebrity",
                             "primary_colour": "sea", "secondary_colour": "terracotta"}}


def _product():
    return {"product_key": "ceramic_mug", "product_name": "Ceramic Mug", "sku": "1-ceramic_mug"}


def test_generates_four_hero_candidates_and_picks_one(optimiser, design_package, tmp_path):
    out = optimiser.choose(design_package, _product(), tmp_path, campaign_id=1)
    assert len(out["candidates"]) == 4
    assert {c["variant"] for c in out["candidates"]} == {v["variant"] for v in VARIANTS}
    chosen = [c for c in out["candidates"] if c["chosen"]]
    assert len(chosen) == 1
    assert out["chosen"] == chosen[0]["variant"]
    # Every candidate is a real file, and the winner becomes hero.jpg.
    for c in out["candidates"]:
        assert (tmp_path / c["filename"]).exists()
    assert (tmp_path / "hero.jpg").exists()


def test_candidates_are_persisted_for_ctr_learning(optimiser, design_package, db, tmp_path):
    optimiser.choose(design_package, _product(), tmp_path, campaign_id=1)
    rows = db.list_thumbnails(product_key="ceramic_mug")
    assert len(rows) == 4
    assert sum(r["chosen"] for r in rows) == 1


def test_learned_ctr_overrides_the_prior_and_steers_the_choice(config, db, design_package,
                                                               tmp_path):
    config.thumbnails = {**(config.thumbnails or {}), "optimise": True}
    # Observe that 'lifestyle' heroes earn a strong CTR historically; the default
    # prior favours white_background, so this is a genuine override.
    tid = db.insert_thumbnail({"product_key": "poster", "variant": "lifestyle",
                               "chosen": True})
    db.record_thumbnail_metrics(tid, impressions=1000, clicks=120)  # 12% CTR (>= ceiling)
    opt = ThumbnailOptimiser(config, db)
    out = opt.choose(design_package, _product(), tmp_path, campaign_id=1)
    lifestyle = next(c for c in out["candidates"] if c["variant"] == "lifestyle")
    white = next(c for c in out["candidates"] if c["variant"] == "white_background")
    assert lifestyle["learned_ctr"] == pytest.approx(0.12)
    assert lifestyle["prior"] >= white["prior"]   # learned CTR lifts lifestyle above the default


def test_record_ctr_accrues_metrics(optimiser, db):
    tid = db.insert_thumbnail({"product_key": "mug", "variant": "white_background",
                               "chosen": True})
    optimiser.record_ctr(tid, impressions=500, clicks=25)
    optimiser.record_ctr(tid, impressions=500, clicks=15)
    assert db.learned_ctr_by_variant()["white_background"] == pytest.approx(0.04)
