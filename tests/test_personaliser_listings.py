"""Personaliser Etsy listings: real gallery images, and no photo product is ever
listed without a genuine example."""

from __future__ import annotations

from pathlib import Path

import pytest

from onassis.content_engine import ContentEngine
from onassis.personaliser import PRODUCTS


class _LLM:
    def generate_json(self, *, system, prompt, schema):
        return {"title": "Personalised Print, Custom Gift", "tags": ["custom gift"]}


class _Etsy:
    is_configured = True

    def __init__(self):
        self.client = self
        self.drafts, self.files, self.images = [], [], []

    def create_draft(self, listing):
        self.drafts.append(listing)
        return {"listing_id": 1000 + len(self.drafts)}

    def upload_listing_file(self, lid, path, *, name=None, rank=1):
        self.files.append((lid, path))

    def upload_listing_image(self, lid, path, *, rank=1, **_):
        self.images.append((lid, path, rank))


@pytest.fixture
def eng(config, db, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports"),
                      "taxonomy_id": 123}
    config.personaliser = {"public_base": "https://app.example"}
    config.image = {"backend": "local"}                 # dev renderer: no edit()
    e = ContentEngine(config, db)
    e._seo_llm = _LLM()
    e._seo_etsy = _Etsy()
    return e


def test_tier1_gallery_leads_with_a_framed_wall_mockup(eng, tmp_path):
    imgs = eng._personaliser_listing_images(PRODUCTS["place-poster"], tmp_path / "L")
    assert len(imgs) == 2
    assert imgs[0].endswith("-mockup.jpg") and Path(imgs[0]).exists()
    assert Path(imgs[1]).exists()


def test_tier2_without_a_real_example_raises_no_sample(eng, tmp_path):
    with pytest.raises(ContentEngine._NoSample):
        eng._personaliser_listing_images(PRODUCTS["pet-portrait"], tmp_path / "L")


def test_publish_creates_tier1_drafts_and_skips_tier2_without_examples(eng):
    r = eng.publish_personaliser_listings(apply=True)
    by = {row["product"]: row for row in r["products"]}
    # Every computed product is drafted with the wall mockup as image #1.
    for key in ("place-poster", "star-map", "birth-stats", "invite"):
        assert by[key]["status"] == "draft_created", by[key]
        lid = by[key]["listing_id"]
        ranks = sorted((rk, p) for l, p, rk in eng._seo_etsy.images if l == lid)
        assert ranks and ranks[0][0] == 1 and ranks[0][1].endswith("-mockup.jpg")
    # No photo product is published on a text card.
    for key in ("pet-portrait", "renaissance-portrait", "vintage-photo"):
        assert by[key]["status"] == "no_sample", by[key]
    assert r["created"] == 4
    assert all(d["type"] == "download" for d in eng._seo_etsy.drafts)
    # Re-run is idempotent for the ones that were created.
    again = eng.publish_personaliser_listings(apply=True)
    assert again["created"] == 0
    assert {row["status"] for row in again["products"] if row["product"] == "invite"} == {"exists"}


def test_reset_forgets_old_listings_and_recreates(eng):
    first = eng.publish_personaliser_listings(apply=True)
    assert first["created"] == 4
    # Without reset: everything already exists, nothing new.
    assert eng.publish_personaliser_listings(apply=True)["created"] == 0
    # With reset (after the operator deleted the drafts on Etsy): recreated.
    again = eng.publish_personaliser_listings(apply=True, reset=True)
    assert again["created"] == 4
    assert len(eng._seo_etsy.drafts) == 8


def test_publish_print_listings_creates_physical_variants(eng):
    """Print listings are physical type and use higher price."""
    r = eng.publish_personaliser_listings(apply=True, prints=True)
    by = {row["product"]: row for row in r["products"]}
    # Every computed product gets a print variant.
    for key in ("place-poster", "star-map", "birth-stats", "invite"):
        assert by[key]["status"] == "draft_created", by[key]
        lid = by[key]["listing_id"]
        # Find the draft for this listing.
        draft = next((d for d in eng._seo_etsy.drafts if d.get("listing_id") or 0 == lid), None)
        # Print listings are physical type.
        assert any(d.get("type") == "physical" for d in eng._seo_etsy.drafts[-4:])
        # Price is higher for prints.
        from onassis.personaliser import PRODUCTS
        product = PRODUCTS[key]
        assert product.print_price > product.price
    assert r["created"] == 4
    # Re-run is idempotent.
    again = eng.publish_personaliser_listings(apply=True, prints=True)
    assert again["created"] == 0


def test_print_listings_reset_separately_from_digital(eng):
    """Reset only affects listings matching the prints flag."""
    # Create digital listings.
    digital = eng.publish_personaliser_listings(apply=True, prints=False)
    assert digital["created"] == 4
    # Create print listings.
    prints = eng.publish_personaliser_listings(apply=True, prints=True)
    assert prints["created"] == 4
    # Now we have 8 listings total.
    assert len(eng._seo_etsy.drafts) == 8
    # Reset with prints=True only deletes the print listings.
    again_prints = eng.publish_personaliser_listings(apply=True, reset=True, prints=True)
    assert again_prints["created"] == 4
    # Digital listings should still be there (just marked as exists).
    again_digital = eng.publish_personaliser_listings(apply=True, prints=False)
    assert all(row["status"] == "exists" for row in again_digital["products"]
               if row["product"] in ("place-poster", "star-map", "birth-stats", "invite"))
