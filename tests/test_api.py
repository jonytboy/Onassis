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
                 "/orders", "/orders/{order_id}"):
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
