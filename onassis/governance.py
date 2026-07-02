"""Governance — the single decision pathway for proposals.

An agent submits a structured :class:`~onassis.proposals.Proposal`; governance
runs it through the two executives and returns one final, recorded verdict:

    1. Compliance Director reviews it (legal / platform / brand risk).
    2. CEO evaluates it as an investment against company policy (ROI hurdle,
       budget, margin, reserve…), using Compliance's brand-consistency score
       and the live cash balance / remaining AI budget from the Profit Engine.
    3. Final verdict: Compliance has veto power and **overrules the CEO** — if
       Compliance does not APPROVE, its verdict stands. Only when Compliance
       approves does the CEO's verdict decide.

:meth:`allocate` extends this to a *batch*: many proposals competing for the
same daily budget. Compliance screens each; the survivors are ranked by
risk-adjusted ROI and funded highest-first until the budget/reserve runs out.

Every proposal, report, and decision is persisted.
"""

from __future__ import annotations

from typing import Any

from onassis.ceo import CEOAgent
from onassis.compliance import ComplianceDirector
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.profit import ProfitEngine
from onassis.proposals import (
    APPROVE, APPROVE_WITH_CHANGES, REJECT, REQUEST_MORE_INFO, Proposal, is_compliant,
)

log = get_logger(__name__)

_STATUS_FOR_VERDICT = {
    APPROVE: "approved",
    APPROVE_WITH_CHANGES: "approved",   # cleared with amendments applied
    REJECT: "rejected",
    REQUEST_MORE_INFO: "needs_info",
}


class Governance:
    """Routes proposals through Compliance (veto) then the CEO (capital)."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.ceo = CEOAgent(config, db)
        self.compliance = ComplianceDirector(config, db)
        self.profit = ProfitEngine(config, db)

    def submit(self, proposal: Proposal | dict[str, Any]) -> dict[str, Any]:
        """Submit one proposal for a decision. Returns the full decision bundle."""
        p = proposal if isinstance(proposal, Proposal) else Proposal.from_dict(proposal)
        p.validate()
        proposal_id = self.db.insert_proposal(p.to_dict())

        report = self.compliance.review_proposal(p, proposal_id=proposal_id)
        self._record_decision(proposal_id, self.compliance.name, report["verdict"], report["reasoning"])

        ceo_decision = self.ceo.evaluate(
            p,
            proposal_id=proposal_id,
            brand_consistency_score=report["brand_consistency_score"],
            remaining_budget=self.profit.remaining_ai_budget(),
            available_cash=self.profit.cash_balance(),
        )

        final_verdict, final_authority = self._resolve(report, ceo_decision)
        self.db.update_proposal_status(
            proposal_id, _STATUS_FOR_VERDICT.get(final_verdict, "submitted")
        )
        log.info("Proposal #%s final verdict: %s (by %s)", proposal_id, final_verdict, final_authority)
        return {
            "proposal_id": proposal_id,
            "final_verdict": final_verdict,
            "final_authority": final_authority,
            "compliance": report,
            "ceo": ceo_decision,
        }

    def allocate(self, proposals: list[Proposal | dict[str, Any]]) -> list[dict[str, Any]]:
        """Decide a batch competing for one budget: screen, rank, fund best-first."""
        # 1. Store + Compliance-screen every proposal.
        screened: list[dict[str, Any]] = []
        vetoed: list[dict[str, Any]] = []
        for proposal in proposals:
            p = proposal if isinstance(proposal, Proposal) else Proposal.from_dict(proposal)
            p.validate()
            pid = self.db.insert_proposal(p.to_dict())
            report = self.compliance.review_proposal(p, proposal_id=pid)
            self._record_decision(pid, self.compliance.name, report["verdict"], report["reasoning"])
            if is_compliant(report["verdict"]):
                screened.append(
                    {
                        "proposal": p,
                        "proposal_id": pid,
                        "brand_consistency_score": report["brand_consistency_score"],
                        "compliance": report,
                    }
                )
            else:
                self.db.update_proposal_status(
                    pid, _STATUS_FOR_VERDICT.get(report["verdict"], "submitted")
                )
                vetoed.append(
                    {
                        "proposal_id": pid,
                        "final_verdict": report["verdict"],
                        "final_authority": self.compliance.name,
                        "compliance": report,
                        "ceo": None,
                        "rank": None,
                    }
                )

        # 2. CEO ranks the survivors and allocates the live budget best-first.
        ceo_results = self.ceo.allocate(
            screened,
            available_budget=self.profit.remaining_ai_budget(),
            available_cash=self.profit.cash_balance(),
        )

        bundles: list[dict[str, Any]] = []
        by_pid = {c["proposal_id"]: c for c in screened}
        for decision in ceo_results:
            pid = decision["proposal_id"]
            self.db.update_proposal_status(
                pid, _STATUS_FOR_VERDICT.get(decision["verdict"], "submitted")
            )
            bundles.append(
                {
                    "proposal_id": pid,
                    "final_verdict": decision["verdict"],
                    "final_authority": self.ceo.name,
                    "compliance": by_pid[pid]["compliance"],
                    "ceo": decision,
                    "rank": decision.get("rank"),
                }
            )
        return bundles + vetoed

    def get_proposal_decisions(self, proposal_id: int) -> dict[str, Any] | None:
        """Read-only: a proposal with its compliance report and decisions."""
        proposal = self.db.get_proposal(proposal_id)
        if proposal is None:
            return None
        return {
            "proposal": proposal,
            "decisions": self.db.get_decisions_for_proposal(proposal_id),
            "compliance": self.db.get_compliance_for_proposal(proposal_id),
        }

    # --- Helpers ----------------------------------------------------

    def _resolve(self, report: dict[str, Any], ceo_decision: dict[str, Any]) -> tuple[str, str]:
        # Compliance overrules only with a hard REJECT; APPROVE / APPROVE_WITH_CHANGES
        # clear the subject through to the CEO's capital decision.
        if not is_compliant(report["verdict"]):
            return report["verdict"], self.compliance.name
        return ceo_decision["verdict"], self.ceo.name

    def _record_decision(
        self, proposal_id: int, authority: str, verdict: str, reasoning: str
    ) -> None:
        self.db.insert_decision(
            {
                "proposal_id": proposal_id,
                "authority": authority,
                "verdict": verdict,
                "reasoning": reasoning,
                "policy_checks": [],
            }
        )
