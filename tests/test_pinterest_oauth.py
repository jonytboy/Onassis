"""Tests for the Pinterest OAuth helper (Sprint 49) — write-scoped tokens."""

from __future__ import annotations

from onassis.connectors.pinterest_oauth import (
    PinterestOAuthError, authorize_url, exchange_code,
)


def test_authorize_url_requests_write_scopes():
    url = authorize_url("appid123", "https://x/callback")
    assert url.startswith("https://www.pinterest.com/oauth/?")
    assert "client_id=appid123" in url
    assert "response_type=code" in url
    # The whole point: write scopes are requested (url-encoded)...
    assert "pins%3Awrite" in url and "boards%3Awrite" in url
    # ...plus user_accounts:read, which the Test Connection / whoami call needs.
    assert "user_accounts%3Aread" in url
    assert "callback" in url


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = str(data)

    def json(self):
        return self._data


class _Transport:
    def __init__(self, resp):
        self.resp = resp
        self.last = None

    def post(self, url, headers=None, data=None, timeout=None):
        self.last = {"url": url, "headers": headers, "data": data}
        return self.resp


def test_exchange_code_posts_and_returns_the_token():
    t = _Transport(_Resp({"access_token": "wr1te", "refresh_token": "r1",
                          "scope": "pins:write", "expires_in": 2592000}))
    tok = exchange_code("id", "secret", "the-code", "https://x/cb", transport=t)
    assert tok["access_token"] == "wr1te" and "write" in tok["scope"]
    # Correct endpoint, basic auth, and the auth-code grant.
    assert t.last["url"].endswith("/oauth/token")
    assert t.last["headers"]["Authorization"].startswith("Basic ")
    assert t.last["data"]["grant_type"] == "authorization_code"
    assert t.last["data"]["code"] == "the-code"


def test_exchange_code_raises_on_http_error():
    t = _Transport(_Resp({"message": "bad"}, status=400))
    try:
        exchange_code("id", "secret", "x", "https://x/cb", transport=t)
        assert False, "should have raised"
    except PinterestOAuthError as exc:
        assert "400" in str(exc)
