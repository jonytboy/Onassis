"""Tests for the Daily Cycle orchestrator."""

from __future__ import annotations

import pytest

from onassis.daily_cycle import DailyCycle
from onassis.revenue import RevenueEngine
from tests.conftest import FakeLLM, make_compliance_response, make_content_response

_PREDICTION = {
    "hypothesis": "h", "variables": ["a"], "predicted_outcome": "o",
    "confidence": 70, "success_metrics": ["saves"], "recommendation": "r",
}
_LISTING = {
    "title": "Linen Throw", "description": "Lovely.",
    "tags": [f"t{i}" for i in range(13)], "materials": ["linen"],
    "primary_colour": "Ecru", "secondary_colour": "Terracotta", "category": "Home",
    "seo_keywords": ["linen throw"], "image_alt_texts": ["a", "b", "c", "d", "e"],
    "product_attributes": [{"name": "room", "value": "Living"}],
}

_EXPECTED_STAGES = [
    "Sync Etsy", "Sync Pinterest", "Import Revenue", "Import Analytics",
    "Run Product Optimiser", "CEO Decision",
    "Create Product Opportunity", "Build Design Package", "Generate Master Artwork",
    "Create Product Campaign", "Expand Products", "Build Etsy Listing Package",
    "Generate Marketing Content", "Publish Live", "Promote on Pinterest",
    "Daily Report", "Record Results",
]

_OPPORTUNITY = {
    "theme": "Slow coastal mornings", "target_customer": "design-loving travellers",
    "emotional_angle": "calm, unhurried luxury", "product_type": "t-shirt",
    "search_intent": "mediterranean lifestyle tee", "seasonal_relevance": "summer",
    "commercial_score": 85, "originality_score": 80, "brand_fit_score": 90,
    "estimated_demand": 80, "estimated_competition": 30, "confidence": 80,
    "product_name": "Amalfi Morning Tee", "concept": "A tee for slow Amalfi mornings.",
    "colour_palette": ["citrus", "whitewash"], "typography_style": "serif",
    "illustration_style": "watercolour", "photography_style": "morning light",
    "mockup_style": "terrace flatlay",
}

_DESIGN = {
    "shirt_colour": "ecru", "print_colour": "terracotta",
    "typography_direction": "serif lowercase", "layout_direction": "centred",
    "print_placement": "centre chest", "print_size_guidance": "25cm wide",
    "artwork_description": "A line-drawn lemon branch.", "mockup_scene": "tee on linen",
    "design_rationale": "On theme.", "listing_title_seed": "Amalfi Tee",
    "listing_tags_seed": ["lemon", "coastal"], "listing_description_seed": "A calm tee.",
}


# --- Dry run (fully offline; observation + decision only) -----------

def test_dry_run_executes_all_stages_without_side_effects(config, db):
    cycle = DailyCycle(config, db)
    summary = cycle.run(mode="dry_run")

    assert [s["stage"] for s in summary["stages"]] == _EXPECTED_STAGES
    assert summary["status"] == "completed"
    # Product creation, marketing, and publishing are skipped in dry run.
    by_stage = {s["stage"]: s for s in summary["stages"]}
    for stage in ("Create Product Opportunity", "Build Design Package",
                  "Generate Master Artwork", "Create Product Campaign",
                  "Expand Products", "Build Etsy Listing Package",
                  "Generate Marketing Content", "Publish Live",
                  "Promote on Pinterest"):
        assert by_stage[stage]["status"] == "skipped"
    # The daily report always runs — even a dry run reports the scoreboard.
    assert by_stage["Daily Report"]["status"] == "ok"
    # The run is recorded and retrievable.
    assert db.get_latest_daily_run()["mode"] == "dry_run"


def test_every_stage_records_duration(config, db):
    summary = DailyCycle(config, db).run(mode="dry_run")
    assert all("duration_seconds" in s for s in summary["stages"])


# --- Failure isolation ----------------------------------------------

def test_failed_stage_does_not_stop_the_cycle(config, db):
    cycle = DailyCycle(config, db)

    class _Boom:
        def company_profit(self):
            raise RuntimeError("revenue source down")

    cycle.revenue = _Boom()
    summary = cycle.run(mode="dry_run")

    by_stage = {s["stage"]: s for s in summary["stages"]}
    assert by_stage["Import Revenue"]["status"] == "failed"
    assert "revenue source down" in by_stage["Import Revenue"]["error"]
    # Later stages still ran.
    assert by_stage["Import Analytics"]["status"] == "ok"
    assert summary["status"] == "completed_with_failures"


# --- Full production path (modules' LLMs / clients faked) ------------

@pytest.fixture
def production_cycle(config, db, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    # Enable live publishing + automatic go-live so the cycle takes products LIVE.
    config.publishing = {"default_mode": "draft",
                         "enabled_modes": ["dry_run", "draft", "live"],
                         "max_retries": 3, "min_go_live_margin": 0.10}
    config.launch = {"policy": "automatic", "auto_go_live": True}
    cycle = DailyCycle(config, db)

    # A profitable product starved of traffic -> optimiser recommends a
    # Pinterest campaign with a strong ROI the CEO will approve.
    db.insert_product({"sku": "9001", "name": "Linen Throw", "production_cost": 100})
    RevenueEngine(config, db).record_order({
        "occurred_at": "2026-06-26T10:00:00+00:00", "sale_date": "2026-06-26",
        "order_ref": "o1", "product_id": "9001", "platform": "etsy",
        "sale_price": 600, "quantity": 1, "production_cost": 100,
    })
    db.upsert_etsy_listing({"listing_id": 9001, "product_id": "9001", "views": 5})

    # Fakes for every module that would call an LLM / external service.
    t = config.content_targets
    cycle.orchestrator.director._llm = FakeLLM({
        "campaign_name": "Salt", "theme": "Coastal", "concept": "c", "tone": "warm",
        "audience": "a", "objective": "o", "visual_direction": "v", "keywords": ["linen"]})
    cycle.orchestrator.creator._llm = FakeLLM(make_content_response(
        t.get("pinterest_posts", 5), t.get("instagram_captions", 3),
        t.get("facebook_posts", 2), t.get("image_prompts", 3)))
    cycle.orchestrator.brain._llm = FakeLLM(_PREDICTION)
    cycle.orchestrator.compliance._llm = FakeLLM(make_compliance_response())
    cycle.listing_factory._llm = FakeLLM(_LISTING)
    cycle.listing_factory.compliance._llm = FakeLLM(make_compliance_response())
    # Product-first stages: opportunity discovery + design package generation.
    cycle.opportunities._llm = FakeLLM({"opportunities": [_OPPORTUNITY]})
    cycle.design._llm = FakeLLM(_DESIGN)
    cycle.design.compliance._llm = FakeLLM(make_compliance_response())

    class _StubDraft:
        def __init__(self):
            self.activated = []
            self._next = 4242

        def create_draft(self, listing):
            self._next += 1
            return {"listing_id": self._next}

        def upload_listing_image(self, listing_id, image_path, *, rank=1,
                                 alt_text=None, overwrite=False):
            return {"listing_image_id": rank}

        def publish_listing(self, listing_id):
            self.activated.append(listing_id)
            return {"listing_id": listing_id, "state": "active"}
    cycle.publisher._draft_client = _StubDraft()
    return cycle


def test_production_runs_full_pipeline_and_publishes(production_cycle, db):
    summary = production_cycle.run(mode="production")
    by_stage = {s["stage"]: s for s in summary["stages"]}

    # Product-first: opportunity -> design -> campaign -> listing, then content.
    assert by_stage["Create Product Opportunity"]["status"] == "ok"
    assert by_stage["Build Design Package"]["status"] == "ok"
    # Real master artwork + print file are generated and pass the quality gate.
    assert by_stage["Generate Master Artwork"]["status"] == "ok"
    art = by_stage["Generate Master Artwork"]["detail"]
    assert art["files"] == ["master_artwork.png", "print_file.png"]
    assert art["master_accepted"] and art["print_accepted"]
    assert by_stage["Create Product Campaign"]["status"] == "ok"
    assert by_stage["Expand Products"]["status"] == "ok"
    assert by_stage["Expand Products"]["detail"]["products_launched"] >= 1
    assert by_stage["Build Etsy Listing Package"]["status"] == "ok"
    # One listing package per approved product (the expansion launched >= 1).
    assert by_stage["Build Etsy Listing Package"]["detail"]["products_built"] >= 1
    # Every product got a full 8-10 image commercial gallery (real files).
    assert by_stage["Build Etsy Listing Package"]["detail"]["images_generated"] >= 8
    assert by_stage["Generate Marketing Content"]["status"] == "ok"
    # Publish Live: draft every approved product, then automatically activate LIVE.
    assert by_stage["Publish Live"]["status"] == "ok"
    assert by_stage["Publish Live"]["detail"]["published"] >= 1
    assert by_stage["Publish Live"]["detail"]["products_live"] >= 1
    # Automatic launch policy approved and took the design live in one cycle.
    assert by_stage["Publish Live"]["detail"]["launch_status"] == "launched"
    assert summary["launch_status"] == "launched"
    assert summary["products_live"] >= 1
    assert summary["status"] == "completed"

    # A product is actually LIVE on Etsy (activated), not just drafted.
    assert production_cycle.publisher._draft_client.activated   # activate was called
    assert any(p["status"] == "live" for p in db.list_publications())

    # The daily report is produced with the required fields.
    report = summary["report"]
    assert set(report["recommendations"]) == {"expand", "hold", "kill"}
    assert "revenue" in report and "profit" in report

    # Marketing was generated only AFTER the product (listing) existed.
    stage_order = [s["stage"] for s in summary["stages"]]
    assert stage_order.index("Build Etsy Listing Package") < stage_order.index("Generate Marketing Content")


def test_production_promotes_live_products_on_pinterest(production_cycle):
    """Every live product is auto-promoted with pins that link to its listing."""
    class _StubPin:
        def __init__(self):
            self.pins = []

        def create_pin(self, *, board_id, title, description, link, image_path,
                       alt_text=None):
            self.pins.append(link)
            return {"id": f"pin_{len(self.pins)}"}

    pin_client = _StubPin()
    # Connect Pinterest with an injected client (no network).
    production_cycle.pinterest.board_id = "board123"
    production_cycle.pinterest.max_pins = 3
    production_cycle.pinterest._pin_client = pin_client

    summary = production_cycle.run(mode="production")
    by_stage = {s["stage"]: s for s in summary["stages"]}
    assert by_stage["Promote on Pinterest"]["status"] == "ok"
    assert summary["pins_posted"] >= 1
    # Pins link back to a live Etsy listing.
    assert all("etsy.com/listing/" in link for link in pin_client.pins)


# --- Reads ----------------------------------------------------------

def test_status_and_history(config, db):
    cycle = DailyCycle(config, db)
    assert cycle.status() == {"message": "No daily runs yet."}
    cycle.run(mode="dry_run")
    cycle.run(mode="dry_run")
    assert cycle.status()["id"] is not None
    assert len(cycle.history()) == 2
