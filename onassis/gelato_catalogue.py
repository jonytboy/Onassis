"""Gelato Catalogue Sync (Sprint 46) — automate the product list.

ONASSIS built from a hand-maintained ten-product list, so it could only ever
make mugs/tees/posters/totes and every new product type meant editing config and
copying a UID by hand. This module removes that: it pulls the **real** product
catalogue from Gelato's Product Catalog API and stores each buildable product
type (with its true ``productUid``) in the database, mapped to a catalogue
category. The Expansion Engine then builds from the synced catalogue
automatically — no manual UIDs, no guessing.

* :class:`GelatoCatalogueClient` — the only place that talks HTTP to Gelato's
  Product Catalog API v3 (list catalogs, search products). Injectable, so tests
  use a fake transport and never hit the network.
* :class:`GelatoCatalogueSync` — walks the catalogs, picks a representative
  product per catalog, maps it to our internal product-type shape (key, category,
  cost, price) and upserts it. Deterministic and idempotent.

Because a Gelato ``productUid`` returned by the API is a real, orderable id, the
synced products are marked available immediately — unlike the hand-entered
placeholders, they are safe to build and fulfil.
"""

from __future__ import annotations

import re
from typing import Any

from onassis.catalogue import category_of
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

_CATALOG_BASE = "https://product.gelatoapis.com/v3"

# Per-category production-cost defaults (GBP). The Catalog API does not return
# price (that is a separate per-UID/per-country call), so we seed a sensible cost
# per category and derive retail via a markup; the operator can refine later.
_CATEGORY_COST: dict[str, float] = {
    "Mugs": 7.5, "T-Shirts": 12.0, "Hoodies": 24.0, "Posters": 8.0,
    "Tote Bags": 9.0, "Cushions": 14.0, "Aprons": 13.0, "Tea Towels": 8.0,
    "Kitchen Accessories": 9.0, "Olive Boards": 15.0, "Candles": 10.0,
    "Other": 12.0,
}
# Per-category brand-fit prior for a premium Mediterranean lifestyle brand.
_CATEGORY_BRAND_FIT: dict[str, int] = {
    "Mugs": 90, "Posters": 90, "T-Shirts": 86, "Tote Bags": 85, "Tea Towels": 85,
    "Cushions": 86, "Aprons": 82, "Kitchen Accessories": 80, "Hoodies": 80,
    "Other": 72,
}


class GelatoError(RuntimeError):
    """Raised on a Gelato Product Catalog API 4xx/5xx."""


class GelatoCatalogueClient:
    """Minimal Gelato Product Catalog API v3 client. Injectable for tests.

    ``transport`` is any object exposing ``get(url, headers)`` and
    ``post(url, headers, json)`` returning a response with ``status_code``,
    ``.json()`` and ``.text`` (httpx's interface). Defaults to httpx.
    """

    def __init__(self, api_key: str, *, base_url: str = _CATALOG_BASE,
                 timeout: float = 30.0, transport: Any | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    def _http(self) -> Any:
        if self._transport is not None:
            return self._transport
        import httpx
        return httpx

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    def list_catalogs(self) -> list[dict[str, Any]]:
        resp = self._http().get(f"{self.base_url}/catalogs", headers=self._headers(),
                                timeout=self.timeout)
        data = _ok(resp, "listCatalogs")
        return data.get("data", data) if isinstance(data, dict) else data

    def search_products(self, catalog_uid: str, *, limit: int = 50,
                        offset: int = 0) -> list[dict[str, Any]]:
        resp = self._http().post(
            f"{self.base_url}/catalogs/{catalog_uid}/products:search",
            headers=self._headers(), json={"limit": limit, "offset": offset},
            timeout=self.timeout)
        data = _ok(resp, "searchProducts")
        if isinstance(data, dict):
            return data.get("products") or data.get("data") or []
        return data or []


def _ok(resp: Any, op: str) -> Any:
    if getattr(resp, "status_code", 200) >= 400:
        raise GelatoError(f"Gelato {op} HTTP {resp.status_code}: {getattr(resp, 'text', '')}"[:300])
    return resp.json()


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(text).lower())).strip("_")


class GelatoCatalogueSync:
    """Pull Gelato's catalogue and store it as buildable product types."""

    def __init__(self, config: Config, db: Database, client: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "gelato", None) or {}
        self._client = client
        self.markup = float(self.cfg.get("catalogue_markup", 2.75))

    @property
    def is_configured(self) -> bool:
        return self._client is not None or bool(self.cfg.get("api_key"))

    def client(self) -> GelatoCatalogueClient:
        if self._client is None:
            key = self.cfg.get("api_key")
            if not key:
                raise GelatoError("Gelato API key not set (GELATO_API_KEY).")
            self._client = GelatoCatalogueClient(key)
        return self._client

    def sync(self, *, catalogs: list[str] | None = None, per_catalog: int = 1,
             available: bool = True) -> dict[str, Any]:
        """Sync catalogs → representative products → stored product types.

        ``catalogs`` restricts to a subset of catalog uids (default: all).
        ``per_catalog`` is how many representative products to keep per catalog
        (one product type per catalog by default). Idempotent — re-running
        refreshes existing rows rather than duplicating them.
        """
        client = self.client()
        wanted = set(catalogs or [])
        available_flag = available
        catalog_rows = client.list_catalogs()
        synced: list[dict[str, Any]] = []
        skipped: list[str] = []
        for cat in catalog_rows:
            cuid = cat.get("catalogUid") or cat.get("catalog_uid") or cat.get("uid")
            if not cuid or (wanted and cuid not in wanted):
                continue
            title = cat.get("title") or cuid
            try:
                products = client.search_products(cuid, limit=max(per_catalog, 1))
            except GelatoError as exc:
                log.warning("Gelato catalog '%s' search failed: %s", cuid, exc)
                skipped.append(cuid)
                continue
            seen_keys: set[str] = set()
            kept = 0
            for prod in products:
                if kept >= per_catalog:
                    break
                entry = self._map_product(cuid, title, prod, available_flag,
                                          disambiguate=per_catalog > 1)
                if entry is None or entry["product_key"] in seen_keys:
                    continue
                seen_keys.add(entry["product_key"])
                self.db.upsert_gelato_product(entry)
                synced.append(entry)
                kept += 1
        log.info("Gelato catalogue sync: %d product type(s) from %d catalog(s).",
                 len(synced), len(catalog_rows))
        return {"synced": len(synced), "catalogs": len(catalog_rows),
                "skipped": skipped, "products": synced}

    def _map_product(self, catalog_uid: str, title: str, prod: dict[str, Any],
                     available: bool, *, disambiguate: bool) -> dict[str, Any] | None:
        product_uid = prod.get("productUid") or prod.get("product_uid")
        if not product_uid:
            return None
        category = category_of(f"{catalog_uid} {title}", product_uid)
        key = _slug(catalog_uid)
        if disambiguate:  # keep multiple products per catalog distinct
            key = f"{key}_{_slug(product_uid)[:24]}"
        cost = _CATEGORY_COST.get(category, _CATEGORY_COST["Other"])
        retail = round(cost * self.markup, 2)
        return {
            "product_uid": product_uid,
            "catalog_uid": catalog_uid,
            "title": title,
            "category": category,
            "product_key": key,
            "production_cost": cost,
            "retail_price": retail,
            "base_brand_fit": _CATEGORY_BRAND_FIT.get(category, _CATEGORY_BRAND_FIT["Other"]),
            "base_commercial": 80,
            "base_conversion": 0.025,
            "attributes": prod.get("productAttributes") or prod.get("attributes") or {},
            "available": available,
        }

    # --- Read (for the operator / dashboard) ------------------------

    def catalogue_entries(self, *, available_only: bool = True) -> list[dict[str, Any]]:
        """The synced catalogue in Expansion-Engine entry shape."""
        return [self._to_entry(r) for r in
                self.db.list_gelato_catalogue(available_only=available_only)]

    @staticmethod
    def _to_entry(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": row["product_key"],
            "name": row.get("title") or row["product_key"],
            "gelato_uid": row["product_uid"],
            "production_cost": row.get("production_cost", 0),
            "retail_price": row.get("retail_price", 0),
            "base_brand_fit": row.get("base_brand_fit", 78),
            "base_commercial": row.get("base_commercial", 76),
            "base_conversion": row.get("base_conversion", 0.025),
            "available": bool(row.get("available", 1)),
            "category": row.get("category"),
            "source": "gelato",
        }
