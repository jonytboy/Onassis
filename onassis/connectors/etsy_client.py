"""Thin, read-only client for the Etsy Open API v3.

This is the only place that talks HTTP to Etsy. It is **read-only** — it issues
GET requests for receipts (orders) and listings and never writes to Etsy.

Credentials come from config/env (``ETSY_API_KEY``, ``ETSY_ACCESS_TOKEN``,
``ETSY_SHOP_ID``). The connector accepts any object with the same method
surface, so tests inject a stub and never touch the network.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from onassis.logger import get_logger

log = get_logger(__name__)


class EtsyConfigError(RuntimeError):
    """Raised when Etsy credentials are missing."""


def api_key_header(keystring: str | None, shared_secret: str | None = None) -> str | None:
    """The value for Etsy's ``x-api-key`` header.

    Etsy enforces (since 9 Feb 2026) that the header be ``keystring:shared_secret``.
    When a shared secret is available we send the combined form; otherwise we
    fall back to the bare keystring (older behaviour / tests).
    """
    if keystring and shared_secret:
        return f"{keystring}:{shared_secret}"
    return keystring


class EtsyClient:
    """Read-only wrapper over the Etsy Open API v3.

    Authentication is either a static ``access_token`` or, preferably, a
    ``token_provider`` callable (e.g. ``EtsyOAuth.valid_access_token``) that
    returns a fresh, auto-refreshed token on every request. Etsy's ``x-api-key``
    header is the ``keystring:shared_secret`` pair (see :func:`api_key_header`).
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        shop_id: str | None,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        shared_secret: str | None = None,
        base_url: str = "https://openapi.etsy.com/v3/application",
        timeout: float = 30.0,
    ) -> None:
        if not (api_key and shop_id and (access_token or token_provider)):
            raise EtsyConfigError(
                "Etsy credentials missing. Set ETSY_CLIENT_ID and ETSY_SHOP_ID, then "
                "authorise via OAuth (or supply ETSY_ACCESS_TOKEN)."
            )
        self.api_key = api_key
        self.shared_secret = shared_secret
        self.access_token = access_token
        self._token_provider = token_provider
        self.shop_id = str(shop_id)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _bearer(self) -> str:
        # A token provider (OAuth) wins — it refreshes automatically.
        return self._token_provider() if self._token_provider else self.access_token

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": api_key_header(self.api_key, self.shared_secret),
            "Authorization": f"Bearer {self._bearer()}",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        resp = httpx.get(url, headers=self._headers(), params=params or {}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk Etsy's limit/offset pagination, collecting all results."""
        results: list[dict[str, Any]] = []
        offset, limit = 0, 100
        while True:
            page = self._get(path, {**params, "limit": limit, "offset": offset})
            batch = page.get("results", []) or []
            results.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return results

    def get_receipts(self, min_created: int | None = None) -> list[dict[str, Any]]:
        """Shop receipts (orders). ``min_created`` is a unix timestamp filter."""
        params: dict[str, Any] = {}
        if min_created:
            params["min_created"] = min_created
        return self._paginate(f"/shops/{self.shop_id}/receipts", params)

    def get_listings(self, state: str = "active") -> list[dict[str, Any]]:
        """Shop listings in the given state (active|inactive|draft|expired)."""
        return self._paginate(
            f"/shops/{self.shop_id}/listings", {"state": state, "includes": "Inventory"}
        )


class EtsyDraftClient(EtsyClient):
    """Write client that creates Etsy listings as **drafts** only.

    The Publisher uses this for Draft-mode publishing. It maps a listing
    package onto Etsy's createDraftListing fields and always sets
    ``state=draft`` — it never lists a product live.
    """

    def create_draft(self, listing: dict[str, Any]) -> dict[str, Any]:
        """Create a draft listing on Etsy and return ``{listing_id, ...}``."""
        body = {
            "quantity": listing.get("quantity", 1),
            "title": listing["title"],
            "description": listing["description"],
            "price": listing.get("price"),
            "who_made": listing.get("who_made", "i_did"),
            "when_made": listing.get("when_made", "made_to_order"),
            "taxonomy_id": listing.get("taxonomy_id"),
            "shipping_profile_id": listing.get("shipping_profile_id"),
            "tags": listing.get("tags", []),
            "materials": listing.get("materials", []),
            "type": "physical",
            "state": "draft",  # NEVER publish live from here
        }
        url = f"{self.base_url}/shops/{self.shop_id}/listings"
        resp = httpx.post(url, headers=self._headers(), data=body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
