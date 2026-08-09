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
                 pricing: Any = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "pricing", None) or {}
        self.pricing = pricing or PricingEngine(config, db)
        self._shop = shopify

    def _shopify(self) -> Any:
        if self._shop is None:
            from onassis.connectors.shopify import ShopifyConnector
            self._shop = ShopifyConnector(self.config, self.db)
        return self._shop

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

            pub = (self.db.get_latest_publication(cid, "shopify", product_id=f"{cid}-{key}")
                   if cid else None)
            listing_id = (pub or {}).get("listing_id")
            row: dict[str, Any] = {
                "name": p.get("name") or key, "product_key": key,
                "sales_window": sales, "old_target": target, "new_target": new_target,
                "direction": direction, "new_price": new_price,
                "listing_id": listing_id, "old_price": None, "status": "ok"}

            if apply:
                moved = direction in ("up", "down", "seed")
                self.db.set_price_state(
                    cid, key, target_profit=new_target, price=new_price,
                    direction=direction,
                    last_adjusted=today if moved else (last_adj or today))
                if not moved:
                    row["status"] = "hold"          # not due — no Shopify call
                elif not listing_id:
                    row["status"] = "not_on_shopify"
                elif shop.can_publish:
                    try:
                        old = shop.product_price(listing_id)
                        row["old_price"] = old
                        if old is None or abs(float(old) - new_price) >= 0.01:
                            shop.set_product_price(listing_id, new_price)
                            row["status"] = "repriced"
                            changed += 1
                        else:
                            row["status"] = "unchanged"
                    except Exception as exc:  # one product never aborts the run
                        row["status"] = "error"
                        row["error"] = str(exc)[:200]
                        errors += 1
                else:
                    row["status"] = "shopify_not_connected"
            rows.append(row)

        log.info("Adaptive reprice %s: %d product(s), %d moved, %d error(s) (window %dd).",
                 "APPLY" if apply else "preview", len(rows), changed, errors, window)
        return {"ok": True, "applied": apply, "window_days": window,
                "changed": changed, "errors": errors, "count": len(rows), "products": rows}
