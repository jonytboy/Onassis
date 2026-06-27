"""Structured proposals — how agents ask for permission to act.

In ONASSIS, no agent executes actions directly. Instead it submits a
structured **proposal** to the governance layer (Compliance Director + CEO),
which decides. ONASSIS is a capital-allocation system, so every proposal is
framed as an **investment**: it carries cost, expected revenue, net profit,
ROI, time-to-payback, confidence, and a risk level. The CEO ranks proposals
by risk-adjusted ROI and funds the highest returns first.

This module defines the proposal shape, the shared verdict vocabulary, and
the (provider/brand/marketplace-agnostic) economics helpers the engine uses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Shared verdict vocabulary (CEO and Compliance both speak it).
APPROVE = "APPROVE"
REJECT = "REJECT"
REQUEST_MORE_INFO = "REQUEST_MORE_INFO"
VERDICTS = (APPROVE, REJECT, REQUEST_MORE_INFO)

RISK_LEVELS = ("low", "medium", "high")
# Default discount applied to ROI per risk level (overridable via policy).
DEFAULT_RISK_WEIGHTS = {"low": 1.0, "medium": 0.7, "high": 0.4}


@dataclass
class Proposal:
    """An investment request, submitted by an agent for a decision.

    Optional ``brand`` / ``marketplace`` tags keep the engine multi-brand and
    multi-marketplace ready without changing any decision logic.
    """

    agent_name: str
    requested_action: str
    estimated_cost: float = 0.0
    expected_revenue: float = 0.0
    expected_net_profit: float | None = None  # computed if omitted
    expected_roi: float | None = None         # computed if omitted
    confidence: int = 0                        # 0-100
    time_to_payback_days: int | None = None
    risk_level: str = "medium"                 # low | medium | high
    risks: list[str] = field(default_factory=list)
    reasoning: str = ""
    campaign_id: int | None = None
    brand: str | None = None
    marketplace: str | None = None
    # Legacy alias kept for backward compatibility; used as revenue if
    # expected_revenue is not provided.
    expected_benefit: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        """Build a Proposal from a dict, ignoring unknown keys."""
        fields = {
            "agent_name",
            "requested_action",
            "estimated_cost",
            "expected_revenue",
            "expected_net_profit",
            "expected_roi",
            "confidence",
            "time_to_payback_days",
            "risk_level",
            "risks",
            "reasoning",
            "campaign_id",
            "brand",
            "marketplace",
            "expected_benefit",
        }
        return cls(**{k: v for k, v in data.items() if k in fields})

    # --- Investment economics ---------------------------------------

    def revenue(self) -> float:
        """Expected revenue (falls back to the legacy ``expected_benefit``)."""
        return float(self.expected_revenue or self.expected_benefit or 0.0)

    def net_profit(self) -> float:
        if self.expected_net_profit is not None:
            return float(self.expected_net_profit)
        return self.revenue() - float(self.estimated_cost or 0.0)

    def roi(self) -> float:
        """Return on investment = net profit / cost."""
        if self.expected_roi is not None:
            return float(self.expected_roi)
        cost = float(self.estimated_cost or 0.0)
        if cost <= 0:
            # No capital at risk: treat any positive profit as a strong return.
            return self.net_profit() if self.net_profit() > 0 else 0.0
        return self.net_profit() / cost

    def risk_adjusted_roi(self, risk_weights: dict[str, float] | None = None) -> float:
        """ROI discounted by confidence and risk level — the ranking key."""
        weights = risk_weights or DEFAULT_RISK_WEIGHTS
        weight = weights.get(self.risk_level, DEFAULT_RISK_WEIGHTS["medium"])
        return self.roi() * (int(self.confidence) / 100.0) * weight

    def economics(self, risk_weights: dict[str, float] | None = None) -> dict[str, Any]:
        """A compact, serializable view of the proposal's investment case."""
        return {
            "estimated_cost": float(self.estimated_cost or 0.0),
            "expected_revenue": self.revenue(),
            "expected_net_profit": self.net_profit(),
            "expected_roi": self.roi(),
            "risk_adjusted_roi": self.risk_adjusted_roi(risk_weights),
            "confidence": int(self.confidence),
            "risk_level": self.risk_level,
            "time_to_payback_days": self.time_to_payback_days,
        }

    def validate(self) -> None:
        """Raise ValueError if the proposal is missing required content."""
        if not self.agent_name:
            raise ValueError("Proposal requires an agent_name.")
        if not self.requested_action:
            raise ValueError("Proposal requires a requested_action.")
        if not (0 <= int(self.confidence) <= 100):
            raise ValueError("Proposal confidence must be 0-100.")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"risk_level must be one of {RISK_LEVELS}.")
