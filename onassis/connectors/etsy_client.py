"""Thin, read-only client for the Etsy Open API v3.

This is the only place that talks HTTP to Etsy. It is **read-only** — it issues
GET requests for receipts (orders) and listings and never writes to Etsy.

Credentials come from config/env (``ETSY_API_KEY``, ``ETSY_ACCESS_TOKEN``,
``ETSY_SHOP_ID``). The connector accepts any object with the same method
surface, so tests inject a stub and never touch the network.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from onassis.logger import get_logger

log = get_logger(__name__)


class EtsyConfigError(RuntimeError):
    """Raised when Etsy credentials are missing."""


class EtsyApiError(RuntimeError):
    """Raised on an Etsy 4xx/5xx — carries Etsy's full response body."""


def _response_detail(resp: httpx.Response) -> str:
    """Etsy's full response body — pretty JSON when parseable, else raw text."""
    try:
        return json.dumps(resp.json(), indent=2, ensure_ascii=False)
    except (ValueError, json.JSONDecodeError):
        return resp.text or "<empty response body>"


def _sanitise_materials(materials: Any) -> list[str]:
    """Conform materials to Etsy's rule: letters, numbers, and whitespace only.

    Etsy rejects any other character (regex ``/[^\\p{L}\\p{Nd}\\p{Zs}]/u``) with
    ``invalid_characters``. Disallowed characters (e.g. ``%``, ``-``, ``&``,
    ``'``) are replaced with a space; whitespace is collapsed and empties dropped.
    """
    out: list[str] = []
    for material in materials or []:
        kept = [
            ch if (ch.isalpha() or ch.isdecimal() or ch.isspace()) else " "
            for ch in str(material)
        ]
        cleaned = " ".join("".join(kept).split())
        if cleaned:
            out.append(cleaned)
    return out


def _trim_inventory_products(products: list[dict[str, Any]],
                             default_readiness_state_id: int | None = None
                             ) -> list[dict[str, Any]]:
    """Reduce Etsy's getInventory ``products`` to the shape updateInventory wants:
    only ``sku``, ``property_values`` and each offering's ``price``/``quantity``/
    ``is_enabled`` (Etsy rejects the read-only fields it returns on GET).

    Etsy now requires every offering to carry a ``readiness_state_id`` — preserve
    the one the GET returned, else fall back to the shop default; otherwise the
    write is rejected with 'All offerings need readiness state'."""
    trimmed: list[dict[str, Any]] = []
    for product in products:
        offerings = []
        for o in product.get("offerings", []) or []:
            off = {"price": round(float(o.get("price", 0) or 0), 2),  # normalised upstream
                   "quantity": int(o.get("quantity", 0) or 0),
                   "is_enabled": bool(o.get("is_enabled", True))}
            rs = o.get("readiness_state_id") or default_readiness_state_id
            if rs is not None:
                off["readiness_state_id"] = int(rs)
            offerings.append(off)
        trimmed.append({
            "sku": product.get("sku", ""),
            "property_values": product.get("property_values", []) or [],
            "offerings": offerings,
        })
    return trimmed


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
        shop_id: str | None = None,
        access_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        shared_secret: str | None = None,
        base_url: str = "https://openapi.etsy.com/v3/application",
        timeout: float = 30.0,
    ) -> None:
        # shop_id is optional: when absent it is resolved from the token via
        # getMe, so a valid OAuth authorisation alone is enough to read.
        if not (api_key and (access_token or token_provider)):
            raise EtsyConfigError(
                "Etsy credentials missing. Set ETSY_CLIENT_ID, then authorise via "
                "OAuth (or supply ETSY_ACCESS_TOKEN)."
            )
        self.api_key = api_key
        self.shared_secret = shared_secret
        self.access_token = access_token
        self._token_provider = token_provider
        self.shop_id = str(shop_id) if shop_id else None
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._shipping_profile_id: int | None = None  # resolved lazily from the shop
        self._readiness_state_id: int | None = None    # resolved lazily from the shop

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

    def get_me(self) -> dict[str, Any]:
        """The token owner's identity, incl. ``user_id`` and ``shop_id``."""
        return self._get("/users/me")

    def resolve_shop_id(self) -> str:
        """Return the shop id, resolving it from the token (getMe) if unset."""
        if not self.shop_id:
            me = self.get_me()
            shop_id = me.get("shop_id")
            if not shop_id:
                raise EtsyConfigError(
                    "The authorised Etsy account has no shop (getMe returned no "
                    "shop_id). Set ETSY_SHOP_ID explicitly."
                )
            self.shop_id = str(shop_id)
        return self.shop_id

    def get_receipts(self, min_created: int | None = None) -> list[dict[str, Any]]:
        """Shop receipts (orders). ``min_created`` is a unix timestamp filter."""
        params: dict[str, Any] = {}
        if min_created:
            params["min_created"] = min_created
        return self._paginate(f"/shops/{self.resolve_shop_id()}/receipts", params)

    def get_listings(self, state: str = "active") -> list[dict[str, Any]]:
        """Shop listings in the given state (active|inactive|draft|expired)."""
        return self._paginate(
            f"/shops/{self.resolve_shop_id()}/listings",
            {"state": state, "includes": "Inventory"},
        )

    def get_shop(self) -> dict[str, Any]:
        """Read-only shop record (GET /shops/{shop_id})."""
        return self._get(f"/shops/{self.resolve_shop_id()}")

    def get_shipping_profiles(self) -> list[dict[str, Any]]:
        """The shop's shipping profiles (GET /shops/{shop_id}/shipping-profiles)."""
        resp = self._get(f"/shops/{self.resolve_shop_id()}/shipping-profiles")
        return resp.get("results", []) or []

    def resolve_shipping_profile_id(self) -> int:
        """Return a shipping profile id, resolved from the shop if unset.

        Mirrors :meth:`resolve_shop_id`: when no shipping profile is configured
        it fetches the authenticated shop's profiles and uses the first active
        one (falling back to the first profile), caching the result. Keeps the
        app portable across shops — no shop-specific id is hardcoded.
        """
        if self._shipping_profile_id is None:
            profiles = self.get_shipping_profiles()
            active = [p for p in profiles if not p.get("is_deleted")]
            chosen = (active or profiles)
            if not chosen:
                raise EtsyApiError(
                    "No Etsy shipping profile found for this shop. Create one "
                    "(Shop Manager → Settings → Shipping settings) or set "
                    "listing.shipping_profile_id."
                )
            self._shipping_profile_id = int(chosen[0]["shipping_profile_id"])
        return self._shipping_profile_id

    def get_readiness_state_definitions(self) -> list[dict[str, Any]]:
        """The shop's readiness (processing) state definitions.

        GET /shops/{shop_id}/readiness-state-definitions. Each definition carries
        a ``readiness_state_id`` and a ``readiness_state`` enum
        (``ready_to_ship`` | ``made_to_order``).
        """
        resp = self._get(f"/shops/{self.resolve_shop_id()}/readiness-state-definitions")
        return resp.get("results", []) or []

    def resolve_readiness_state_id(self) -> int:
        """Return a readiness_state_id, resolved from the shop if unset.

        Etsy requires ``readiness_state_id`` for physical listings. It is a
        shop-specific id (not a fixed enum — the enum is ``readiness_state``), so
        this mirrors :meth:`resolve_shipping_profile_id`: fetch the shop's
        readiness state definitions and use the first active one, caching the
        result. No shop-specific id is hardcoded, so the app stays portable.
        """
        if self._readiness_state_id is None:
            definitions = self.get_readiness_state_definitions()
            active = [d for d in definitions if not d.get("is_deleted")]
            chosen = active or definitions
            if not chosen:
                raise EtsyApiError(
                    "No Etsy readiness state definition found for this shop. Create "
                    "a processing profile in your shop, or set listing.readiness_state_id."
                )
            self._readiness_state_id = int(chosen[0]["readiness_state_id"])
        return self._readiness_state_id


class EtsyDraftClient(EtsyClient):
    """Write client that creates Etsy listings as **drafts** only.

    The Publisher uses this for Draft-mode publishing. It maps a listing
    package onto Etsy's createDraftListing fields and always sets
    ``state=draft`` — it never lists a product live.
    """

    def create_draft(self, listing: dict[str, Any]) -> dict[str, Any]:
        """Create a draft listing on Etsy and return ``{listing_id, ...}``.

        ``listing['type']`` may be ``'physical'`` (default) or ``'download'`` for
        an instant-download digital product. Digital listings must NOT carry a
        shipping profile or readiness state (Etsy rejects them), so those fields
        — and the API calls that resolve them — are skipped for downloads."""
        is_download = str(listing.get("type", "physical")).lower() == "download"
        body: dict[str, Any] = {
            "quantity": listing.get("quantity", 1),
            "title": listing["title"],
            "description": listing["description"],
            "price": listing.get("price"),
            "who_made": listing.get("who_made", "i_did"),
            "when_made": listing.get("when_made", "made_to_order"),
            "taxonomy_id": listing.get("taxonomy_id"),
            "tags": listing.get("tags", []),
            # Etsy allows only letters/numbers/whitespace in materials.
            "materials": _sanitise_materials(listing.get("materials")),
            "type": "download" if is_download else "physical",
            "state": "draft",  # NEVER publish live from here
        }
        if not is_download:
            # Physical-only: Etsy requires shipping_profile_id and
            # readiness_state_id (as ints). Use configured values, else resolve
            # them from the shop. Digital downloads must omit both entirely.
            configured = listing.get("shipping_profile_id")
            body["shipping_profile_id"] = (
                int(configured) if configured not in (None, "")
                else self.resolve_shipping_profile_id())
            configured_rs = listing.get("readiness_state_id")
            body["readiness_state_id"] = (
                int(configured_rs) if configured_rs not in (None, "")
                else self.resolve_readiness_state_id())
        url = f"{self.base_url}/shops/{self.resolve_shop_id()}/listings"
        # Send JSON so integer fields keep their type — form-urlencoded stringifies
        # every value, which makes Etsy reject shipping_profile_id as a string.
        resp = httpx.post(url, headers=self._headers(), json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            # Surface Etsy's actual validation message (field-level errors),
            # not just the bare status line, so the real issue is visible.
            detail = _response_detail(resp)
            log.error(
                "Etsy createDraftListing failed: HTTP %s\n%s", resp.status_code, detail
            )
            raise EtsyApiError(
                f"Etsy createDraftListing returned HTTP {resp.status_code}: {detail}"
            )
        return resp.json()

    def publish_listing(self, listing_id: int | str) -> dict[str, Any]:
        """Activate a draft listing — take it **LIVE** on Etsy.

        PUT ``/shops/{shop_id}/listings/{listing_id}`` with ``state=active``. The
        listing must already have at least one image and valid inventory/price
        (the Publisher uploads images before calling this). This is the only
        method that makes a product buyable; the Publisher gates it behind the
        launch approval + a margin check.
        """
        url = (f"{self.base_url}/shops/{self.resolve_shop_id()}"
               f"/listings/{listing_id}")
        resp = httpx.put(url, headers=self._headers(), json={"state": "active"},
                         timeout=self.timeout)
        if resp.status_code >= 400:
            detail = _response_detail(resp)
            log.error("Etsy publish (activate) failed: HTTP %s\n%s",
                      resp.status_code, detail)
            raise EtsyApiError(
                f"Etsy updateListing (activate) returned HTTP {resp.status_code}: {detail}")
        return resp.json()

    def update_listing(self, listing_id: int | str,
                       fields: dict[str, Any]) -> dict[str, Any]:
        """Edit a live/draft listing's editable fields (title/description/tags/
        materials/state/price). PATCH-style PUT /shops/{shop}/listings/{id}."""
        body: dict[str, Any] = {}
        for key in ("title", "description", "state"):
            if fields.get(key) is not None:
                body[key] = fields[key]
        if fields.get("tags") is not None:
            body["tags"] = list(fields["tags"])
        if fields.get("materials") is not None:
            body["materials"] = _sanitise_materials(fields["materials"])
        if fields.get("price") is not None:
            body["price"] = round(float(fields["price"]), 2)
        if not body:
            return {"skipped": "no editable fields"}
        url = (f"{self.base_url}/shops/{self.resolve_shop_id()}"
               f"/listings/{listing_id}")
        resp = httpx.put(url, headers=self._headers(), json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            detail = _response_detail(resp)
            log.error("Etsy updateListing failed: HTTP %s\n%s", resp.status_code, detail)
            raise EtsyApiError(
                f"Etsy updateListing returned HTTP {resp.status_code}: {detail}")
        return resp.json()

    def deactivate_listing(self, listing_id: int | str) -> dict[str, Any]:
        """Take a live listing OUT of the shop (state=inactive) — used to retire it."""
        return self.update_listing(listing_id, {"state": "inactive"})

    def get_listing_inventory(self, listing_id: int | str) -> dict[str, Any]:
        """GET the listing's inventory (offerings carry price + quantity)."""
        return self._get(f"/listings/{listing_id}/inventory")

    def update_listing_inventory(self, listing_id: int | str,
                                 inventory: dict[str, Any]) -> dict[str, Any]:
        """PUT the listing's inventory (the way price/quantity are changed for a
        listing with product offerings)."""
        url = f"{self.base_url}/listings/{listing_id}/inventory"
        resp = httpx.put(url, headers=self._headers(), json=inventory, timeout=self.timeout)
        if resp.status_code >= 400:
            detail = _response_detail(resp)
            log.error("Etsy updateInventory failed: HTTP %s\n%s", resp.status_code, detail)
            raise EtsyApiError(
                f"Etsy updateListingInventory returned HTTP {resp.status_code}: {detail}")
        return resp.json()

    def set_price_and_quantity(self, listing_id: int | str, *, price: float | None = None,
                               quantity: int | None = None) -> dict[str, Any]:
        """Read the listing's inventory, set price/quantity on every offering, and
        write it back. This is Etsy's supported path for changing price/stock."""
        inventory = self.get_listing_inventory(listing_id)
        products = inventory.get("products", []) or []
        for product in products:
            for offering in product.get("offerings", []) or []:
                current = offering.get("price")
                if isinstance(current, dict):  # Etsy GET returns {amount, divisor}
                    existing = float(current.get("amount", 0) or 0) / (current.get("divisor") or 100)
                else:
                    existing = float(current or 0)
                offering["price"] = round(price if price is not None else existing, 2)
                if quantity is not None:
                    offering["quantity"] = int(quantity)
        # Etsy's update payload wants a trimmed shape (products + the *_on_property
        # arrays echoed back). Send products plus the property arrays it returned.
        # Every offering must carry a readiness_state_id — resolve a shop default
        # only if the GET didn't already give each offering one.
        needs_default = any(not o.get("readiness_state_id")
                            for p in products for o in (p.get("offerings") or []))
        default_rs = self.resolve_readiness_state_id() if needs_default else None
        payload: dict[str, Any] = {
            "products": _trim_inventory_products(products, default_rs)}
        for key in ("price_on_property", "quantity_on_property", "sku_on_property"):
            payload[key] = inventory.get(key, []) or []
        return self.update_listing_inventory(listing_id, payload)

    def delete_listing_image(self, listing_id: int | str,
                             image_id: int | str) -> dict[str, Any]:
        """Remove one image from a listing (DELETE …/images/{image_id})."""
        url = (f"{self.base_url}/shops/{self.resolve_shop_id()}"
               f"/listings/{listing_id}/images/{image_id}")
        resp = httpx.delete(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            detail = _response_detail(resp)
            log.error("Etsy deleteListingImage failed: HTTP %s\n%s",
                      resp.status_code, detail)
            raise EtsyApiError(
                f"Etsy deleteListingImage returned HTTP {resp.status_code}: {detail}")
        return {"deleted": True, "image_id": image_id}

    def upload_listing_image(
        self, listing_id: int | str, image_path: str, *, rank: int = 1,
        alt_text: str | None = None, overwrite: bool = False,
    ) -> dict[str, Any]:
        """Attach one image file to a draft listing (uploadListingImage).

        POST multipart/form-data to
        ``/shops/{shop_id}/listings/{listing_id}/images``. ``rank`` sets the
        gallery order (1 = primary). The image bytes are sent as a file part, so
        the ``x-api-key``/``Authorization`` headers are used *without* forcing a
        JSON content type (httpx sets the multipart boundary).
        """
        from pathlib import Path

        url = (f"{self.base_url}/shops/{self.resolve_shop_id()}"
               f"/listings/{listing_id}/images")
        data: dict[str, Any] = {"rank": int(rank), "overwrite": str(bool(overwrite)).lower()}
        if alt_text:
            data["alt_text"] = alt_text[:250]
        path = Path(image_path)
        with path.open("rb") as fh:
            files = {"image": (path.name, fh, "image/jpeg")}
            resp = httpx.post(url, headers=self._headers(), data=data, files=files,
                              timeout=self.timeout)
        if resp.status_code >= 400:
            detail = _response_detail(resp)
            log.error("Etsy uploadListingImage failed: HTTP %s\n%s",
                      resp.status_code, detail)
            raise EtsyApiError(
                f"Etsy uploadListingImage returned HTTP {resp.status_code}: {detail}"
            )
        return resp.json()

    def upload_listing_file(
        self, listing_id: int | str, file_path: str, *, name: str | None = None,
        rank: int = 1,
    ) -> dict[str, Any]:
        """Attach one downloadable file to a digital listing (uploadListingFile).

        POST multipart/form-data to
        ``/shops/{shop_id}/listings/{listing_id}/files``. ``name`` is the buyer-
        facing file name (defaults to the file's own name); ``rank`` orders the
        files. Only valid on ``type='download'`` listings.
        """
        from pathlib import Path

        url = (f"{self.base_url}/shops/{self.resolve_shop_id()}"
               f"/listings/{listing_id}/files")
        path = Path(file_path)
        data: dict[str, Any] = {"name": (name or path.name)[:255], "rank": int(rank)}
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            resp = httpx.post(url, headers=self._headers(), data=data, files=files,
                              timeout=self.timeout)
        if resp.status_code >= 400:
            detail = _response_detail(resp)
            log.error("Etsy uploadListingFile failed: HTTP %s\n%s",
                      resp.status_code, detail)
            raise EtsyApiError(
                f"Etsy uploadListingFile returned HTTP {resp.status_code}: {detail}"
            )
        return resp.json()

    def get_listing_videos(self, listing_id: int | str) -> list[dict[str, Any]]:
        """Videos already on a listing (getListingVideos) — so we don't add two."""
        try:
            data = self._get(f"/listings/{listing_id}/videos")
        except EtsyApiError:
            return []
        return data.get("results") or []

    def upload_listing_video(
        self, listing_id: int | str, video_path: str, *, name: str | None = None,
    ) -> dict[str, Any]:
        """Attach one mp4 to a listing (uploadListingVideo). POST multipart to
        ``/shops/{shop_id}/listings/{listing_id}/videos`` — the video bytes go as
        a file part (httpx sets the multipart boundary)."""
        from pathlib import Path

        url = (f"{self.base_url}/shops/{self.resolve_shop_id()}"
               f"/listings/{listing_id}/videos")
        path = Path(video_path)
        data: dict[str, Any] = {"name": (name or path.stem)[:70]}
        with path.open("rb") as fh:
            files = {"video": (path.name, fh, "video/mp4")}
            resp = httpx.post(url, headers=self._headers(), data=data, files=files,
                              timeout=max(self.timeout, 120.0))
        if resp.status_code >= 400:
            detail = _response_detail(resp)
            log.error("Etsy uploadListingVideo failed: HTTP %s\n%s",
                      resp.status_code, detail)
            raise EtsyApiError(
                f"Etsy uploadListingVideo returned HTTP {resp.status_code}: {detail}")
        return resp.json()
