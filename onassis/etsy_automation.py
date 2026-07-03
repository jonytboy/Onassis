"""The Etsy Automation Engine — act on our own decisions.

Until now ONASSIS could *decide* to reprice, re-tag, refresh, or retire a
listing, but it could not *do* it — the marketplace never changed. This engine
closes that loop: it writes the Learning Engine's and Portfolio Manager's
decisions back to live Etsy listings, safely and with a full audit trail.

Every write is:

* **guarded** — validated (title length, exactly 13 tags, positive price) and
  skipped when it would be a no-op;
* **audited** — the field, old→new value, reason and source are logged to
  ``etsy_changes`` whether it succeeds, is skipped, or fails;
* **isolated** — one failed change never stops the others, and nothing is
  attempted until Etsy is actually configured for writes.

It owns the write primitives (title / description / tags / price / inventory /
images / deactivate) and the two decision-driven flows: **reprice an IMPROVE
listing** and **deactivate a RETIRE listing** — so a Learning/Portfolio verdict
is reflected on Etsy automatically.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.pricing import PricingEngine

log = get_logger(__name__)

_MAX_TITLE = 140
_TAG_COUNT = 13


class EtsyAutomationEngine:
    """Writes ONASSIS's decisions back to live Etsy listings, with an audit log."""

    def __init__(self, config: Config, db: Database, client: Any | None = None) -> None:
        self.config = config
        self.db = db
        self._client = client
        self.pricing = PricingEngine(config, db)

    # --- Client / configuration -------------------------------------

    @property
    def is_configured(self) -> bool:
        if self._client is not None:
            return True
        e = self.config.etsy or {}
        if not e.get("api_key"):
            return False
        if e.get("access_token"):
            return True
        from onassis.connectors.etsy_oauth import build_etsy_oauth
        return build_etsy_oauth(self.config).is_authorised

    @property
    def client(self) -> Any:
        if self._client is None:
            from onassis.connectors.etsy_client import EtsyDraftClient
            from onassis.connectors.etsy_oauth import build_etsy_oauth

            e = self.config.etsy or {}
            token_provider = (None if e.get("access_token")
                              else build_etsy_oauth(self.config).valid_access_token)
            self._client = EtsyDraftClient(
                api_key=e.get("api_key"), shop_id=e.get("shop_id"),
                access_token=e.get("access_token"), token_provider=token_provider,
                shared_secret=e.get("client_secret"),
                base_url=e.get("base_url", "https://openapi.etsy.com/v3/application"))
        return self._client

    # --- Write primitives (each audited) ----------------------------

    def update_title(self, listing_id, title, *, old=None, reason="", source="manual"):
        title = (title or "").strip()[:_MAX_TITLE]
        if not title:
            return self._skip(listing_id, "title", old, title, "empty title", source)
        return self._apply(listing_id, "title", old, title, reason, source,
                           lambda: self.client.update_listing(listing_id, {"title": title}))

    def update_description(self, listing_id, description, *, old=None, reason="",
                           source="manual"):
        description = (description or "").strip()
        if not description:
            return self._skip(listing_id, "description", old, description,
                              "empty description", source)
        return self._apply(listing_id, "description", old, description, reason, source,
                           lambda: self.client.update_listing(
                               listing_id, {"description": description}))

    def update_tags(self, listing_id, tags, *, old=None, reason="", source="manual"):
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()][:_TAG_COUNT]
        if not tags:
            return self._skip(listing_id, "tags", old, tags, "no tags", source)
        return self._apply(listing_id, "tags", old, tags, reason, source,
                           lambda: self.client.update_listing(listing_id, {"tags": tags}))

    def update_price(self, listing_id, price, *, old=None, reason="", source="manual"):
        price = round(float(price), 2)
        if price <= 0:
            return self._skip(listing_id, "price", old, price, "non-positive price", source)
        if old is not None and round(float(old), 2) == price:
            return self._skip(listing_id, "price", old, price, "unchanged", source)
        return self._apply(listing_id, "price", old, price, reason, source,
                           lambda: self.client.set_price_and_quantity(listing_id, price=price))

    def update_inventory(self, listing_id, quantity, *, old=None, reason="",
                         source="manual"):
        quantity = int(quantity)
        if quantity < 0:
            return self._skip(listing_id, "quantity", old, quantity, "negative quantity", source)
        return self._apply(listing_id, "quantity", old, quantity, reason, source,
                           lambda: self.client.set_price_and_quantity(
                               listing_id, quantity=quantity))

    def update_images(self, listing_id, image_paths, *, alt_texts=None, reason="",
                      source="manual"):
        """Add new images to a listing (rank 1..n). Existing images untouched
        unless overwrite is used by the caller elsewhere."""
        paths = list(image_paths or [])
        if not paths:
            return self._skip(listing_id, "images", None, paths, "no images", source)

        def _do():
            uploaded = []
            for i, path in enumerate(paths, start=1):
                alt = (alt_texts[i - 1] if alt_texts and i - 1 < len(alt_texts) else None)
                uploaded.append(self.client.upload_listing_image(
                    listing_id, path, rank=i, alt_text=alt, overwrite=True))
            return {"uploaded": len(uploaded)}

        return self._apply(listing_id, "images", None, [str(p) for p in paths],
                           reason, source, _do)

    def deactivate(self, listing_id, *, reason="", source="portfolio"):
        return self._apply(listing_id, "state", "active", "inactive", reason, source,
                           lambda: self.client.deactivate_listing(listing_id))

    # --- Decision-driven flows --------------------------------------

    def apply_learning_actions(self, digest: dict[str, Any]) -> dict[str, Any]:
        """Reflect a Learning Engine digest on Etsy: reprice IMPROVE listings and
        deactivate RETIRE listings. Only the concretely-actionable changes are
        pushed; everything attempted is audited."""
        if not self.is_configured:
            return {"applied": 0, "skipped": 0, "failed": 0,
                    "reason": "Etsy not configured for writes"}
        actions = digest.get("actions", {})
        applied = skipped = failed = 0
        results: list[dict[str, Any]] = []

        for item in actions.get("retire", []):
            res = self._retire_sku(item.get("sku"), item.get("product_key"),
                                   item.get("why", "retired by lifecycle"))
            results.append(res)
            applied, skipped, failed = self._tally(res, applied, skipped, failed)

        for item in actions.get("adjust", []):
            if "reprice" not in (item.get("improvements") or []):
                continue
            res = self._reprice_sku(item.get("sku"), item.get("product_key"),
                                    item.get("why", "improve: reprice"))
            results.append(res)
            applied, skipped, failed = self._tally(res, applied, skipped, failed)

        log.info("Etsy automation: %d applied, %d skipped, %d failed.",
                 applied, skipped, failed)
        return {"applied": applied, "skipped": skipped, "failed": failed,
                "results": results}

    def _retire_sku(self, sku, product_key, reason) -> dict[str, Any]:
        listing_id = self._listing_for(sku)
        if not listing_id:
            return self._skip(None, "state", "active", "inactive",
                              f"no live Etsy listing for {sku}", "portfolio",
                              product_key=product_key)
        return self.deactivate(listing_id, reason=reason, source="portfolio")

    def _reprice_sku(self, sku, product_key, reason) -> dict[str, Any]:
        listing_id = self._listing_for(sku)
        if not listing_id:
            return self._skip(None, "price", None, None,
                              f"no live Etsy listing for {sku}", "learning",
                              product_key=product_key)
        product = self.db.get_product_by_sku(str(sku)) if sku else None
        listing = self.db.get_etsy_listing(int(listing_id))
        current = float((listing or {}).get("price") or 0) or None
        cost = float((product or {}).get("production_cost") or 0)
        opt = self.pricing.optimise(cost, current)
        return self.update_price(listing_id, opt["price"], old=current,
                                 reason=f"{reason} -> {opt['rationale']}", source="learning")

    # --- Helpers ----------------------------------------------------

    def _listing_for(self, sku: str | None) -> str | None:
        """The live Etsy listing id for a product sku, via its publication."""
        if not sku:
            return None
        product = self.db.get_product_by_sku(str(sku))
        campaign_id = (product or {}).get("campaign_id")
        if campaign_id is None:
            return None
        pub = self.db.get_active_publication(campaign_id, "etsy", product_id=str(sku))
        return (pub or {}).get("listing_id")

    def _apply(self, listing_id, field, old, new, reason, source, fn) -> dict[str, Any]:
        product_key = None
        try:
            fn()
            self.db.insert_etsy_change({
                "listing_id": str(listing_id) if listing_id is not None else None,
                "product_key": product_key, "field": field, "old_value": old,
                "new_value": new, "reason": reason, "source": source, "status": "applied"})
            log.info("Etsy %s updated on listing %s (%s).", field, listing_id, source)
            return {"status": "applied", "field": field, "listing_id": listing_id,
                    "old": old, "new": new}
        except Exception as exc:  # isolate — audit and continue
            self.db.insert_etsy_change({
                "listing_id": str(listing_id) if listing_id is not None else None,
                "field": field, "old_value": old, "new_value": new, "reason": reason,
                "source": source, "status": "failed", "error": str(exc)})
            log.warning("Etsy %s update FAILED on listing %s: %s", field, listing_id, exc)
            return {"status": "failed", "field": field, "listing_id": listing_id,
                    "error": str(exc)}

    def _skip(self, listing_id, field, old, new, why, source, *, product_key=None):
        self.db.insert_etsy_change({
            "listing_id": str(listing_id) if listing_id is not None else None,
            "product_key": product_key, "field": field, "old_value": old,
            "new_value": new, "reason": why, "source": source, "status": "skipped"})
        return {"status": "skipped", "field": field, "listing_id": listing_id, "why": why}

    @staticmethod
    def _tally(res, applied, skipped, failed):
        s = res["status"]
        return (applied + (s == "applied"), skipped + (s == "skipped"),
                failed + (s == "failed"))

    # --- Reads ------------------------------------------------------

    def audit(self, listing_id: str | None = None) -> list[dict[str, Any]]:
        return self.db.list_etsy_changes(listing_id)
