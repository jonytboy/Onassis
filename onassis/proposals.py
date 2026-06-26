"""Structured proposals — how agents ask for permission to act.

In ONASSIS, no agent executes actions directly. Instead it submits a
structured **proposal** to the governance layer (Compliance Director + CEO),
which decides. This module defines the proposal shape and the shared verdict
vocabulary used by both decision-makers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Shared verdict vocabulary (CEO and Compliance both speak it).
APPROVE = "APPROVE"
REJECT = "REJECT"
REQUEST_MORE_INFO = "REQUEST_MORE_INFO"
VERDICTS = (APPROVE, REJECT, REQUEST_MORE_INFO)


@dataclass
class Proposal:
    """A request to take an action, submitted by an agent for a decision."""

    agent_name: str
    requested_action: str
    estimated_cost: float = 0.0
    expected_benefit: float = 0.0
    confidence: int = 0  # 0-100
    risks: list[str] = field(default_factory=list)
    reasoning: str = ""
    campaign_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        """Build a Proposal from a dict, ignoring unknown keys."""
        fields = {
            "agent_name",
            "requested_action",
            "estimated_cost",
            "expected_benefit",
            "confidence",
            "risks",
            "reasoning",
            "campaign_id",
        }
        return cls(**{k: v for k, v in data.items() if k in fields})

    def validate(self) -> None:
        """Raise ValueError if the proposal is missing required content."""
        if not self.agent_name:
            raise ValueError("Proposal requires an agent_name.")
        if not self.requested_action:
            raise ValueError("Proposal requires a requested_action.")
        if not (0 <= int(self.confidence) <= 100):
            raise ValueError("Proposal confidence must be 0-100.")
