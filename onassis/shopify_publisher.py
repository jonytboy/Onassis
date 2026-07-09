"""Shopify Publisher — records a product's Shopify publication.

Mirrors :class:`PublisherService` for the second sales channel: dedupes per
product (one Shopify product per SKU), publishes via the injectable
:class:`ShopifyConnector`, and records a ``publications`` row with
``platform='shopify'`` — so the Product Status Engine and dashboards see the
Shopify listing exactly as they see the Etsy one. Safe no-op until Shopify is
credentialled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from onassis.config import Config
from onassis.connectors.shopify import ShopifyConnector
from onassis.database import Database
from onassis.logger import get_logger
from onassis.product_status import is_valid_listing_id

log = get_logger(__name__)

PLATFORM = "shopify"


class ShopifyPublisher:
    def __init__(self, config: Config, db: Database,
                 connector: ShopifyConnector | None = None) -> None:
        self.config = config
        self.db = db
        self.connector = connector or ShopifyConnector(config, db)
        self.auto_activate = bool((config.shopify or {}).get("auto_activate", False))

    @property
    def can_publish(self) -> bool:
        return self.connector.can_publish

    def publish(self, campaign_id: int, product_key: str, listing: dict[str, Any],
                images_dir: Path | None = None) -> dict[str, Any]:
        sku = listing.get("product_id") or f"{campaign_id}-{product_key}"
        if not self.connector.can_publish:
            return {"status": "not_configured",
                    "reason": "Shopify credentials are not set."}
        # Mockup Quality Gate (P1): the same hard rule as Etsy — require at least
        # one real, quality-passed mockup; never publish placeholder/fallback art.
        from onassis.mockup_gate import evaluate_listing as _eval_mockups
        mq = _eval_mockups(listing)
        if not mq["ok"]:
            reason = f"{mq['message']} {mq['reason']}".strip()
            pub = {"platform": PLATFORM, "product_id": sku, "campaign_id": campaign_id,
                   "listing_id": None, "mode": "draft", "status": "failed", "attempts": 0,
                   "failure_reason": reason}
            pub["id"] = self.db.insert_publication(pub)
            log.warning("Mockup gate blocked Shopify publish for %s: %s", sku, reason)
            return {"status": "failed", "reason": reason, "mockup_quality": mq,
                    "mockup_blocked": True, "publication": pub}
        existing = self.db.get_active_publication(campaign_id, PLATFORM, product_id=sku)
        if existing:
            return {"status": "skipped", "reason": "Already on Shopify.",
                    "publication": existing}
        try:
            res = self.connector.publish_product(
                listing, images_dir=images_dir, active=self.auto_activate)
        except Exception as exc:  # record the failure so it shows Failed/Retry
            pub = {"platform": PLATFORM, "product_id": sku, "campaign_id": campaign_id,
                   "listing_id": None, "mode": "live" if self.auto_activate else "draft",
                   "status": "failed", "attempts": 1, "failure_reason": str(exc)}
            pub["id"] = self.db.insert_publication(pub)
            log.warning("Shopify publish failed for %s: %s", sku, exc)
            return {"status": "failed", "reason": str(exc), "publication": pub}

        if not is_valid_listing_id(res.get("product_id")):
            pub = {"platform": PLATFORM, "product_id": sku, "campaign_id": campaign_id,
                   "listing_id": None, "mode": "draft", "status": "failed", "attempts": 1,
                   "failure_reason": "Shopify returned no valid product id."}
            pub["id"] = self.db.insert_publication(pub)
            return {"status": "failed", "reason": pub["failure_reason"], "publication": pub}

        status = "live" if res.get("status") == "active" else "draft"
        pub = {"platform": PLATFORM, "product_id": sku, "campaign_id": campaign_id,
               "listing_id": res["product_id"], "mode": status, "status": status,
               "attempts": 1}
        pub["id"] = self.db.insert_publication(pub)
        log.info("Published %s to Shopify product %s (%s, %d image(s)).",
                 sku, res["product_id"], status, res.get("images_uploaded", 0))
        return {"status": status, "publication": pub, "url": res.get("url", "")}
