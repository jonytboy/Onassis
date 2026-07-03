"""The Commercial Pricing Engine — price for maximum expected profit.

Cost-plus pricing leaves money on the table. This engine chooses the price that
maximises **expected profit**, not maximum margin:

    expected conversion  ×  expected volume  ×  unit margin  =  expected profit

Conversion falls as price rises (a configurable price-elasticity curve anchored
on a reference price), so a cheaper price can sell more units and a dearer price
can make more per sale — the engine evaluates a grid of candidate prices and
picks the peak of the profit curve. Sometimes £24 sells better; sometimes £36
makes more money.

Unit margin uses the **real** economics via :class:`~onassis.fees.FeeModel` —
Etsy fees, Gelato production cost, shipping, VAT and Offsite Ads — so a price is
never chosen that loses money. Market signals (average selling price, estimated
monthly sales) anchor the reference price and the traffic estimate when
available. Deterministic and fully testable — no new AI agent.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.fees import FeeModel
from onassis.logger import get_logger

log = get_logger(__name__)


class PricingEngine:
    """Chooses the profit-maximising price from a candidate grid."""

    def __init__(self, config: Config, db: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "pricing", None) or {}
        self.fee_model = FeeModel.from_config(config)
        self.base_conversion = float(self.cfg.get("base_conversion", 0.025))
        self.elasticity = float(self.cfg.get("elasticity", 1.6))
        self.candidates = max(3, int(self.cfg.get("candidates", 13)))
        self.min_margin = float(self.cfg.get("min_margin", 0.10))
        self.price_span = float(self.cfg.get("price_span", 0.45))
        self.shipping_cost = float(self.cfg.get("shipping_cost", 0.0))
        self.default_monthly_visitors = float(self.cfg.get("default_monthly_visitors", 500))

    # --- Optimisation -----------------------------------------------

    def optimise(
        self, production_cost: float, reference_price: float | None = None,
        *, market: dict[str, Any] | None = None, monthly_visitors: float | None = None,
    ) -> dict[str, Any]:
        """Return the profit-max price + the full expected-profit curve."""
        production_cost = float(production_cost or 0)
        ref = float(reference_price or 0) or (market or {}).get("avg_selling_price") \
            or self._cost_plus(production_cost, 0.55)
        ref = max(ref, self._floor(production_cost))
        visitors = float(monthly_visitors if monthly_visitors is not None
                         else self._visitors(ref, market))

        curve: list[dict[str, Any]] = []
        for price in self._grid(production_cost, ref):
            conversion = self._conversion(price, ref)
            volume = visitors * conversion
            unit_margin = self.fee_model.unit_net_profit(
                price, production_cost + self.shipping_cost)
            expected_profit = round(volume * unit_margin, 2)
            net_margin = round(unit_margin / price, 4) if price else 0.0
            curve.append({
                "price": price, "conversion": round(conversion, 4),
                "volume": round(volume, 2), "unit_margin": unit_margin,
                "net_margin": net_margin, "expected_profit": expected_profit,
            })

        # Never recommend a loss-making price; among the rest, maximise profit.
        viable = [c for c in curve if c["unit_margin"] > 0
                  and c["net_margin"] >= self.min_margin] or \
            [c for c in curve if c["unit_margin"] > 0] or curve
        best = max(viable, key=lambda c: c["expected_profit"])
        cost_plus = self._cost_plus(production_cost, float(self.cfg.get("target_margin", 0.60)))
        uplift = round(best["expected_profit"]
                       - self._profit_at(cost_plus, ref, visitors, production_cost), 2)
        result = {
            "price": best["price"],
            "expected_profit": best["expected_profit"],
            "expected_conversion": best["conversion"],
            "expected_volume": best["volume"],
            "unit_margin": best["unit_margin"],
            "net_margin": best["net_margin"],
            "reference_price": round(ref, 2),
            "monthly_visitors": round(visitors, 1),
            "cost_plus_price": cost_plus,
            "profit_uplift_vs_cost_plus": uplift,
            "curve": curve,
        }
        result["rationale"] = (
            f"£{best['price']:.2f} maximises expected monthly profit "
            f"(~£{best['expected_profit']:.0f} at {best['conversion']:.1%} conversion, "
            f"{best['unit_margin']:.2f}/sale) vs a cost-plus £{cost_plus:.2f}."
        )
        log.info("Pricing: chose £%.2f (expected profit £%.0f) over cost-plus £%.2f.",
                 best["price"], best["expected_profit"], cost_plus)
        return result

    # --- Model ------------------------------------------------------

    def _conversion(self, price: float, ref: float) -> float:
        """Conversion vs price — a constant-elasticity demand curve about ref."""
        if price <= 0:
            return 0.0
        conv = self.base_conversion * (ref / price) ** self.elasticity
        return max(0.0, min(0.6, conv))

    def _grid(self, production_cost: float, ref: float) -> list[float]:
        low = max(self._floor(production_cost), ref * (1 - self.price_span))
        high = ref * (1 + self.price_span)
        if high <= low:
            high = low * 1.5
        step = (high - low) / (self.candidates - 1)
        return [round(low + step * i, 2) for i in range(self.candidates)]

    def _profit_at(self, price: float, ref: float, visitors: float,
                   production_cost: float) -> float:
        margin = self.fee_model.unit_net_profit(price, production_cost + self.shipping_cost)
        return round(visitors * self._conversion(price, ref) * margin, 2)

    def _visitors(self, ref: float, market: dict[str, Any] | None) -> float:
        sales = (market or {}).get("est_monthly_sales")
        if sales and self.base_conversion > 0:
            return float(sales) / self.base_conversion   # implied traffic at reference
        return self.default_monthly_visitors

    def _floor(self, production_cost: float) -> float:
        """The lowest price that could still clear the minimum net margin."""
        eff = (self.fee_model.transaction_rate + self.fee_model.payment_rate
               + self.fee_model.regulatory_rate + self.fee_model.vat_rate
               + self.fee_model.offsite_ads_rate * self.fee_model.offsite_ads_share)
        denom = max(0.05, 1 - eff - self.min_margin)
        cost = production_cost + self.shipping_cost + self.fee_model.payment_fixed \
            + self.fee_model.listing_fee
        return round(cost / denom, 2)

    @staticmethod
    def _cost_plus(production_cost: float, margin: float) -> float:
        margin = min(0.95, max(0.0, margin))
        return round(production_cost / (1 - margin), 2) if margin < 1 else production_cost
