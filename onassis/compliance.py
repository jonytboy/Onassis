"""The Compliance Director — permanent executive with veto power.

Company Law: ONASSIS will never knowingly infringe copyright, trademarks,
intellectual-property rights, or platform policies in pursuit of profit. The
Compliance Director enforces this and may overrule every other agent,
**including the CEO**.

For any subject (a proposal, or a whole campaign) the Director produces a
compliance report:

    Compliance Score (0-100), Trademark Risk, Copyright Risk, Platform Risk,
    Brand Consistency Score, a verdict, written reasoning, and suggested
    lower-risk corrections.

Risk *assessment* is LLM-reasoned (trademark/copyright/platform/brand are
judgment calls); the *verdict* is then computed deterministically from the
configured thresholds, so the veto is explainable and testable. Every report
is stored, and past rejections are fed back into the prompt so the Director
learns from previous decisions.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.llm import LLMClient
from onassis.logger import get_logger
from onassis.proposals import APPROVE, REJECT, REQUEST_MORE_INFO, Proposal

log = get_logger(__name__)

_DEFAULTS = {
    "high_risk_threshold": 70,
    "medium_risk_threshold": 40,
    "brand_consistency_min": 70,
}

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trademark_risk": {"type": "integer"},
        "copyright_risk": {"type": "integer"},
        "platform_risk": {"type": "integer"},
        "brand_consistency_score": {"type": "integer"},
        "reasoning": {"type": "string"},
        "corrections": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "trademark_risk",
        "copyright_risk",
        "platform_risk",
        "brand_consistency_score",
        "reasoning",
        "corrections",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the Compliance Director of a premium Mediterranean lifestyle brand "
    "and the final authority on legal and platform risk. You protect the company "
    "from trademark conflicts, copyright/IP infringement, and violations of Etsy, "
    "Pinterest, Instagram and Facebook policies, and you guard brand integrity "
    "(premium, original, authentic, no misleading claims, no offensive or "
    "discriminatory content, no celebrity likenesses, no copyrighted characters, "
    "no third-party logos). For each subject you score four risks 0-100 "
    "(higher = riskier; brand_consistency_score is higher = better), explain your "
    "reasoning, and propose concrete lower-risk alternatives. Be conservative: "
    "when in doubt, score risk higher. Company Law: never knowingly infringe IP "
    "or platform policy in pursuit of profit."
)


def _clamp(value: Any, default: int = 50) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


class ComplianceDirector:
    """Reviews subjects for risk, vetoes violations, and remembers decisions."""

    name = "Compliance"

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.thresholds = {**_DEFAULTS, **(config.compliance or {})}
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    # --- Public review entry points ---------------------------------

    def review_proposal(
        self, proposal: Proposal | dict[str, Any], proposal_id: int | None = None
    ) -> dict[str, Any]:
        p = proposal if isinstance(proposal, Proposal) else Proposal.from_dict(proposal)
        subject = (
            f"Proposed action by {p.agent_name}: {p.requested_action}. "
            f"Rationale: {p.reasoning}"
        )
        return self._review(subject, label=p.requested_action, proposal_id=proposal_id)

    def review_campaign(
        self, campaign: dict[str, Any], content: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Review a whole campaign (name, story, and its generated assets)."""
        campaign_id = campaign["id"]
        if content is None:
            content = self.db.get_content_for_brief(campaign.get("brief_id"))
        asset_lines = "\n".join(
            f"- [{c['platform']}] {c.get('title') or ''}: {c['body']}" for c in content
        )
        subject = (
            f"Campaign '{campaign.get('name', '')}' (theme: {campaign.get('theme', '')}).\n"
            f"Story: {campaign.get('story', '')}\nAssets:\n{asset_lines}"
        )
        return self._review(
            subject, label=campaign.get("name", "campaign"), campaign_id=campaign_id
        )

    # --- Read -------------------------------------------------------

    def get_for_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        return self.db.get_compliance_for_campaign(campaign_id)

    def get_for_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        return self.db.get_compliance_for_proposal(proposal_id)

    def list_reports(self) -> list[dict[str, Any]]:
        return self.db.list_compliance_reports()

    # --- Core review ------------------------------------------------

    def _review(
        self,
        subject: str,
        *,
        label: str,
        proposal_id: int | None = None,
        campaign_id: int | None = None,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(subject)
        log.info("Compliance reviewing: %s", label)
        generated = self.llm.generate_json(system=_SYSTEM, prompt=prompt, schema=_SCHEMA)

        tm = _clamp(generated["trademark_risk"])
        cr = _clamp(generated["copyright_risk"])
        pl = _clamp(generated["platform_risk"])
        brand = _clamp(generated["brand_consistency_score"])
        verdict = self._verdict(tm, cr, pl, brand)
        score = round(((100 - tm) + (100 - cr) + (100 - pl) + brand) / 4)

        report: dict[str, Any] = {
            "proposal_id": proposal_id,
            "campaign_id": campaign_id,
            "subject": label,
            "compliance_score": score,
            "trademark_risk": tm,
            "copyright_risk": cr,
            "platform_risk": pl,
            "brand_consistency_score": brand,
            "verdict": verdict,
            "reasoning": generated["reasoning"],
            "corrections": generated.get("corrections", []),
        }
        report["id"] = self.db.insert_compliance_report(report)
        log.info("Compliance verdict for %s: %s (score %d)", label, verdict, score)
        return report

    def _verdict(self, tm: int, cr: int, pl: int, brand: int) -> str:
        high = self.thresholds["high_risk_threshold"]
        medium = self.thresholds["medium_risk_threshold"]
        brand_min = self.thresholds["brand_consistency_min"]
        worst = max(tm, cr, pl)
        if worst >= high:
            return REJECT
        if brand < brand_min:
            return REJECT
        if worst >= medium:
            return REQUEST_MORE_INFO
        return APPROVE

    # --- Learning ---------------------------------------------------

    def _build_prompt(self, subject: str) -> str:
        return f"""Review this subject for compliance risk.

SUBJECT
{subject}

{self._past_rejections_block()}
Score each 0-100 (higher = riskier; brand_consistency_score: higher = better):
- `trademark_risk`: likely conflicts with existing trademarks (names, slogans, phrases).
- `copyright_risk`: any sign of copying artwork, photos, logos, distinctive styles,
  or protected marketing copy — anything derivative rather than original.
- `platform_risk`: likely violations of Etsy, Pinterest, Instagram, or Facebook policy.
- `brand_consistency_score`: fit with premium Mediterranean lifestyle — original,
  authentic, no misleading claims, no offensive/discriminatory content, no celebrity
  likenesses, no copyrighted characters, no third-party logos.
- `reasoning`: explain the scores and name any specific risky element.
- `corrections`: concrete lower-risk alternatives (e.g. safer name/phrase/visual).
"""

    def _past_rejections_block(self) -> str:
        """Feed prior rejections back in so decisions stay consistent (learning)."""
        past = self.db.get_recent_compliance_rejections(limit=10)
        if not past:
            return ""
        lines = "\n".join(
            f"- {r.get('subject', '?')}: {r.get('verdict')} — {r.get('reasoning', '')[:160]}"
            for r in past
        )
        return (
            "PAST COMPLIANCE DECISIONS (stay consistent with these precedents):\n"
            f"{lines}\n\n"
        )
