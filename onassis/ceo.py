"""The CEO Agent — the single business decision-making authority.

No other agent executes actions directly. Each submits a structured
:class:`~onassis.proposals.Proposal`; the CEO evaluates it against company
policy and returns one of APPROVE / REJECT / REQUEST_MORE_INFO, with written
reasoning, and stores the decision.

The engine is **deterministic** — a transparent rule check against policy —
so decisions are explainable, reproducible, and fully testable without any
external calls. (The Compliance Director, Sprint 7, can overrule the CEO.)

Company policy (from ``config.yaml -> policy``):
    available_cash, cash_reserve, min_profit_margin, max_ai_spend,
    max_experiment_budget, min_confidence, brand_consistency_min.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.proposals import APPROVE, REJECT, REQUEST_MORE_INFO, Proposal

log = get_logger(__name__)

_DEFAULTS = {
    "available_cash": 10000.0,
    "cash_reserve": 5000.0,
    "min_profit_margin": 0.30,
    "max_ai_spend": 50.0,
    "max_experiment_budget": 200.0,
    "min_confidence": 50,
    "brand_consistency_min": 70,
}


class CEOAgent:
    """Evaluates proposals against company policy and records the decision."""

    name = "CEO"

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.policy = {**_DEFAULTS, **(config.policy or {})}

    def evaluate(
        self,
        proposal: Proposal | dict[str, Any],
        *,
        proposal_id: int | None = None,
        brand_consistency_score: int | None = None,
        store: bool = True,
    ) -> dict[str, Any]:
        """Decide on a proposal. Returns the decision (and stores it).

        Args:
            proposal: the proposal under review.
            proposal_id: the stored proposal's id (required to persist).
            brand_consistency_score: optional score (0-100) from Compliance;
                if given, it's checked against the brand-consistency policy.
            store: whether to persist the decision.
        """
        p = proposal if isinstance(proposal, Proposal) else Proposal.from_dict(proposal)
        checks = self._run_checks(p, brand_consistency_score)

        verdict, reasoning = self._decide(p, checks)

        decision: dict[str, Any] = {
            "proposal_id": proposal_id,
            "authority": self.name,
            "verdict": verdict,
            "reasoning": reasoning,
            "policy_checks": checks,
        }
        if store and proposal_id is not None:
            decision["id"] = self.db.insert_decision(decision)
        log.info("CEO verdict on proposal #%s: %s", proposal_id, verdict)
        return decision

    # --- Policy checks ----------------------------------------------

    def _run_checks(
        self, p: Proposal, brand_consistency_score: int | None
    ) -> list[dict[str, Any]]:
        """Evaluate every policy rule and return a transparent checklist."""
        pol = self.policy
        cost = float(p.estimated_cost or 0)
        benefit = float(p.expected_benefit or 0)
        margin = (benefit - cost) / benefit if benefit > 0 else -1.0

        checks: list[dict[str, Any]] = [
            {
                "name": "confidence",
                "passed": int(p.confidence) >= pol["min_confidence"],
                "blocking": False,  # low confidence -> request info, not reject
                "detail": f"confidence {p.confidence} vs. minimum {pol['min_confidence']}",
            },
            {
                "name": "max_ai_spend",
                "passed": cost <= pol["max_ai_spend"],
                "blocking": True,
                "detail": f"estimated cost {cost:g} vs. max AI spend {pol['max_ai_spend']:g}",
            },
            {
                "name": "max_experiment_budget",
                "passed": cost <= pol["max_experiment_budget"],
                "blocking": True,
                "detail": (
                    f"estimated cost {cost:g} vs. max experiment budget "
                    f"{pol['max_experiment_budget']:g}"
                ),
            },
            {
                "name": "cash_reserve",
                "passed": (pol["available_cash"] - cost) >= pol["cash_reserve"],
                "blocking": True,
                "detail": (
                    f"cash after spend {pol['available_cash'] - cost:g} vs. "
                    f"required reserve {pol['cash_reserve']:g}"
                ),
            },
            {
                "name": "min_profit_margin",
                "passed": margin >= pol["min_profit_margin"],
                "blocking": True,
                "detail": (
                    f"projected margin {margin:.0%} vs. minimum "
                    f"{pol['min_profit_margin']:.0%}"
                ),
            },
        ]
        if brand_consistency_score is not None:
            checks.append(
                {
                    "name": "brand_consistency",
                    "passed": brand_consistency_score >= pol["brand_consistency_min"],
                    "blocking": True,
                    "detail": (
                        f"brand consistency {brand_consistency_score} vs. minimum "
                        f"{pol['brand_consistency_min']}"
                    ),
                }
            )
        return checks

    def _decide(self, p: Proposal, checks: list[dict[str, Any]]) -> tuple[str, str]:
        """Turn the checklist into a verdict + written reasoning."""
        failed_blocking = [c for c in checks if c["blocking"] and not c["passed"]]
        confidence_ok = next(c["passed"] for c in checks if c["name"] == "confidence")

        if failed_blocking:
            reasons = "; ".join(f"{c['name']} ({c['detail']})" for c in failed_blocking)
            verdict = REJECT
            reasoning = (
                f"REJECTED '{p.requested_action}' from {p.agent_name}. It violates "
                f"company policy on: {reasons}. Per policy these are hard limits, so "
                f"the action cannot proceed as proposed."
            )
        elif not confidence_ok:
            conf_detail = next(c["detail"] for c in checks if c["name"] == "confidence")
            verdict = REQUEST_MORE_INFO
            reasoning = (
                f"MORE INFORMATION NEEDED for '{p.requested_action}' from {p.agent_name}. "
                f"It is within budget and margin policy, but {conf_detail} is below the "
                f"bar to commit. Provide stronger evidence or a tighter estimate to proceed."
            )
        else:
            verdict = APPROVE
            reasoning = (
                f"APPROVED '{p.requested_action}' from {p.agent_name}. It satisfies every "
                f"company-policy check: spend within limits, cash reserve preserved, "
                f"projected margin above the minimum, and sufficient confidence."
            )
        return verdict, reasoning
