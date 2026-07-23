"""Mint a WRITE-scoped Pinterest token (the dashboard button can't).

The Pinterest app dashboard's "generate token" only grants read scopes, so pins
can't be created. This runs the OAuth authorization flow to get a token that
carries ``pins:write``.

Prerequisites (from your Pinterest app → Configure):
  * App ID and App secret key           → PINTEREST_APP_ID / PINTEREST_APP_SECRET
  * A Redirect URI registered on the app → PINTEREST_REDIRECT_URI
    (any URL you can read the redirect back from, e.g. https://api.onassismed.com/pinterest/callback)

Run it and follow the two prompts:

    python -m scripts.pinterest_oauth

1. Open the printed URL, approve the scopes.
2. Pinterest redirects to your redirect URI with ``?code=XXXX`` — paste that code
   (or the whole redirected URL) back here.

It prints the access token (and refresh token). Put the access token in
PINTEREST_ACCESS_TOKEN and restart.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from onassis.config import load_config
from onassis.connectors.pinterest_oauth import (
    DEFAULT_SCOPES, authorize_url, exchange_code,
)


def _extract_code(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("http"):
        qs = parse_qs(urlparse(raw).query)
        return (qs.get("code") or [""])[0]
    return raw


def main() -> None:
    cfg = load_config().pinterest or {}
    client_id = cfg.get("app_id")
    client_secret = cfg.get("app_secret")
    redirect_uri = cfg.get("redirect_uri")
    base_url = cfg.get("base_url", "https://api.pinterest.com/v5")

    missing = [name for name, v in (("PINTEREST_APP_ID", client_id),
                                    ("PINTEREST_APP_SECRET", client_secret),
                                    ("PINTEREST_REDIRECT_URI", redirect_uri)) if not v]
    if missing:
        print("Missing: " + ", ".join(missing) + " — set them and re-run.")
        return

    print("\n1) Open this URL in your browser and approve the scopes "
          f"({', '.join(DEFAULT_SCOPES)}):\n")
    print("   " + authorize_url(client_id, redirect_uri))
    print("\n2) Pinterest redirects to your redirect URI with ?code=…")
    code = _extract_code(input("\nPaste the code (or the full redirected URL): "))
    if not code:
        print("No code found — aborted.")
        return

    try:
        tok = exchange_code(client_id, client_secret, code, redirect_uri, base_url=base_url)
    except Exception as exc:  # noqa: BLE001
        print(f"Token exchange failed: {exc}")
        return

    print("\n✓ Success. Scopes granted:", tok.get("scope"))
    print("\nSet this and restart:\n")
    print(f"  PINTEREST_ACCESS_TOKEN={tok.get('access_token')}")
    if tok.get("refresh_token"):
        print(f"  (refresh token, keep safe: {tok.get('refresh_token')})")
    exp = tok.get("expires_in")
    if exp:
        print(f"\nNote: this access token expires in ~{int(exp)//86400} day(s); "
              "re-run this to mint a new one, or wire the refresh token later.")


if __name__ == "__main__":
    main()
