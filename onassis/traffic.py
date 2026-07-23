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

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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
        self.horizon_days = max(1, int(cfg.get("schedule_horizon_days", 7)))
        self.boards = cfg.get("boards") or []

    # --- Schedule ----------------------------------------------------

    def schedule(self, today: str | None = None,
                 campaign_id: int | None = None) -> dict[str, Any]:
        """Fill a rolling calendar: spread fresh pins across today..+horizon days,
        each day capped at ``max_pins_per_day``, across boards/keywords/seasons.
        Each pin carries its product's hero image. Never re-queues a scheduled pin."""
        base = today or date.today().isoformat()
        seen = self.db.scheduled_pin_keys()
        candidates = self._candidates(campaign_id, seen)

        # Per-day remaining capacity across the horizon.
        days = [self._add_days(base, i) for i in range(self.horizon_days)]
        capacity = {d: max(0, self.max_per_day - self.db.count_pins_scheduled_on(d))
                    for d in days}

        scheduled = 0
        boards_used: set[str] = set()
        by_day: dict[str, int] = {}
        slot = 0
        for pin in candidates:
            target = next((d for d in days if capacity[d] > 0), None)
            if target is None:
                break  # the whole horizon is full
            board = self._board(pin, slot)
            row = {**pin, "board": board, "season": season_for(target),
                   "scheduled_date": target, "status": "scheduled"}
            if self.db.insert_pin_schedule(row) is not None:
                capacity[target] -= 1
                scheduled += 1
                slot += 1
                boards_used.add(board)
                by_day[target] = by_day.get(target, 0) + 1
        log.info("Traffic: scheduled %d pin(s) across %d day(s), %d board(s).",
                 scheduled, len(by_day), len(boards_used))
        return {"date": base, "season": season_for(base), "scheduled": scheduled,
                "available": len(candidates), "boards": sorted(boards_used),
                "by_day": by_day, "horizon_days": self.horizon_days,
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
            hero = self._hero_path(asset.get("campaign_id"), product_key)
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
                    "image_path": hero, "_board_hint": pin.get("board"),
                })
        return out

    def _hero_path(self, campaign_id: Any, product_key: str | None) -> str | None:
        """The product's chosen hero image (what a live pin must display)."""
        if campaign_id is None or not product_key:
            return None
        from onassis.config import ROOT_DIR

        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        hero = base / str(campaign_id) / str(product_key) / "images" / "hero.jpg"
        return str(hero) if hero.exists() else None

    def _board(self, pin: dict[str, Any], slot: int) -> str:
        if self.boards:
            return self.boards[slot % len(self.boards)]
        return pin.get("_board_hint") or "Local Celebrity"

    @staticmethod
    def _add_days(day: str, n: int) -> str:
        try:
            return (date.fromisoformat(day[:10]) + timedelta(days=n)).isoformat()
        except ValueError:
            return day

    # --- Bulk pin the whole catalogue (Sprint 49) --------------------

    def publish_all_products(self, *, limit: int | None = None,
                             require_link: bool = False) -> dict[str, Any]:
        """Post one pin for EVERY active product that has a hero image, straight
        to the configured board — building the board out from the whole catalogue
        in one go. The image is uploaded inline (no public URL needed); a listing
        link is attached when the product is live. Best-effort per pin.

        Pinterest has no natural dedupe, so re-running re-pins — run it once.
        ``require_link`` skips products with no live listing to link to."""
        if not self.pinterest.can_publish:
            return {"posted": 0, "failed": 0, "no_image": 0, "skipped": 0, "total": 0,
                    "reason": "Pinterest not connected — set the access token and board id."}
        products = [p for p in self.db.list_products()
                    if p.get("active", 1) and p.get("product_key") and p.get("campaign_id")]
        if limit:
            products = products[:limit]
        posted = failed = no_image = skipped = 0
        results: list[dict[str, Any]] = []
        for p in products:
            cid, key, sku = p["campaign_id"], p["product_key"], p["sku"]
            image = self._hero_path(cid, key)
            if not image:
                no_image += 1
                continue
            meta = self._listing_meta(cid, key)
            link = self._product_link(cid, sku, meta)
            if require_link and not link:
                skipped += 1
                continue
            title = str(meta.get("title") or p.get("name") or key)[:100]
            res = self.pinterest.publish_pins([{
                "title": title, "description": self._pin_description(meta, p),
                "link": link, "image_path": image, "alt_text": title}])
            if res.get("posted"):
                posted += 1
                ref = (res.get("results") or [{}])[0].get("pin_id")
                results.append({"sku": sku, "pin_id": ref, "link": link})
            else:
                failed += 1
                reason = (res.get("results") or [{}])[0].get("reason") or res.get("reason")
                results.append({"sku": sku, "error": reason})
        log.info("Pinterest bulk: posted %d, failed %d, no-image %d of %d product(s).",
                 posted, failed, no_image, len(products))
        return {"posted": posted, "failed": failed, "no_image": no_image,
                "skipped": skipped, "total": len(products), "results": results[:200]}

    def _listing_meta(self, campaign_id: Any, product_key: str) -> dict[str, Any]:
        from onassis.config import ROOT_DIR
        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        lp = base / str(campaign_id) / str(product_key) / "listing.json"
        if not lp.exists():
            return {}
        try:
            return json.loads(lp.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _product_link(self, campaign_id: int, sku: str, meta: dict[str, Any]) -> str | None:
        if meta.get("listing_url"):
            return meta["listing_url"]
        for platform in ("etsy", "shopify"):
            pub = self.db.get_latest_publication(campaign_id, platform, product_id=sku)
            if pub and pub.get("status") in ("published", "live", "draft"):
                url = pub.get("listing_url") or pub.get("url")
                if url:
                    return url
                lid = pub.get("listing_id")
                if platform == "etsy" and lid and str(lid).isdigit():
                    return f"https://www.etsy.com/listing/{lid}"
        return None

    @staticmethod
    def _pin_description(meta: dict[str, Any], product: dict[str, Any]) -> str:
        theme = meta.get("theme") or ""
        tags = meta.get("tags") or meta.get("seo_keywords") or []
        hashtags = " ".join("#" + str(t).replace(" ", "") for t in tags[:5])
        parts = []
        base = str(meta.get("description") or "").strip()[:300]
        if base:
            parts.append(base)
        if theme:
            parts.append(f"Inspired by {theme}.")
        if hashtags:
            parts.append(hashtags)
        return (" ".join(parts)[:500]) or str(product.get("name") or "")

    # --- Distribute --------------------------------------------------

    def distribute(self, today: str | None = None) -> dict[str, Any]:
        """Post every pin whose scheduled date has arrived, with its hero image
        attached. A pin with no image file is left queued (Pinterest needs media).
        Safe no-op until Pinterest is configured."""
        day = today or date.today().isoformat()
        due = self.db.due_pins(day)
        if not due:
            return {"date": day, "posted": 0, "queued": 0, "detail": "nothing due"}
        if not self.pinterest.can_publish:
            return {"date": day, "posted": 0, "queued": len(due),
                    "detail": "Pinterest not configured — pins stay queued"}
        posted = failed = no_image = 0
        for pin in due:
            image = pin.get("image_path")
            if not image or not Path(image).exists():
                no_image += 1
                continue  # can't post an imageless pin — keep it queued
            payload = [{"title": pin.get("title"), "description": pin.get("description"),
                        "link": pin.get("listing_url"), "alt_text": pin.get("title"),
                        "image_path": image}]
            res = self.pinterest.publish_pins(payload)
            if res.get("posted"):
                ref = (res.get("results") or [{}])[0].get("pin_id")
                self.db.set_pin_status(pin["id"], "posted", pin_ref=ref)
                posted += 1
            else:
                self.db.set_pin_status(pin["id"], "failed")
                failed += 1
        return {"date": day, "posted": posted, "failed": failed,
                "no_image": no_image, "queued": no_image}

    # --- Import metrics (impressions / clicks / CTR, attributed) ----

    def import_metrics(self, today: str | None = None) -> dict[str, Any]:
        """Pull per-pin analytics from Pinterest, attribute impressions/clicks to
        each product, and record the day's increments to the funnel + metric
        history. Safe no-op until analytics is available."""
        day = today or date.today().isoformat()
        pins = self.db.posted_pins()
        if not pins or not self.pinterest.can_read_analytics:
            return {"date": day, "pins": len(pins), "impressions": 0, "clicks": 0,
                    "detail": "no posted pins or analytics unavailable"}
        per_product: dict[str, dict[str, Any]] = {}
        for pin in pins:
            stats = self.pinterest.pin_analytics(pin["pin_ref"])
            new_imp, new_clk = int(stats["impressions"]), int(stats["clicks"])
            d_imp = max(0, new_imp - int(pin.get("impressions", 0) or 0))
            d_clk = max(0, new_clk - int(pin.get("clicks", 0) or 0))
            self.db.record_pin_metrics(pin["id"], impressions=new_imp, clicks=new_clk)
            if d_imp or d_clk:
                bucket = per_product.setdefault(
                    pin.get("product_key") or "unknown",
                    {"impressions": 0, "clicks": 0, "listing_id": pin.get("listing_id")})
                bucket["impressions"] += d_imp
                bucket["clicks"] += d_clk

        snaps: list[dict[str, Any]] = []
        total_imp = total_clk = 0
        for product_key, b in per_product.items():
            total_imp += b["impressions"]
            total_clk += b["clicks"]
            # Attribute to the product in the funnel...
            self.record_funnel(product_key=product_key, listing_id=b.get("listing_id"),
                               impressions=b["impressions"], clicks=b["clicks"], today=day)
            # ...and into the metric history the funnel snapshot / dashboard read.
            for metric, value in (("impressions", b["impressions"]), ("clicks", b["clicks"])):
                snaps.append({"platform": "pinterest", "product_id": product_key,
                              "metric": metric, "value": value, "snapshot_date": day})
        if snaps:
            self.db.insert_metric_snapshots(snaps)
        log.info("Traffic metrics: %d impression(s), %d click(s) across %d product(s).",
                 total_imp, total_clk, len(per_product))
        return {"date": day, "pins": len(pins), "impressions": total_imp,
                "clicks": total_clk, "products": len(per_product)}

    def run(self, today: str | None = None,
            campaign_id: int | None = None) -> dict[str, Any]:
        """The daily traffic push: schedule, distribute due pins, import metrics."""
        sched = self.schedule(today, campaign_id)
        dist = self.distribute(today)
        metrics = self.import_metrics(today)
        return {"schedule": sched, "distribute": dist, "metrics": metrics}

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
