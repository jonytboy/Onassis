"""The Portfolio Manager — the 30-day product lifecycle.

Every listing is an investment with a probation period. After it has been live
for ``review_after_days`` (default 30) the manager judges it on real evidence —
CTR, conversion, visits and sales — and issues one verdict:

* **KEEP**    — it sells and it makes money. Leave it live; it earns its slot.
* **IMPROVE** — there is interest (traffic or favourites) but it is not
  converting yet, or it is starved of traffic. Adjust the price, keywords and
  hero image and give it another window.
* **RETIRE**  — it loses money, or the market has seen it and said no. Archive
  it (``active = 0``) and never touch it again — unless, much later, fresh
  market data says the trend has turned (:meth:`reconsider_archived`).

This is a deterministic module (no AI agent). It reads data the system already
owns — products, Etsy listing traffic, orders — and writes only the lifecycle
verdict (an append-only ``portfolio_reviews`` row) and, on RETIRE, the archive
flag. The Learning Engine consumes its output to increase winners and act on the
IMPROVE adjustments.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

KEEP = "KEEP"
IMPROVE = "IMPROVE"
RETIRE = "RETIRE"

_BANDS = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "VERY HIGH": 3}


class PortfolioManager:
    """Runs the 30-day KEEP / IMPROVE / RETIRE lifecycle over live listings."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        cfg = config.portfolio or {}
        self.review_after_days = int(cfg.get("review_after_days", 30))
        # Traffic a listing needs before "no sales" counts as a real rejection.
        self.min_traffic = int(cfg.get("min_traffic_to_judge", 60))
        # A listing already told IMPROVE once that still hasn't turned is retired.
        self.max_improves = int(cfg.get("max_improve_cycles", 1))
        self.reconsider_band = str(cfg.get("reconsider_band", "VERY HIGH")).upper()

    # --- The lifecycle review ---------------------------------------

    def review(self, today: str | None = None,
               ctr_lookup: Callable[[str], float] | None = None) -> dict[str, Any]:
        """Review every active listing past its window; act on the verdict.

        Args:
            today: ISO date to age against (defaults to today).
            ctr_lookup: optional ``sku -> click-through-rate`` (from the Traffic
                Engine's funnel); used only as extra evidence, never required.
        """
        day = today or date.today().isoformat()
        listings = self._listings_by_sku()
        reviews: list[dict[str, Any]] = []
        kept = improved = retired = 0

        for product in self.db.list_products():
            if not product.get("active", 1):
                continue  # already archived — never touch it again here
            sku = product.get("sku")
            if not sku:
                continue
            age = self._age_days(product, day)
            if age < self.review_after_days:
                continue  # still on probation — not judged yet

            metrics = self._metrics(sku, listings)
            ctr = float(ctr_lookup(sku)) if ctr_lookup else 0.0
            decision, reason = self._decide(sku, metrics)
            record = {
                "sku": sku, "product_key": product.get("product_key"),
                "campaign_id": product.get("campaign_id"), "age_days": age,
                "views": metrics["views"], "favourites": metrics["favourites"],
                "units": metrics["units"], "net_profit": metrics["net_profit"],
                "conversion": metrics["conversion"], "ctr": round(ctr, 4),
                "decision": decision, "reason": reason,
                "name": product.get("name") or sku,
            }
            if decision == RETIRE:
                self.db.set_product_active(sku, False)  # archive — the only write
                retired += 1
            elif decision == IMPROVE:
                record["improvements"] = self._improvements(metrics)
                improved += 1
            else:
                kept += 1
            self.db.insert_portfolio_review(record)
            reviews.append(record)

        log.info("Portfolio review (%s): %d kept, %d to improve, %d retired.",
                 day, kept, improved, retired)
        return {
            "date": day, "reviewed": len(reviews),
            "kept": kept, "improve": improved, "retired": retired,
            "reviews": reviews,
            "keep": [r for r in reviews if r["decision"] == KEEP],
            "to_improve": [r for r in reviews if r["decision"] == IMPROVE],
            "retired_list": [r for r in reviews if r["decision"] == RETIRE],
        }

    def _decide(self, sku: str, m: dict[str, Any]) -> tuple[str, str]:
        """The deterministic verdict from real evidence — money first."""
        units, net = m["units"], m["net_profit"]
        views, favs = m["views"], m["favourites"]
        prior = self.db.get_last_portfolio_review(sku)
        improved_before = sum(
            1 for r in self.db.list_portfolio_reviews(sku) if r["decision"] == IMPROVE)

        if net < 0:
            return RETIRE, (f"Loses money (net {net:.2f}) after {views} views — "
                            f"archive it, don't keep funding a loss.")
        if units > 0 and net > 0:
            return KEEP, (f"Sells profitably ({units} sold, net {net:.2f}) — a winner; "
                          f"keep it live and scale it.")
        # No sales yet. Has the market had a real look?
        if views >= self.min_traffic:
            if favs > 0 and improved_before < self.max_improves:
                return IMPROVE, (f"{views} views, {favs} favourite(s), 0 sales — "
                                 f"interest but no conversion; fix price/keywords/hero.")
            return RETIRE, (f"{views} views and no sales — the market has seen it and "
                            f"said no. Archive it.")
        # Under-exposed: a traffic problem, not (yet) a product problem.
        if improved_before >= self.max_improves:
            return RETIRE, (f"Still only {views} views after an improvement window — "
                            f"can't earn its slot; archive it.")
        _ = prior  # (kept for clarity; decision uses the improve count)
        return IMPROVE, (f"Only {views} views in {self.review_after_days} days — "
                         f"starved of traffic; drive traffic and refresh the hero.")

    def _improvements(self, m: dict[str, Any]) -> list[str]:
        """Concrete, deterministic adjustments for an IMPROVE verdict."""
        actions: list[str] = []
        if m["views"] < self.min_traffic:
            actions.append("drive_traffic")   # more pins / boards / seasons
            actions.append("refresh_hero")    # a stronger thumbnail may lift CTR
        else:
            # Traffic is fine but it isn't converting: price + hero + keywords.
            actions.append("reprice")
            actions.append("refresh_hero")
            actions.append("refresh_keywords")
        return actions

    # --- Reactivation (only when the trend turns) -------------------

    def reconsider_archived(self, min_band: str | None = None) -> dict[str, Any]:
        """Bring an archived product type back only if fresh market data now shows
        a strong trend for it — the one exception to 'never touch it again'."""
        band = (min_band or self.reconsider_band).upper()
        floor = _BANDS.get(band, 3)
        hot = {s.get("product_type") for s in self.db.top_market_signals(50)
               if _BANDS.get(str(s.get("opportunity", "")).upper(), 0) >= floor}
        hot.discard(None)
        reactivated: list[dict[str, Any]] = []
        for product in self.db.list_products():
            if product.get("active", 1):
                continue
            if product.get("product_key") in hot:
                self.db.set_product_active(product["sku"], True)
                reactivated.append({"sku": product["sku"],
                                    "product_key": product.get("product_key")})
        if reactivated:
            log.info("Portfolio reconsidered %d archived product(s) on a turning trend.",
                     len(reactivated))
        return {"band": band, "reactivated": reactivated}

    # --- Reads ------------------------------------------------------

    def history(self, sku: str | None = None) -> list[dict[str, Any]]:
        return self.db.list_portfolio_reviews(sku)

    def archived(self) -> list[dict[str, Any]]:
        return [p for p in self.db.list_products() if not p.get("active", 1)]

    # --- Helpers ----------------------------------------------------

    def _age_days(self, product: dict[str, Any], day: str) -> int:
        stamp = product.get("launched_at") or product.get("created_at")
        if not stamp:
            return 0
        try:
            launched = datetime.fromisoformat(stamp)
        except ValueError:
            return 0
        if launched.tzinfo is None:
            launched = launched.replace(tzinfo=timezone.utc)
        try:
            now = datetime.fromisoformat(day)
        except ValueError:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return max(0, (now - launched).days)

    def _listings_by_sku(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for l in self.db.list_etsy_listings():
            sku = str(l.get("product_id"))
            bucket = out.setdefault(sku, {"views": 0, "favourites": 0})
            bucket["views"] += int(l.get("views", 0) or 0)
            bucket["favourites"] += int(l.get("num_favorers", 0) or 0)
        return out

    def _metrics(self, sku: str, listings: dict[str, dict[str, int]]) -> dict[str, Any]:
        traffic = listings.get(str(sku), {"views": 0, "favourites": 0})
        orders = self.db.get_orders_for_product(str(sku))
        units = sum(int(o.get("quantity", 1) or 1) for o in orders)
        net = round(sum(float(o.get("net_profit", 0) or 0) for o in orders), 2)
        views = int(traffic["views"])
        conversion = round(units / views, 4) if views > 0 else 0.0
        return {"views": views, "favourites": int(traffic["favourites"]),
                "units": units, "net_profit": net, "conversion": conversion}
