"""Etsy Operations Connector — read-only observation of the real shop.

Implements the :class:`~onassis.connectors.base.RevenueConnector` contract and
extends it to import orders, listings, listing statistics, favourites, visits,
and conversion data from Etsy into ONASSIS. It is:

* **Read-only** — it never modifies Etsy (no listing creation/editing).
* **Incremental** — it tracks a per-resource cursor and only pulls what's new.
* **Idempotent** — orders dedupe by external ref; listings/stats upsert. A
  re-sync never duplicates a record.
* **Linked** — every imported listing is linked to a Product, a Campaign, and
  (via the orders it generated) its Revenue and Profit.
* **Automatic** — a sync feeds the Revenue Intelligence Engine, so the CEO's
  business metrics (which read the live ledger) update after every sync.

Etsy access is injected, so this runs live with real credentials and is fully
tested offline with a stub client.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from onassis.config import Config
from onassis.connectors.base import RevenueConnector
from onassis.connectors.etsy_client import EtsyClient
from onassis.database import Database
from onassis.logger import get_logger
from onassis.profit import ProfitEngine
from onassis.revenue import RevenueEngine

log = get_logger(__name__)

_RECEIPTS_CURSOR = "etsy_receipts"


class EtsyConnector(RevenueConnector):
    """Imports orders, listings, and stats from Etsy (read-only)."""

    name = "etsy"

    def __init__(self, config: Config, db: Database, client: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.revenue = RevenueEngine(config, db)
        self.profit = ProfitEngine(config, db)
        self.etsy_cfg = config.etsy or {}
        self._client = client
        self._oauth: Any | None = None
        # The REAL Etsy fee model (transaction + payment + listing + Offsite Ads).
        from onassis.fees import FeeModel

        self.fee_model = FeeModel.from_config(config)

    # --- Configuration / client -------------------------------------

    @property
    def oauth(self) -> Any:
        if self._oauth is None:
            from onassis.connectors.etsy_oauth import build_etsy_oauth

            self._oauth = build_etsy_oauth(self.config)
        return self._oauth

    @property
    def is_configured(self) -> bool:
        if self._client is not None:
            return True
        c = self.etsy_cfg
        if not c.get("api_key"):
            return False
        # Ready with a static token or an OAuth grant. The shop id is optional —
        # it is resolved from the token (getMe) when not configured.
        return bool(c.get("access_token")) or self.oauth.is_authorised

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = EtsyClient(
                api_key=self.etsy_cfg.get("api_key"),
                shop_id=self.etsy_cfg.get("shop_id"),
                access_token=self.etsy_cfg.get("access_token"),
                token_provider=(
                    None if self.etsy_cfg.get("access_token")
                    else self.oauth.valid_access_token
                ),
                shared_secret=self.etsy_cfg.get("client_secret"),
                base_url=self.etsy_cfg.get("base_url", "https://openapi.etsy.com/v3/application"),
            )
        return self._client

    def attach_listing_videos(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Upload a video to each listing in ``items`` ({listing_id, video_path,
        name}). Idempotent: listings that already have a video — or whose local
        clip is missing — are skipped. Returns ``{checked, added, skipped}``."""
        from pathlib import Path

        checked = added = skipped = 0
        details = []
        for it in items:
            checked += 1
            lid, vp = it.get("listing_id"), it.get("video_path")
            if not lid or not vp or not Path(vp).exists():
                skipped += 1
                details.append({"listing_id": lid, "ok": False,
                                "reason": "no local clip on disk"})
                continue
            try:
                if self.client.get_listing_videos(lid):
                    skipped += 1
                    details.append({"listing_id": lid, "ok": True,
                                    "reason": "already has video"})
                    continue
                self.client.upload_listing_video(lid, vp, name=it.get("name"))
                added += 1
                details.append({"listing_id": lid, "ok": True})
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                details.append({"listing_id": lid, "ok": False, "reason": str(exc)})
        return {"checked": checked, "added": added, "skipped": skipped,
                "details": details[:50]}

    # --- RevenueConnector contract ----------------------------------

    def fetch_orders(self) -> list[dict[str, Any]]:
        """New, de-duplicated canonical orders since the last cursor."""
        cursor = self.db.get_sync_cursor(_RECEIPTS_CURSOR)
        min_created = int(cursor) if cursor else None
        receipts = self.client.get_receipts(min_created=min_created)
        orders, _ = self._collect_new_orders(receipts)
        return orders

    # --- Sync -------------------------------------------------------

    def sync(self) -> dict[str, Any]:
        """Import orders, listings and stats; update metrics. Returns a summary."""
        if not self.is_configured:
            log.warning("Etsy not configured — skipping sync")
            return {"configured": False, "message": "Etsy credentials not set."}

        cursor = self.db.get_sync_cursor(_RECEIPTS_CURSOR)
        min_created = int(cursor) if cursor else None
        receipts = self.client.get_receipts(min_created=min_created)
        new_orders, max_created = self._collect_new_orders(receipts)

        for order in new_orders:
            self.revenue.record_order(order)
        if max_created:
            self.db.set_sync_cursor(_RECEIPTS_CURSOR, str(max_created))

        listings = self.client.get_listings()
        for raw in listings:
            self._import_listing(raw)

        # Updated business metrics — the CEO reads these from the live ledger.
        metrics = {**self.revenue.company_profit(), "dashboard": self.profit.dashboard()}
        summary = {
            "configured": True,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "imported_orders": len(new_orders),
            "imported_listings": len(listings),
            "imported_stats": len(listings),  # one stat snapshot per listing
            "metrics": metrics,
        }
        log.info(
            "Etsy sync: %d new order(s), %d listing(s). Net profit now %.2f",
            len(new_orders),
            len(listings),
            metrics["net_profit"],
        )
        return summary

    # --- Reads (for the API) ----------------------------------------

    def imported_orders(self) -> list[dict[str, Any]]:
        return self.db.get_orders_by_platform("etsy")

    def listings(self) -> list[dict[str, Any]]:
        """Listings enriched with their linked Revenue and Profit (from orders)."""
        enriched = []
        for listing in self.db.list_etsy_listings():
            orders = self.db.get_orders_for_product(str(listing["listing_id"]))
            enriched.append(
                {
                    **listing,
                    "revenue": round(sum(o["gross_revenue"] for o in orders), 2),
                    "net_profit": round(sum(o["net_profit"] for o in orders), 2),
                    "orders": len(orders),
                }
            )
        return enriched

    def stats(self) -> list[dict[str, Any]]:
        return self.db.list_listing_stats()

    # --- Mapping helpers --------------------------------------------

    def _collect_new_orders(
        self, receipts: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Map receipts -> canonical orders, skipping any already imported."""
        existing = self.db.get_existing_order_refs()
        seen_this_run: set[str] = set()
        orders: list[dict[str, Any]] = []
        max_created: int | None = None

        for receipt in receipts:
            created = int(receipt.get("created_timestamp") or 0)
            if created:
                max_created = created if max_created is None else max(max_created, created)
            for tx in receipt.get("transactions", []) or []:
                ref = f"etsy-{receipt.get('receipt_id')}-{tx.get('transaction_id')}"
                if ref in existing or ref in seen_this_run:
                    continue  # never import a duplicate
                seen_this_run.add(ref)
                orders.append(self._map_transaction(receipt, tx, ref, created))
        return orders, max_created

    def _map_transaction(
        self, receipt: dict[str, Any], tx: dict[str, Any], ref: str, created: int
    ) -> dict[str, Any]:
        occurred = (
            datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
            if created
            else datetime.now(timezone.utc).isoformat()
        )
        price = tx.get("price", {}) or {}
        divisor = price.get("divisor") or 100
        unit_price = float(price.get("amount", 0) or 0) / divisor
        qty = int(tx.get("quantity", 1) or 1)
        gross = unit_price * qty

        product_id = str(tx.get("listing_id"))
        product = self.db.get_product_by_sku(product_id)
        production = float(product["production_cost"]) * qty if product else 0.0
        campaign_id = product["campaign_id"] if product else None

        # Real Etsy fees on the actual sale — including Offsite Ads when the
        # receipt is flagged as attributed to Etsy Ads.
        shipping = self._shipping_total(receipt)
        offsite = self._is_offsite(receipt)
        fees = self.fee_model.order_fees(gross, shipping, offsite=offsite)

        return {
            "order_ref": ref,
            "occurred_at": occurred,
            "sale_date": occurred[:10],
            "product_id": product_id,
            "campaign_id": campaign_id,
            "platform": "etsy",
            "sale_price": round(unit_price, 2),
            "currency": price.get("currency_code", "GBP"),
            "quantity": qty,
            "production_cost": round(production, 2),
            "marketplace_fees": fees["marketplace_fees"],
            "payment_fees": fees["payment_fees"],
            "ai_cost": 0.0,
            "advertising_cost": 0.0,
            "other_costs": 0.0,
            # The recipient — carried so the Gelato Fulfilment Engine can ship it.
            "shipping_address": self._recipient(receipt),
        }

    @staticmethod
    def _recipient(receipt: dict[str, Any]) -> dict[str, Any]:
        """Map an Etsy receipt's shipping fields to a normalised recipient."""
        r = receipt or {}
        return {
            "name": r.get("name"),
            "first_line": r.get("first_line"),
            "second_line": r.get("second_line"),
            "city": r.get("city"),
            "state": r.get("state"),
            "zip": r.get("zip"),
            "country_iso": r.get("country_iso"),
            "email": r.get("buyer_email") or r.get("email"),
            "formatted_address": r.get("formatted_address"),
        }

    @staticmethod
    def _shipping_total(receipt: dict[str, Any]) -> float:
        cost = (receipt or {}).get("total_shipping_cost") or {}
        divisor = cost.get("divisor") or 100
        return float(cost.get("amount", 0) or 0) / divisor

    @staticmethod
    def _is_offsite(receipt: dict[str, Any]) -> bool | None:
        """Whether the order was attributed to Etsy Offsite Ads, if the receipt
        says so; ``None`` (unknown → blended expectation) otherwise."""
        for key in ("is_offsite_ads", "is_offsite_ad", "offsite_ads"):
            if key in (receipt or {}):
                return bool(receipt[key])
        return None

    def _import_listing(self, raw: dict[str, Any]) -> None:
        """Store a listing snapshot + a stat snapshot, linked to product/campaign."""
        listing_id = raw.get("listing_id")
        product_id = str(listing_id)
        product = self.db.get_product_by_sku(product_id)
        price = raw.get("price", {}) or {}
        divisor = price.get("divisor") or 100
        views = int(raw.get("views", 0) or 0)
        favourers = int(raw.get("num_favorers", 0) or 0)
        created = raw.get("created_timestamp")

        self.db.upsert_etsy_listing(
            {
                "listing_id": listing_id,
                "product_id": product_id,
                "campaign_id": product["campaign_id"] if product else None,
                "title": raw.get("title"),
                "state": raw.get("state"),
                "price": float(price.get("amount", 0) or 0) / divisor if price else None,
                "currency": price.get("currency_code"),
                "url": raw.get("url"),
                "num_favorers": favourers,
                "views": views,
                "created_ts": (
                    datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat()
                    if created
                    else None
                ),
                "raw": raw,
            }
        )

        # Derive a dated stat snapshot (favourites, visits, conversion where available).
        orders = self.db.get_orders_for_product(product_id)
        order_count = len(orders)
        revenue = sum(o["gross_revenue"] for o in orders)
        conversion = (order_count / views) if views > 0 else 0.0
        self.db.upsert_listing_stat(
            {
                "listing_id": listing_id,
                "stat_date": date.today().isoformat(),
                "views": views,
                "visits": views,  # Etsy reports views; treated as visits here
                "favourites": favourers,
                "orders": order_count,
                "revenue": round(revenue, 2),
                "conversion_rate": round(conversion, 4),
            }
        )
