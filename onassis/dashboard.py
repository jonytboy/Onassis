"""The CEO Dashboard — money, and nothing else.

Every morning the CEO reads one screen. Not vanity metrics, not output volume —
money. This module aggregates what the rest of the system already recorded into
the numbers a CEO actually cares about:

    Revenue yesterday · Profit yesterday · Visitors · Conversion ·
    Pinterest clicks · Best seller · Worst seller · Products retired ·
    Products launched · Cash generated · AI cost · ROI

It is a **read-only aggregator** — it makes no decisions and writes nothing. It
turns the ledger, the traffic funnel, the portfolio lifecycle and the daily
report into the single scoreboard that answers "did we make money yesterday, and
where did it come from?".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.reporting import DailyReport
from onassis.revenue import RevenueEngine
from onassis.traffic import TrafficEngine

log = get_logger(__name__)


class CEODashboard:
    """Builds the one-screen, money-first morning scoreboard for the CEO."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.revenue = RevenueEngine(config, db)
        self.report = DailyReport(config, db)
        self.traffic = TrafficEngine(config, db)

    def build(self, today: str | None = None) -> dict[str, Any]:
        """The morning scoreboard. ``today`` is the morning; metrics are for
        *yesterday* (the day that just closed)."""
        day = today or date.today().isoformat()
        yesterday = self._yesterday(day)

        rev = self.revenue.revenue_today(yesterday)
        funnel = self.traffic.funnel(yesterday)
        report = self.report.build(yesterday)
        company = self.revenue.company_profit()

        # Conversion: prefer the measured funnel; fall back to orders / visitors.
        visitors = funnel["visits"]
        conversion = (funnel["conversion_rate"] if visitors
                      else (round(rev["orders"] / visitors, 4) if visitors else 0.0))

        metrics = {
            "date": yesterday,
            "revenue_yesterday": rev["gross_revenue"],
            "profit_yesterday": rev["net_profit"],
            "visitors": visitors,
            "conversion": conversion,
            "pinterest_clicks": funnel["clicks"],
            "impressions": funnel["impressions"],
            "best_seller": report["best_seller"],
            "worst_seller": report["worst_seller"],
            "products_retired": self._retired(yesterday),
            "products_launched": self._launched(yesterday),
            "cash_generated": rev["net_profit"],          # cash the day actually made
            "cash_balance": company["cash_balance"],
            "ai_cost": round(self.db.ai_cost_on(yesterday), 2),
            "roi": rev["roi"],
            # Money context beyond the single day.
            "company": {"revenue": company["gross_revenue"],
                        "net_profit": company["net_profit"],
                        "roi": company["roi"], "margin": company["profit_margin"]},
        }
        metrics["headline"] = self._headline(metrics)
        log.info("[CEO] %s", metrics["headline"])
        return metrics

    # --- Helpers ----------------------------------------------------

    @staticmethod
    def _yesterday(day: str) -> str:
        try:
            return (date.fromisoformat(day[:10]) - timedelta(days=1)).isoformat()
        except ValueError:
            return day

    def _retired(self, day: str) -> dict[str, int]:
        on_day = sum(1 for r in self.db.list_portfolio_reviews()
                     if r["decision"] == "RETIRE" and (r["created_at"] or "")[:10] == day)
        total = sum(1 for p in self.db.list_products() if not p.get("active", 1))
        return {"yesterday": on_day, "total": total}

    def _launched(self, day: str) -> dict[str, int]:
        def stamp(p: dict[str, Any]) -> str:
            return (p.get("launched_at") or p.get("created_at") or "")[:10]

        on_day = sum(1 for p in self.db.list_products() if stamp(p) == day)
        total = len(self.db.list_products())
        return {"yesterday": on_day, "total": total}

    @staticmethod
    def _headline(m: dict[str, Any]) -> str:
        best = m["best_seller"]
        made = ("made money" if m["profit_yesterday"] > 0
                else ("lost money" if m["profit_yesterday"] < 0 else "broke even"))
        parts = [f"Yesterday we {made}: revenue {m['revenue_yesterday']:.2f}, "
                 f"profit {m['profit_yesterday']:.2f} (ROI {m['roi']:.0%})."]
        if m["visitors"]:
            parts.append(f"{m['visitors']} visitor(s) at {m['conversion']:.1%} conversion, "
                         f"{m['pinterest_clicks']} Pinterest click(s).")
        if best:
            parts.append(f"Best seller: {best['name']} (net {best['net_profit']:.2f}).")
        r, l = m["products_retired"], m["products_launched"]
        parts.append(f"Launched {l['yesterday']}, retired {r['yesterday']}.")
        return " ".join(parts)
