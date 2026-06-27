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
    assert "failed compliance" in result["reason"]
    # Nothing should have been written.
    assert not (tmp_path / "exports" / str(cid)).exists()


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


def test_every_mockup_image_is_created_and_validated(factory, db, tmp_path):
    cid = _approved_campaign(db)
    pkg = factory.export(cid)
    images_dir = tmp_path / "exports" / str(cid) / "images"

    assert pkg["validation"]["all_images_present"] is True
    assert pkg["validation"]["required_images"] == 5
    for img in pkg["listing"]["images"]:
        path = images_dir / img["filename"]
        assert path.exists() and path.stat().st_size > 0   # real, non-empty file


def test_folder_structure_and_files_written(factory, db, tmp_path):
    cid = _approved_campaign(db)
    factory.export(cid)
    folder = tmp_path / "exports" / str(cid)

    assert (folder / "listing.json").exists()
    assert (folder / "manifest.json").exists()
    assert (folder / "images").is_dir()

    listing = json.loads((folder / "listing.json").read_text())
    assert listing["title"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["validation"]["all_images_present"] is True
    assert manifest["compliance"]["verdict"] == "APPROVE"
    assert len(manifest["image_order"]) == 5


def test_alt_text_coerced_to_one_per_mockup(factory, db):
    cid = _approved_campaign(db)
    images = factory.export(cid)["listing"]["images"]
    assert len(images) == 5
    assert all(img["alt_text"] for img in images)   # every image has alt text
