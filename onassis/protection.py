"""The Financial Protection Engine — a business safeguard, not a barrier.

ONASSIS exists to grow profitable sales while protecting the business from
unnecessary financial risk. The Pricing Engine *recommends*; this engine *decides*
whether the recommendation is commercially safe. Every commercial action —
launch, price change, discount, advertising, portfolio optimisation — passes
through it before execution. It has final authority: if the rules fail, the action
does not happen, the reason is logged, and the CEO is notified.

The rules are **universal** (never product-specific) and deterministic. Each
decision evaluates, on real economics (real Etsy fees included):

* **Gross margin** — (price − production) / price.
* **Contribution margin** — (price − all variable costs) / price.
* **Risk reserve** — a configurable buffer deducted BEFORE profit is protected,
  to absorb returns, refunds, chargebacks, banking/payment costs, fee changes,
  currency swings, AI and shipping variation, and other unforeseen costs.
* **Protected profit** — contribution − risk reserve. We never knowingly sell
  below this.
* **Commercial confidence** — how comfortably the protected economics clear the
  thresholds.

**Unknown costs never auto-block.** A missing cost is replaced by a conservative
default and the risk reserve is *raised* to reflect the uncertainty; the action is
rejected only if protected profit still falls below the thresholds. And the safety
buffer is **dynamic**: a brand-new product starts with a larger reserve and earns
a slimmer one as real sales and cost data build confidence — commercially
efficient over time, financially safe throughout.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.fees import FeeModel
from onassis.logger import get_logger

log = get_logger(__name__)

APPROVE = "APPROVE"
REJECT = "REJECT"

# Actions whose whole point is to CHANGE an existing state — these are gated on
# commercial confidence (only act when confident). A launch is not: a healthy new
# product should trade, just with a larger safety buffer.
_CHANGE_ACTIONS = {"price_change", "discount", "advertising", "portfolio_optimisation"}
_ACTIONS = {"launch", *_CHANGE_ACTIONS}


class FinancialProtectionEngine:
    """Deterministic final-authority gate above the Pricing Engine."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.fees = FeeModel.from_config(config)
        p = config.protection or {}
        # Core controls (fractions).
        self.min_gross = float(p.get("min_gross_margin_percent", 25)) / 100.0
        self.min_contribution = float(p.get("min_contribution_margin_percent", 18)) / 100.0
        self.default_reserve = float(p.get("default_risk_reserve_percent", 8)) / 100.0
        self.max_reserve = float(p.get("max_risk_reserve_percent", 15)) / 100.0
        self.max_price_change = float(p.get("max_single_price_change_percent", 15)) / 100.0
        self.min_confidence = float(p.get("min_confidence_score", 0.85))
        # Business logic (config/code, never env).
        self.comfort_margin = float(p.get("comfort_protected_margin_percent", 15)) / 100.0
        listing = config.listing or {}
        self.default_production = float(p.get("default_production_cost",
                                             listing.get("default_production_cost", 12.0)))
        self.default_shipping = float(p.get("default_shipping_cost", 4.0))
        self.default_ai = float(p.get("default_ai_cost_per_unit", 0.05))
        self.unknown_premium = float(p.get("unknown_cost_reserve_premium_percent", 3)) / 100.0
        self.new_product_confidence = float(p.get("new_product_confidence", 0.5))
        self.confidence_sales_target = max(1, int(p.get("confidence_sales_target", 20)))

    # --- The gate ---------------------------------------------------

    def evaluate(self, action: str, price: float, *, production_cost: float | None = None,
                 shipping_cost: float | None = None, ai_cost: float | None = None,
                 ad_cost: float = 0.0, reference_price: float | None = None,
                 product_key: str | None = None, listing_id: str | None = None,
                 notify: bool = True, store: bool = True) -> dict[str, Any]:
        """Decide whether a commercial action is safe. Returns the full decision."""
        if action not in _ACTIONS:
            raise ValueError(f"Unknown protection action '{action}'")
        price = round(float(price), 2)

        # 1. Resolve costs; unknowns become conservative estimates (never a block).
        production, prod_est = self._known(production_cost, self.default_production)
        shipping, ship_est = self._known(shipping_cost, self.default_shipping)
        ai, ai_est = self._known(ai_cost, self.default_ai)
        fees = self.fees.total_fees(price) if price > 0 else 0.0
        ad = max(0.0, float(ad_cost or 0.0))
        estimated_costs = round((production if prod_est else 0)
                                + (shipping if ship_est else 0)
                                + (ai if ai_est else 0), 2)
        unknown_count = sum((prod_est, ship_est, ai_est))

        # 2. Dynamic safety buffer: bigger for new/low-data products and for each
        #    unknown cost; smaller as data confidence grows — within [default,max].
        data_conf = self._data_confidence(product_key)
        reserve_pct = self._reserve_pct(data_conf, unknown_count)

        # 3. Economics (real fees included).
        variable_costs = round(production + shipping + fees + ai + ad, 2)
        contribution = round(price - variable_costs, 2)
        gross_profit = round(price - production, 2)
        gross_margin = round(gross_profit / price, 4) if price > 0 else 0.0
        contribution_margin = round(contribution / price, 4) if price > 0 else 0.0
        reserve_amount = round(price * reserve_pct, 2)
        protected_profit = round(contribution - reserve_amount, 2)
        protected_margin = round(protected_profit / price, 4) if price > 0 else 0.0

        # 4. Commercial confidence = how comfortably protected profit clears the bar.
        confidence = self._commercial_confidence(protected_margin)

        # 5. Universal rules.
        reasons: list[str] = []
        if price <= 0:
            reasons.append("price must be positive")
        if gross_margin < self.min_gross:
            reasons.append(f"gross margin {gross_margin:.1%} < min {self.min_gross:.0%}")
        if contribution_margin < self.min_contribution:
            reasons.append(f"contribution margin {contribution_margin:.1%} "
                           f"< min {self.min_contribution:.0%}")
        if protected_profit < 0:
            reasons.append(f"protected profit {protected_profit:.2f} below zero "
                           f"(after {reserve_pct:.0%} risk reserve)")
        if action in _CHANGE_ACTIONS and confidence < self.min_confidence:
            reasons.append(f"commercial confidence {confidence:.2f} "
                           f"< min {self.min_confidence:.2f}")
        if reference_price and action in ("price_change", "discount"):
            change = abs(price - float(reference_price)) / float(reference_price)
            if change > self.max_price_change + 1e-9:
                reasons.append(f"price change {change:.1%} exceeds max single change "
                               f"{self.max_price_change:.0%}")

        decision = APPROVE if not reasons else REJECT
        reason = "; ".join(reasons) if reasons else "clears all protection thresholds"

        record = {
            "action": action, "product_key": product_key, "listing_id": listing_id,
            "expected_revenue": price, "expected_costs": variable_costs,
            "estimated_costs": estimated_costs, "risk_reserve_percent": round(reserve_pct, 4),
            "risk_reserve_amount": reserve_amount, "protected_profit": protected_profit,
            "gross_margin": gross_margin, "contribution_margin": contribution_margin,
            "confidence": confidence, "decision": decision, "reason": reason,
            "approved": decision == APPROVE, "protected_margin": protected_margin,
            "unknown_costs": unknown_count, "data_confidence": round(data_conf, 3),
            "min_safe_price": self._min_safe_price(production, shipping, ai, ad, reserve_pct),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if store:
            self.db.insert_protection_decision(record)
        if decision == REJECT and notify:
            self._notify_ceo(record)
        else:
            log.info("Protection %s %s (%s): protected profit %.2f, confidence %.2f.",
                     decision, action, product_key or "-", protected_profit, confidence)
        return record

    # --- Action guards (thin, self-documenting wrappers) ------------

    def guard_launch(self, price, *, production_cost=None, shipping_cost=None,
                     product_key=None, listing_id=None, **kw):
        return self.evaluate("launch", price, production_cost=production_cost,
                             shipping_cost=shipping_cost, product_key=product_key,
                             listing_id=listing_id, **kw)

    def guard_price_change(self, new_price, reference_price, *, production_cost=None,
                           shipping_cost=None, product_key=None, listing_id=None, **kw):
        return self.evaluate("price_change", new_price, reference_price=reference_price,
                             production_cost=production_cost, shipping_cost=shipping_cost,
                             product_key=product_key, listing_id=listing_id, **kw)

    def guard_discount(self, discounted_price, reference_price, *, production_cost=None,
                       shipping_cost=None, product_key=None, listing_id=None, **kw):
        return self.evaluate("discount", discounted_price, reference_price=reference_price,
                             production_cost=production_cost, shipping_cost=shipping_cost,
                             product_key=product_key, listing_id=listing_id, **kw)

    def guard_advertising(self, price, ad_cost, *, production_cost=None,
                          shipping_cost=None, product_key=None, listing_id=None, **kw):
        return self.evaluate("advertising", price, ad_cost=ad_cost,
                             production_cost=production_cost, shipping_cost=shipping_cost,
                             product_key=product_key, listing_id=listing_id, **kw)

    def guard_portfolio(self, price, *, production_cost=None, shipping_cost=None,
                        product_key=None, listing_id=None, **kw):
        return self.evaluate("portfolio_optimisation", price, production_cost=production_cost,
                             shipping_cost=shipping_cost, product_key=product_key,
                             listing_id=listing_id, **kw)

    # --- Helpers ----------------------------------------------------

    @staticmethod
    def _known(value: float | None, default: float) -> tuple[float, bool]:
        """(cost, is_estimated). An unknown cost uses a conservative default."""
        if value is None:
            return round(float(default), 2), True
        return round(float(value), 2), False

    def _reserve_pct(self, data_confidence: float, unknown_count: int) -> float:
        span = max(0.0, self.max_reserve - self.default_reserve)
        # Low data-confidence -> larger buffer; each unknown adds a premium.
        pct = self.default_reserve + span * (1.0 - data_confidence) \
            + unknown_count * self.unknown_premium
        return max(self.default_reserve, min(self.max_reserve, pct))

    def _data_confidence(self, product_key: str | None) -> float:
        """Confidence from real sales history: a new product starts low and earns
        its way up over `confidence_sales_target` sales."""
        if not product_key:
            return self.new_product_confidence
        perf = self.db.get_product_performance(product_key) or {}
        units = int(perf.get("units_sold", 0) or 0)
        earned = min(1.0, units / self.confidence_sales_target)
        return self.new_product_confidence + (1.0 - self.new_product_confidence) * earned

    def _commercial_confidence(self, protected_margin: float) -> float:
        if self.comfort_margin <= 0:
            return 1.0 if protected_margin >= 0 else 0.0
        return round(max(0.0, min(1.0, protected_margin / self.comfort_margin)), 4)

    def _min_safe_price(self, production, shipping, ai, ad, reserve_pct) -> float:
        """The lowest price that clears the gross/contribution/protected thresholds,
        so callers can adjust UP instead of just being refused."""
        fixed = production + shipping + ai + ad  # excludes % fees + reserve
        fee_rate = (self.fees.transaction_rate + self.fees.payment_rate
                    + self.fees.regulatory_rate + self.fees.vat_rate
                    + self.fees.offsite_ads_rate * self.fees.offsite_ads_share)
        fee_fixed = self.fees.payment_fixed + self.fees.listing_fee
        candidates = [production / (1 - self.min_gross) if self.min_gross < 1 else fixed]
        # contribution margin: price - fixed - fee_rate*price - fee_fixed >= min_c*price
        denom_c = 1 - fee_rate - self.min_contribution
        if denom_c > 0:
            candidates.append((fixed + fee_fixed) / denom_c)
        # protected profit >= 0: price - fixed - fee_rate*price - fee_fixed - reserve*price >= 0
        denom_p = 1 - fee_rate - reserve_pct
        if denom_p > 0:
            candidates.append((fixed + fee_fixed) / denom_p)
        return round(max(candidates), 2)

    def _notify_ceo(self, record: dict[str, Any]) -> None:
        log.warning("[CEO ALERT] Financial Protection REJECTED %s for %s: %s "
                    "(protected profit %.2f, min safe price %.2f).",
                    record["action"], record.get("product_key") or "-", record["reason"],
                    record["protected_profit"], record["min_safe_price"])

    # --- Reads ------------------------------------------------------

    def audit(self, decision: str | None = None) -> list[dict[str, Any]]:
        return self.db.list_protection_decisions(decision=decision)

    def alerts(self) -> list[dict[str, Any]]:
        return self.db.list_protection_decisions(decision=REJECT)
