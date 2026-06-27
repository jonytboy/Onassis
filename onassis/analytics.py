"""The Analytics Collector — historical, append-only performance data.

Collects real-world metrics from the marketplaces (Etsy: views, visits,
favourites, orders, revenue; Pinterest: impressions, saves, outbound clicks,
CTR) and stores **every** observation as a timestamped snapshot. History is
never overwritten — each collection appends new rows — so trends can be
computed over time.

For every product it derives Traffic, Conversion, Revenue, and Profit trends
from the stored history. This module only collects, persists, and reports
trends — no dashboards, no advertising, no recommendations.

Sources are pluggable (anything with ``fetch_metrics() -> list[dict]``), so new
marketplaces add data without changing the engine. The CEO (via proposal risk)
and the Product Optimiser consume these historical trends.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# Which stored metric drives each required trend.
_TREND_METRICS = {
    "traffic_trend": "views",
    "conversion_trend": "conversion",
    "revenue_trend": "revenue",
    "profit_trend": "net_profit",
}


def _trend(series: list[tuple[str, float]]) -> dict[str, Any]:
    """Direction of a metric over its history (first vs latest observation)."""
    if len(series) < 2:
        return {"value": 0.0, "label": "flat", "points": len(series)}
    first, last = series[0][1], series[-1][1]
    value = round(last - first, 4)
    pct = round((value / first), 4) if first else 0.0
    label = "up" if value > 0 else ("down" if value < 0 else "flat")
    return {"value": value, "pct_change": pct, "label": label, "points": len(series)}


class EtsyAnalyticsSource:
    """Derives Etsy metrics from already-imported listings, stats, and orders."""

    name = "etsy"

    def __init__(self, db: Database) -> None:
        self.db = db

    def fetch_metrics(self) -> list[dict[str, Any]]:
        today = date.today().isoformat()
        rows: list[dict[str, Any]] = []
        for listing in self.db.list_etsy_listings():
            product_id = str(listing["listing_id"])
            orders = self.db.get_orders_for_product(product_id)
            order_count = len(orders)
            revenue = round(sum(o["gross_revenue"] for o in orders), 2)
            net_profit = round(sum(o["net_profit"] for o in orders), 2)
            views = int(listing.get("views", 0) or 0)
            favourites = int(listing.get("num_favorers", 0) or 0)
            conversion = round(order_count / views, 4) if views > 0 else 0.0
            for metric, value in (
                ("views", views), ("visits", views), ("favourites", favourites),
                ("orders", order_count), ("revenue", revenue),
                ("net_profit", net_profit), ("conversion", conversion),
            ):
                rows.append({
                    "platform": "etsy", "product_id": product_id,
                    "campaign_id": listing.get("campaign_id"), "metric": metric,
                    "value": value, "snapshot_date": today,
                })
        return rows


class AnalyticsEngine:
    """Collects metric snapshots and computes historical trends."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # --- Collection -------------------------------------------------

    def default_sources(self) -> list[Any]:
        from onassis.connectors.pinterest import PinterestConnector

        return [EtsyAnalyticsSource(self.db), PinterestConnector(self.config)]

    def collect(self, sources: list[Any] | None = None) -> dict[str, Any]:
        """Append a fresh snapshot from each source. Never overwrites history."""
        sources = sources if sources is not None else self.default_sources()
        per_source: dict[str, int] = {}
        total = 0
        for src in sources:
            rows = src.fetch_metrics()
            if rows:
                total += self.db.insert_metric_snapshots(rows)
            per_source[getattr(src, "name", src.__class__.__name__)] = len(rows)
        log.info("Collected %d metric snapshot(s): %s", total, per_source)
        return {"collected": total, "by_source": per_source}

    # --- Reads ------------------------------------------------------

    def overall(self) -> dict[str, Any]:
        products = self.db.distinct_metric_products()
        platforms = sorted({s["platform"] for s in self.db.get_metric_series()})
        return {
            "snapshots": self.db.count_metric_snapshots(),
            "products_tracked": len(products),
            "platforms": platforms,
            "products": products,
        }

    def product_analytics(self, product_id: str) -> dict[str, Any]:
        snaps = self.db.get_metric_series(product_id=product_id)
        return {
            "product_id": product_id,
            "history_points": len(snaps),
            "latest": self._latest_by_metric(snaps),
            "trends": self._trends(snaps),
            "history": snaps,
        }

    def campaign_analytics(self, campaign_id: int) -> dict[str, Any]:
        snaps = self.db.get_metric_series(campaign_id=campaign_id)
        return {
            "campaign_id": campaign_id,
            "history_points": len(snaps),
            "latest": self._latest_by_metric(snaps),
            "trends": self._trends(snaps),
        }

    # --- Trend helpers ----------------------------------------------

    def _series_for(self, snaps: list[dict[str, Any]], metric: str) -> list[tuple[str, float]]:
        """Aggregate a metric per snapshot_date (summing across products)."""
        by_date: dict[str, float] = {}
        for s in snaps:
            if s["metric"] == metric:
                by_date[s["snapshot_date"]] = by_date.get(s["snapshot_date"], 0.0) + s["value"]
        return sorted(by_date.items())

    def _trends(self, snaps: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            name: _trend(self._series_for(snaps, metric))
            for name, metric in _TREND_METRICS.items()
        }

    @staticmethod
    def _latest_by_metric(snaps: list[dict[str, Any]]) -> dict[str, float]:
        latest: dict[str, float] = {}
        for s in snaps:  # snaps are ordered oldest-first, so last wins
            latest[s["metric"]] = s["value"]
        return latest

    # --- Used by the optimiser (historical trends, not just today) --

    def product_trends(self, product_id: str) -> dict[str, Any] | None:
        """Trends for a product, or None if there's no usable history."""
        snaps = self.db.get_metric_series(product_id=product_id)
        if len({s["snapshot_date"] for s in snaps}) < 2:
            return None
        return self._trends(snaps)
