"""Tests for production security — auth, rate limiting, headers, public routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from onassis.api import create_app
from onassis.security import security_enabled

_KEY = "test-secret-key-123"


def _prod_app(config, tmp_path, **security):
    config.environment = "production"
    config.security = {"api_key": _KEY, **security}
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    # A public export file so Gelato-style fetches can be exercised.
    folder = tmp_path / "exports" / "1" / "ceramic_mug"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "print_file.png").write_bytes(b"\x89PNG\r\n\x1a\nfile")
    return TestClient(create_app(config))


# --- Environment resolution -------------------------------------------

def test_security_off_in_development_on_in_production(config):
    config.environment = "development"
    assert security_enabled(config) is False
    config.environment = "production"
    assert security_enabled(config) is True
    config.environment = "production"
    config.security = {"enabled": False}          # explicit override wins
    assert security_enabled(config) is False


def test_development_requires_no_key(config, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    client = TestClient(create_app(config))       # dev by default
    assert client.get("/campaigns").status_code == 200   # no key needed


# --- Authentication ----------------------------------------------------

def test_protected_endpoint_401_without_key_in_production(config, tmp_path):
    client = _prod_app(config, tmp_path)
    r = client.get("/campaigns")
    assert r.status_code == 401


def test_protected_endpoint_200_with_correct_key(config, tmp_path):
    client = _prod_app(config, tmp_path)
    r = client.get("/campaigns", headers={"X-API-Key": _KEY})
    assert r.status_code == 200


def test_wrong_key_is_401(config, tmp_path):
    client = _prod_app(config, tmp_path)
    assert client.get("/campaigns", headers={"X-API-Key": "nope"}).status_code == 401


def test_bearer_token_is_accepted(config, tmp_path):
    client = _prod_app(config, tmp_path)
    r = client.get("/campaigns", headers={"Authorization": f"Bearer {_KEY}"})
    assert r.status_code == 200


def test_missing_api_key_config_fails_closed(config, tmp_path):
    # Security on but NO key configured -> everything protected is 401 (fail closed).
    config.environment = "production"
    config.security = {"enabled": True}           # no api_key
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    client = TestClient(create_app(config))
    assert client.get("/campaigns").status_code == 401


# --- Public routes stay open ------------------------------------------

def test_public_routes_need_no_key_in_production(config, tmp_path):
    client = _prod_app(config, tmp_path)
    assert client.get("/health").status_code == 200        # load-balancer probe
    assert client.get("/").status_code == 200              # root
    # Gelato can still fetch the print file (success criterion).
    assert client.get("/exports/1/ceramic_mug/print_file.png").status_code == 200


# --- Swagger protection -----------------------------------------------

def test_swagger_is_unavailable_publicly_in_production(config, tmp_path):
    client = _prod_app(config, tmp_path)
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    # ...but reachable with a key.
    assert client.get("/openapi.json", headers={"X-API-Key": _KEY}).status_code == 200


# --- Rate limiting -----------------------------------------------------

def test_rate_limit_returns_429_when_exceeded(config, tmp_path):
    client = _prod_app(config, tmp_path, rate_limit_requests=3, rate_limit_window_seconds=60)
    h = {"X-API-Key": _KEY}
    codes = [client.get("/campaigns", headers=h).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


def test_public_routes_are_not_rate_limited(config, tmp_path):
    client = _prod_app(config, tmp_path, rate_limit_requests=2, rate_limit_window_seconds=60)
    codes = [client.get("/health").status_code for _ in range(6)]
    assert all(c == 200 for c in codes)            # health probes never throttled


# --- Security headers --------------------------------------------------

def test_security_headers_present(config, tmp_path):
    client = _prod_app(config, tmp_path)
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" in r.headers        # production only
    assert "referrer-policy" in r.headers


def test_headers_applied_but_no_hsts_in_development(config, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    client = TestClient(create_app(config))       # dev
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in r.headers    # no HSTS on plain http
