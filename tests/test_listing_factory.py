"""Tests for the Autonomous Listing Factory (LLM + compliance mocked)."""

from __future__ import annotations

import json

import pytest

from onassis.listing_factory import ListingError, ListingFactory
from tests.conftest import FakeLLM, make_compliance_response

_LISTING_GEN = {
    "title": "Linen Throw — Slow Mediterranean Mornings, Stonewashed Coastal Blanket",
    "description": "A stonewashed linen throw made for unhurried coastal mornings...",
    "tags": [f"tag{i}" for i in range(11)],  # only 11 — factory must coerce to 13
    "materials": ["European linen", "cotton thread"],
    "primary_colour": "Ecru",
    "secondary_colour": "Terracotta",
    "category": "Home & Living > Home Decor > Blankets & Throws",
    "seo_keywords": ["linen throw", "coastal blanket", "stonewashed linen", "slow living"],
    "image_alt_texts": ["primary shot"],  # only 1 — factory must coerce to mockup count
    "product_attributes": [{"name": "room", "value": "Living room"},
                           {"name": "style", "value": "Mediterranean"}],
}


@pytest.fixture
def factory(config, db, tmp_path):
    config.listing = {
        "exports_dir": str(tmp_path / "exports"),
        "currency": "GBP", "quantity": 50, "target_margin": 0.60,
        "default_production_cost": 12.0, "who_made": "i_did",
        "when_made": "made_to_order", "taxonomy_id": 1,
        "mockups": ["primary", "lifestyle_1", "lifestyle_2", "scale", "detail"],
    }
    f = ListingFactory(config, db)
    f._llm = FakeLLM(_LISTING_GEN)
    f.compliance._llm = FakeLLM(make_compliance_response())
    return f


def _approved_campaign(db, *, verdict="APPROVE") -> int:
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "Coastal mornings",
                                "campaign_name": "Salt & Citrus", "concept": "story",
                                "keywords": ["linen", "coast"]})
    campaign_id = db.insert_campaign({"name": "Salt & Citrus", "theme": "Coastal mornings",
                                      "story": "story", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": campaign_id, "verdict": verdict,
                                 "reasoning": "ok", "compliance_score": 90})
    return campaign_id


def _launch(db, cid, key, name, launched, cost, retail):
    db.insert_product_score({
        "campaign_id": cid, "product_key": key, "product_name": name,
        "launched": launched, "production_cost": cost, "retail_price": retail,
        "composite_score": 85 if launched else 60,
        "ceo_verdict": "APPROVE" if launched else "REJECT",
    })


# --- Multi-product export (the CEO-approved set only) ----------------

def test_export_products_builds_one_package_per_approved_product(factory, db, tmp_path):
    cid = _approved_campaign(db)
    _launch(db, cid, "ceramic_mug", "Ceramic Mug", 1, 7.5, 22.0)
    _launch(db, cid, "premium_poster", "Premium Poster", 1, 8.0, 28.0)
    _launch(db, cid, "hardcover_notebook", "Hardcover Notebook", 0, 8.5, 24.0)  # rejected

    result = factory.export_products(cid)
    assert result["status"] == "ready" and result["count"] == 2
    assert {p["product_key"] for p in result["products"]} == {"ceramic_mug", "premium_poster"}

    base = tmp_path / "exports" / str(cid)
    for key, retail in (("ceramic_mug", 22.0), ("premium_poster", 28.0)):
        listing = json.loads((base / key / "listing.json").read_text())
        assert listing["product_key"] == key
        assert listing["product_id"] == f"{cid}-{key}"
        assert listing["price"] == retail          # catalogue retail, per-product pricing
        assert listing["artwork_source"] == "design master asset"
    # The rejected product gets NO listing package.
    assert not (base / "hardcover_notebook").exists()


def test_export_products_blocked_without_an_approved_set(factory, db):
    cid = _approved_campaign(db)  # nothing launched
    assert factory.export_products(cid)["status"] == "blocked"


# --- Gates ----------------------------------------------------------

def test_missing_campaign_raises(factory):
    with pytest.raises(ListingError):
        factory.export(999)


def test_unapproved_campaign_is_blocked(factory, db):
    cid = _approved_campaign(db, verdict="REJECT")
    result = factory.export(cid)
    assert result["status"] == "blocked"
    assert "not compliance-approved" in result["reason"]


def test_campaign_without_compliance_is_blocked(factory, db):
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    cid = db.insert_campaign({"name": "C", "brief_id": brief_id})  # no compliance report
    assert factory.export(cid)["status"] == "blocked"


def test_listing_failing_compliance_is_not_exported(factory, db, tmp_path):
    cid = _approved_campaign(db)
    factory.compliance._llm = FakeLLM(make_compliance_response(copyright=95))  # listing trips veto
    result = factory.export(cid)
    assert result["status"] == "blocked"
    assert "REJECTED by compliance" in result["reason"]
    # Nothing should have been written.
    assert not (tmp_path / "exports" / str(cid)).exists()


def test_listing_compliance_amends_then_approves(factory, db):
    cid = _approved_campaign(db)
    # First pass needs changes; the amended copy is clean -> exported.
    factory.compliance._llm = FakeLLM([
        make_compliance_response(platform=55, corrections=["Rewrite the bold health claim"]),
        make_compliance_response(),
    ])
    result = factory.export(cid)
    assert result["status"] == "ready"
    assert result["compliance_attempts"] == 2
    # The listing copy was REGENERATED with the required amendment applied.
    assert "Rewrite the bold health claim" in factory._llm.calls[1]["prompt"]


def test_listing_compliance_regeneration_is_bounded(factory, db, tmp_path):
    cid = _approved_campaign(db)
    factory.compliance.max_remediation_attempts = 2
    factory.compliance._llm = FakeLLM(make_compliance_response(
        platform=55, corrections=["still non-compliant"]))
    result = factory.export(cid)
    assert result["status"] == "blocked"
    assert "changes" in result["reason"].lower()
    assert result["missing"] == ["still non-compliant"]
    assert not (tmp_path / "exports" / str(cid)).exists()   # nothing written


# --- Successful export ----------------------------------------------

def test_export_produces_complete_package(factory, db):
    cid = _approved_campaign(db)
    pkg = factory.export(cid)
    assert pkg["status"] == "ready"
    listing = pkg["listing"]

    # Every required field is present and upload-ready.
    for field in ("title", "description", "tags", "materials", "primary_colour",
                  "secondary_colour", "category", "seo_keywords",
                  "product_attributes", "pricing_recommendation",
                  "mockup_manifest", "image_order", "file_manifest"):
        assert field in listing
    assert len(listing["tags"]) == 13            # coerced to exactly 13
    assert len(listing["title"]) <= 140
    assert listing["state"] == "draft"           # never published


def test_pricing_is_deterministic(factory, db):
    cid = _approved_campaign(db)
    # Register a product with a known production cost.
    db.insert_product({"sku": str(cid), "production_cost": 12, "campaign_id": cid})
    listing = factory.export(cid)["listing"]
    # price = 12 / (1 - 0.60) = 30.00
    assert listing["price"] == 30.0
    assert listing["currency"] == "GBP"


def test_every_gallery_image_is_a_real_file(factory, db, tmp_path):
    from PIL import Image

    cid = _approved_campaign(db)
    pkg = factory.export(cid)
    images_dir = tmp_path / "exports" / str(cid) / "images"

    assert pkg["validation"]["all_images_present"] is True
    assert pkg["validation"]["required_images"] == factory.gallery_count
    assert pkg["validation"]["gallery_size_ok"] is True   # 8-10 images
    for img in pkg["listing"]["images"]:
        path = images_dir / img["filename"]
        assert path.exists() and path.stat().st_size > 0   # real, non-empty file
        Image.open(path).verify()                          # a valid, decodable image


def test_master_artwork_and_print_file_are_produced(factory, db, tmp_path):
    from PIL import Image

    cid = _approved_campaign(db)
    pkg = factory.export(cid)
    folder = tmp_path / "exports" / str(cid)

    for production_file in ("master_artwork.png", "print_file.png"):
        path = folder / production_file
        assert path.exists() and path.stat().st_size > 0
        Image.open(path).verify()
    assert pkg["validation"]["master_artwork_present"] is True
    assert pkg["validation"]["print_file_present"] is True
    # The artwork backend is recorded (local renderer by default).
    assert pkg["listing"]["artwork_backend"] == "local"


def test_folder_structure_and_files_written(factory, db, tmp_path):
    cid = _approved_campaign(db)
    factory.export(cid)
    folder = tmp_path / "exports" / str(cid)

    assert (folder / "listing.json").exists()
    assert (folder / "manifest.json").exists()
    assert (folder / "images").is_dir()
    assert (folder / "master_artwork.png").exists()
    assert (folder / "print_file.png").exists()

    listing = json.loads((folder / "listing.json").read_text())
    assert listing["title"]
    # Internal studio hand-off keys are stripped before the listing is written.
    assert "_studio_brief" not in listing and "_alt_texts" not in listing
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["validation"]["all_images_present"] is True
    assert manifest["compliance"]["verdict"] == "APPROVE"
    assert len(manifest["image_order"]) == factory.gallery_count
    # The QC review of the master/print files is recorded.
    assert manifest["master_review"]["accepted"] is True


def test_gallery_has_hero_mockups_and_gallery_images(factory, db):
    cid = _approved_campaign(db)
    filenames = [img["filename"] for img in factory.export(cid)["listing"]["images"]]
    assert filenames[0] == "hero.jpg"
    assert any(f.startswith("mockup_") for f in filenames)
    assert any(f.startswith("gallery_") for f in filenames)


def test_alt_text_present_on_every_gallery_image(factory, db):
    cid = _approved_campaign(db)
    images = factory.export(cid)["listing"]["images"]
    assert len(images) == factory.gallery_count
    assert 8 <= len(images) <= 10
    assert all(img["alt_text"] for img in images)   # every image has alt text
