"""Tests for the REST API — one per endpoint, all LLMs mocked (offline)."""

from __future__ import annotations

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


# --- Swagger / OpenAPI docs -----------------------------------------

def test_swagger_docs_available(app_and_client):
    _, client = app_and_client
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    for path in ("/health", "/campaign/create", "/campaigns",
                 "/campaign/{campaign_id}", "/campaign/latest"):
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
