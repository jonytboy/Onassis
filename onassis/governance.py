"""Governance — the single decision pathway for proposals.

An agent submits a structured :class:`~onassis.proposals.Proposal`; governance
runs it through the two executives and returns one final, recorded verdict:

    1. Compliance Director reviews it (legal / platform / brand risk).
    2. CEO evaluates it against company policy (budget, margin, reserve…),
       using Compliance's brand-consistency score.
    3. Final verdict: Compliance has veto power and **overrules the CEO** — if
       Compliance does not APPROVE, its verdict stands regardless of the CEO.
       Only when Compliance approves does the CEO's verdict decide the outcome.

Every step is persisted: the proposal, the compliance report, and both
executives' decisions (with written reasoning).
"""

from __future__ import annotations

from typing import Any

from onassis.ceo import CEOAgent
from onassis.compliance import ComplianceDirector
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.proposals import APPROVE, REJECT, REQUEST_MORE_INFO, Proposal

log = get_logger(__name__)

_STATUS_FOR_VERDICT = {
    APPROVE: "approved",
    REJECT: "rejected",
    REQUEST_MORE_INFO: "needs_info",
}


class Governance:
    """Routes proposals through Compliance (veto) then the CEO (policy)."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.ceo = CEOAgent(config, db)
        self.compliance = ComplianceDirector(config, db)

    def submit(self, proposal: Proposal | dict[str, Any]) -> dict[str, Any]:
        """Submit a proposal for a decision. Returns the full decision bundle."""
        p = proposal if isinstance(proposal, Proposal) else Proposal.from_dict(proposal)
        p.validate()

        proposal_id = self.db.insert_proposal(p.to_dict())

        # 1. Compliance review (veto authority).
        report = self.compliance.review_proposal(p, proposal_id=proposal_id)
        self._record_decision(proposal_id, self.compliance.name, report["verdict"], report["reasoning"])

        # 2. CEO evaluation against company policy.
        ceo_decision = self.ceo.evaluate(
            p,
            proposal_id=proposal_id,
            brand_consistency_score=report["brand_consistency_score"],
        )

        # 3. Compliance overrules the CEO: its non-approval stands.
        if report["verdict"] != APPROVE:
            final_verdict = report["verdict"]
            final_authority = self.compliance.name
        else:
            final_verdict = ceo_decision["verdict"]
            final_authority = self.ceo.name

        self.db.update_proposal_status(
            proposal_id, _STATUS_FOR_VERDICT.get(final_verdict, "submitted")
        )
        log.info(
            "Proposal #%s final verdict: %s (by %s)",
            proposal_id,
            final_verdict,
            final_authority,
        )
        return {
            "proposal_id": proposal_id,
            "final_verdict": final_verdict,
            "final_authority": final_authority,
            "compliance": report,
            "ceo": ceo_decision,
        }

    def get_proposal_decisions(self, proposal_id: int) -> dict[str, Any] | None:
        """Read-only: a proposal with its compliance report and CEO decision."""
        proposal = self.db.get_proposal(proposal_id)
        if proposal is None:
            return None
        return {
            "proposal": proposal,
            "decisions": self.db.get_decisions_for_proposal(proposal_id),
            "compliance": self.db.get_compliance_for_proposal(proposal_id),
        }

    def _record_decision(
        self, proposal_id: int, authority: str, verdict: str, reasoning: str
    ) -> None:
        """Store a verdict in the unified decisions log (audit trail)."""
        self.db.insert_decision(
            {
                "proposal_id": proposal_id,
                "authority": authority,
                "verdict": verdict,
                "reasoning": reasoning,
                "policy_checks": [],
            }
        )
