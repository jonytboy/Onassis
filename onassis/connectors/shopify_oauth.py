"""Shopify access-token provider — the client-credentials grant.

New Shopify Dev Dashboard apps issue a **Client ID + Client Secret** rather than
a static Admin API access token. ONASSIS exchanges those for a short-lived access
token via the client-credentials grant, caches it (in memory + a 0600 file so it
survives a restart), and refreshes it automatically when it expires or a request
comes back 401.

The operator therefore enters only **Store URL + Client ID + Client Secret** — no
manual token handling. A legacy static Admin token is still honoured when present
(see ``ShopifyConnector``), so existing installs keep working.

The HTTP POST and clock are injectable, so the whole flow is offline-testable and
never touches the network in tests.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from onassis.config import runtime_root
from onassis.logger import get_logger

log = get_logger(__name__)

HttpPost = Callable[[str, dict], dict]


def _default_store_path() -> Path:
    return runtime_root() / "data" / "shopify_token.json"


class ShopifyTokenProvider:
    """Obtains, caches and refreshes a Shopify access token (client credentials)."""

    def __init__(
        self, *, store_domain: str | None, client_id: str | None,
        client_secret: str | None, api_version: str = "2024-10",
        store_path: str | Path | None = None, http_post: HttpPost | None = None,
        clock: Callable[[], float] = time.time, refresh_leeway: int = 120,
    ) -> None:
        self.store_domain = (store_domain or "").replace("https://", "").strip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_version = api_version
        self.store_path = Path(store_path) if store_path else _default_store_path()
        self._http_post = http_post or self._default_post
        self._clock = clock
        self.refresh_leeway = int(refresh_leeway)
        self._token: str | None = None
        self._expiry: float = 0.0
        self._load()

    @property
    def is_authorised(self) -> bool:
        """True when we can obtain a token (creds present) or already hold one."""
        return bool((self.client_id and self.client_secret) or self._cached_valid())

    def _cached_valid(self) -> bool:
        return bool(self._token) and self._clock() < (self._expiry - self.refresh_leeway)

    def valid_access_token(self, force: bool = False) -> str:
        """Return a usable access token, fetching/refreshing when needed."""
        if not force and self._cached_valid():
            return self._token  # type: ignore[return-value]
        return self._fetch()

    # --- Token exchange ---------------------------------------------

    def _fetch(self) -> str:
        if not (self.client_id and self.client_secret):
            raise RuntimeError("Shopify client_id/client_secret are not set.")
        url = f"https://{self.store_domain}/admin/oauth/access_token"
        data = self._http_post(url, {
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "client_credentials"})
        token = (data or {}).get("access_token")
        if not token:
            raise RuntimeError(f"Shopify token endpoint returned no access_token: {data!r}")
        self._token = str(token)
        # expires_in is seconds; default to 1h if the store omits it.
        self._expiry = self._clock() + int((data or {}).get("expires_in", 3600) or 3600)
        self._save()
        log.info("Obtained a Shopify access token for %s (expires in %ss).",
                 self.store_domain, int(self._expiry - self._clock()))
        return self._token

    @staticmethod
    def _default_post(url: str, body: dict) -> dict:
        import httpx

        resp = httpx.post(url, json=body, timeout=30.0)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Shopify token exchange HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # --- Cache persistence (0600) -----------------------------------

    def _load(self) -> None:
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                if data.get("store_domain") == self.store_domain:
                    self._token = data.get("access_token")
                    self._expiry = float(data.get("expiry", 0) or 0)
        except Exception:  # a corrupt cache is not fatal — we just re-fetch
            self._token, self._expiry = None, 0.0

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.store_path.parent, 0o700)
            except OSError:
                pass
            tmp = self.store_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"store_domain": self.store_domain,
                           "access_token": self._token, "expiry": self._expiry}, fh)
            os.replace(tmp, self.store_path)
            try:
                os.chmod(self.store_path, 0o600)
            except OSError:
                pass
        except Exception as exc:  # caching is best-effort
            log.debug("Could not cache Shopify token: %s", exc)
