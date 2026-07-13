"""The CFO — deterministic AI-spend and Return-on-AI-Investment manager
(Sprint 42.2).

Consistent with the deterministic CEO/CMO managers (no new LLM agent), the CFO
reads the per-request ``ai_requests`` accounting and the commercial ledger to
answer four questions an operator actually asks:

* **Where is AI money going?** (cost dashboard: today, per-product, per-sale,
  per-published, breakdown by Research/Artwork/Marketing/Compliance/Mockups, and
  a 30-day trend)
* **Is it efficient?** (average cost/product vs. business targets)
* **Is it paying off?** (Return on AI Investment per product)
* **How do we spend less for the same output?** (optimisation recommendations)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from onassis.ai_accounting import COST_TARGETS, rate_cost

# Which spend bucket a recorded AI request belongs to (dashboard breakdown).
_RESEARCH_STAGES = {"Market Research", "Create Product Opportunity",
                    "Run Product Optimiser", "Learn & Review Portfolio"}
_MARKETING_STAGES = {"Generate Marketing Content", "Promote on Pinterest"}
_COMPLIANCE_STAGES = {"Build Design Package", "CEO Decision", "CEO Dashboard"}


def _category(stage: str | None, kind: str) -> str:
    if kind == "image":
        return "artwork" if stage == "Generate Master Artwork" else "mockups"
    if stage in _RESEARCH_STAGES:
        return "research"
    if stage in _MARKETING_STAGES:
        return "marketing"
    if stage in _COMPLIANCE_STAGES:
        return "compliance"
    return "other"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class CFOManager:
    """Reads AI accounting + ledger; produces the AI Cost Dashboard and advice."""

    CATEGORIES = ("research", "artwork", "mockups", "marketing", "compliance", "other")

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    # --- Dashboard (Obj 2 + 10) -------------------------------------

    def dashboard(self, date: str | None = None) -> dict[str, Any]:
        date = date or _today()
        db = self.db
        spend_today = round(db.ai_spend_on(date), 4)
        per_product = db.ai_cost_per_product(date)
        products_costed = len(per_product)
        cost_per_product = (round(spend_today / products_costed, 4)
                            if products_costed else None)

        # Cost per PUBLISHED product (real drafts/live created).
        published = self._published_count()
        cost_per_published = (round(spend_today / published, 4) if published else None)
        # Cost per SALE (units sold today, best-effort via performance rollup).
        sales = self._units_sold()
        cost_per_sale = (round(db.ai_spend_total() / sales, 4) if sales else None)

        breakdown = self._breakdown(date)
        return {
            "date": date,
            "ai_spend_today": spend_today,
            "products_costed": products_costed,
            "cost_per_product": cost_per_product,
            "cost_per_product_rating": rate_cost(cost_per_product),
            "cost_per_published": cost_per_published,
            "cost_per_sale": cost_per_sale,
            "breakdown": breakdown,
            "trend": db.ai_spend_trend(30),
            "targets": [{"label": k, "under": v} for k, v in COST_TARGETS],
            "top_products": per_product[:10],
        }

    def _breakdown(self, date: str) -> dict[str, float]:
        out = {c: 0.0 for c in self.CATEGORIES}
        with self.db._connect() as conn:  # single grouped read
            rows = conn.execute(
                "SELECT stage, kind, COALESCE(SUM(cost_usd),0) AS cost FROM ai_requests "
                "WHERE request_date = ? GROUP BY stage, kind", (date,)).fetchall()
        for r in rows:
            out[_category(r["stage"], r["kind"])] += float(r["cost"])
        return {k: round(v, 4) for k, v in out.items()}

    # --- ROAI (Obj 11) ----------------------------------------------

    def roai(self) -> list[dict[str, Any]]:
        """Per-product Return on AI Investment: profit earned per AI dollar."""
        db = self.db
        perf = {p["product_key"]: p for p in db.list_product_performance()}
        ai_by_product: dict[str, float] = {
            row["product_id"]: float(row["cost"]) for row in db.ai_cost_per_product()}
        out: list[dict[str, Any]] = []
        for product in db.list_products():
            sku = product.get("sku")
            key = product.get("product_key")
            pf = perf.get(key, {})
            ai_cost = round(ai_by_product.get(sku, 0.0), 4)
            revenue = float(pf.get("gross_revenue", 0) or 0)
            net = float(pf.get("net_profit", 0) or 0)
            mkt = float(pf.get("marketing_cost", 0) or 0)
            ratio = round(net / ai_cost, 1) if ai_cost > 0 else None
            out.append({
                "sku": sku, "name": product.get("name") or key,
                "revenue": round(revenue, 2), "ai_cost": ai_cost,
                "marketing_cost": round(mkt, 2), "net_profit": round(net, 2),
                "roai": ratio, "roai_rating": self._rate_roai(ratio, ai_cost)})
        out.sort(key=lambda r: (r["roai"] is None, -(r["roai"] or 0)))
        return out

    @staticmethod
    def _rate_roai(ratio: float | None, ai_cost: float) -> str:
        if ai_cost <= 0:
            return "no_ai_cost"
        if ratio is None:
            return "unknown"
        if ratio <= 0:
            return "loss"
        if ratio >= 20:
            return "excellent"
        if ratio >= 10:
            return "good"
        if ratio >= 5:
            return "acceptable"
        return "marginal"

    # --- Optimisation recommendations (Obj 3 + 12) ------------------

    def optimisation_report(self, date: str | None = None) -> dict[str, Any]:
        date = date or _today()
        dash = self.dashboard(date)
        breakdown = dash["breakdown"]
        total = sum(breakdown.values()) or 0.0
        recs: list[dict[str, Any]] = []

        largest = max(breakdown.items(), key=lambda kv: kv[1], default=("none", 0.0))
        largest_share = round(largest[1] / total * 100) if total else 0
        if largest[1] > 0:
            recs.append({
                "area": largest[0], "share_pct": largest_share,
                "message": f"{largest[0].title()} is {largest_share}% of today's AI spend."})

        # Image spend is the usual dominant lever — advise fewer variations.
        image_spend = breakdown.get("artwork", 0) + breakdown.get("mockups", 0)
        if total and image_spend / total > 0.5:
            recs.append({
                "area": "artwork", "share_pct": round(image_spend / total * 100),
                "message": "Image generation dominates AI spend — reduce mockup "
                           "variations / gallery count and reuse the master artwork.",
                "potential_saving_pct": 28})

        # Cost-per-product vs target.
        cpp = dash["cost_per_product"]
        if cpp is not None and cpp >= 2.0:
            recs.append({
                "area": "efficiency", "message":
                f"Average AI cost/product £${cpp:.2f} is above the £$2.00 investigate "
                "threshold — enable copy reuse and cascade marketing generation.",
                "potential_saving_pct": 41})

        est_saving = round(dash["ai_spend_today"] *
                           max((r.get("potential_saving_pct", 0) for r in recs), default=0)
                           / 100.0, 2)
        return {
            "date": date,
            "ai_spend_today": dash["ai_spend_today"],
            "average_product": dash["cost_per_product"],
            "average_rating": dash["cost_per_product_rating"],
            "largest_cost": largest[0],
            "recommendations": recs,
            "estimated_saving_usd": est_saving,
        }

    # --- helpers ----------------------------------------------------

    def _published_count(self) -> int:
        try:
            pubs = self.db.list_publications()
        except Exception:
            return 0
        seen = {(p.get("campaign_id"), p.get("product_id")) for p in pubs
                if (p.get("status") or "") in ("draft", "published", "live")}
        return len(seen)

    def _units_sold(self) -> int:
        try:
            return sum(int(p.get("units_sold", 0) or 0)
                       for p in self.db.list_product_performance())
        except Exception:
            return 0
