"""Tests for the REST API — one per endpoint, all LLMs mocked (offline)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from onassis.api import create_app
from tests.conftest import FakeLLM, make_compliance_response, make_content_response

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


@pytest.fixture
def app_and_client(config, sample_brief):
    """An app on a throwaway DB, with the pipeline's LLMs faked."""
    app = create_app(config)
    orch = app.state.orchestrator
    t = config.content_targets
    orch.director._llm = FakeLLM(sample_brief)
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

def test_get_campaign_full_view(app_and_client, config, sample_brief):
    _, client = app_and_client
    cid = client.post("/campaign/create").json()["campaign_id"]
    r = client.get(f"/campaign/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == cid
    assert body["name"] == sample_brief["campaign_name"]
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


# --- Daily cycle endpoints ------------------------------------------

def test_daily_run_and_status_history(app_and_client):
    _, client = app_and_client
    r = client.post("/daily/run?mode=dry_run")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "dry_run"
    assert len(body["stages"]) == 10

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
                 "/optimiser", "/listing/{campaign_id}",
                 "/publish/{campaign_id}", "/publishing/status",
                 "/analytics", "/analytics/product/{product_id}",
                 "/analytics/campaign/{campaign_id}",
                 "/experiments", "/experiments/{experiment_id}", "/experiments/active",
                 "/daily/run", "/daily/status", "/daily/history"):
        assert path in paths


def test_create_failure_returns_500(config):
    """If generation raises, the endpoint surfaces a clean 500 (JSON)."""
    app = create_app(config)
    # Don't inject fakes — the real LLMClient has no usable key path here.
    # Force a failure by pointing the director at a raising fake.
    class _Boom:
        def generate_json(self, **_):
            raise RuntimeError("kaboom")

    app.state.orchestrator.director._llm = _Boom()
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/campaign/create")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    assert "detail" in r.json()
