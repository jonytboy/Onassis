"""Tests for the Etsy OAuth 2.0 (PKCE) flow, token storage, and refresh.

Everything is exercised offline: the HTTP call to Etsy's token endpoint and the
clock are injected, so no network and no real credentials are involved.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat

import pytest

from onassis.connectors.etsy_client import EtsyClient, EtsyConfigError
from onassis.connectors.etsy_oauth import (
    EtsyAuthError,
    EtsyOAuth,
    TokenStore,
    build_etsy_oauth,
    code_challenge,
    generate_code_verifier,
)


class FakeClock:
    """A controllable monotonic-ish clock."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeTokenEndpoint:
    """Records token requests and returns canned Etsy token responses."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.counter = 0

    def __call__(self, url: str, payload: dict) -> dict:
        self.calls.append({"url": url, "payload": payload})
        self.counter += 1
        return {
            "access_token": f"123.access-{self.counter}",
            "refresh_token": f"123.refresh-{self.counter}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "listings_r listings_w",
        }


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "etsy_tokens.json")


@pytest.fixture
def http():
    return FakeTokenEndpoint()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def oauth(store, http, clock):
    return EtsyOAuth(
        client_id="key-123",
        client_secret="secret-xyz",
        redirect_uri="http://localhost:8000/etsy/oauth/callback",
        scopes=["listings_r", "listings_w"],
        store=store,
        http_post=http,
        clock=clock,
        refresh_leeway=120,
    )


# --- PKCE ------------------------------------------------------------

def test_code_verifier_is_url_safe_and_long():
    v = generate_code_verifier()
    assert 43 <= len(v) <= 128
    assert all(c.isalnum() or c in "-_" for c in v)


def test_code_challenge_is_s256_of_verifier():
    v = "test-verifier-value"
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()
    ).rstrip(b"=").decode()
    assert code_challenge(v) == expected


# --- Token storage ---------------------------------------------------

def test_token_store_round_trip_and_permissions(store):
    store.save({"access_token": "a", "refresh_token": "r"})
    assert store.load() == {"access_token": "a", "refresh_token": "r"}
    assert store.exists
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600  # never world/group readable
    store.clear()
    assert store.load() is None


def test_token_store_pending_state(store):
    store.save_pending("state-1", "verifier-1")
    assert store.load_pending() == {"state": "state-1", "code_verifier": "verifier-1"}
    store.clear_pending()
    assert store.load_pending() is None


# --- Authorisation URL ----------------------------------------------

def test_authorization_url_has_required_params_and_saves_pending(oauth, store):
    from urllib.parse import parse_qs, urlparse

    auth = oauth.create_authorization_url()
    q = parse_qs(urlparse(auth["url"]).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["key-123"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["listings_r listings_w"]
    # The challenge must match the saved verifier.
    pending = store.load_pending()
    assert q["code_challenge"] == [code_challenge(pending["code_verifier"])]
    assert q["state"] == [pending["state"]]


def test_authorization_url_requires_configuration(store, http, clock):
    bad = EtsyOAuth(client_id=None, redirect_uri=None, scopes=[], store=store,
                    http_post=http, clock=clock)
    assert bad.is_configured is False
    with pytest.raises(EtsyAuthError):
        bad.create_authorization_url()


# --- Code exchange ---------------------------------------------------

def test_exchange_code_stores_tokens_and_clears_pending(oauth, store, http, clock):
    auth = oauth.create_authorization_url()
    tokens = oauth.exchange_code("auth-code", state=auth["state"])

    payload = http.calls[-1]["payload"]
    assert payload["grant_type"] == "authorization_code"
    assert payload["client_id"] == "key-123"
    assert payload["code"] == "auth-code"
    assert payload["code_verifier"] == auth["code_verifier"]
    # PKCE flow never transmits the shared secret.
    assert "client_secret" not in payload

    assert tokens["access_token"] == "123.access-1"
    assert tokens["expires_at"] == clock.t + 3600
    assert store.load()["refresh_token"] == "123.refresh-1"
    assert store.load_pending() is None  # consumed


def test_exchange_code_rejects_state_mismatch(oauth):
    oauth.create_authorization_url()
    with pytest.raises(EtsyAuthError):
        oauth.exchange_code("auth-code", state="not-the-saved-state")


def test_exchange_without_verifier_fails(oauth):
    with pytest.raises(EtsyAuthError):
        oauth.exchange_code("auth-code")  # no pending state, no verifier


# --- Refresh + valid_access_token -----------------------------------

def test_refresh_uses_refresh_token(oauth, http):
    oauth.create_authorization_url()
    oauth.exchange_code("auth-code")
    new = oauth.refresh()
    payload = http.calls[-1]["payload"]
    assert payload["grant_type"] == "refresh_token"
    assert payload["refresh_token"] == "123.refresh-1"
    assert new["access_token"] == "123.access-2"


def test_valid_access_token_refreshes_only_when_expired(oauth, http, clock):
    oauth.create_authorization_url()
    oauth.exchange_code("auth-code")
    n_after_exchange = http.counter

    # Still valid -> no refresh.
    assert oauth.valid_access_token() == "123.access-1"
    assert http.counter == n_after_exchange

    # Past expiry (minus leeway) -> refresh once.
    clock.advance(3600)
    assert oauth.valid_access_token() == "123.access-2"
    assert http.counter == n_after_exchange + 1


def test_valid_access_token_without_authorisation_raises(oauth):
    with pytest.raises(EtsyAuthError):
        oauth.valid_access_token()


def test_refresh_keeps_old_refresh_token_if_omitted(store, clock):
    """Be defensive if Etsy doesn't return a new refresh token on refresh."""
    def endpoint(url, payload):
        return {"access_token": "123.acc", "token_type": "Bearer", "expires_in": 3600}

    store.save({"access_token": "old", "refresh_token": "keep-me",
                "expires_at": clock.t - 1})
    oauth = EtsyOAuth(client_id="k", redirect_uri="r", scopes=[], store=store,
                      http_post=endpoint, clock=clock)
    tokens = oauth.refresh()
    assert tokens["refresh_token"] == "keep-me"


# --- status ----------------------------------------------------------

def test_status_reports_authorisation(oauth, clock):
    assert oauth.status()["authorised"] is False
    oauth.create_authorization_url()
    oauth.exchange_code("auth-code")
    st = oauth.status()
    assert st["configured"] and st["authorised"]
    assert st["has_access_token"] is True
    assert st["expired"] is False
    assert st["expires_in"] == 3600


# --- EtsyClient integration -----------------------------------------

def test_client_uses_token_provider(oauth, http):
    oauth.create_authorization_url()
    oauth.exchange_code("auth-code")
    client = EtsyClient(api_key="key-123", shop_id="9", token_provider=oauth.valid_access_token)
    headers = client._headers()
    assert headers["x-api-key"] == "key-123"
    assert headers["Authorization"] == "Bearer 123.access-1"


def test_client_requires_a_token_or_provider():
    with pytest.raises(EtsyConfigError):
        EtsyClient(api_key="k", shop_id="9")  # neither access_token nor provider


def test_static_access_token_still_works():
    client = EtsyClient(api_key="k", shop_id="9", access_token="static-tok")
    assert client._headers()["Authorization"] == "Bearer static-tok"


# --- build_etsy_oauth from config -----------------------------------

def test_config_reads_client_id_and_secret_from_env(monkeypatch):
    from onassis.config import load_config

    monkeypatch.setenv("ETSY_CLIENT_ID", "env-client-id")
    monkeypatch.setenv("ETSY_CLIENT_SECRET", "env-client-secret")
    monkeypatch.setenv("ETSY_SHOP_ID", "424242")
    cfg = load_config()
    assert cfg.etsy["client_id"] == "env-client-id"
    assert cfg.etsy["client_secret"] == "env-client-secret"
    # The keystring doubles as the x-api-key.
    assert cfg.etsy["api_key"] == "env-client-id"
    assert cfg.etsy["shop_id"] == "424242"


def test_build_etsy_oauth_reads_config(config, tmp_path):
    config.etsy = {
        **config.etsy,
        "client_id": "cfg-id", "client_secret": "cfg-secret", "shop_id": "9",
        "redirect_uri": "http://localhost/cb",
        "scopes": ["listings_r"],
        "token_store": str(tmp_path / "tok.json"),
    }
    oauth = build_etsy_oauth(config)
    assert oauth.client_id == "cfg-id"
    assert oauth.client_secret == "cfg-secret"
    assert oauth.redirect_uri == "http://localhost/cb"
    assert oauth.is_configured is True
