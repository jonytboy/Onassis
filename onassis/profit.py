"""The Profit Engine — ONASSIS as a capital-allocation system.

Mission: maximise long-term sustainable net profit. This module is the
financial memory and scoreboard. It records costs and revenue in the ledger
and produces the **company dashboard**, whose metrics are ordered by priority:

    1. Net Profit   2. ROI         3. Cash Balance   4. AI Cost
    5. Advertising Cost            6. Active Products
    7. Profit Per Product          8. Profit Per Campaign

Vanity metrics (followers, likes, impressions, reach) are deliberately absent
— they matter only insofar as they improve profit.

It is brand- and marketplace-agnostic: ledger entries may carry ``brand`` /
``marketplace`` / ``product_id`` tags, so multiple brands and marketplaces
roll up through the same engine without any change to the decision logic.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)


class ProfitEngine:
    """Records financial entries and computes the profit-first dashboard."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.starting_cash = float((config.policy or {}).get("available_cash", 0) or 0)
        self.daily_ai_budget = float((config.policy or {}).get("daily_ai_budget", 0) or 0)

    # --- Recording --------------------------------------------------

    def record_cost(
        self,
        amount: float,
        *,
        category: str = "other",
        campaign_id: int | None = None,
        product_id: str | None = None,
        brand: str | None = None,
        marketplace: str | None = None,
        note: str = "",
        entry_date: str | None = None,
    ) -> int:
        return self.db.insert_ledger_entry(
            {
                "kind": "cost",
                "category": category,
                "amount": amount,
                "campaign_id": campaign_id,
                "product_id": product_id,
                "brand": brand,
                "marketplace": marketplace,
                "note": note,
                "entry_date": entry_date,
            }
        )

    def record_revenue(
        self,
        amount: float,
        *,
        category: str = "sale",
        campaign_id: int | None = None,
        product_id: str | None = None,
        brand: str | None = None,
        marketplace: str | None = None,
        note: str = "",
        entry_date: str | None = None,
    ) -> int:
        return self.db.insert_ledger_entry(
            {
                "kind": "revenue",
                "category": category,
                "amount": amount,
                "campaign_id": campaign_id,
                "product_id": product_id,
                "brand": brand,
                "marketplace": marketplace,
                "note": note,
                "entry_date": entry_date,
            }
        )

    def record_campaign_ai_cost(
        self, campaign_id: int, amount: float, *, entry_date: str | None = None
    ) -> int:
        """Record the estimated AI cost of producing a campaign."""
        return self.record_cost(
            amount,
            category="ai",
            campaign_id=campaign_id,
            note="campaign generation",
            entry_date=entry_date,
        )

    # --- Budgeting --------------------------------------------------

    def ai_spend_today(self, today: str | None = None) -> float:
        return self.db.ai_cost_on(today or date.today().isoformat())

    def remaining_ai_budget(self, today: str | None = None) -> float:
        """How much of today's AI budget is left (never negative)."""
        return max(0.0, self.daily_ai_budget - self.ai_spend_today(today))

    def cash_balance(self) -> float:
        """Starting cash adjusted by all recorded revenue and cost."""
        return self.starting_cash + self.db.total_revenue() - self.db.total_cost()

    # --- Dashboard --------------------------------------------------

    def dashboard(self) -> dict[str, Any]:
        """The company scoreboard — profit-first, in priority order."""
        revenue = self.db.total_revenue()
        cost = self.db.total_cost()
        net_profit = revenue - cost
        roi = (net_profit / cost) if cost > 0 else 0.0
        per_product = self.db.net_by_product()

        return {
            "net_profit": round(net_profit, 2),
            "roi": round(roi, 4),
            "cash_balance": round(self.cash_balance(), 2),
            "ai_cost": round(self.db.cost_by_category("ai"), 2),
            "advertising_cost": round(self.db.cost_by_category("advertising"), 2),
            "active_products": len(per_product),
            "profit_per_product": per_product,
            "profit_per_campaign": self.db.net_by_campaign(),
            # context, not vanity metrics:
            "total_revenue": round(revenue, 2),
            "total_cost": round(cost, 2),
            "ai_spend_today": round(self.ai_spend_today(), 2),
            "remaining_ai_budget_today": round(self.remaining_ai_budget(), 2),
        }
