"""The CEO Agent — the capital-allocation decision authority.

ONASSIS exists to maximise long-term sustainable net profit. Every proposal
is treated as an **investment**: the CEO evaluates its risk-adjusted ROI,
ranks competing proposals, and allocates the available daily budget to the
highest expected returns first. Before funding anything it asks:

    "If I invest £1 here, is this the highest expected return currently
     available?" — if not (it fails the ROI hurdle), it is rejected.

The engine is **deterministic** — a transparent rule + ranking check against
company policy — so decisions are explainable, reproducible, and testable.
(The Compliance Director, Sprint 7, can still overrule the CEO.)

Company policy (from ``config.yaml -> policy``):
    available_cash, cash_reserve, min_profit_margin, max_ai_spend,
    max_experiment_budget, min_confidence, brand_consistency_min,
    daily_ai_budget, min_roi (the risk-adjusted ROI hurdle), risk_weights.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.proposals import (
    APPROVE,
    DEFAULT_RISK_WEIGHTS,
    REJECT,
    REQUEST_MORE_INFO,
    Proposal,
)

log = get_logger(__name__)

_DEFAULTS = {
    "available_cash": 10000.0,
    "cash_reserve": 5000.0,
    "min_profit_margin": 0.30,
    "max_ai_spend": 50.0,
    "max_experiment_budget": 200.0,
    "min_confidence": 50,
    "brand_consistency_min": 70,
    "daily_ai_budget": 100.0,
    "min_roi": 0.50,  # minimum risk-adjusted ROI to fund (the "£1" hurdle)
}


class CEOAgent:
    """Ranks proposals by risk-adjusted ROI and allocates capital to the best."""

    name = "CEO"

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.policy = {**_DEFAULTS, **(config.policy or {})}
        self.risk_weights = self.policy.get("risk_weights") or DEFAULT_RISK_WEIGHTS

    # --- Single-proposal evaluation ---------------------------------

    def evaluate(
        self,
        proposal: Proposal | dict[str, Any],
        *,
        proposal_id: int | None = None,
        brand_consistency_score: int | None = None,
        remaining_budget: float | None = None,
        available_cash: float | None = None,
        store: bool = True,
    ) -> dict[str, Any]:
        """Decide on a single proposal as an investment, and record it."""
        p = proposal if isinstance(proposal, Proposal) else Proposal.from_dict(proposal)
        econ = p.economics(self.risk_weights)
        budget = self.policy["daily_ai_budget"] if remaining_budget is None else remaining_budget
        cash = self.policy["available_cash"] if available_cash is None else available_cash

        checks = self._run_checks(p, econ, brand_consistency_score, budget, cash)
        verdict, reasoning = self._decide(p, econ, checks)

        decision: dict[str, Any] = {
            "proposal_id": proposal_id,
            "authority": self.name,
            "verdict": verdict,
            "reasoning": reasoning,
            "economics": econ,
            "policy_checks": checks,
        }
        if store and proposal_id is not None:
            decision["id"] = self.db.insert_decision(decision)
        log.info(
            "CEO verdict on proposal #%s: %s (risk-adj ROI %.2f)",
            proposal_id,
            verdict,
            econ["risk_adjusted_roi"],
        )
        return decision

    # --- Ranking & capital allocation -------------------------------

    def rank(self, proposals: list[Proposal]) -> list[Proposal]:
        """Order proposals by risk-adjusted ROI, highest first."""
        return sorted(
            proposals, key=lambda p: p.risk_adjusted_roi(self.risk_weights), reverse=True
        )

    def allocate(
        self,
        candidates: list[dict[str, Any]],
        *,
        available_budget: float | None = None,
        available_cash: float | None = None,
        store: bool = True,
    ) -> list[dict[str, Any]]:
        """Allocate the daily budget across competing proposals, best first.

        Each candidate is ``{"proposal": Proposal, "proposal_id": int|None,
        "brand_consistency_score": int|None}``. Proposals are ranked by
        risk-adjusted ROI; capital is committed to the highest returns until
        the daily AI budget or cash reserve is exhausted. Returns one decision
        per candidate, in ranked order, annotated with ``rank``.
        """
        ranked = sorted(
            candidates,
            key=lambda c: c["proposal"].risk_adjusted_roi(self.risk_weights),
            reverse=True,
        )
        remaining = self.policy["daily_ai_budget"] if available_budget is None else available_budget
        cash = self.policy["available_cash"] if available_cash is None else available_cash

        results: list[dict[str, Any]] = []
        for rank, c in enumerate(ranked, start=1):
            p: Proposal = c["proposal"]
            decision = self.evaluate(
                p,
                proposal_id=c.get("proposal_id"),
                brand_consistency_score=c.get("brand_consistency_score"),
                remaining_budget=remaining,
                available_cash=cash,
                store=store,
            )
            decision["rank"] = rank
            if decision["verdict"] == APPROVE:
                spend = float(p.estimated_cost or 0)
                remaining -= spend
                cash -= spend
            results.append(decision)
        return results

    # --- Policy checks ----------------------------------------------

    def _run_checks(
        self,
        p: Proposal,
        econ: dict[str, Any],
        brand_consistency_score: int | None,
        budget: float,
        cash: float,
    ) -> list[dict[str, Any]]:
        pol = self.policy
        cost = econ["estimated_cost"]
        revenue = econ["expected_revenue"]
        net = econ["expected_net_profit"]
        rar = econ["risk_adjusted_roi"]
        margin = (revenue - cost) / revenue if revenue > 0 else -1.0

        checks: list[dict[str, Any]] = [
            {
                "name": "confidence",
                "passed": int(p.confidence) >= pol["min_confidence"],
                "blocking": False,  # low confidence -> request info, not reject
                "detail": f"confidence {p.confidence} vs. minimum {pol['min_confidence']}",
            },
            {
                "name": "positive_net_profit",
                "passed": net > 0,
                "blocking": True,
                "detail": f"expected net profit {net:g}",
            },
            {
                "name": "roi_hurdle",
                "passed": rar >= pol["min_roi"],
                "blocking": True,
                "detail": (
                    f"risk-adjusted ROI {rar:.2f} vs. hurdle {pol['min_roi']:.2f} "
                    f"(is £1 here the best available return?)"
                ),
            },
            {
                "name": "daily_ai_budget",
                "passed": cost <= budget,
                "blocking": True,
                "detail": f"cost {cost:g} vs. remaining daily AI budget {budget:g}",
            },
            {
                "name": "max_ai_spend",
                "passed": cost <= pol["max_ai_spend"],
                "blocking": True,
                "detail": f"cost {cost:g} vs. max AI spend {pol['max_ai_spend']:g}",
            },
            {
                "name": "max_experiment_budget",
                "passed": cost <= pol["max_experiment_budget"],
                "blocking": True,
                "detail": f"cost {cost:g} vs. max experiment budget {pol['max_experiment_budget']:g}",
            },
            {
                "name": "cash_reserve",
                "passed": (cash - cost) >= pol["cash_reserve"],
                "blocking": True,
                "detail": (
                    f"cash after spend {cash - cost:g} vs. required reserve "
                    f"{pol['cash_reserve']:g}"
                ),
            },
            {
                "name": "min_profit_margin",
                "passed": margin >= pol["min_profit_margin"],
                "blocking": True,
                "detail": f"projected margin {margin:.0%} vs. minimum {pol['min_profit_margin']:.0%}",
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

    def _decide(
        self, p: Proposal, econ: dict[str, Any], checks: list[dict[str, Any]]
    ) -> tuple[str, str]:
        failed_blocking = [c for c in checks if c["blocking"] and not c["passed"]]
        confidence_ok = next(c["passed"] for c in checks if c["name"] == "confidence")

        if failed_blocking:
            reasons = "; ".join(f"{c['name']} ({c['detail']})" for c in failed_blocking)
            verdict = REJECT
            reasoning = (
                f"REJECTED '{p.requested_action}' from {p.agent_name}. As an investment "
                f"(cost {econ['estimated_cost']:g}, expected net profit "
                f"{econ['expected_net_profit']:g}, risk-adjusted ROI "
                f"{econ['risk_adjusted_roi']:.2f}) it fails: {reasons}. Capital is better "
                f"deployed elsewhere."
            )
        elif not confidence_ok:
            conf_detail = next(c["detail"] for c in checks if c["name"] == "confidence")
            verdict = REQUEST_MORE_INFO
            reasoning = (
                f"MORE INFORMATION NEEDED for '{p.requested_action}' from {p.agent_name}. "
                f"The investment case clears policy and the ROI hurdle, but {conf_detail} "
                f"is too low to commit capital. Provide stronger evidence to proceed."
            )
        else:
            verdict = APPROVE
            reasoning = (
                f"APPROVED '{p.requested_action}' from {p.agent_name} as a sound investment: "
                f"expected net profit {econ['expected_net_profit']:g} on cost "
                f"{econ['estimated_cost']:g} (risk-adjusted ROI {econ['risk_adjusted_roi']:.2f} "
                f"clears the {self.policy['min_roi']:.2f} hurdle). Spend is within the daily AI "
                f"budget and limits, and the cash reserve is preserved."
            )
        return verdict, reasoning
