"""Tests for the Operations Centre — the browser control room."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from onassis.api import create_app


@pytest.fixture
def client(config, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    config.environment = "development"          # security off -> open
    return TestClient(create_app(config))


def _prod_client(config, tmp_path, key="ops-key"):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    config.environment = "production"
    config.security = {"api_key": key}
    return TestClient(create_app(config)), key


# --- Shell + status board --------------------------------------------

def test_shell_renders_with_run_business(client):
    r = client.get("/operations")
    assert r.status_code == 200
    assert "RUN BUSINESS" in r.text
    assert "ONASSIS" in r.text and "Operations Centre" in r.text


def test_status_board_has_all_traffic_lights(client):
    s = client.get("/operations/api/status").json()
    lights = s["lights"]
    for k in ("environment", "server", "service", "api", "disk", "memory", "database",
              "anthropic", "openai", "etsy", "gelato", "pinterest", "facebook",
              "instagram", "https"):
        assert k in lights and lights[k]["status"] in {"green", "amber", "red", "grey"}
    assert s["overall"]["status"] in {"green", "amber", "red"}
    assert "git_commit" in s and "business_mode" in s


def test_uncredentialled_integrations_are_red_or_grey(client):
    lights = client.get("/operations/api/status").json()["lights"]
    # No Pinterest/Gelato creds in the test config -> red; FB/IG not built -> grey.
    assert lights["gelato"]["status"] in {"red", "amber"}
    assert lights["facebook"]["status"] == "grey"
    assert lights["instagram"]["status"] == "grey"


# --- Data tabs -------------------------------------------------------

def test_business_products_marketing_approvals_system(client):
    b = client.get("/operations/api/business").json()
    for k in ("revenue_yesterday", "profit_yesterday", "roi", "recommendation"):
        assert k in b
    assert client.get("/operations/api/products").json()["products"] == []
    m = client.get("/operations/api/marketing").json()
    assert "channels" in m and "funnel" in m
    a = client.get("/operations/api/approvals").json()
    assert set(a) >= {"auto_approved", "needs_review", "blocked", "confidence_threshold"}
    sysd = client.get("/operations/api/system").json()
    assert "git_commit" in sysd and "env_masked" in sysd
    # Secrets are masked, never returned raw.
    assert all(e["value"] in ("—", "set") or "…" in e["value"] or e["key"] == "ONASSIS_ENV"
               for e in sysd["env_masked"])


# --- RUN BUSINESS (faked cycle) --------------------------------------

def test_run_business_executes_and_reports(client):
    app = client.app
    app.state.daily.run = lambda mode="production": {"status": "completed",
                                                     "products_launched": 2}
    assert client.post("/operations/api/run-business").json()["status"] == "started"
    # The worker runs in a thread; poll the run state until it finishes.
    for _ in range(40):
        run = client.get("/operations/api/run").json()
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "completed"
    assert run["summary"]["products_launched"] == 2
    # The live log captured the run.
    logs = client.get("/operations/api/logs").json()["logs"]
    assert any("RUN BUSINESS" in x["line"] for x in logs)


def test_run_business_blocked_when_stopped(client):
    client.post("/operations/api/control/emergency-stop")
    r = client.post("/operations/api/run-business")
    assert r.status_code == 409


# --- Business controls ----------------------------------------------

def test_pause_resume_emergency_stop(client):
    assert client.post("/operations/api/control/pause").json()["mode"] == "paused"
    assert client.get("/operations/api/status").json()["business_mode"] == "paused"
    assert client.post("/operations/api/control/resume").json()["mode"] == "running"
    assert client.post("/operations/api/control/emergency-stop").json()["mode"] == "emergency_stop"


def test_unknown_control_action_is_400(client):
    assert client.post("/operations/api/control/nonsense").status_code == 400


# --- Auth (production) ------------------------------------------------

def test_shell_is_public_but_api_requires_key_in_production(config, tmp_path):
    cl, key = _prod_client(config, tmp_path)
    assert cl.get("/operations").status_code == 200                 # shell is public
    assert cl.get("/operations/api/status").status_code == 401      # data needs the key
    assert cl.get("/operations/api/status", headers={"X-API-Key": key}).status_code == 200
    # Query-param key works too (for browser EventSource-style calls).
    assert cl.get(f"/operations/api/status?key={key}").status_code == 200


def test_legacy_operations_endpoints_stay_globally_protected(config, tmp_path):
    cl, key = _prod_client(config, tmp_path)
    # /operations/status (the OperationsManager, not the centre) is NOT exempt.
    assert cl.get("/operations/status").status_code == 401
    assert cl.get("/operations/status", headers={"X-API-Key": key}).status_code == 200
