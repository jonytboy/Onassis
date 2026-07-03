"""The Traffic Engine — actually distribute the marketing, starting with Pinterest.

Content that is produced but never posted drives no traffic. This engine takes
the Marketing Engine's output and **distributes it**, beginning with the channel
that works hardest for a visual Etsy shop: Pinterest.

* **Schedule** — it queues 5–10 pins a day, spread across boards, keywords and
  the current season, and never double-posts the same pin (a stable ``pin_key``
  dedupes the queue).
* **Distribute** — when Pinterest is configured it posts the day's due pins (a
  safe no-op that keeps them queued otherwise), each linking back to the Etsy
  listing.
* **Measure** — it logs the whole funnel: **Impressions → Clicks → Visits →
  Sales**, so the system can see where money actually comes from.

Deterministic and offline-safe: with no Pinterest credentials it still builds the
schedule and records the funnel from data the system already collects.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from onassis.config import Config
from onassis.connectors.pinterest import PinterestConnector
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

_SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring",
            5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn",
            10: "autumn", 11: "autumn"}


def season_for(day: str) -> str:
    try:
        month = datetime.fromisoformat(day).month
    except ValueError:
        month = 1
    return _SEASONS.get(month, "all-season")


class TrafficEngine:
    """Schedules and distributes pins, and logs the traffic funnel."""

    def __init__(self, config: Config, db: Database,
                 pinterest: PinterestConnector | None = None) -> None:
        self.config = config
        self.db = db
        self.pinterest = pinterest or PinterestConnector(config)
        cfg = getattr(config, "traffic", None) or {}
        self.min_per_day = int(cfg.get("min_pins_per_day", 5))
        self.max_per_day = int(cfg.get("max_pins_per_day", 10))
        self.boards = cfg.get("boards") or []

    # --- Schedule ----------------------------------------------------

    def schedule(self, today: str | None = None,
                 campaign_id: int | None = None) -> dict[str, Any]:
        """Queue up to ``max_pins_per_day`` fresh pins for today across boards/
        keywords/seasons. Never re-queues a pin already scheduled."""
        day = today or date.today().isoformat()
        season = season_for(day)
        already = self.db.count_pins_scheduled_on(day)
        remaining = self.max_per_day - already
        if remaining <= 0:
            return {"date": day, "scheduled": 0, "season": season,
                    "reason": f"daily pin cap reached ({already}/{self.max_per_day})"}

        seen = self.db.scheduled_pin_keys()
        candidates = self._candidates(campaign_id, seen)
        chosen = candidates[:remaining]
        scheduled = 0
        boards_used: set[str] = set()
        for slot, pin in enumerate(chosen):
            board = self._board(pin, slot)
            row = {**pin, "board": board, "season": season,
                   "scheduled_date": day, "status": "scheduled"}
            if self.db.insert_pin_schedule(row) is not None:
                scheduled += 1
                boards_used.add(board)
        log.info("Traffic: scheduled %d pin(s) for %s (%s) across %d board(s).",
                 scheduled, day, season, len(boards_used))
        return {"date": day, "season": season, "scheduled": scheduled,
                "available": len(candidates), "boards": sorted(boards_used),
                "met_minimum": scheduled >= min(self.min_per_day, len(candidates))}

    def _candidates(self, campaign_id: int | None,
                    seen: set[str]) -> list[dict[str, Any]]:
        """Flatten unscheduled Pinterest pins from the marketing assets."""
        out: list[dict[str, Any]] = []
        rows = self.db.list_marketing_assets(channel="pinterest", campaign_id=campaign_id)
        # Oldest assets first so every product gets promoted in turn.
        for asset in reversed(rows):
            product_key = asset.get("product_key")
            pins = (asset.get("payload") or {}).get("pins", [])
            for i, pin in enumerate(pins):
                pin_key = f"{product_key}:{i}:{pin.get('aspect_ratio')}"
                if pin_key in seen:
                    continue
                out.append({
                    "pin_key": pin_key, "campaign_id": asset.get("campaign_id"),
                    "product_key": product_key, "listing_id": asset.get("listing_id"),
                    "listing_url": asset.get("listing_url"),
                    "keyword": (pin.get("keywords") or [None])[0] or pin.get("title"),
                    "aspect_ratio": pin.get("aspect_ratio"),
                    "title": pin.get("title"), "description": pin.get("description"),
                    "_board_hint": pin.get("board"),
                })
        return out

    def _board(self, pin: dict[str, Any], slot: int) -> str:
        if self.boards:
            return self.boards[slot % len(self.boards)]
        return pin.get("_board_hint") or "Local Celebrity"

    # --- Distribute --------------------------------------------------

    def distribute(self, today: str | None = None) -> dict[str, Any]:
        """Post today's due pins to Pinterest (safe no-op until configured)."""
        day = today or date.today().isoformat()
        due = self.db.list_pin_schedule(scheduled_date=day, status="scheduled")
        if not due:
            return {"date": day, "posted": 0, "queued": 0, "detail": "nothing due"}
        if not self.pinterest.can_publish:
            return {"date": day, "posted": 0, "queued": len(due),
                    "detail": "Pinterest not configured — pins stay queued"}
        posted = failed = 0
        for pin in due:
            payload = [{"title": pin.get("title"), "description": pin.get("description"),
                        "link": pin.get("listing_url"), "alt_text": pin.get("title"),
                        "image_path": pin.get("image_path")}]
            res = self.pinterest.publish_pins(payload)
            if res.get("posted"):
                ref = (res.get("results") or [{}])[0].get("pin_id")
                self.db.set_pin_status(pin["id"], "posted", pin_ref=ref)
                posted += 1
            else:
                self.db.set_pin_status(pin["id"], "failed")
                failed += 1
        return {"date": day, "posted": posted, "failed": failed, "queued": 0}

    def run(self, today: str | None = None,
            campaign_id: int | None = None) -> dict[str, Any]:
        """Schedule then distribute in one call — the daily traffic push."""
        sched = self.schedule(today, campaign_id)
        dist = self.distribute(today)
        return {"schedule": sched, "distribute": dist}

    # --- Funnel: Impressions -> Clicks -> Visits -> Sales -----------

    def record_funnel(self, *, product_key: str | None, listing_id: str | None = None,
                      impressions: int = 0, clicks: int = 0, visits: int = 0,
                      sales: int = 0, today: str | None = None,
                      source: str = "pinterest") -> int:
        day = today or date.today().isoformat()
        return self.db.insert_traffic_funnel({
            "funnel_date": day, "product_key": product_key, "listing_id": listing_id,
            "source": source, "impressions": impressions, "clicks": clicks,
            "visits": visits, "sales": sales})

    def snapshot(self, today: str | None = None) -> dict[str, Any]:
        """Log one funnel row for the day from the metrics the system collects:
        Pinterest impressions/clicks, Etsy visits, and orders (sales)."""
        day = today or date.today().isoformat()
        snaps = [s for s in self.db.get_metric_series() if s["snapshot_date"] == day]

        def total(platform: str, *metrics: str) -> int:
            return int(sum(s["value"] for s in snaps
                           if s["platform"] == platform and s["metric"] in metrics))

        impressions = total("pinterest", "impressions")
        clicks = total("pinterest", "clicks", "outbound_clicks")
        visits = total("etsy", "visits", "views")
        sales = len(self.db.get_orders_on(day))
        self.record_funnel(product_key=None, impressions=impressions, clicks=clicks,
                           visits=visits, sales=sales, today=day)
        totals = self.funnel(day)
        log.info("Traffic funnel %s — impressions %d -> clicks %d -> visits %d -> sales %d.",
                 day, totals["impressions"], totals["clicks"], totals["visits"],
                 totals["sales"])
        return totals

    def funnel(self, today: str | None = None) -> dict[str, Any]:
        """The funnel totals + conversion rates (today, or all time if None)."""
        totals = self.db.funnel_totals(funnel_date=today)
        imp, clk, vis, sal = (totals["impressions"], totals["clicks"],
                              totals["visits"], totals["sales"])
        return {
            "date": today, **totals,
            "click_through_rate": round(clk / imp, 4) if imp else 0.0,
            "visit_rate": round(vis / clk, 4) if clk else 0.0,
            "conversion_rate": round(sal / vis, 4) if vis else 0.0,
        }
