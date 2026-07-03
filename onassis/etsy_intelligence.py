"""The Etsy Intelligence Engine — learn from real customer behaviour.

ONASSIS must stop guessing how its products perform and read the real numbers.
This engine turns the data the Etsy sync already imports — views, visits,
favourites, orders — into **real conversion** per product and shop-wide, stores a
dated snapshot so trends can be tracked, and imports **search terms** (the queries
that surfaced each listing) through a replaceable provider so keyword decisions
are grounded in what customers actually search for.

It reads real Etsy data; where a signal has no public Etsy API (search terms), it
uses a ``SearchTermsProvider`` seam — the default is an honest no-op that imports
nothing until a real provider (a Shop-Stats export/feed) is registered, exactly
like the Market Intelligence signals provider. Conversion and traffic come from
real synced data, not estimates.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)


class SearchTermsProvider:
    """Interface: yield search-term rows for the shop's listings."""

    name = "none"

    def fetch(self, shop_id: str | None,
              listings: list[dict[str, Any]]) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


class NullSearchTermsProvider(SearchTermsProvider):
    """Default: Etsy's public API exposes no search terms, so import nothing until
    a real provider (Shop-Stats export/feed) is registered. Honest, not faked."""

    name = "none"

    def fetch(self, shop_id, listings):
        return []


class EtsyIntelligence:
    """Real conversion + search-term intelligence from synced Etsy data."""

    def __init__(self, config: Config, db: Database,
                 search_provider: SearchTermsProvider | None = None) -> None:
        self.config = config
        self.db = db
        self.search_provider = search_provider or NullSearchTermsProvider()

    # --- Conversion (real, from views + orders) ---------------------

    def conversion_report(self) -> dict[str, Any]:
        """Per-product and shop-wide real conversion from views and orders."""
        listings = self.db.list_etsy_listings()
        products: list[dict[str, Any]] = []
        total_views = total_favs = total_orders = 0
        total_revenue = 0.0
        for l in listings:
            sku = str(l.get("product_id"))
            orders = self.db.get_orders_for_product(sku)
            units = sum(int(o.get("quantity", 1) or 1) for o in orders)
            revenue = round(sum(float(o.get("gross_revenue", 0) or 0) for o in orders), 2)
            views = int(l.get("views", 0) or 0)
            favs = int(l.get("num_favorers", 0) or 0)
            conversion = round(units / views, 4) if views > 0 else 0.0
            fav_rate = round(favs / views, 4) if views > 0 else 0.0
            product = self.db.get_product_by_sku(sku)
            products.append({
                "listing_id": l.get("listing_id"), "sku": sku,
                "product_key": (product or {}).get("product_key"),
                "views": views, "favourites": favs, "units": units,
                "revenue": revenue, "conversion": conversion, "favourite_rate": fav_rate})
            total_views += views
            total_favs += favs
            total_orders += units
            total_revenue += revenue
        shop_conversion = round(total_orders / total_views, 4) if total_views else 0.0
        products.sort(key=lambda p: p["conversion"], reverse=True)
        return {
            "products": products,
            "shop": {"views": total_views, "favourites": total_favs,
                     "orders": total_orders, "revenue": round(total_revenue, 2),
                     "conversion": shop_conversion,
                     "favourite_rate": round(total_favs / total_views, 4) if total_views else 0.0},
        }

    def conversion_lookup(self) -> dict[str, float]:
        """sku -> real conversion, for the Learning Engine / Portfolio Manager."""
        return {p["sku"]: p["conversion"]
                for p in self.conversion_report()["products"]}

    # --- Search terms (import via provider) -------------------------

    def import_search_terms(self, today: str | None = None) -> dict[str, Any]:
        """Import the queries that surfaced each listing, via the provider.

        Rows are linked to a product where the listing maps to one. With the
        default provider this is a safe no-op (nothing imported)."""
        day = today or date.today().isoformat()
        listings = self.db.list_etsy_listings()
        shop_id = (self.config.etsy or {}).get("shop_id")
        raw = self.search_provider.fetch(shop_id, listings) or []
        by_listing = {str(l.get("listing_id")): l for l in listings}
        rows: list[dict[str, Any]] = []
        for r in raw:
            listing_id = r.get("listing_id")
            sku = by_listing.get(str(listing_id), {}).get("product_id") if listing_id else None
            product = self.db.get_product_by_sku(str(sku)) if sku else None
            rows.append({
                "snapshot_date": r.get("snapshot_date") or day,
                "term": r["term"], "listing_id": (str(listing_id) if listing_id else None),
                "product_key": r.get("product_key") or (product or {}).get("product_key"),
                "impressions": r.get("impressions", 0), "clicks": r.get("clicks", 0),
                "orders": r.get("orders", 0), "position": r.get("position"),
                "source": getattr(self.search_provider, "name", "provider")})
        imported = self.db.insert_etsy_search_terms(rows) if rows else 0
        if imported:
            log.info("Etsy intelligence imported %d search-term row(s) (%s).",
                     imported, self.search_provider.name)
        return {"imported": imported, "source": self.search_provider.name}

    def keyword_performance(self, limit: int = 50) -> list[dict[str, Any]]:
        """Aggregated search-term performance with a derived CTR."""
        out = []
        for row in self.db.top_search_terms(limit):
            imp = int(row.get("impressions", 0) or 0)
            out.append({**row, "ctr": round(int(row.get("clicks", 0) or 0) / imp, 4)
                        if imp else 0.0})
        return out

    # --- Feed the Learning Engine -----------------------------------

    def intelligence(self) -> dict[str, Any]:
        """The consolidated real-data view the Learning Engine / dashboard read."""
        report = self.conversion_report()
        return {
            "shop": report["shop"],
            "products": report["products"],
            "keywords": self.keyword_performance(),
            "search_terms_source": self.search_provider.name,
        }
