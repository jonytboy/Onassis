"""The Etsy fee model — ONASSIS's real unit economics.

A "60% margin" on paper is a **loss** once Etsy's real fees land: the 6.5%
transaction fee, ~4% + £0.20 payment processing, the £0.20 listing fee, optional
regulatory operating fees, and — the silent killer — **Offsite Ads at 12-15%** on
attributed orders. This module computes fees the way Etsy actually charges them,
so every forecast, every recorded order, and every go-live margin check uses the
true number.

It is a pure, deterministic helper (no new agent, no architecture): one
:class:`FeeModel` built from config, reused by the Expansion Engine (forecast),
the Etsy connector (real imported orders), the launch margin guard, and the
daily report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeeModel:
    """Etsy's fee schedule. Rates are fractions of the item+shipping total."""

    transaction_rate: float = 0.065      # Etsy transaction fee (item + shipping)
    payment_rate: float = 0.04           # payment processing %
    payment_fixed: float = 0.20          # payment processing flat, per order
    listing_fee: float = 0.20            # per listing / renewal on sale
    regulatory_rate: float = 0.0         # regulatory operating fee (region-specific)
    offsite_ads_rate: float = 0.15       # Offsite Ads fee on ATTRIBUTED orders
    offsite_ads_share: float = 0.30      # blended fraction of orders attributed
    currency: str = "GBP"

    @classmethod
    def from_config(cls, config: Any) -> "FeeModel":
        cfg = (getattr(config, "fees", None) or {})
        return cls(
            transaction_rate=float(cfg.get("transaction_rate", 0.065)),
            payment_rate=float(cfg.get("payment_rate", 0.04)),
            payment_fixed=float(cfg.get("payment_fixed", 0.20)),
            listing_fee=float(cfg.get("listing_fee", 0.20)),
            regulatory_rate=float(cfg.get("regulatory_rate", 0.0)),
            offsite_ads_rate=float(cfg.get("offsite_ads_rate", 0.15)),
            offsite_ads_share=float(cfg.get("offsite_ads_share", 0.30)),
            currency=str(cfg.get("currency", "GBP")),
        )

    # --- Fee computation --------------------------------------------

    def order_fees(
        self, item_total: float, shipping: float = 0.0, *, offsite: bool | None = None
    ) -> dict[str, float]:
        """Break an order's fees into marketplace vs payment (for the ledger).

        ``offsite``: ``True`` — the order was attributed to Offsite Ads (full
        rate); ``False`` — not attributed (no ad fee); ``None`` — unknown, so use
        the **blended** expectation (rate × attributed share) for forecasting.
        """
        base = float(item_total) + float(shipping)
        marketplace = self.transaction_rate * base + self.listing_fee \
            + self.regulatory_rate * base
        payment = self.payment_rate * base + self.payment_fixed
        if offsite is True:
            offsite_fee = self.offsite_ads_rate * base
        elif offsite is False:
            offsite_fee = 0.0
        else:  # unknown → blended expectation for forecasts
            offsite_fee = self.offsite_ads_rate * self.offsite_ads_share * base
        marketplace += offsite_fee
        return {
            "marketplace_fees": round(marketplace, 2),
            "payment_fees": round(payment, 2),
            "offsite_fees": round(offsite_fee, 2),
            "total_fees": round(marketplace + payment, 2),
        }

    def total_fees(self, item_total: float, shipping: float = 0.0,
                   *, offsite: bool | None = None) -> float:
        return self.order_fees(item_total, shipping, offsite=offsite)["total_fees"]

    def unit_net_profit(
        self, retail_price: float, production_cost: float, *, offsite: bool | None = None
    ) -> float:
        """Per-unit net profit after production AND real Etsy fees."""
        fees = self.total_fees(float(retail_price), offsite=offsite)
        return round(float(retail_price) - float(production_cost) - fees, 2)

    def net_margin(
        self, retail_price: float, production_cost: float, *, offsite: bool | None = None
    ) -> float:
        """Net margin (0-1) after production and fees; 0 if price is zero."""
        price = float(retail_price)
        if price <= 0:
            return 0.0
        return round(self.unit_net_profit(price, production_cost, offsite=offsite) / price, 4)
