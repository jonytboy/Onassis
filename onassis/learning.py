"""The Learning Engine — what sold, what didn't, and why.

Every morning this engine asks the only questions that matter: *what sold, what
didn't, and why?* — and then acts on the answers without waiting to be told:

* **Increase winners** — products that sell profitably (or pass their 30-day
  review as KEEP) are flagged to get more of everything: more marketing, more
  traffic, more variants.
* **Retire losers** — money-losers and listings the market has rejected are
  archived by the Portfolio Manager. The Learning Engine records that they died
  and why.
* **Adjust the rest** — listings with interest but no conversion get concrete
  adjustments: reprice, refresh the hero image, refresh the keywords.

It learns from the evidence the system already collects — favourites, CTR,
conversion, sales and the market signals — and it makes **no new decisions of
its own**: it delegates to the modules that own each responsibility
(:class:`RevenueExpansionEngine` for sales learning, :class:`DailyReport` for
the per-product Expand/Hold/Kill economics, :class:`PortfolioManager` for the
lifecycle) and turns their output into one actionable morning digest.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from onassis.config import Config
from onassis.database import Database
from onassis.expansion import RevenueExpansionEngine
from onassis.logger import get_logger
from onassis.portfolio import IMPROVE, KEEP, RETIRE, PortfolioManager
from onassis.reporting import EXPAND, KILL, DailyReport

log = get_logger(__name__)


class LearningEngine:
    """Turns yesterday's evidence into today's actions: increase / retire / adjust."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.expansion = RevenueExpansionEngine(config, db)
        self.report = DailyReport(config, db)
        self.portfolio = PortfolioManager(config, db)

    def run(self, today: str | None = None,
            ctr_lookup: Callable[[str], float] | None = None) -> dict[str, Any]:
        """Learn from the day's evidence and act. Returns the morning digest."""
        day = today or date.today().isoformat()

        # 1. Refresh learned per-product performance from REAL sales.
        learned = self.expansion.learn_from_sales()
        # 2. The daily economics: per-product Expand / Hold / Kill.
        report = self.report.build(day)
        # 3. The 30-day lifecycle: archive losers, flag improvements.
        lifecycle = self.portfolio.review(day, ctr_lookup=ctr_lookup)

        products = report["products"]
        sold = [p for p in products if p["units"] > 0]
        no_sale = [p for p in products if p["units"] == 0 and p["views"] > 0]

        # Winners: increase them (more marketing / traffic / variants).
        winners = [p for p in products if p["recommendation"] == EXPAND]
        winner_keys = {p["product_key"] for p in winners}
        increase = [{"product_key": p["product_key"], "name": p["name"],
                     "net_profit": p["net_profit"], "units": p["units"],
                     "action": "scale", "why": p["reason"]} for p in winners]

        # Losers: retired by the lifecycle, plus anything the report says Kill.
        retired = [{"sku": r["sku"], "product_key": r.get("product_key"),
                    "name": r.get("name"), "why": r["reason"]}
                   for r in lifecycle["retired_list"]]
        kill_flags = [{"product_key": p["product_key"], "name": p["name"],
                       "why": p["reason"]}
                      for p in products if p["recommendation"] == KILL
                      and p["product_key"] not in winner_keys]

        # Adjustments: concrete price/keyword/hero changes for IMPROVE listings.
        adjust = [{"sku": r["sku"], "product_key": r.get("product_key"),
                   "name": r.get("name"), "improvements": r.get("improvements", []),
                   "why": r["reason"]} for r in lifecycle["to_improve"]]

        digest = {
            "date": day,
            "what_sold": [self._line(p) for p in sold],
            "what_didnt": [self._line(p) for p in no_sale],
            "why": self._why(report, lifecycle),
            "actions": {
                "increase": increase,       # winners to scale
                "retire": retired,          # archived losers (already actioned)
                "kill_flags": kill_flags,   # loss-makers the report flags to cut
                "adjust": adjust,           # reprice / refresh hero / refresh keywords
            },
            "learned_product_types": learned["products_learned"],
            "lifecycle": {"kept": lifecycle["kept"], "improve": lifecycle["improve"],
                          "retired": lifecycle["retired"]},
        }
        digest["headline"] = self._headline(digest)
        log.info("[learning] %s", digest["headline"])
        return digest

    # --- Composition helpers ----------------------------------------

    @staticmethod
    def _line(p: dict[str, Any]) -> dict[str, Any]:
        return {"product_key": p["product_key"], "name": p["name"],
                "units": p["units"], "views": p["views"],
                "conversion": p["conversion"], "net_profit": p["net_profit"]}

    @staticmethod
    def _why(report: dict[str, Any], lifecycle: dict[str, Any]) -> list[str]:
        why: list[str] = []
        best, worst = report.get("best_seller"), report.get("worst_seller")
        if best:
            why.append(f"{best['name']} is the money-maker (net {best['net_profit']:.2f}) "
                       f"— do more like it.")
        if worst and worst.get("recommendation") == KILL:
            why.append(f"{worst['name']} lost or never converted — it's being cut.")
        for r in lifecycle["retired_list"]:
            why.append(f"Retired {r.get('name')}: {r['reason']}")
        if not why:
            why.append("Not enough signal yet — keep driving traffic and keep watching.")
        return why

    @staticmethod
    def _headline(digest: dict[str, Any]) -> str:
        a = digest["actions"]
        return (f"Learned from {len(digest['what_sold'])} seller(s): scale "
                f"{len(a['increase'])}, adjust {len(a['adjust'])}, "
                f"retire {len(a['retire'])}.")
