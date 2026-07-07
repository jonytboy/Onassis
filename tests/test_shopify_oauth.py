"""Tests for the Shopify client-credentials token provider (Sprint 41.1 fix)."""

from __future__ import annotations

import pytest

from onassis.connectors.shopify import ShopifyAdminClient, ShopifyConnector
from onassis.connectors.shopify_oauth import ShopifyTokenProvider


class FakeClock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


def _provider(tmp_path, clock, calls, *, expires_in=3600, client_id="cid",
              client_secret="csec"):
    def http_post(url, body):
        calls.append(body)
        assert url.endswith("/admin/oauth/access_token")
        assert body["grant_type"] == "client_credentials"
        return {"access_token": f"tok-{len(calls)}", "expires_in": expires_in}
    return ShopifyTokenProvider(
        store_domain="x.myshopify.com", client_id=client_id, client_secret=client_secret,
        store_path=tmp_path / "shopify_token.json", http_post=http_post, clock=clock)


def test_fetches_token_via_client_credentials(tmp_path):
    calls = []
    p = _provider(tmp_path, FakeClock(), calls)
    assert p.valid_access_token() == "tok-1"
    assert len(calls) == 1                      # one exchange


def test_caches_token_until_expiry(tmp_path):
    calls = []
    clock = FakeClock(1000.0)
    p = _provider(tmp_path, clock, calls, expires_in=3600)
    p.valid_access_token()
    p.valid_access_token()                      # still fresh → no new call
    assert len(calls) == 1
    clock.t = 1000.0 + 3600                      # now expired (past leeway)
    assert p.valid_access_token() == "tok-2"
    assert len(calls) == 2


def test_force_refreshes(tmp_path):
    calls = []
    p = _provider(tmp_path, FakeClock(), calls)
    p.valid_access_token()
    assert p.valid_access_token(force=True) == "tok-2"
    assert len(calls) == 2


def test_token_persisted_across_instances(tmp_path):
    calls = []
    clock = FakeClock(1000.0)
    _provider(tmp_path, clock, calls).valid_access_token()   # writes cache
    # A fresh provider (same store path) loads the cached token — no new call.
    p2 = _provider(tmp_path, clock, calls)
    assert p2.valid_access_token() == "tok-1"
    assert len(calls) == 1


def test_is_authorised_and_missing_creds(tmp_path):
    calls = []
    assert _provider(tmp_path, FakeClock(), calls).is_authorised is True
    nocreds = ShopifyTokenProvider(store_domain="x.myshopify.com", client_id=None,
                                   client_secret=None, store_path=tmp_path / "t.json")
    assert nocreds.is_authorised is False
    with pytest.raises(RuntimeError, match="client_id/client_secret"):
        nocreds.valid_access_token()


def test_raises_when_no_token_returned(tmp_path):
    p = ShopifyTokenProvider(store_domain="x.myshopify.com", client_id="c",
                             client_secret="s", store_path=tmp_path / "t.json",
                             http_post=lambda url, body: {"error": "invalid_client"})
    with pytest.raises(RuntimeError, match="no access_token"):
        p.valid_access_token()


# --- Connector wiring ------------------------------------------------

def test_connector_uses_client_credentials_when_no_static_token(config):
    config.shopify = {"store_domain": "x.myshopify.com", "client_id": "c",
                      "client_secret": "s", "api_version": "2024-10"}
    conn = ShopifyConnector(config)
    assert conn.is_configured is True
    client = conn._c()
    assert client._token_provider is not None      # provider-backed
    assert client._access_token is None


def test_connector_still_accepts_legacy_static_token(config):
    config.shopify = {"store_domain": "x.myshopify.com", "admin_token": "shpat_legacy"}
    conn = ShopifyConnector(config)
    assert conn.is_configured is True
    client = conn._c()
    assert client._token_provider is None
    assert client._token() == "shpat_legacy"


def test_admin_client_refreshes_token_on_provider(config):
    # The client asks the provider for a token; force=True yields a fresh one.
    tokens = iter(["first", "second"])
    client = ShopifyAdminClient("x.myshopify.com",
                                token_provider=lambda force=False: next(tokens))
    assert client._token() == "first"
    assert client._token(force=True) == "second"
