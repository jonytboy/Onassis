"""Adaptive Pricing — automated price discovery from real sales.

Start each product at a comfortable margin (``adaptive_start_profit``, e.g. £3
net). Then, on a slow cadence (``adaptive_window_days``), read the real sales
signal from the orders table and move the price:

* **no sales in the window → walk the target margin DOWN** one step (cheaper,
  more likely to sell) — but never below ``adaptive_min_profit`` (a hard floor;
  the price always covers cost + shipping + fees + that minimum, so it can never
  sell at a loss).
* **sales in the window → nudge the target margin UP** one step (capture more per
  sale) up to ``adaptive_max_profit``, as long as it keeps selling.

State lives in ``price_state`` so it survives restarts and only moves once per
window (no thrashing). Deterministic and fully testable — the Shopify connector
is injectable, so tests never hit the network.

**Honest limitation:** with ~zero traffic, "no sales" means "no visitors", not
"too expensive" — lowering the price can't create demand that isn't there. This
loop earns its keep once there's real traffic; until then it simply settles each
product near the floor, which is the right default.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from onassis.logger import get_logger
from onassis.pricing import PricingEngine

log = get_logger(__name__)


class AdaptivePricer:
    def __init__(self, config: Any, db: Any, *, shopify: Any = None,
                 etsy: Any = None, pricing: Any = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "pricing", None) or {}
        self.pricing = pricing or PricingEngine(config, db)
        self._shop = shopify
        self._etsy = etsy

    def _shopify(self) -> Any:
        if self._shop is None:
            from onassis.connectors.shopify import ShopifyConnector
            self._shop = ShopifyConnector(self.config, self.db)
        return self._shop

    def _etsy_auto(self) -> Any:
        if self._etsy is None:
            from onassis.etsy_automation import EtsyAutomation
            self._etsy = EtsyAutomation(self.config, self.db)
        return self._etsy

    def _apply_to_platform(self, platform: str, listing_id: str,
                           new_price: float) -> dict[str, Any]:
        """Push ``new_price`` to one live listing on one platform. Returns
        ``{platform, status, old_price?}`` and never raises."""
        try:
            if platform == "shopify":
                shop = self._shopify()
                if not shop.can_publish:
                    return {"platform": platform, "status": "not_connected"}
                old = shop.product_price(listing_id)
                if old is not None and abs(float(old) - new_price) < 0.01:
                    return {"platform": platform, "status": "unchanged", "old_price": old}
                shop.set_product_price(listing_id, new_price)
                return {"platform": platform, "status": "repriced", "old_price": old}
            if platform == "etsy":
                etsy = self._etsy_auto()
                if not etsy.is_configured:
                    return {"platform": platform, "status": "not_connected"}
                r = etsy.update_price(listing_id, new_price,
                                      reason="adaptive price discovery", source="adaptive")
                ok = bool(r.get("ok")) or r.get("status") in ("applied", "skipped")
                return {"platform": platform,
                        "status": "repriced" if ok else "error",
                        "error": None if ok else str(r)[:150]}
        except Exception as exc:  # one platform never aborts the run
            return {"platform": platform, "status": "error", "error": str(exc)[:150]}
        return {"platform": platform, "status": "error", "error": "unknown platform"}

    def reprice(self, *, apply: bool = False, today: str | None = None) -> dict[str, Any]:
        """Walk each live product's price on the sales signal. Preview by default
        (changes nothing); ``apply`` persists the new target and pushes the price
        to Shopify. Returns a per-product breakdown."""
        start = float(self.cfg.get("adaptive_start_profit", 3.0))
        floor = float(self.cfg.get("adaptive_min_profit", 0.5))
        ceil = float(self.cfg.get("adaptive_max_profit", 8.0))
        step = float(self.cfg.get("adaptive_step", 0.5))
        window = max(1, int(self.cfg.get("adaptive_window_days", 7)))
        today = today or date.today().isoformat()  # noqa: DTZ011 (date only, fine)
        since = (date.fromisoformat(today) - timedelta(days=window)).isoformat()

        shop = self._shopify()
        rows: list[dict[str, Any]] = []
        changed = errors = 0
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            cost = float(p.get("production_cost") or 0)
            if cost <= 0:
                continue
            key = p.get("product_key") or p.get("sku")
            cid = p.get("campaign_id")
            state = self.db.get_price_state(cid, key)
            target = float(state["target_profit"]) if state else start
            last_adj = (state or {}).get("last_adjusted")
            ids = [key, p.get("sku"), f"{cid}-{key}" if cid else None]
            sales = self.db.count_product_sales_since(ids, since)

            direction, new_target = "hold", target
            if state is None:
                direction, new_target = "seed", start
            elif not last_adj or last_adj <= since:   # only move once per window
                if sales > 0:
                    direction = "up"
                    new_target = round(min(ceil, target + step), 2)
                else:
                    direction = "down"
                    new_target = round(max(floor, target - step), 2)
            new_price = self.pricing.flat_price_for(cost, new_target)

            # Every live listing this product has, on either platform.
            targets = []
            for platform in ("shopify", "etsy"):
                pub = (self.db.get_latest_publication(cid, platform,
                                                      product_id=f"{cid}-{key}")
                       if cid else None)
                lid = (pub or {}).get("listing_id")
                if lid:
                    targets.append((platform, lid))
            row: dict[str, Any] = {
                "name": p.get("name") or key, "product_key": key,
                "sales_window": sales, "old_target": target, "new_target": new_target,
                "direction": direction, "new_price": new_price,
                "old_price": None, "platforms": [], "status": "ok"}

            if apply:
                moved = direction in ("up", "down", "seed")
                if not moved:                       # not due — keep state, no API calls
                    row["status"] = "hold"
                    self.db.set_price_state(cid, key, target_profit=new_target,
                                            price=(state or {}).get("price"),
                                            direction=direction,
                                            last_adjusted=last_adj or today)
                elif not targets:
                    row["status"] = "not_published"   # nothing live to price yet
                    self.db.set_price_state(cid, key, target_profit=new_target,
                                            price=None, direction=direction,
                                            last_adjusted=today)
                else:
                    results = [self._apply_to_platform(pl, lid, new_price)
                               for pl, lid in targets]
                    row["platforms"] = results
                    row["old_price"] = next((r.get("old_price") for r in results
                                             if r.get("old_price") is not None), None)
                    landed = any(r["status"] in ("repriced", "unchanged") for r in results)
                    if any(r["status"] == "repriced" for r in results):
                        changed += 1
                    if any(r["status"] == "error" for r in results):
                        errors += 1
                    row["status"] = " ".join(f"{r['platform']}:{r['status']}"
                                             for r in results)
                    # Only advance the window when a price actually landed — a total
                    # failure keeps the old date so the NEXT run retries it.
                    self.db.set_price_state(
                        cid, key, target_profit=new_target, price=new_price,
                        direction=direction,
                        last_adjusted=today if landed else (last_adj or ""))
            else:
                row["platforms"] = [{"platform": pl} for pl, _ in targets]
            rows.append(row)

        log.info("Adaptive reprice %s: %d product(s), %d moved, %d error(s) (window %dd).",
                 "APPLY" if apply else "preview", len(rows), changed, errors, window)
        return {"ok": True, "applied": apply, "window_days": window,
                "changed": changed, "errors": errors, "count": len(rows), "products": rows}
