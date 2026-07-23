"""The dashboard Pinterest OAuth flow (Sprint 49) — login redirect + callback."""

from __future__ import annotations

from fastapi.testclient import TestClient

from onassis.api import create_app


def _client(config, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    config.environment = "development"          # global auth off
    config.pinterest = {"app_id": "appid123", "app_secret": "sek",
                        "redirect_uri": "https://x/cb", "board_id": "b1"}
    return TestClient(create_app(config))


def test_login_redirects_to_pinterest_with_write_scope(config, tmp_path):
    client = _client(config, tmp_path)
    r = client.get("/pinterest/oauth/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert "pinterest.com/oauth" in loc and "client_id=appid123" in loc
    assert "pins%3Awrite" in loc                 # the whole point


def test_login_requires_app_credentials(config, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    config.environment = "development"
    config.pinterest = {}                        # nothing configured
    client = TestClient(create_app(config))
    assert client.get("/pinterest/oauth/login", follow_redirects=False).status_code == 409


def test_callback_exchanges_code_and_saves_the_write_token(config, tmp_path, monkeypatch):
    client = _client(config, tmp_path)

    def fake_exchange(app_id, app_secret, code, redirect_uri, *, base_url):
        assert code == "the-code" and app_id == "appid123"
        return {"access_token": "wr1te-token", "scope": "pins:write"}

    monkeypatch.setattr("onassis.connectors.pinterest_oauth.exchange_code", fake_exchange)
    r = client.get("/pinterest/oauth/callback?code=the-code", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "connected=pinterest" in r.headers["location"]
    # The write token was persisted to the Pinterest integration.
    resolved = client.app.state.integrations.resolve("pinterest")
    assert resolved["access_token"] == "wr1te-token"


def test_callback_rejects_a_denied_authorisation(config, tmp_path):
    client = _client(config, tmp_path)
    r = client.get("/pinterest/oauth/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 400
