"""The Daily Report — ONASSIS's operating scoreboard.

Once products are live, promoted and generating metrics, this is the view an
operator reads every morning: **Revenue, Profit, Best seller, Worst seller, and
a per-product Recommendation — Expand / Hold / Kill.**

It is a **read-only aggregator** over data the system already owns (orders via
the Revenue Engine, learned per-product performance, and Etsy listing traffic).
It adds no agent and makes no writes — it turns existing data into the daily
decision. The recommendation is deterministic and explainable:

* **Expand** — it sells and it makes money (net profit > 0). Do more of it.
* **Kill**   — it loses money, or it has had real traffic and still hasn't sold.
* **Hold**   — not enough signal yet; keep it live and keep watching.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.fees import FeeModel
from onassis.logger import get_logger
from onassis.revenue import RevenueEngine

log = get_logger(__name__)

EXPAND = "Expand"
HOLD = "Hold"
KILL = "Kill"


class DailyReport:
    """Builds the daily Revenue / Profit / Best / Worst / Recommendation report."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.revenue = RevenueEngine(config, db)
        self.fee_model = FeeModel.from_config(config)
        cfg = config.report or {}
        self.min_traffic = int(cfg.get("min_traffic_to_judge", 60))
        self.kill_margin = float(cfg.get("kill_margin", 0.0))

    def build(self, today: str | None = None) -> dict[str, Any]:
        day = today or date.today().isoformat()
        today_rev = self.revenue.revenue_today(day)
        month_rev = self.revenue.revenue_month(day[:7])
        company = self.revenue.company_profit()

        products = self._products()
        sellers = [p for p in products if p["units"] > 0]
        best = max(sellers, key=lambda p: p["net_profit"], default=None)
        # Worst = the biggest money-loser, else a traffic-but-no-sale product.
        losers = [p for p in products if p["net_profit"] < 0] or \
            [p for p in products if p["recommendation"] == KILL]
        worst = min(losers, key=lambda p: p["net_profit"], default=None)

        report = {
            "date": day,
            "revenue": {
                "today": today_rev["gross_revenue"],
                "month": month_rev["gross_revenue"],
                "company": company["gross_revenue"],
            },
            "profit": {
                "today_net": today_rev["net_profit"],
                "month_net": month_rev["net_profit"],
                "company_net": company["net_profit"],
                "company_margin": company["profit_margin"],
            },
            "orders": {"today": today_rev["orders"], "month": month_rev["orders"],
                       "company": company["orders"]},
            "best_seller": self._brief(best),
            "worst_seller": self._brief(worst),
            "products": products,
            "recommendations": {"expand": [p["product_key"] for p in products
                                           if p["recommendation"] == EXPAND],
                                "hold": [p["product_key"] for p in products
                                         if p["recommendation"] == HOLD],
                                "kill": [p["product_key"] for p in products
                                         if p["recommendation"] == KILL]},
        }
        report["headline"] = self._headline(report)
        return report

    # --- Per-product economics + recommendation ---------------------

    def _products(self) -> list[dict[str, Any]]:
        perf = {p["product_key"]: p for p in self.db.list_product_performance()}
        names, skus = self._product_index()
        traffic = self._traffic_by_key(skus)

        keys = set(perf) | set(names) | set(traffic)
        rows: list[dict[str, Any]] = []
        for key in sorted(keys):
            p = perf.get(key, {})
            units = int(p.get("units_sold", 0) or 0)
            revenue = float(p.get("gross_revenue", 0) or 0)
            net = float(p.get("net_profit", 0) or 0)
            views = int(traffic.get(key, {}).get("views", 0))
            favourites = int(traffic.get(key, {}).get("favourites", 0))
            margin = round(net / revenue, 4) if revenue > 0 else 0.0
            conversion = round(units / views, 4) if views > 0 else 0.0
            rec, reason = self._recommend(units, net, views, margin)
            rows.append({
                "product_key": key, "name": names.get(key, key),
                "units": units, "revenue": round(revenue, 2),
                "net_profit": round(net, 2), "margin": margin,
                "views": views, "favourites": favourites, "conversion": conversion,
                "recommendation": rec, "reason": reason,
            })
        # Most profitable first — the operator's natural reading order.
        rows.sort(key=lambda r: (r["net_profit"], r["units"]), reverse=True)
        return rows

    def _recommend(self, units: int, net: float, views: int,
                   margin: float) -> tuple[str, str]:
        if net < 0:
            return KILL, (f"Losing money (net {net:.2f}); cut it or fix the price/cost.")
        if units > 0 and net > 0:
            return EXPAND, (f"Sells profitably ({units} sold, net {net:.2f}); "
                            f"do more of this.")
        if views >= self.min_traffic and units == 0:
            return KILL, (f"{views} views and no sales — the market has seen it and "
                          f"said no.")
        return HOLD, ("Not enough signal yet; keep it live and keep watching."
                      if views < self.min_traffic else "Converting slowly; hold.")

    # --- Helpers ----------------------------------------------------

    def _product_index(self) -> tuple[dict[str, str], dict[str, list[str]]]:
        names: dict[str, str] = {}
        skus: dict[str, list[str]] = {}
        for product in self.db.list_products():
            key = product.get("product_key")
            if not key:
                continue
            names.setdefault(key, product.get("name") or key)
            skus.setdefault(key, []).append(str(product.get("sku")))
        return names, skus

    def _traffic_by_key(self, skus: dict[str, list[str]]) -> dict[str, dict[str, int]]:
        listings = {str(l.get("product_id")): l for l in self.db.list_etsy_listings()}
        out: dict[str, dict[str, int]] = {}
        for key, key_skus in skus.items():
            views = favourites = 0
            for sku in key_skus:
                lst = listings.get(sku)
                if lst:
                    views += int(lst.get("views", 0) or 0)
                    favourites += int(lst.get("favourites", 0) or 0)
            out[key] = {"views": views, "favourites": favourites}
        return out

    @staticmethod
    def _brief(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {k: row[k] for k in ("product_key", "name", "units", "net_profit",
                                    "views", "recommendation")}

    def _headline(self, report: dict[str, Any]) -> str:
        best, worst = report["best_seller"], report["worst_seller"]
        parts = [f"Net profit today {report['profit']['today_net']:.2f} "
                 f"({report['orders']['today']} order(s))."]
        if best:
            parts.append(f"Best: {best['name']} (net {best['net_profit']:.2f}).")
        if worst and worst["recommendation"] == KILL:
            parts.append(f"Worst: {worst['name']} — {worst['recommendation']}.")
        if not best and not report["orders"]["company"]:
            parts.append("No sales yet — keep driving traffic to the live listings.")
        return " ".join(parts)
