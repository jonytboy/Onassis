"""Tests for the Daily Cycle orchestrator."""

from __future__ import annotations

import pytest

from onassis.daily_cycle import DailyCycle
from onassis.revenue import RevenueEngine
from tests.conftest import (
    FakeLLM, FakeSignals, make_compliance_response, make_content_response,
)

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
    "Sync Etsy", "Sync Pinterest", "Import Revenue", "Fulfil Orders",
    "Import Analytics",
    "Learn & Review Portfolio", "Run Product Optimiser", "CEO Decision",
    "Market Research", "Create Product Opportunity", "Build Design Package",
    "Generate Master Artwork", "Create Product Campaign", "Expand Products",
    "Publish Products", "Generate Marketing Content", "Promote on Pinterest",
    "Daily Report", "CEO Dashboard", "Record Results",
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
    "artwork_description": "A line-drawn lemon branch.",
    "design_rationale": "On theme.", "listing_title_seed": "Amalfi Tee",
    "listing_tags_seed": ["lemon", "coastal"], "listing_description_seed": "A calm tee.",
}


# --- Marketing push (decoupled from production) ---------------------

def test_run_marketing_pushes_without_creating_products(config, db):
    """The standalone marketing run pins + distributes but creates NO products
    and spends no LLM/image credit — safe to run daily on its own."""
    cycle = DailyCycle(config, db)
    before = len(db.list_products())
    result = cycle.run_marketing()
    assert result["status"] == "ok"
    assert "traffic" in result and "evergreen" in result["traffic"]
    assert len(db.list_products()) == before          # nothing created


# --- Dry run (fully offline; observation + decision only) -----------

def test_daily_listing_cap_honours_business_setting(config, db):
    cycle = DailyCycle(config, db)
    assert isinstance(cycle._daily_listing_cap(), int)   # config default
    db.set_setting("business.max_campaigns_per_day", 25)  # operator raises it in the UI
    assert cycle._daily_listing_cap() == 25


def test_dry_run_executes_all_stages_without_side_effects(config, db):
    cycle = DailyCycle(config, db)
    summary = cycle.run(mode="dry_run")

    assert [s["stage"] for s in summary["stages"]] == _EXPECTED_STAGES
    assert summary["status"] == "completed"
    # Product creation, learning/archiving, marketing, and publishing are
    # skipped in dry run (observation + decision only, no writes).
    by_stage = {s["stage"]: s for s in summary["stages"]}
    for stage in ("Learn & Review Portfolio", "Market Research",
                  "Create Product Opportunity", "Build Design Package",
                  "Generate Master Artwork", "Create Product Campaign",
                  "Expand Products", "Publish Products",
                  "Generate Marketing Content", "Promote on Pinterest"):
        assert by_stage[stage]["status"] == "skipped"
    # The daily report + CEO scoreboard always run — even a dry run reports money.
    assert by_stage["Daily Report"]["status"] == "ok"
    assert by_stage["CEO Dashboard"]["status"] == "ok"
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
    # Product-first stages: market research feeds opportunity discovery.
    cycle.market.provider = FakeSignals()
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
    # Streaming publish: each product built + drafted + taken LIVE independently.
    pub = by_stage["Publish Products"]
    assert pub["status"] == "ok"
    assert pub["detail"]["published"] >= 1
    assert pub["detail"]["live"] >= 1
    assert pub["detail"]["first_draft_at"]                 # the KPI is recorded
    # The per-product timeline is visible (product-by-product, not a global blob).
    timeline = pub["detail"]["timeline"]
    assert len(timeline) >= 1
    first = timeline[0]
    assert first["status"] == "live" and first["listing_id"]
    assert first["images_uploaded"] >= 1 and first["published_at"]
    assert by_stage["Generate Marketing Content"]["status"] == "ok"
    assert summary["launch_status"] == "launched"
    assert summary["products_live"] >= 1
    assert summary["first_draft_at"]
    assert summary["status"] == "completed"

    # A product is actually LIVE on Etsy (activated), not just drafted.
    assert production_cycle.publisher._draft_client.activated   # activate was called
    assert any(p["status"] == "live" for p in db.list_publications())

    # The daily report is produced with the required fields.
    report = summary["report"]
    assert set(report["recommendations"]) == {"expand", "hold", "kill"}
    assert "revenue" in report and "profit" in report

    # Revenue-first: products are published BEFORE marketing content is generated.
    stage_order = [s["stage"] for s in summary["stages"]]
    assert stage_order.index("Publish Products") < stage_order.index("Generate Marketing Content")


def test_streaming_isolates_a_failed_product(production_cycle):
    """If one product fails, the others still publish — never rolled back."""
    original = production_cycle.listing_factory.export_product
    calls = {"n": 0}

    def flaky(campaign_id, spec, **kw):
        calls["n"] += 1
        if calls["n"] == 2:                       # the 2nd product blows up
            raise RuntimeError("boom on product 2")
        return original(campaign_id, spec, **kw)

    production_cycle.listing_factory.export_product = flaky
    summary = production_cycle.run(mode="production")
    pub = {s["stage"]: s for s in summary["stages"]}["Publish Products"]["detail"]

    timeline = pub["timeline"]
    assert len(timeline) >= 3
    assert timeline[1]["status"] == "failed"       # product 2 isolated
    assert timeline[1]["stage"] == "exception"
    assert pub["failed"] == 1
    # Products 1 and 3 still shipped and went live — no rollback, no cancellation.
    assert pub["published"] >= 2 and pub["live"] >= 2
    assert summary["products_live"] >= 2
    assert summary["status"] == "completed"        # the cycle itself did not fail


def test_streaming_publishes_first_product_before_finishing_the_rest(production_cycle):
    """The first product reaches a draft with a recorded listing id + images —
    the KPI (time to first draft) is captured product-by-product."""
    summary = production_cycle.run(mode="production")
    pub = {s["stage"]: s for s in summary["stages"]}["Publish Products"]["detail"]
    timeline = pub["timeline"]
    # Each product carries its own outcome (id, images, timestamp) — not a blob.
    for rec in timeline:
        if rec["status"] in ("live", "draft"):
            assert rec["listing_id"] and rec["published_at"]
            assert rec["images_uploaded"] >= 1
    assert pub["first_draft_at"] == summary["first_draft_at"]


def test_streaming_fixes_truncated_copy_before_the_draft(production_cycle, tmp_path):
    """A truncated description is regenerated to complete copy BEFORE the Etsy
    draft is created — it is never published unfinished."""
    import json
    from pathlib import Path

    lf = production_cycle.listing_factory
    truncated = {**_LISTING, "description": "A calm coastal piece for slow mornings and"}
    complete = {**_LISTING, "description": "A calm coastal piece for slow mornings, "
                "finished by hand with genuine care and a soft, lived-in feel."}
    lf._llm = FakeLLM([truncated, complete])   # first cut off, then complete (sticks)

    summary = production_cycle.run(mode="production")
    pub = {s["stage"]: s for s in summary["stages"]}["Publish Products"]["detail"]
    assert pub["published"] >= 1               # fixed and shipped, not blocked

    cid = summary["campaign_id"]
    shipped = next(r for r in pub["timeline"] if r["status"] in ("live", "draft"))
    listing = json.loads(
        (Path(tmp_path) / "exports" / str(cid) / shipped["product_key"] / "listing.json")
        .read_text())
    assert listing["description"].rstrip().endswith("feel.")     # complete
    assert not listing["description"].rstrip().endswith("and")   # not the truncated copy


def test_portfolio_cap_limits_new_listings_per_day(production_cycle):
    """Never flood Etsy — no more than max_new_listings_per_day go live per day."""
    production_cycle.config.portfolio = {"max_new_listings_per_day": 1}
    summary = production_cycle.run(mode="production")
    pub = {s["stage"]: s for s in summary["stages"]}["Publish Products"]["detail"]
    assert pub["published"] == 1                 # capped, even though >1 launched
    assert pub["deferred_by_cap"] >= 1           # the rest are deferred, not lost
    assert production_cycle.db.count_new_listings_today() == 1


def test_production_researches_market_before_inventing(production_cycle):
    summary = production_cycle.run(mode="production")
    by_stage = {s["stage"]: s for s in summary["stages"]}
    research = by_stage["Market Research"]
    assert research["status"] == "ok"
    assert research["detail"]["keywords_scored"] >= 1
    assert research["detail"]["top"]             # top keywords surfaced to the CEO
    # Market Research runs BEFORE the opportunity is created.
    order = [s["stage"] for s in summary["stages"]]
    assert order.index("Market Research") < order.index("Create Product Opportunity")


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
