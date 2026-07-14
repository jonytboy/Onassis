"""The Revenue Expansion Engine.

The **design is the master asset**; products are **investments**. For every
approved design this engine scores each catalogue product and lets the CEO
launch only those that clear a configurable threshold (default 80/100) — one
product or all ten, whichever maximises **lifetime profit per design**. It does
not maximise product count; a weak product is simply not launched.

For each product it scores: brand fit, commercial suitability, estimated
conversion, expected profit, production cost, retail price, and historical
performance (from real sales). A deterministic composite (0-100) drives the
launch decision, which the CEO makes as an investment under company policy.

**Learning:** :meth:`learn_from_sales` recomputes per-product-type performance
from actual orders, so products that sell well become more likely to launch and
under-performers become less likely — the system learns from its own history.

This is a deterministic module (like the Optimiser), not an AI agent, and it
reuses the existing CEO, catalogue config, Revenue data, and Product records —
no new agents, no duplicated logic. Phase 1 catalogue = ten Gelato products; a
product flagged unavailable is skipped gracefully.
"""

from __future__ import annotations

from typing import Any

from onassis.ceo import CEOAgent
from onassis.config import Config
from onassis.database import Database
from onassis.fees import FeeModel
from onassis.logger import get_logger
from onassis.proposals import APPROVE, Proposal

log = get_logger(__name__)

_DEFAULT_WEIGHTS = {
    "brand_fit": 0.25,
    "commercial_suitability": 0.20,
    "estimated_conversion": 0.15,
    "expected_profit": 0.25,
    "historical_performance": 0.15,
}
# A product converting at this rate scores 100 on estimated conversion.
_CONVERSION_CEILING = 0.05
# A product hitting this net margin scores 100 on expected profit.
_MARGIN_TARGET = 0.60


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return round(max(lo, min(hi, value)), 1)


class RevenueExpansionEngine:
    """Scores catalogue products for a design and launches the profitable set."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.cfg = config.expansion or {}
        self.threshold = float(self.cfg.get("score_threshold", 80))
        self.min_variants = int(self.cfg.get("min_variants", 3))
        self.max_variants = int(self.cfg.get("max_variants", 5))
        self.cold_start = bool(self.cfg.get("cold_start", True))
        # The REAL Etsy fee model (incl. Offsite Ads) drives profit forecasts.
        self.fee_model = FeeModel.from_config(config)
        self.weights = {**_DEFAULT_WEIGHTS, **(self.cfg.get("weights") or {})}
        # Category-diversity guard (Sprint 42): keep apparel represented even as
        # the learning loop favours what already sells. Off unless enabled.
        self.balance_categories = bool(self.cfg.get("balance_categories", False))
        self.ceo = CEOAgent(config, db)

    # --- Catalogue --------------------------------------------------

    def catalogue(self, include_unavailable: bool = False) -> list[dict[str, Any]]:
        """The Phase-1 product catalogue (available products by default)."""
        items = self.cfg.get("catalogue") or []
        if include_unavailable:
            return list(items)
        return [p for p in items if p.get("available", True)]

    def _unavailable(self) -> list[str]:
        return [p["key"] for p in (self.cfg.get("catalogue") or [])
                if not p.get("available", True)]

    # --- Scoring (deterministic) ------------------------------------

    def expected_profit(self, product: dict[str, Any]) -> float:
        """Per-unit net profit after production AND real Etsy fees (blended
        Offsite Ads included) — the true unit economics, not an optimistic rate."""
        retail = float(product.get("retail_price", 0) or 0)
        production = float(product.get("production_cost", 0) or 0)
        return self.fee_model.unit_net_profit(retail, production)

    def _historical_scorer(self):
        """A function key -> 0-100 reflecting learned sales performance."""
        perf = {p["product_key"]: p for p in self.db.list_product_performance()}
        net = {k: v["net_profit"] for k, v in perf.items() if v.get("orders", 0) > 0}
        if not net:
            return lambda key: 50.0  # no history yet -> neutral
        scale = max((abs(v) for v in net.values()), default=1.0) or 1.0

        def score(key: str) -> float:
            if key not in net:
                return 50.0
            return _clamp(50.0 + 50.0 * (net[key] / scale))

        return score

    def score_design(
        self, campaign: dict[str, Any] | None = None,
        opportunity: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Score every available product for a design, best composite first."""
        products = self.catalogue()
        hist = self._historical_scorer()

        scored = [self._score_product(p, opportunity, hist) for p in products]
        scored.sort(key=lambda s: s["composite_score"], reverse=True)
        return scored

    def _score_product(
        self, product: dict[str, Any], opportunity: dict[str, Any] | None, hist,
    ) -> dict[str, Any]:
        opp = opportunity or {}
        brand_fit = float(product.get("base_brand_fit", 0) or 0)
        if opp.get("brand_fit_score"):
            brand_fit = 0.7 * brand_fit + 0.3 * float(opp["brand_fit_score"])
        commercial = float(product.get("base_commercial", 0) or 0)
        if opp.get("commercial_score"):
            commercial = 0.7 * commercial + 0.3 * float(opp["commercial_score"])

        conversion_score = _clamp(
            float(product.get("base_conversion", 0) or 0) / _CONVERSION_CEILING * 100.0
        )
        profit = self.expected_profit(product)
        retail = float(product.get("retail_price", 0) or 0)
        target = float(self.cfg.get("profit_target_margin", _MARGIN_TARGET)) or _MARGIN_TARGET
        margin = (profit / retail) if retail > 0 else 0.0
        profit_score = _clamp(margin / target * 100.0)
        historical = hist(product["key"])

        parts = {
            "brand_fit": _clamp(brand_fit),
            "commercial_suitability": _clamp(commercial),
            "estimated_conversion": conversion_score,
            "expected_profit": profit_score,
            "historical_performance": historical,
        }
        weight_sum = sum(self.weights.values()) or 1.0
        composite = sum(parts[k] * self.weights.get(k, 0) for k in parts) / weight_sum

        return {
            "product_key": product["key"],
            "product_name": product.get("name", product["key"]),
            "gelato_uid": product.get("gelato_uid"),
            "brand_fit": parts["brand_fit"],
            "commercial_suitability": parts["commercial_suitability"],
            "estimated_conversion": parts["estimated_conversion"],
            "expected_profit": profit,               # per-unit net profit (£)
            "production_cost": float(product.get("production_cost", 0) or 0),
            "retail_price": float(product.get("retail_price", 0) or 0),
            "historical_performance": parts["historical_performance"],
            "composite_score": _clamp(composite),
        }

    # --- Launch plan (CEO decides) ----------------------------------

    def _catalogue_reorder(self, ceo_ok: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Move products in saturated (at-target) categories to the back so gaps
        fill first. Stable within each group; no-op when nothing is saturated."""
        try:
            from onassis.catalogue import CatalogueManager, category_of
            saturated = CatalogueManager(self.config, self.db).saturated_categories()
        except Exception:  # never let catalogue analysis break expansion
            return ceo_ok
        if not saturated:
            return ceo_ok
        fresh = [s for s in ceo_ok
                 if category_of(s["product_key"], s.get("product_name")) not in saturated]
        full = [s for s in ceo_ok
                if category_of(s["product_key"], s.get("product_name")) in saturated]
        return fresh + full

    def plan(
        self, campaign: dict[str, Any] | int,
        opportunity: dict[str, Any] | None = None, *, store: bool = True,
    ) -> dict[str, Any]:
        """Score, let the CEO launch the profitable set, and record it.

        Launch rule (cold-start aware, quality over quantity):
        * The CEO evaluates every product as an investment; only approved
          products can ever launch — never launch a product the CEO rejects.
        * Products with composite >= threshold form the strict set.
        * If that yields at least ``min_variants``, launch them (capped at
          ``max_variants``).
        * Otherwise, with ``cold_start`` on, launch the top CEO-approved
          products to reach ``min_variants`` (still capped at ``max_variants``) —
          so ONASSIS keeps creating products while sales history builds.

        Launched products are registered as Product investments.
        """
        campaign_id = campaign["id"] if isinstance(campaign, dict) else campaign
        opp_id = (opportunity or {}).get("opportunity_id")
        scored = self.score_design(campaign if isinstance(campaign, dict) else None, opportunity)

        # The CEO evaluates each product as an investment (deterministic).
        for s in scored:
            decision = self._ceo_review(s, campaign_id)
            s["ceo_verdict"] = decision["verdict"]
            s["_ceo_reasoning"] = decision["reasoning"]

        ceo_ok = [s for s in scored if s["ceo_verdict"] == APPROVE]  # composite desc
        # Catalogue gap selection (Sprint 44): in Build mode, push products whose
        # category is already at target to the back of the pool so ONASSIS stops
        # over-producing full categories (mugs/posters/totes) and fills the gaps.
        # No-op when no category is saturated (a fresh catalogue), so early-stage
        # selection is unchanged.
        if self.cfg.get("catalogue_gap_selection", True):
            ceo_ok = self._catalogue_reorder(ceo_ok)
        strict = [s for s in ceo_ok if s["composite_score"] >= self.threshold]
        if len(strict) >= self.min_variants:
            chosen, mode = strict[: self.max_variants], "threshold"
        elif self.cold_start and ceo_ok:
            count = min(self.max_variants, max(self.min_variants, len(strict)))
            chosen, mode = ceo_ok[:count], "cold_start"
        else:
            chosen, mode = strict[: self.max_variants], "threshold"
        # Category-diversity guard: rescue under-represented categories (apparel)
        # from the wider CEO-approved pool. No-op when already diverse.
        if self.balance_categories and len(chosen) >= 2:
            from onassis.collections import balance_selection
            chosen = balance_selection(chosen, ceo_ok)
        chosen_keys = {s["product_key"] for s in chosen}

        launched: list[dict[str, Any]] = []
        for s in scored:
            s["launched"] = s["product_key"] in chosen_keys
            s["reasoning"] = self._launch_reason(s, mode)
            s.pop("_ceo_reasoning", None)
            if store:
                self.db.insert_product_score(
                    {**s, "campaign_id": campaign_id, "opportunity_id": opp_id})
                if s["launched"]:
                    self._register_product(campaign_id, opportunity, s)
            if s["launched"]:
                launched.append(s)

        log.info(
            "Expansion plan for campaign #%s: launched %d/%d product(s) (%s, threshold %.0f).",
            campaign_id, len(launched), len(scored), mode, self.threshold,
        )
        from onassis.collections import describe_collection
        return {
            "campaign_id": campaign_id,
            "opportunity_id": opp_id,
            "threshold": self.threshold,
            "selection_mode": mode,
            "products_scored": len(scored),
            "products_launched": len(launched),
            "skipped_unavailable": self._unavailable(),
            "launched": launched,
            "scored": scored,
            # The launched set as a branded collection (Sprint 42, Obj 9).
            "collection": describe_collection(
                launched, opportunity,
                campaign if isinstance(campaign, dict) else None),
        }

    def _launch_reason(self, s: dict[str, Any], mode: str) -> str:
        ceo = s.get("_ceo_reasoning", "")
        if s["launched"]:
            if s["composite_score"] >= self.threshold:
                return (f"Launched: composite {s['composite_score']:.0f} >= "
                        f"{self.threshold:.0f}, CEO-approved investment.")
            return (f"Launched (cold start): CEO-approved investment while sales "
                    f"history builds (composite {s['composite_score']:.0f}).")
        if s["ceo_verdict"] != APPROVE:
            return f"Not launched: CEO rejected the investment. {ceo}"
        return (f"Not launched: composite {s['composite_score']:.0f} outside the top "
                f"{self.max_variants} for this design.")

    def _ceo_review(self, s: dict[str, Any], campaign_id: int | None) -> dict[str, Any]:
        composite = s["composite_score"]
        risk = "low" if composite >= 85 else ("medium" if composite >= 70 else "high")
        estimated_cost = round(
            s["production_cost"] + self.fee_model.total_fees(s["retail_price"]), 2)
        proposal = Proposal(
            agent_name="RevenueExpansionEngine",
            requested_action=f"Launch {s['product_name']} for design (campaign #{campaign_id})",
            estimated_cost=estimated_cost,
            expected_revenue=s["retail_price"],
            confidence=int(round(composite)),
            risk_level=risk,
            reasoning=(
                f"{s['product_name']}: composite {composite:.0f}/100 (brand-fit "
                f"{s['brand_fit']:.0f}, commercial {s['commercial_suitability']:.0f}, "
                f"conversion {s['estimated_conversion']:.0f}, historical "
                f"{s['historical_performance']:.0f}); per-unit net profit "
                f"{s['expected_profit']:.2f}."
            ),
        )
        return self.ceo.evaluate(proposal, store=False)

    def _register_product(
        self, campaign_id: int | None, opportunity: dict[str, Any] | None, s: dict[str, Any]
    ) -> None:
        """Record a launched product as an investment (idempotent by sku)."""
        sku = f"{campaign_id}-{s['product_key']}"
        if self.db.get_product_by_sku(sku):
            return
        self.db.insert_product({
            "sku": sku,
            "name": s["product_name"],
            "campaign_id": campaign_id,
            "brand": (opportunity or {}).get("brand"),
            "marketplace": "gelato",
            "production_cost": s["production_cost"],
            "product_key": s["product_key"],
        })

    # --- Learning ---------------------------------------------------

    def learn_from_sales(self) -> dict[str, Any]:
        """Recompute per-product-type performance from real orders.

        Products that sell profitably raise their type's historical score (and
        so its launch likelihood); under-performers lower it. Idempotent.
        """
        agg: dict[str, dict[str, float]] = {}
        for product in self.db.list_products():
            key = product.get("product_key")
            if not key:
                continue
            bucket = agg.setdefault(key, {"units": 0, "orders": 0, "rev": 0.0, "net": 0.0})
            for order in self.db.get_orders_for_product(str(product["sku"])):
                bucket["units"] += int(order.get("quantity", 1) or 1)
                bucket["orders"] += 1
                bucket["rev"] += float(order.get("gross_revenue", 0) or 0)
                bucket["net"] += float(order.get("net_profit", 0) or 0)

        for key, b in agg.items():
            self.db.upsert_product_performance({
                "product_key": key, "units_sold": int(b["units"]),
                "orders": int(b["orders"]), "gross_revenue": round(b["rev"], 2),
                "net_profit": round(b["net"], 2),
            })
        log.info("Expansion learning updated %d product-type(s) from sales.", len(agg))
        return {"products_learned": len(agg), "performance": self.db.list_product_performance()}

    # --- Reads ------------------------------------------------------

    def performance(self) -> list[dict[str, Any]]:
        return self.db.list_product_performance()

    def plan_for(self, campaign_id: int) -> list[dict[str, Any]]:
        return self.db.list_product_scores(campaign_id)
