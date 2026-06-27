"""The Autonomous Product Optimiser.

Increase profit from **existing** products before creating new ones. For every
live product the optimiser computes a full performance picture and recommends
exactly **one** highest-value action. It is a deterministic analytics module
(not an AI agent), so recommendations are explainable and fully testable.

For each product it calculates: Views, Visits, Favourites, Conversion Rate,
Revenue, Net Profit, ROI, Profit Trend, Traffic Trend, and a Confidence.

It recommends one action from a fixed set, each with an estimated cost,
expected increase in profit, confidence, and reasoning. The recommendation is
then evaluated by the **CEO** using existing company policy. Nothing is
executed automatically.

Marketplace-agnostic: traffic/favourites come from `listing_stats` and
revenue/profit from `orders`, both keyed by product — so additional
marketplaces work with no change to the decision logic.
"""

from __future__ import annotations

from typing import Any

from onassis.analytics import AnalyticsEngine
from onassis.ceo import CEOAgent
from onassis.config import Config
from onassis.database import Database
from onassis.experiments import ExperimentEngine
from onassis.logger import get_logger
from onassis.proposals import Proposal

# Maps each optimiser action to the experiment variable it would change.
_ACTION_VARIABLE = {
    "rewrite_title": "title",
    "rewrite_description": "description",
    "improve_seo": "keywords",
    "fresh_images": "images",
    "lifestyle_mockups": "mockup",
    "design_variation": "design",
    "pinterest_campaign": "pinterest_campaign",
}

log = get_logger(__name__)

# The fixed action vocabulary (key -> human label).
ACTIONS = {
    "leave_unchanged": "Leave unchanged",
    "pinterest_campaign": "Create new Pinterest campaign",
    "fresh_images": "Create fresh product images",
    "lifestyle_mockups": "Generate new lifestyle mockups",
    "rewrite_title": "Rewrite Etsy title",
    "rewrite_description": "Rewrite Etsy description",
    "improve_seo": "Improve SEO keywords",
    "design_variation": "Create one design variation",
    "archive": "Archive product",
}

_DEFAULT_COSTS = {
    "leave_unchanged": 0.0, "pinterest_campaign": 5.0, "fresh_images": 8.0,
    "lifestyle_mockups": 6.0, "rewrite_title": 1.0, "rewrite_description": 1.0,
    "improve_seo": 1.0, "design_variation": 10.0, "archive": 0.0,
}
_DEFAULT_UPLIFT = {
    "pinterest_campaign": 0.25, "fresh_images": 0.15, "lifestyle_mockups": 0.12,
    "rewrite_title": 0.08, "rewrite_description": 0.08, "improve_seo": 0.10,
    "design_variation": 0.20,
}


def _trend_label(value: float) -> str:
    if value > 0.01:
        return "up"
    if value < -0.01:
        return "down"
    return "flat"


class ProductOptimiser:
    """Analyses live products and recommends the single highest-value action."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        cfg = config.optimiser or {}
        self.min_views = float(cfg.get("min_views", 100))
        self.good_conversion = float(cfg.get("good_conversion", 0.02))
        self.profit_floor = float(cfg.get("potential_profit_floor", 5.0))
        self.costs = {**_DEFAULT_COSTS, **(cfg.get("action_costs") or {})}
        self.uplift = {**_DEFAULT_UPLIFT, **(cfg.get("action_uplift") or {})}
        self.ceo = CEOAgent(config, db)
        self.analytics = AnalyticsEngine(config, db)
        self.experiments = ExperimentEngine(config, db)

    # --- Public API -------------------------------------------------

    def analyse_all(self) -> list[dict[str, Any]]:
        """Analyse every live (active) product, ranked by value (best first)."""
        products = [p for p in self.db.list_products() if p.get("active", 1)]
        recs = [self.analyse_product(p) for p in products]
        # Prefer improving existing profitable products: rank by a value score
        # that rewards expected ROI and confidence, and favours profitable ones.
        recs.sort(key=self._value_score, reverse=True)
        return recs

    def top_recommendation(self) -> dict[str, Any] | None:
        """The single highest-value recommendation, evaluated by the CEO."""
        recs = self.analyse_all()
        if not recs:
            return None
        rec = recs[0]
        rec["ceo"] = self._ceo_review(rec)
        return rec

    def analyse_product(self, product: dict[str, Any]) -> dict[str, Any]:
        """Compute a product's metrics and its single best action."""
        sku = str(product.get("sku") or product.get("id") or "")
        metrics = self._metrics(product)
        action = self._decide(metrics)

        # Use completed/active experiments: never re-run a variable already
        # under test, and adjust confidence from what past experiments learned.
        variable = _ACTION_VARIABLE.get(action)
        note = ""
        if variable and self.experiments.has_active(sku, variable):
            note = f"An experiment on '{variable}' is already running; awaiting its result."
            action, variable = "leave_unchanged", None

        cost = float(self.costs.get(action, 0.0))
        expected_increase = self._expected_increase(action, metrics)
        expected_roi = round(expected_increase / cost, 4) if cost > 0 else (
            round(expected_increase, 4) if expected_increase > 0 else 0.0
        )
        confidence = self._confidence(metrics)
        if variable:
            last = self.experiments.last_result(sku, variable)
            if last == "loss":
                confidence = max(0, confidence - 20)
                note += " A previous experiment on this variable lost — lower confidence."
            elif last == "win":
                confidence = min(100, confidence + 10)
                note += " A previous experiment on this variable won — higher confidence."

        reasoning = self._reasoning(product, metrics, action, expected_increase)
        if note:
            reasoning = f"{reasoning} {note.strip()}"

        return {
            "product": sku,
            "product_name": product.get("name", ""),
            "marketplace": product.get("marketplace"),
            "metrics": metrics,
            "recommendation": ACTIONS[action],
            "action_key": action,
            "estimated_cost": round(cost, 2),
            "expected_increase_in_profit": expected_increase,
            "expected_roi": expected_roi,
            "confidence": confidence,
            "reasoning": reasoning,
        }

    # --- Metrics ----------------------------------------------------

    def _metrics(self, product: dict[str, Any]) -> dict[str, Any]:
        sku = str(product.get("sku") or "")
        orders = self.db.get_orders_for_product(sku)
        revenue = round(sum(o["gross_revenue"] for o in orders), 2)
        net_profit = round(sum(o["net_profit"] for o in orders), 2)
        total_cost = round(sum(o["total_cost"] for o in orders), 2)
        roi = round(net_profit / total_cost, 4) if total_cost > 0 else 0.0

        stats = self.db.get_listing_stats_for(int(sku)) if sku.isdigit() else []
        listing = self.db.get_etsy_listing(int(sku)) if sku.isdigit() else None
        latest = stats[-1] if stats else None

        views = int(latest["views"]) if latest else (int(listing["views"]) if listing else 0)
        visits = int(latest["visits"]) if latest else views
        favourites = (
            int(latest["favourites"]) if latest
            else (int(listing["num_favorers"]) if listing else 0)
        )
        conversion = (
            float(latest["conversion_rate"]) if latest
            else (round(len(orders) / views, 4) if views > 0 else 0.0)
        )

        # Default trends from this product's own orders/stats...
        profit_trend = self._profit_trend(orders)
        traffic_trend = self._traffic_trend(stats)
        conversion_trend = {"value": 0.0, "label": "flat"}
        revenue_trend = {"value": 0.0, "label": "flat"}
        # ...but prefer historical analytics trends when enough history exists,
        # so decisions use trends rather than today's values alone.
        hist = self.analytics.product_trends(sku)
        if hist:
            profit_trend = hist["profit_trend"]
            traffic_trend = hist["traffic_trend"]
            conversion_trend = hist["conversion_trend"]
            revenue_trend = hist["revenue_trend"]

        return {
            "views": views,
            "visits": visits,
            "favourites": favourites,
            "conversion_rate": conversion,
            "revenue": revenue,
            "net_profit": net_profit,
            "roi": roi,
            "orders": len(orders),
            "profit_trend": profit_trend,
            "traffic_trend": traffic_trend,
            "conversion_trend": conversion_trend,
            "revenue_trend": revenue_trend,
            "_stat_snapshots": len(stats),
        }

    def _profit_trend(self, orders: list[dict[str, Any]]) -> dict[str, Any]:
        if len(orders) < 2:
            return {"value": 0.0, "label": "flat"}
        mid = len(orders) // 2
        older = sum(o["net_profit"] for o in orders[:mid])
        recent = sum(o["net_profit"] for o in orders[mid:])
        value = round(recent - older, 2)
        return {"value": value, "label": _trend_label(value)}

    def _traffic_trend(self, stats: list[dict[str, Any]]) -> dict[str, Any]:
        if len(stats) < 2:
            return {"value": 0.0, "label": "flat"}
        prev, cur = stats[-2]["views"], stats[-1]["views"]
        value = round((cur - prev) / prev, 4) if prev > 0 else 0.0
        return {"value": value, "label": _trend_label(value)}

    # --- Decision (one action) --------------------------------------

    def _decide(self, m: dict[str, Any]) -> str:
        net = m["net_profit"]
        views = m["views"]
        conv = m["conversion_rate"]
        fav_rate = (m["favourites"] / views) if views > 0 else 0.0

        # No signal yet → drive initial traffic to learn.
        if m["orders"] == 0 and views == 0:
            return "pinterest_campaign"

        # Losing money.
        if net < 0:
            if views < self.min_views:
                return "archive"            # no traffic and unprofitable → cut it
            return "rewrite_description"    # has traffic but unprofitable → fix listing cheaply

        # Profitable but starved of traffic → scale a proven product.
        if views < self.min_views:
            return "pinterest_campaign"

        # Has traffic but converts poorly.
        if conv < self.good_conversion:
            if fav_rate >= self.good_conversion * 2:
                return "improve_seo"        # people love it but don't buy → copy/SEO
            return "fresh_images"           # weak appeal → better visuals

        # Healthy traffic and conversion.
        if m["profit_trend"]["label"] == "down":
            return "design_variation"       # refresh a fading winner
        return "leave_unchanged"            # don't spend on a healthy product

    def _expected_increase(self, action: str, m: dict[str, Any]) -> float:
        if action == "leave_unchanged":
            return 0.0
        if action == "archive":
            # The gain from archiving is the loss it stops.
            return round(-m["net_profit"], 2) if m["net_profit"] < 0 else 0.0
        base = max(m["net_profit"], self.profit_floor)
        return round(self.uplift.get(action, 0.0) * base, 2)

    def _confidence(self, m: dict[str, Any]) -> int:
        c = 35
        if m["orders"] >= 1:
            c += 15
        if m["orders"] >= 3:
            c += 15
        if m["views"] >= self.min_views:
            c += 15
        if m["_stat_snapshots"] >= 2:
            c += 10
        return max(0, min(100, c))

    def _value_score(self, rec: dict[str, Any]) -> tuple[int, float]:
        """Rank key (tier, value). Improving an existing *profitable* product
        always outranks other actions, satisfying the 'improve earners first'
        rule; within a tier, rank by expected ROI × confidence."""
        action = rec["action_key"]
        profitable = rec["metrics"]["net_profit"] > 0
        if profitable and action not in ("leave_unchanged", "archive"):
            tier = 2  # improving a profitable product — top priority
        elif action == "leave_unchanged":
            tier = 0  # doing nothing ranks last
        else:
            tier = 1  # fixing/retiring underperformers
        value = rec["expected_roi"] * (rec["confidence"] / 100.0)
        return (tier, value)

    def _reasoning(
        self, product: dict[str, Any], m: dict[str, Any], action: str, expected: float
    ) -> str:
        name = product.get("name") or product.get("sku")
        head = (
            f"{name}: {m['views']} views, {m['favourites']} favourites, "
            f"{m['conversion_rate']:.1%} conversion, net profit {m['net_profit']:.2f} "
            f"(ROI {m['roi']:.2f}); profit trend {m['profit_trend']['label']}, "
            f"traffic trend {m['traffic_trend']['label']}."
        )
        tail = {
            "leave_unchanged": "It is healthy and converting well — spending here "
            "would not beat leaving capital deployed elsewhere.",
            "pinterest_campaign": "It is under-exposed; driving qualified traffic to a "
            "proven listing is the cheapest route to more profit.",
            "fresh_images": "Traffic is fine but conversion is weak and interest is low — "
            "stronger imagery should lift purchases.",
            "lifestyle_mockups": "Lifestyle context should help shoppers picture the product "
            "and convert browsing into sales.",
            "rewrite_title": "A sharper, search-aligned title should lift both traffic and "
            "click-through.",
            "rewrite_description": "The listing draws traffic but isn't converting; clearer "
            "copy is a low-cost fix to try first.",
            "improve_seo": "Strong favouriting but low conversion suggests discovery/intent "
            "mismatch — better keywords should convert existing interest.",
            "design_variation": "A proven winner whose profit is fading; one fresh variation "
            "can renew demand without a new product.",
            "archive": "Unprofitable with little traffic and no positive trend — retire it and "
            "redeploy effort to earners.",
        }[action]
        return f"{head} Recommended: {ACTIONS[action]} (≈+{expected:.2f} profit). {tail}"

    # --- CEO review -------------------------------------------------

    def _ceo_review(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Have the CEO evaluate the recommendation under company policy."""
        # Risk reflects the historical profit trend: a declining product is a
        # riskier place to invest, so the CEO discounts its ROI accordingly.
        trend = rec["metrics"]["profit_trend"]["label"]
        risk_level = {"down": "high", "up": "low"}.get(trend, "medium")
        proposal = Proposal(
            agent_name="ProductOptimiser",
            requested_action=f"{rec['recommendation']} for {rec['product']}",
            estimated_cost=rec["estimated_cost"],
            expected_revenue=rec["expected_increase_in_profit"],
            confidence=rec["confidence"],
            risk_level=risk_level,
            reasoning=rec["reasoning"],
        )
        return self.ceo.evaluate(proposal, store=False)
