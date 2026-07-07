"""Commercial Intelligence — measurement, attribution and the CEO business view.

Sprint 42 Phase 1. Success is no longer "products created" but revenue, profit,
ROI, conversion and marketing performance. This deterministic manager aggregates
the data ONASSIS already records (orders, ledger, product performance, listing
stats, the traffic funnel, marketing assets) into four commercial views:

* **Product analytics** — per product: views, clicks, favourites, conversion,
  sales, refunds, revenue, profit, AI cost, marketing cost, ROI.
* **Channel performance** — per marketing channel: assets, impressions, clicks,
  CTR, visits, attributed sales/revenue.
* **Attribution** — where sales came from (by traffic source).
* **CEO commercial dashboard** — revenue / profit / orders / best-worst product /
  best channel / highest-ROI campaign / CAC / AOV / conversion / spend / net
  margin / recommendations.

No new data collection is invented here — metrics we do not yet capture (e.g.
refunds, follower growth) are reported honestly as 0 rather than guessed.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

MARKETING_CHANNELS = ["pinterest", "instagram", "facebook", "tiktok", "email", "blog"]
# Ledger cost categories that count as marketing spend.
_MARKETING_COST_CATEGORIES = {"advertising", "marketing"}


def _safe_div(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


class CommercialIntelligence:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # --- shared aggregations ----------------------------------------

    def _ledger_by_product(self) -> dict[str, dict[str, float]]:
        """{product_id: {ai, marketing, cost, revenue}} from the ledger."""
        out: dict[str, dict[str, float]] = {}
        for e in self.db.list_ledger():
            pid = e.get("product_id")
            if not pid:
                continue
            b = out.setdefault(pid, {"ai": 0.0, "marketing": 0.0, "cost": 0.0, "revenue": 0.0})
            amt = float(e.get("amount", 0) or 0)
            if e.get("kind") == "cost":
                b["cost"] += amt
                if e.get("category") == "ai":
                    b["ai"] += amt
                if e.get("category") in _MARKETING_COST_CATEGORIES:
                    b["marketing"] += amt
            elif e.get("kind") == "revenue":
                b["revenue"] += amt
        return out

    def _listing_stats_by_listing(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for s in self.db.list_listing_stats():
            lid = str(s.get("listing_id"))
            b = out.setdefault(lid, {"views": 0, "favourites": 0, "orders": 0, "revenue": 0.0})
            b["views"] += int(s.get("views", 0) or 0)
            b["favourites"] += int(s.get("favourites", 0) or 0)
            b["orders"] += int(s.get("orders", 0) or 0)
            b["revenue"] += float(s.get("revenue", 0) or 0)
        return out

    def _funnel_by_product(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for f in self.db.list_traffic_funnel():
            key = f.get("product_key")
            if not key:
                continue
            b = out.setdefault(key, {"impressions": 0, "clicks": 0, "visits": 0, "sales": 0})
            for m in ("impressions", "clicks", "visits", "sales"):
                b[m] += int(f.get(m, 0) or 0)
        return out

    # --- Product analytics (Obj 7) ----------------------------------

    def product_analytics(self, limit: int = 100) -> list[dict[str, Any]]:
        db = self.db
        perf = {p["product_key"]: p for p in db.list_product_performance()}
        ledger = self._ledger_by_product()
        stats = self._listing_stats_by_listing()
        funnel = self._funnel_by_product()
        rows: list[dict[str, Any]] = []
        for product in db.list_products():
            sku = product.get("sku")
            key = product.get("product_key")
            cid = product.get("campaign_id")
            pf = perf.get(key, {})
            led = ledger.get(sku, {})
            fn = funnel.get(key, {})
            # Etsy listing stats via the product's active publication.
            pub = db.get_latest_publication(cid, "etsy", product_id=sku) if cid else None
            lstat = stats.get(str((pub or {}).get("listing_id")), {}) if pub else {}
            units = int(pf.get("units_sold", 0) or 0)
            revenue = float(pf.get("gross_revenue", 0) or 0)
            profit = float(pf.get("net_profit", 0) or 0)
            views = int(lstat.get("views", 0) or 0)
            visits = int(fn.get("visits", 0) or 0)
            ai_cost = round(float(led.get("ai", 0.0)), 2)
            marketing_cost = round(float(led.get("marketing", 0.0)), 2)
            spend = ai_cost + marketing_cost
            rows.append({
                "sku": sku, "name": product.get("name") or key, "type": key,
                "views": views, "clicks": int(fn.get("clicks", 0) or 0),
                "favourites": int(lstat.get("favourites", 0) or 0),
                "conversion": _safe_div(units, visits or views),
                "sales": units, "refunds": 0,
                "revenue": round(revenue, 2), "profit": round(profit, 2),
                "ai_cost": ai_cost, "marketing_cost": marketing_cost,
                "roi": _safe_div(profit, spend) if spend else None,
            })
        rows.sort(key=lambda r: r["profit"], reverse=True)
        return rows[:limit]

    # --- Channel performance (Obj 5) --------------------------------

    def channel_performance(self) -> list[dict[str, Any]]:
        db = self.db
        # Pinterest funnel + pin metrics; other channels: asset counts (+ funnel
        # when a source matches). Revenue is attributed from funnel sales × AOV.
        funnel_by_source: dict[str, dict[str, int]] = {}
        for f in db.list_traffic_funnel():
            src = (f.get("source") or "").lower()
            b = funnel_by_source.setdefault(src, {"impressions": 0, "clicks": 0,
                                                  "visits": 0, "sales": 0})
            for m in ("impressions", "clicks", "visits", "sales"):
                b[m] += int(f.get(m, 0) or 0)
        aov = self._aov()
        rows = []
        for ch in MARKETING_CHANNELS:
            fn = funnel_by_source.get(ch, {})
            posted = db.count_marketing_assets(channel=ch)
            delivered = len([a for a in db.list_marketing_assets(channel=ch)
                             if a.get("status") == "posted"])
            impressions = int(fn.get("impressions", 0) or 0)
            clicks = int(fn.get("clicks", 0) or 0)
            sales = int(fn.get("sales", 0) or 0)
            rows.append({
                "channel": ch, "assets": posted, "delivered": delivered,
                "impressions": impressions, "clicks": clicks,
                "ctr": _safe_div(clicks, impressions),
                "visits": int(fn.get("visits", 0) or 0), "sales": sales,
                "revenue": round(sales * aov, 2) if aov else 0.0,
                "saves": 0, "shares": 0, "followers_gained": 0,
            })
        return rows

    # --- Attribution (Obj 6) ----------------------------------------

    def attribution(self) -> dict[str, Any]:
        db = self.db
        by_source: dict[str, dict[str, int]] = {}
        for f in db.list_traffic_funnel():
            src = (f.get("source") or "unknown").lower()
            b = by_source.setdefault(src, {"visits": 0, "sales": 0})
            b["visits"] += int(f.get("visits", 0) or 0)
            b["sales"] += int(f.get("sales", 0) or 0)
        total_sales = sum(b["sales"] for b in by_source.values())
        aov = self._aov()
        sources = []
        for src, b in sorted(by_source.items(), key=lambda kv: kv[1]["sales"], reverse=True):
            sources.append({
                "source": src, "visits": b["visits"], "sales": b["sales"],
                "revenue": round(b["sales"] * aov, 2) if aov else 0.0,
                "share": _safe_div(b["sales"], total_sales),
            })
        return {"sources": sources, "attributed_sales": total_sales}

    # --- CEO commercial dashboard (Obj 12) --------------------------

    def _aov(self) -> float:
        orders = self.db.list_orders()
        rev = sum(float(o.get("gross_revenue", 0) or 0) for o in orders)
        n = len(orders)
        return round(rev / n, 2) if n else 0.0

    def ceo_commercial(self) -> dict[str, Any]:
        db = self.db
        orders = db.list_orders()
        n_orders = len(orders)
        revenue = round(sum(float(o.get("gross_revenue", 0) or 0) for o in orders), 2)
        profit = round(sum(float(o.get("net_profit", 0) or 0) for o in orders), 2)
        visits = sum(int(f.get("visits", 0) or 0) for f in db.list_traffic_funnel())
        ai_spend = round(db.cost_by_category("ai"), 2)
        marketing_spend = round(sum(db.cost_by_category(c) for c in _MARKETING_COST_CATEGORIES), 2)

        perf = db.list_product_performance()
        best = max(perf, key=lambda p: p.get("net_profit", 0), default=None)
        worst = min(perf, key=lambda p: p.get("net_profit", 0), default=None)

        channels = self.channel_performance()
        best_channel = max((c for c in channels if c["sales"] or c["clicks"]),
                           key=lambda c: (c["sales"], c["clicks"]), default=None)

        campaigns = db.net_by_campaign()
        best_campaign = max(campaigns, key=lambda c: c.get("net_profit", 0), default=None)

        recs = self._recommendations(best, worst, best_channel, profit, revenue)
        return {
            "revenue_today": revenue, "profit_today": profit, "orders": n_orders,
            "best_product": (best or {}).get("product_key"),
            "worst_product": (worst or {}).get("product_key"),
            "best_channel": (best_channel or {}).get("channel"),
            "highest_roi_campaign": (best_campaign or {}).get("campaign_id"),
            "customer_acquisition_cost": _safe_div(marketing_spend, n_orders),
            "average_order_value": _safe_div(revenue, n_orders),
            "conversion_rate": _safe_div(n_orders, visits),
            "ai_spend": ai_spend, "marketing_spend": marketing_spend,
            "net_margin": _safe_div(profit, revenue),
            "recommendations": recs,
        }

    @staticmethod
    def _recommendations(best, worst, best_channel, profit, revenue) -> list[str]:
        recs: list[str] = []
        if best and best.get("net_profit", 0) > 0:
            recs.append(f"Scale '{best['product_key']}' — your most profitable product.")
        if worst and worst.get("net_profit", 0) < 0:
            recs.append(f"Review '{worst['product_key']}' — it is losing money.")
        if best_channel and (best_channel.get("sales") or best_channel.get("clicks")):
            recs.append(f"Lean into {best_channel['channel']} — your best-performing channel.")
        if revenue and profit / revenue < 0.15:
            recs.append("Net margin is thin — raise prices or cut marketing spend.")
        if not recs:
            recs.append("Not enough sales data yet — keep publishing and driving traffic.")
        return recs
