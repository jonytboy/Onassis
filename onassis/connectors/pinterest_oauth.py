"""Pinterest OAuth 2.0 — mint a token that can actually WRITE pins.

The "generate token" button in the Pinterest app dashboard only ever grants
read scopes (pins:read, boards:read, …); it can never give ``pins:write``. Write
access requires the full authorization-code flow, requesting ``pins:write``
explicitly and having the account owner approve it. This module does exactly
that, in two steps:

1. :func:`authorize_url` — the URL the operator opens in a browser to approve the
   write scopes; Pinterest redirects back with a ``?code=``.
2. :func:`exchange_code` — trades that code for an access token (+ refresh token)
   that carries ``pins:write``.

HTTP is injectable so the exchange is offline-testable.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlencode

# Everything the bulk pinner and catalogue tools need: read + write pins/boards.
DEFAULT_SCOPES = ["boards:read", "boards:write", "pins:read", "pins:write"]
_AUTH_HOST = "https://www.pinterest.com/oauth/"


class PinterestOAuthError(RuntimeError):
    """Raised when the token exchange fails."""


def authorize_url(client_id: str, redirect_uri: str, *,
                  scopes: list[str] | None = None, state: str = "onassis") -> str:
    """The browser URL that asks the account owner to approve the scopes."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes or DEFAULT_SCOPES),
        "state": state,
    }
    return f"{_AUTH_HOST}?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str, *,
                  base_url: str = "https://api.pinterest.com/v5",
                  transport: Any | None = None) -> dict[str, Any]:
    """Exchange an authorization ``code`` for an access token (+ refresh token).

    Returns the raw Pinterest token payload: ``access_token``, ``refresh_token``,
    ``expires_in``, ``scope``, …
    """
    http = transport
    if http is None:
        import httpx
        http = httpx
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    resp = http.post(
        f"{base_url.rstrip('/')}/oauth/token",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": redirect_uri},
        timeout=30.0,
    )
    if getattr(resp, "status_code", 200) >= 400:
        raise PinterestOAuthError(
            f"Pinterest token exchange HTTP {resp.status_code}: {getattr(resp, 'text', '')}"[:300])
    return resp.json()
