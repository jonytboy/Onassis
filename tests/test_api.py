"""Tests for the REST API — one per endpoint, all LLMs mocked (offline)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from onassis.api import create_app
from tests.conftest import (
    FakeLLM, FakeSignals, make_compliance_response, make_content_response,
)

_PREDICTION = {
    "hypothesis": "Authentic slow-living content outperforms aspirational yacht content.",
    "variables": ["lifestyle", "dining", "golden hour"],
    "predicted_outcome": "Saves above the rolling average.",
    "confidence": 74,
    "success_metrics": ["Pinterest saves", "Instagram shares"],
    "recommendation": "If saves beat the average by 20%, make three more slow-dining campaigns.",
}


def _expected_assets(config) -> int:
    t = config.content_targets
    return (
        t.get("pinterest_posts", 5)
        + t.get("instagram_captions", 3)
        + t.get("facebook_posts", 2)
        + t.get("image_prompts", 3)
    )


# The one production workflow is product-first: the campaign is created from the
# top-ranked product opportunity, so the campaign is named after the product.
_TOP_PRODUCT = "Amalfi Morning Tee"


def _opportunity(i: int, score: int) -> dict:
    return {
        "theme": f"Theme {i}", "target_customer": "design-loving travellers",
        "emotional_angle": f"angle {i}", "product_type": f"tee {i}",
        "search_intent": "mediterranean tee", "seasonal_relevance": "summer",
        "commercial_score": score, "originality_score": score, "brand_fit_score": score,
        "estimated_demand": score, "estimated_competition": 20, "confidence": 80,
        "product_name": _TOP_PRODUCT if i == 0 else f"Product {i}",
        "concept": "A linen-soft tee for slow Amalfi mornings.",
        "colour_palette": ["citrus", "whitewash"], "typography_style": "serif",
        "illustration_style": "watercolour", "photography_style": "morning light",
        "mockup_style": "terrace flatlay",
    }


_OPPORTUNITIES = {"opportunities": [_opportunity(i, 90 - i * 5) for i in range(6)]}
_DESIGN = {
    "shirt_colour": "ecru", "print_colour": "terracotta",
    "typography_direction": "serif lowercase", "layout_direction": "centred",
    "print_placement": "centre chest", "print_size_guidance": "25cm wide",
    "artwork_description": "A line-drawn lemon branch.", "mockup_scene": "tee on linen",
    "design_rationale": "On theme.", "listing_title_seed": "Amalfi Tee",
    "listing_tags_seed": ["lemon", "coastal"], "listing_description_seed": "A calm tee.",
}
_LISTING = {
    "title": "Amalfi Morning Tee", "description": "Lovely.",
    "tags": [f"t{i}" for i in range(13)], "materials": ["cotton"],
    "primary_colour": "Ecru", "secondary_colour": "Terracotta", "category": "Apparel",
    "seo_keywords": ["mediterranean tee"], "image_alt_texts": ["a", "b", "c", "d", "e"],
    "product_attributes": [{"name": "fit", "value": "Relaxed"}],
}


@pytest.fixture
def app_and_client(config, sample_brief, tmp_path):
    """An app on a throwaway DB, with the product-first workflow's LLMs faked."""
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    app = create_app(config)
    orch = app.state.orchestrator
    t = config.content_targets
    orch.creator._llm = FakeLLM(
        make_content_response(
            t.get("pinterest_posts", 5),
            t.get("instagram_captions", 3),
            t.get("facebook_posts", 2),
            t.get("image_prompts", 3),
        )
    )
    orch.brain._llm = FakeLLM(_PREDICTION)
    orch.compliance._llm = FakeLLM(make_compliance_response())
    # Product-first stages: opportunity, design package, Etsy listing.
    app.state.daily.market.provider = FakeSignals()   # offline market research
    app.state.opportunities._llm = FakeLLM(_OPPORTUNITIES)
    app.state.opportunities.generate()  # seed the backlog
    app.state.design_builder._llm = FakeLLM(_DESIGN)
    app.state.design_builder.compliance._llm = FakeLLM(make_compliance_response())
    app.state.listing_factory._llm = FakeLLM(_LISTING)
    app.state.listing_factory.compliance._llm = FakeLLM(make_compliance_response())
    return app, TestClient(app)


# --- GET /health ----------------------------------------------------

def test_health(app_and_client):
    _, client = app_and_client
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["campaigns"] == 0  # fresh db


# --- POST /campaign/create ------------------------------------------

def test_create_campaign_matches_success_contract(app_and_client, config):
    _, client = app_and_client
    r = client.post("/campaign/create")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"campaign_id", "status", "assets_created", "duration_seconds"}
    assert isinstance(body["campaign_id"], int)
    assert body["status"] == "completed"
    assert body["assets_created"] == _expected_assets(config)
    assert isinstance(body["duration_seconds"], int)
    assert body["duration_seconds"] >= 0


def test_create_campaign_persists(app_and_client):
    app, client = app_and_client
    cid = client.post("/campaign/create").json()["campaign_id"]
    assert app.state.db.get_campaign(cid) is not None


# --- GET /campaigns -------------------------------------------------

def test_list_campaigns(app_and_client):
    _, client = app_and_client
    client.post("/campaign/create")
    r = client.get("/campaigns")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["content_count"] > 0
    assert rows[0]["status"] == "Draft"


def test_list_campaigns_empty(app_and_client):
    _, client = app_and_client
    assert client.get("/campaigns").json() == []


# --- GET /campaign/{id} ---------------------------------------------

def test_get_campaign_full_view(app_and_client, config):
    _, client = app_and_client
    cid = client.post("/campaign/create").json()["campaign_id"]
    r = client.get(f"/campaign/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == cid
    # Product-first: the campaign is named after the top product opportunity.
    assert body["name"] == _TOP_PRODUCT
    assert len(body["content_ids"]) == _expected_assets(config)
    assert len(body["content"]) == _expected_assets(config)
    assert body["brief"] is not None
    assert body["knowledge"]["confidence"] == _PREDICTION["confidence"]


def test_get_campaign_not_found(app_and_client):
    _, client = app_and_client
    r = client.get("/campaign/999")
    assert r.status_code == 404


# --- GET /campaign/latest -------------------------------------------

def test_latest_campaign(app_and_client, config):
    _, client = app_and_client
    client.post("/campaign/create")
    second = client.post("/campaign/create").json()["campaign_id"]
    r = client.get("/campaign/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == second  # newest
    assert len(body["content_ids"]) == _expected_assets(config)
    assert body["knowledge"] is not None


def test_latest_campaign_empty(app_and_client):
    _, client = app_and_client
    assert client.get("/campaign/latest").status_code == 404


# --- GET /dashboard -------------------------------------------------

def test_dashboard_endpoint(app_and_client):
    _, client = app_and_client
    r = client.get("/dashboard")
    assert r.status_code == 200
    body = r.json()
    # Profit-first priority order.
    assert list(body)[:8] == [
        "net_profit", "roi", "cash_balance", "ai_cost", "advertising_cost",
        "active_products", "profit_per_product", "profit_per_campaign",
    ]


def test_dashboard_reflects_campaign_ai_cost(app_and_client):
    _, client = app_and_client
    client.post("/campaign/create")  # records the per-campaign AI cost
    body = client.get("/dashboard").json()
    assert body["ai_cost"] > 0
    assert body["cash_balance"] < 10000  # starting cash reduced by AI cost


# --- Revenue endpoints ----------------------------------------------

def _order(**kw):
    base = dict(
        occurred_at="2026-06-26T10:00:00+00:00", product_id="SKU1", campaign_id=1,
        platform="etsy", sale_price=25, quantity=2, ai_cost=1, advertising_cost=4,
        production_cost=8, marketplace_fees=5, payment_fees=2,
    )
    base.update(kw)
    return base


def test_orders_endpoints(app_and_client):
    app, client = app_and_client
    order = app.state.revenue.record_order(_order())

    listed = client.get("/orders")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    one = client.get(f"/orders/{order['id']}")
    assert one.status_code == 200
    assert one.json()["net_profit"] == 30

    assert client.get("/orders/999").status_code == 404


def test_revenue_today_endpoint(app_and_client):
    app, client = app_and_client
    today = date.today().isoformat()
    app.state.revenue.record_order(_order(occurred_at=f"{today}T09:00:00+00:00"))

    body = client.get("/revenue/today").json()
    assert body["orders"] == 1
    assert body["gross_revenue"] == 50
    assert body["net_profit"] == 30


def test_revenue_month_endpoint(app_and_client):
    app, client = app_and_client
    ym = date.today().strftime("%Y-%m")
    app.state.revenue.record_order(_order(occurred_at=f"{ym}-15T09:00:00+00:00"))
    body = client.get("/revenue/month").json()
    assert body["period"] == ym
    assert body["net_profit"] == 30


def test_profit_endpoint(app_and_client):
    app, client = app_and_client
    app.state.revenue.record_order(_order())
    body = client.get("/profit").json()
    assert body["gross_revenue"] == 50
    assert body["net_profit"] == 30
    assert body["orders"] == 1


# --- Etsy endpoints -------------------------------------------------

class _StubEtsy:
    def __init__(self, receipts, listings):
        self._receipts, self._listings = receipts, listings

    def get_receipts(self, min_created=None):
        return list(self._receipts)

    def get_listings(self, state="active"):
        return list(self._listings)


def test_etsy_sync_and_reads(app_and_client):
    app, client = app_and_client
    # Inject a stub Etsy client into the app's connector (no network).
    app.state.etsy._client = _StubEtsy(
        receipts=[{
            "receipt_id": 1, "created_timestamp": 1782950400,
            "transactions": [{
                "transaction_id": 7, "listing_id": 9001, "quantity": 1,
                "price": {"amount": 4800, "divisor": 100, "currency_code": "GBP"},
            }],
        }],
        listings=[{
            "listing_id": 9001, "title": "Linen Throw", "state": "active",
            "price": {"amount": 4800, "divisor": 100, "currency_code": "GBP"},
            "views": 200, "num_favorers": 12, "created_timestamp": 1782950400,
        }],
    )

    sync = client.get("/etsy/sync").json()
    assert sync["configured"] is True
    assert sync["imported_orders"] == 1
    assert "metrics" in sync  # CEO business metrics returned after sync

    assert len(client.get("/etsy/orders").json()) == 1
    listings = client.get("/etsy/listings").json()
    assert listings[0]["revenue"] == 48.0
    stats = client.get("/etsy/stats").json()
    assert stats[0]["favourites"] == 12


def test_etsy_sync_not_configured(app_and_client):
    app, client = app_and_client
    app.state.etsy._client = None
    app.state.etsy.etsy_cfg = {}  # no credentials
    body = client.get("/etsy/sync").json()
    assert body["configured"] is False


# --- Publishing endpoints -------------------------------------------

def test_publish_and_status_endpoints(app_and_client, tmp_path):
    import json as _json
    from pathlib import Path

    app, client = app_and_client
    db = app.state.db
    # Approved campaign + a listing package on disk (isolated temp exports dir).
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    cid = db.insert_campaign({"name": "Salt", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": cid, "verdict": "APPROVE",
                                 "reasoning": "ok", "compliance_score": 90})
    app.state.publisher.listing_cfg = {"exports_dir": str(tmp_path / "exports")}
    folder = Path(tmp_path / "exports") / str(cid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "listing.json").write_text(_json.dumps(
        {"campaign_id": cid, "product_id": "SKU1", "title": "T", "description": "d",
         "tags": ["a"], "price": 30.0, "quantity": 50}))

    # Inject a stub write client so a real draft "publishes" offline.
    class _Stub:
        def create_draft(self, listing):
            return {"listing_id": 999}
    app.state.publisher._draft_client = _Stub()

    r = client.post(f"/publish/{cid}?mode=draft")
    assert r.status_code == 200
    assert r.json()["status"] == "draft"

    status = client.get("/publishing/status").json()
    assert status["total"] == 1
    assert status["by_status"]["draft"] == 1


def test_publish_unapproved_blocked(app_and_client):
    app, client = app_and_client
    db = app.state.db
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    cid = db.insert_campaign({"name": "C", "brief_id": brief_id})  # no approval
    assert client.post(f"/publish/{cid}").json()["status"] == "blocked"


def test_market_endpoints(app_and_client):
    app, client = app_and_client
    app.state.market.provider = FakeSignals()
    research = client.post("/market/research").json()
    assert research["count"] == 3
    report = client.get("/market/report").json()
    assert report and report[0]["opportunity_score"] >= report[-1]["opportunity_score"]
    assert {"keyword", "demand", "competition", "opportunity"} <= set(report[0])


def test_daily_report_endpoint(app_and_client):
    _, client = app_and_client
    r = client.get("/report/daily")
    assert r.status_code == 200
    body = r.json()
    # The five required sections are present.
    for key in ("revenue", "profit", "best_seller", "worst_seller", "recommendations"):
        assert key in body
    assert set(body["recommendations"]) == {"expand", "hold", "kill"}


def test_ceo_dashboard_endpoint(app_and_client):
    _, client = app_and_client
    r = client.get("/ceo/dashboard")
    assert r.status_code == 200
    body = r.json()
    for key in ("revenue_yesterday", "profit_yesterday", "visitors", "conversion",
                "pinterest_clicks", "products_launched", "products_retired",
                "cash_generated", "ai_cost", "roi", "headline"):
        assert key in body


def test_learning_and_portfolio_and_traffic_endpoints(app_and_client):
    _, client = app_and_client
    learning = client.get("/learning/daily")
    assert learning.status_code == 200
    assert "actions" in learning.json()

    assert client.get("/portfolio/reviews").status_code == 200
    assert client.get("/portfolio/archived").status_code == 200

    funnel = client.get("/traffic/funnel").json()
    for key in ("impressions", "clicks", "visits", "sales"):
        assert key in funnel
    assert client.get("/traffic/schedule").status_code == 200


def test_launch_endpoints_single_approval(app_and_client, tmp_path):
    import json as _json
    from pathlib import Path

    app, client = app_and_client
    db = app.state.db
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    cid = db.insert_campaign({"name": "Salt", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": cid, "verdict": "APPROVE",
                                 "reasoning": "ok", "compliance_score": 90})
    app.state.publisher.listing_cfg = {"exports_dir": str(tmp_path / "exports")}
    # This test covers the MANUAL single-approval flow (drafts → wait → approve).
    app.state.publisher.launch_policy = "manual"
    app.state.publisher.auto_go_live = False

    class _Stub:
        def create_draft(self, listing):
            return {"listing_id": 999}
    app.state.publisher._draft_client = _Stub()

    for key in ("ceramic_mug", "premium_poster"):
        db.insert_product_score({"campaign_id": cid, "product_key": key, "product_name": key,
                                 "launched": 1, "composite_score": 85, "ceo_verdict": "APPROVE"})
        folder = Path(tmp_path / "exports") / str(cid) / key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "listing.json").write_text(_json.dumps(
            {"campaign_id": cid, "product_id": f"{cid}-{key}", "product_key": key,
             "title": "T", "description": "d", "tags": ["a"], "price": 22.0, "quantity": 50}))

    # Manual policy: drafting happens via the daily cycle; here we reach
    # Launch Ready by drafting the products, then approve in one action.
    app.state.publisher.launch(cid, mode="draft")
    assert cid in {r["campaign_id"] for r in client.get("/launch/pending").json()}
    assert client.get(f"/launch/status/{cid}").json()["status"] == "launch_ready"

    approved = client.post(f"/launch/approve/{cid}").json()
    assert approved["status"] == "launched"
    assert set(approved["products"]) == {"ceramic_mug", "premium_poster"}
    assert client.get(f"/launch/status/{cid}").json()["status"] == "launched"
    assert client.get("/launch/pending").json() == []


# --- Listing endpoint -----------------------------------------------

def test_listing_endpoint(app_and_client):
    app, client = app_and_client
    db = app.state.db
    # An approved campaign is required.
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T",
                                "campaign_name": "Salt", "concept": "s", "keywords": []})
    cid = db.insert_campaign({"name": "Salt", "theme": "T", "story": "s", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": cid, "verdict": "APPROVE",
                                 "reasoning": "ok", "compliance_score": 90})
    # Inject fakes so the factory runs offline.
    app.state.listing_factory._llm = FakeLLM({
        "title": "Linen Throw", "description": "A lovely throw.",
        "tags": [f"t{i}" for i in range(13)], "materials": ["linen"],
        "primary_colour": "Ecru", "secondary_colour": "Terracotta",
        "category": "Home", "seo_keywords": ["linen throw"],
        "image_alt_texts": ["a", "b", "c", "d", "e"],
        "product_attributes": [{"name": "room", "value": "Living"}],
    })
    app.state.listing_factory.compliance._llm = FakeLLM(make_compliance_response())

    r = client.get(f"/listing/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["listing"]["title"] == "Linen Throw"
    assert body["validation"]["all_images_present"] is True


def test_listing_endpoint_missing_campaign(app_and_client):
    _, client = app_and_client
    assert client.get("/listing/999").status_code == 404


def test_listing_endpoint_blocked_when_unapproved(app_and_client):
    app, client = app_and_client
    db = app.state.db
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    cid = db.insert_campaign({"name": "C", "brief_id": brief_id})  # no compliance approval
    r = client.get(f"/listing/{cid}")
    assert r.status_code == 409


# --- Operations endpoints -------------------------------------------

def test_operations_check_status_report(app_and_client):
    _, client = app_and_client

    check = client.post("/operations/check?mode=production").json()
    assert "healthy" in check and "checks" in check

    # Before any run, status reports it's never run.
    assert client.get("/operations/status").json()["status"] == "never_run"

    # A dry run goes through Operations and produces a report.
    run = client.post("/daily/run?mode=dry_run").json()
    assert run["aborted"] is False
    assert "operations_report" in run

    status = client.get("/operations/status").json()
    assert status["status"] in ("completed", "completed_with_failures")
    report = client.get("/operations/report").json()
    assert set(report["business"]) and "system" in report


def test_production_readiness_endpoint(app_and_client):
    _, client = app_and_client
    report = client.get("/production/readiness").json()
    assert set(report) >= {"modules", "subsystems", "blockers", "checklist",
                           "summary", "production_ready"}
    assert len(report["subsystems"]) == 9
    assert all(c["mark"] in ("✅", "❌") for c in report["checklist"])


# --- Daily cycle endpoints ------------------------------------------

def test_daily_run_and_status_history(app_and_client):
    _, client = app_and_client
    r = client.post("/daily/run?mode=dry_run")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "dry_run"
    assert len(body["stages"]) == 20

    status = client.get("/daily/status").json()
    assert status["mode"] == "dry_run"
    assert len(client.get("/daily/history").json()) == 1


# --- Experiments endpoints ------------------------------------------

def test_experiments_endpoints(app_and_client):
    app, client = app_and_client

    started = client.post("/experiments", json={
        "product_id": "SKU1", "variable": "title",
        "hypothesis": "Better title sells more", "success_metric": "conversion_rate",
        "baseline_value": 0.02,
    }).json()
    eid = started["id"]
    assert started["status"] == "active"

    # Duplicate active experiment on the same variable -> 409.
    dup = client.post("/experiments", json={
        "product_id": "SKU1", "variable": "title",
        "hypothesis": "again", "success_metric": "conversion_rate"})
    assert dup.status_code == 409

    assert len(client.get("/experiments/active").json()) == 1
    assert client.get(f"/experiments/{eid}").json()["id"] == eid

    done = client.post(f"/experiments/{eid}/complete",
                       json={"result_value": 0.05}).json()
    assert done["result"] == "win"
    assert client.get("/experiments/active").json() == []
    assert client.get("/experiments/999").status_code == 404


# --- Analytics endpoints --------------------------------------------

def test_analytics_endpoints(app_and_client):
    app, client = app_and_client
    db = app.state.db
    # Seed two days of history for one product.
    for day, views in (("2026-06-20", 100), ("2026-06-27", 250)):
        db.insert_metric_snapshots([
            {"platform": "etsy", "product_id": "P1", "campaign_id": 5, "metric": "views",
             "value": views, "snapshot_date": day},
            {"platform": "etsy", "product_id": "P1", "campaign_id": 5, "metric": "revenue",
             "value": views, "snapshot_date": day},
        ])

    overall = client.get("/analytics").json()
    assert overall["snapshots"] == 4
    assert "P1" in overall["products"]

    product = client.get("/analytics/product/P1").json()
    assert product["trends"]["traffic_trend"]["label"] == "up"

    campaign = client.get("/analytics/campaign/5").json()
    assert campaign["history_points"] == 4


# --- Optimiser endpoint ---------------------------------------------

def test_optimiser_endpoint(app_and_client):
    app, client = app_and_client
    db = app.state.db
    db.insert_product({"sku": "9001", "name": "Linen Throw", "production_cost": 14})
    app.state.revenue.record_order(_order(product_id="9001"))

    body = client.get("/optimiser").json()
    for key in ("product_analysed", "recommendation", "expected_roi",
                "reasoning", "confidence"):
        assert key in body
    assert body["product_analysed"] == "9001"
    assert body["ceo_verdict"] in ("APPROVE", "REJECT", "REQUEST_MORE_INFO")


def test_optimiser_endpoint_no_products(app_and_client):
    _, client = app_and_client
    assert "message" in client.get("/optimiser").json()


# --- Opportunities endpoints ----------------------------------------

def test_opportunities_endpoints(app_and_client):
    app, client = app_and_client
    app.state.opportunities._llm = FakeLLM({"opportunities": [{
        "theme": "Slow coastal mornings", "target_customer": "travellers",
        "emotional_angle": "calm luxury", "product_type": "linen tea towel",
        "search_intent": "mediterranean linen", "seasonal_relevance": "summer",
        "commercial_score": 85, "originality_score": 80, "brand_fit_score": 90,
        "estimated_demand": 80, "estimated_competition": 30, "confidence": 80,
        "product_name": "Amalfi Morning Linen",
        "concept": "A linen tea towel evoking slow Amalfi mornings.",
        "colour_palette": ["citrus", "whitewash"], "typography_style": "serif",
        "illustration_style": "watercolour", "photography_style": "morning light",
        "mockup_style": "terrace flatlay",
    }]})

    gen = client.post("/opportunities/generate?count=1").json()
    assert gen["generated"] == 1
    oid = gen["opportunities"][0]["opportunity_id"]
    assert oid.startswith("OPP-")

    all_opps = client.get("/opportunities").json()
    assert any(o["opportunity_id"] == oid for o in all_opps)

    # The backlog is ranked by expected commercial value (best first).
    top = client.get("/opportunities/top?limit=20").json()
    values = [o["expected_value"] for o in top]
    assert values == sorted(values, reverse=True)
    assert any(o["opportunity_id"] == oid for o in top)


# --- Expansion endpoints --------------------------------------------

def test_expansion_endpoints(app_and_client):
    _, client = app_and_client

    catalogue = client.get("/expansion/catalogue").json()
    assert len(catalogue) == 10

    plan = client.post("/expansion/plan/1").json()
    assert plan["products_scored"] == 10
    assert 1 <= plan["products_launched"] < 10   # the commercial set, not all ten

    scores = client.get("/expansion/plan/1").json()
    assert len(scores) == 10 and scores[0]["composite_score"] >= scores[-1]["composite_score"]

    assert isinstance(client.get("/expansion/performance").json(), list)


def test_build_design_package_endpoint(app_and_client, tmp_path):
    app, client = app_and_client
    app.state.config.design = {**app.state.config.design, "exports_dir": str(tmp_path)}
    app.state.opportunities._llm = FakeLLM({"opportunities": [{
        "theme": "Slow coastal mornings", "target_customer": "travellers",
        "emotional_angle": "calm luxury", "product_type": "t-shirt",
        "search_intent": "mediterranean tee", "seasonal_relevance": "summer",
        "commercial_score": 85, "originality_score": 80, "brand_fit_score": 90,
        "estimated_demand": 80, "estimated_competition": 30, "confidence": 80,
        "product_name": "Amalfi Morning Tee", "concept": "A tee for slow mornings.",
        "colour_palette": ["citrus"], "typography_style": "serif",
        "illustration_style": "watercolour", "photography_style": "light",
        "mockup_style": "flatlay",
    }]})
    app.state.design_builder._llm = FakeLLM({
        "shirt_colour": "ecru", "print_colour": "terracotta",
        "typography_direction": "serif lowercase", "layout_direction": "centred",
        "print_placement": "centre chest", "print_size_guidance": "25cm wide",
        "artwork_description": "A line-drawn lemon branch.",
        "mockup_scene": "tee on linen", "design_rationale": "On theme.",
        "listing_title_seed": "Amalfi Tee", "listing_tags_seed": ["lemon", "coastal"],
        "listing_description_seed": "A calm tee.",
    })
    app.state.design_builder.compliance._llm = FakeLLM(make_compliance_response())

    oid = client.post("/opportunities/generate?count=1").json()["opportunities"][0]["opportunity_id"]
    built = client.post(f"/opportunities/{oid}/build-design-package").json()
    assert built["status"] == "ready"
    assert set(built["files"]) == {
        "design_brief.json", "print_spec.json", "artwork_prompt.txt",
        "mockup_prompt.txt", "listing_seed.json", "compliance_report.json"}

    fetched = client.get(f"/opportunities/{oid}/design-package").json()
    assert fetched["design_brief"]["product_name"] == "Amalfi Morning Tee"
    # A package was never built for an unknown opportunity.
    assert client.get("/opportunities/OPP-missing/design-package").status_code == 404


# --- Swagger / OpenAPI docs -----------------------------------------

def test_swagger_docs_available(app_and_client):
    _, client = app_and_client
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    for path in ("/health", "/campaign/create", "/campaigns",
                 "/campaign/{campaign_id}", "/campaign/latest", "/dashboard",
                 "/revenue/today", "/revenue/month", "/profit",
                 "/orders", "/orders/{order_id}",
                 "/etsy/orders", "/etsy/listings", "/etsy/stats", "/etsy/sync",
                 "/etsy/oauth/login", "/etsy/oauth/callback", "/etsy/oauth/status",
                 "/optimiser", "/listing/{campaign_id}",
                 "/listing/{campaign_id}/products", "/publish/{campaign_id}/products",
                 "/opportunities", "/opportunities/top", "/opportunities/generate",
                 "/opportunities/{opportunity_id}/build-design-package",
                 "/opportunities/{opportunity_id}/design-package",
                 "/expansion/catalogue", "/expansion/plan/{campaign_id}",
                 "/expansion/performance",
                 "/publish/{campaign_id}", "/publishing/status",
                 "/launch/approve/{campaign_id}", "/launch/status/{campaign_id}",
                 "/launch/pending", "/report/daily",
                 "/ceo/dashboard", "/learning/daily", "/portfolio/reviews",
                 "/portfolio/archived", "/traffic/schedule", "/traffic/funnel",
                 "/traffic/run",
                 "/marketing/{product_key}",
                 "/fulfilment/status", "/fulfilment/run", "/etsy/changes",
                 "/etsy/intelligence", "/etsy/search-terms",
                 "/protection/audit", "/protection/alerts",
                 "/market/report", "/market/research",
                 "/analytics", "/analytics/product/{product_id}",
                 "/analytics/campaign/{campaign_id}",
                 "/experiments", "/experiments/{experiment_id}", "/experiments/active",
                 "/daily/run", "/daily/status", "/daily/history",
                 "/operations/check", "/operations/status", "/operations/report",
                 "/production/readiness"):
        assert path in paths


def test_create_without_product_returns_409(config):
    """If the cycle can't create a product (e.g. generation fails), the one
    workflow returns a clean 409 — never a half-finished campaign."""
    app = create_app(config)
    # No fakes — opportunity generation fails, so no product/campaign is created.
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/campaign/create")
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/json")
    assert "detail" in r.json()
