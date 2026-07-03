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

**Autonomous verdicts.** The production pipeline has no human to answer a
request for more information, so the Director speaks only three verdicts:

* ``APPROVE`` — clean, proceed.
* ``APPROVE_WITH_CHANGES`` — medium risk: approved *provided* the stated
  amendments are applied. The producer must apply them and re-run compliance.
* ``REJECT`` — high risk / off-brand: a hard veto.

:meth:`resolve` drives a subject to a terminal decision: it re-generates with
the required corrections and re-reviews, terminating only on approval, a
rejection, or a **bounded** number of failed amendment attempts — the pipeline
never stalls waiting for input.
"""

from __future__ import annotations

from typing import Any, Callable

from onassis.config import Config
from onassis.database import Database
from onassis.llm import LLMClient
from onassis.logger import get_logger
from onassis.proposals import (
    APPROVE, APPROVE_WITH_CHANGES, REJECT, Proposal,
)

log = get_logger(__name__)

_DEFAULTS = {
    "high_risk_threshold": 70,
    "medium_risk_threshold": 40,
    "brand_consistency_min": 70,
    "max_remediation_attempts": 3,
}

# Compliance exists to maximise SAFE REVENUE, not to minimise theoretical legal
# risk. Only a concrete violation in one of these categories blocks a listing.
# ``incomplete_copy`` is a commercial QUALITY block: truncated / unfinished
# customer-facing copy makes a listing look broken, so it blocks and is fixed by
# regenerating (never a hard reject).
_BLOCKING_CATEGORIES = {"copyright", "trademark", "prohibited_claim", "etsy_policy",
                        "illegal", "incomplete_copy"}
# Blocks that a fresh, amended design/copy cannot fix — regenerating won't help,
# so these are a hard REJECT rather than an auto-remediation.
_HARD_CATEGORIES = {"copyright", "trademark", "illegal"}
# Phrases marking a copy-completeness problem the LLM raised as an "advisory":
# promoted to a fixable blocking issue (unfinished copy must not ship).
_INCOMPLETE_HINTS = (
    "truncat", "incomplete", "cut off", "cut-off", "unfinished", "mid-sentence",
    "mid sentence", "mid-word", "mid word", "placeholder", "lorem ipsum",
    "appears to end", "abruptly",
)

_OUTCOME = {APPROVE: "pass", APPROVE_WITH_CHANGES: "pass_with_changes", REJECT: "blocked"}

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trademark_risk": {"type": "integer"},
        "copyright_risk": {"type": "integer"},
        "platform_risk": {"type": "integer"},
        "brand_consistency_score": {"type": "integer"},
        "reasoning": {"type": "string"},
        # CONCRETE violations that must block publishing (with the specific reason).
        "blocking_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},   # copyright|trademark|prohibited_claim|etsy_policy|illegal
                    "detail": {"type": "string"},
                    "fixable": {"type": "boolean"},    # can amended copy/design resolve it?
                },
                "required": ["category", "detail", "fixable"],
                "additionalProperties": False,
            },
        },
        # Everything else — logged, recommended, but NEVER blocks publishing.
        "advisories": {"type": "array", "items": {"type": "string"}},
        "corrections": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "trademark_risk", "copyright_risk", "platform_risk",
        "brand_consistency_score", "reasoning", "blocking_issues", "advisories",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the Commercial Compliance gatekeeper for a premium Mediterranean "
    "lifestyle brand. Your job is to MAXIMISE SAFE REVENUE — get original, "
    "policy-compliant products to market — not to minimise theoretical legal "
    "risk. You are decisive, not a cautious legal consultant.\n\n"
    "You BLOCK a listing ONLY for a concrete violation in one of these five "
    "categories, and you name the specific evidence in `blocking_issues`:\n"
    "  1. copyright — the artwork/text copies a specific protected work.\n"
    "  2. trademark — a real, named trademark conflict WITH concrete evidence "
    "(an actual brand/slogan), not a vague 'someone might own this'.\n"
    "  3. prohibited_claim — a false/unverifiable claim in the copy (e.g. "
    "'hand-blocked' when it is printed, medical/health claims).\n"
    "  4. etsy_policy — a concrete violation of Etsy policy.\n"
    "  5. illegal — genuinely illegal content.\n\n"
    "EVERYTHING ELSE is an `advisory`: a recommendation that is logged but does "
    "NOT prevent publishing — e.g. 'a trademark search is recommended', 'keep the "
    "artwork source files', 'confirm the font licence', 'state the production "
    "method', 'consider adding the mug capacity'. Never put these in "
    "`blocking_issues`. When in doubt, prefer an advisory over a block. Original, "
    "on-brand work with only advisories PASSES. Mark a blocking issue `fixable` "
    "when rewriting the copy/design would resolve it (e.g. a prohibited claim); "
    "copyright/trademark/illegal are not fixable by rewording."
)


def _clamp(value: Any, default: int = 50) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def format_compliance(report: dict[str, Any]) -> str:
    """Render a compliance report the commercial way: PASS + advisories, or BLOCKED."""
    outcome = report.get("outcome") or _OUTCOME.get(report.get("verdict", ""), "pass")
    advisories = report.get("advisories") or []
    blocking = report.get("blocking_issues") or []
    lines: list[str] = []
    if outcome == "blocked":
        lines.append("BLOCKED")
        lines.append("")
        lines.append("Blocking issues")
        for b in blocking:
            lines.append(f"• [{b.get('category')}] {b.get('detail')}")
        lines.append("")
        lines.append("Do not publish until resolved.")
        return "\n".join(lines)
    lines.append("PASS_WITH_CHANGES" if outcome == "pass_with_changes" else "PASS")
    if advisories:
        lines.append("")
        lines.append("Advisories")
        for a in advisories:
            lines.append(f"• {a}")
    lines.append("")
    lines.append("Proceed to publish.")
    return "\n".join(lines)


class ComplianceDirector:
    """Reviews subjects for risk, vetoes violations, and remembers decisions."""

    name = "Compliance"

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.thresholds = {**_DEFAULTS, **(config.compliance or {})}
        self.max_remediation_attempts = int(self.thresholds["max_remediation_attempts"])
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    # --- Public review entry points ---------------------------------

    def review_proposal(
        self, proposal: Proposal | dict[str, Any], proposal_id: int | None = None,
        *, extra_blocking: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Review a proposal. ``extra_blocking`` are deterministic blocking issues
        the caller detected (e.g. truncated/incomplete copy) — merged before the
        verdict so they drive regeneration just like an LLM-found violation."""
        p = proposal if isinstance(proposal, Proposal) else Proposal.from_dict(proposal)
        subject = (
            f"Proposed action by {p.agent_name}: {p.requested_action}. "
            f"Rationale: {p.reasoning}"
        )
        return self._review(subject, label=p.requested_action, proposal_id=proposal_id,
                            extra_blocking=extra_blocking)

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
        extra_blocking: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(subject)
        log.info("Compliance reviewing: %s", label)
        generated = self.llm.generate_json(system=_SYSTEM, prompt=prompt, schema=_SCHEMA)

        tm = _clamp(generated.get("trademark_risk", 0))
        cr = _clamp(generated.get("copyright_risk", 0))
        pl = _clamp(generated.get("platform_risk", 0))
        brand = _clamp(generated.get("brand_consistency_score", 100))
        blocking, advisories = self._classify(generated, tm, cr, pl, brand)
        # Deterministic blocking issues the caller found (e.g. truncated copy).
        for it in (extra_blocking or []):
            cat = str(it.get("category", "incomplete_copy")).strip().lower()
            fixable = False if cat in _HARD_CATEGORIES else bool(it.get("fixable", True))
            blocking.append({"category": cat, "detail": str(it.get("detail", "")).strip(),
                             "fixable": fixable})
        verdict = self._verdict(blocking)
        score = round(((100 - tm) + (100 - cr) + (100 - pl) + brand) / 4)
        # Only FIXABLE blocking issues drive regeneration; advisories never do.
        corrections = [b["detail"] for b in blocking if b["fixable"]]

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
            "outcome": _OUTCOME[verdict],
            "reasoning": generated.get("reasoning", ""),
            "blocking_issues": blocking,
            "advisories": advisories,
            "corrections": corrections,
        }
        report["id"] = self.db.insert_compliance_report(report)
        if advisories:
            log.info("Compliance advisories for %s: %s", label, "; ".join(advisories))
        log.info("Compliance %s for %s: %s (%d advisory/ies)",
                 verdict, label, _OUTCOME[verdict], len(advisories))
        return report

    def _classify(
        self, generated: dict[str, Any], tm: int, cr: int, pl: int, brand: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Split concerns into concrete BLOCKING violations vs ADVISORIES.

        A concern only blocks if it names a real violation in one of the five
        legal categories; anything else (recommendations, best practices, "confirm
        X") is an advisory that is logged but never prevents publishing.
        """
        blocking: list[dict[str, Any]] = []
        advisories: list[str] = []
        for item in (generated.get("blocking_issues") or []):
            cat = str(item.get("category", "")).strip().lower().replace("-", "_").replace(" ", "_")
            detail = str(item.get("detail", "")).strip()
            if cat in _BLOCKING_CATEGORIES:
                fixable = False if cat in _HARD_CATEGORIES else bool(item.get("fixable", True))
                blocking.append({"category": cat, "detail": detail, "fixable": fixable})
            elif detail:  # not a real legal category -> downgrade to advisory
                advisories.append(detail)
        advisories += [str(a).strip() for a in (generated.get("advisories") or []) if str(a).strip()]

        # Commercial fallback for pure risk-score responses (no explicit lists):
        # only HIGH concrete risk blocks; medium risk and off-brand are advisories.
        if not generated.get("blocking_issues") and not generated.get("advisories"):
            high = self.thresholds["high_risk_threshold"]
            medium = self.thresholds["medium_risk_threshold"]
            if cr >= high:
                blocking.append({"category": "copyright",
                                 "detail": f"High copyright-similarity risk ({cr}).",
                                 "fixable": False})
            if tm >= high:
                blocking.append({"category": "trademark",
                                 "detail": f"High trademark-conflict risk ({tm}).",
                                 "fixable": False})
            if pl >= high:
                blocking.append({"category": "etsy_policy",
                                 "detail": f"Likely Etsy-policy issue in the copy ({pl}).",
                                 "fixable": True})
            for name, sc in (("Trademark", tm), ("Copyright", cr), ("Platform", pl)):
                if medium <= sc < high:
                    advisories.append(
                        f"{name} caution ({sc}) — a manual review is recommended, not blocking.")
            if brand < self.thresholds["brand_consistency_min"]:
                advisories.append(
                    f"Brand consistency below target ({brand}); consider tightening the "
                    f"design to the brand (advisory, not blocking).")
            advisories += [str(c).strip() for c in (generated.get("corrections") or [])
                           if str(c).strip()]

        # Promote any advisory that flags unfinished/truncated copy to a FIXABLE
        # blocking issue — an incomplete listing must never ship.
        kept: list[str] = []
        for a in advisories:
            if any(h in a.lower() for h in _INCOMPLETE_HINTS):
                blocking.append({"category": "incomplete_copy", "detail": a, "fixable": True})
            else:
                kept.append(a)
        return blocking, kept

    def _verdict(self, blocking_issues: list[dict[str, Any]]) -> str:
        """Commercial gatekeeper verdict — never REQUEST_MORE_INFO.

        * A non-fixable blocking violation (copyright/trademark/illegal) → REJECT.
        * A fixable blocking violation (e.g. a prohibited claim) →
          APPROVE_WITH_CHANGES: the producer amends the copy and re-reviews.
        * No blocking violation → APPROVE (PASS) — advisories are logged, not a gate.
        """
        if any(not b["fixable"] for b in blocking_issues):
            return REJECT
        if blocking_issues:
            return APPROVE_WITH_CHANGES
        return APPROVE

    # --- Autonomous remediation loop --------------------------------

    def resolve(
        self,
        produce: "Callable[[list[str] | None], Any]",
        review: "Callable[[Any], dict[str, Any]]",
        *,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        """Drive a subject to a terminal compliance decision, autonomously.

        ``produce(corrections)`` builds (or amends) the artifact — ``corrections``
        is ``None`` on the first attempt, then the list of required amendments to
        apply. ``review(artifact)`` returns a compliance report.

        The loop applies APPROVE_WITH_CHANGES amendments and re-reviews, and
        terminates only on:

        * ``approved`` — a clean APPROVE, or
        * ``rejected`` — a hard REJECT, or
        * ``exhausted`` — still requiring changes after ``max_attempts`` (a bounded
          number of failed regeneration attempts).

        Returns ``{status, artifact, report, attempts, missing, history}``. There
        is no ``needs_info`` outcome — the pipeline never waits on a human.
        """
        limit = max_attempts or self.max_remediation_attempts
        corrections: list[str] | None = None
        history: list[dict[str, Any]] = []
        artifact: Any = None
        report: dict[str, Any] = {}
        for attempt in range(1, limit + 1):
            artifact = produce(corrections)
            report = review(artifact)
            verdict = report["verdict"]
            history.append({"attempt": attempt, "verdict": verdict,
                            "corrections": report.get("corrections", [])})
            if verdict == APPROVE:
                return {"status": "approved", "artifact": artifact, "report": report,
                        "attempts": attempt, "missing": [], "history": history}
            if verdict == REJECT:
                return {"status": "rejected", "artifact": artifact, "report": report,
                        "attempts": attempt, "missing": report.get("corrections", []),
                        "history": history}
            # APPROVE_WITH_CHANGES — state what's missing, amend, and re-review.
            corrections = report.get("corrections", []) or [report.get("reasoning", "")]
            log.info(
                "Compliance requires changes (attempt %d/%d) for '%s': %s — "
                "amending and re-reviewing.", attempt, limit,
                report.get("subject", "?"), "; ".join(corrections),
            )
        return {"status": "exhausted", "artifact": artifact, "report": report,
                "attempts": limit, "missing": report.get("corrections", []),
                "history": history}

    # --- Learning ---------------------------------------------------

    def _build_prompt(self, subject: str) -> str:
        return f"""Review this subject as a COMMERCIAL compliance gatekeeper: clear
original, policy-compliant products to publish. Block only concrete violations.

SUBJECT
{subject}

{self._past_rejections_block()}Return:
- `trademark_risk`, `copyright_risk`, `platform_risk` (0-100, higher = riskier) and
  `brand_consistency_score` (0-100, higher = better) — for context only.
- `reasoning`: a short explanation of your decision.
- `blocking_issues`: ONLY concrete violations that must stop publishing, each with a
  `category` (copyright | trademark | prohibited_claim | etsy_policy | illegal), a
  specific `detail` (name the evidence), and `fixable` (true if rewriting the
  copy/design resolves it). Leave EMPTY if there is no concrete violation.
- `advisories`: everything else — recommendations logged but that do NOT block
  publishing (e.g. "a trademark search is recommended", "keep artwork source
  files", "confirm the font licence", "state the production method"). Prefer an
  advisory over a block when in doubt.
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
