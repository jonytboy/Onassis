"""The ONASSIS Brain — predict and remember.

The Brain is the learning layer. For every campaign it forms a falsifiable
**hypothesis** about why the campaign will perform, the **variables** it's
effectively testing, a **predicted outcome**, a calibrated **confidence**
(0-100), the **success metrics** worth watching, and a concrete **future
recommendation** tied to a metric threshold. All of this is persisted to the
``knowledge`` table — the Brain's long-term memory.

In this sprint the Brain only *predicts and remembers*. It does **not** read
real analytics. But the design leaves a clean seam for that future: every
record carries ``status``, ``actual_outcome``, and ``observed_metrics``
columns, and :meth:`record_outcome` is the hook a future analytics layer will
call to fold real results back in and revise predictions automatically.

This is **not** an agent — it's a manager module (like CampaignManager), so
the agent roster is unchanged. It uses the LLM directly to reason.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.llm import LLMClient
from onassis.logger import get_logger

log = get_logger(__name__)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "variables": {"type": "array", "items": {"type": "string"}},
        "predicted_outcome": {"type": "string"},
        "confidence": {"type": "integer"},
        "success_metrics": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string"},
    },
    "required": [
        "hypothesis",
        "variables",
        "predicted_outcome",
        "confidence",
        "success_metrics",
        "recommendation",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the ONASSIS Brain — the growth-strategy mind behind a premium "
    "Mediterranean lifestyle brand. For each campaign you form a single, "
    "falsifiable hypothesis about why it will (or won't) perform, name the "
    "variables it effectively tests, predict the outcome, and give a calibrated "
    "confidence from 0 to 100 (be honest — not everything is 90%). You choose "
    "concrete, measurable success metrics and end with one actionable future "
    "recommendation tied to a specific metric threshold. Think like a sharp "
    "analyst: specific, testable, no fluff."
)


def _clamp_confidence(value: Any) -> int:
    """Coerce the model's confidence into a safe 0-100 integer."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 50


class OnassisBrain:
    """Generates and stores predictions (knowledge) for campaigns."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        """Lazily-built LLM client (so a missing key only errors when used)."""
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    # --- Predict & remember -----------------------------------------

    def generate_for_campaign(
        self, campaign: dict[str, Any], brief: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Form and persist a knowledge record for a campaign (idempotent).

        Args:
            campaign: A campaign dict (must include ``id``).
            brief: The campaign's brief, for richer context. If omitted, it's
                loaded from the database.

        Returns:
            The knowledge record (existing one if already present).
        """
        campaign_id = campaign["id"]
        existing = self.db.get_knowledge_for_campaign(campaign_id)
        if existing:
            return existing

        if brief is None:
            brief = self.db.get_brief(campaign.get("brief_id")) or {}

        prompt = self._build_prompt(campaign, brief)
        log.info("Predicting knowledge for campaign #%s", campaign_id)
        generated = self.llm.generate_json(
            system=_SYSTEM, prompt=prompt, schema=_SCHEMA
        )

        knowledge: dict[str, Any] = {
            "campaign_id": campaign_id,
            "hypothesis": generated["hypothesis"],
            "variables": generated["variables"],
            "predicted_outcome": generated["predicted_outcome"],
            "confidence": _clamp_confidence(generated["confidence"]),
            "success_metrics": generated["success_metrics"],
            "recommendation": generated["recommendation"],
            "status": "predicted",
            "actual_outcome": None,
            "observed_metrics": {},
        }
        knowledge["id"] = self.db.insert_knowledge(knowledge)
        log.info(
            "Knowledge #%s stored (confidence=%d%%)", knowledge["id"], knowledge["confidence"]
        )
        return knowledge

    def generate_missing(self) -> int:
        """Backfill knowledge for any campaigns that don't have it.

        Returns how many records were created. (Makes one LLM call per
        campaign, so it's an explicit operation — never run implicitly.)
        """
        pending = self.db.get_campaigns_without_knowledge()
        for campaign in pending:
            self.generate_for_campaign(campaign)
        if pending:
            log.info("Backfilled knowledge for %d campaign(s)", len(pending))
        return len(pending)

    # --- Read -------------------------------------------------------

    def get_for_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        """Read-only: the knowledge record for a campaign (no generation)."""
        return self.db.get_knowledge_for_campaign(campaign_id)

    def list_knowledge(self) -> list[dict[str, Any]]:
        """All knowledge records, newest first (read-only)."""
        return self.db.list_knowledge()

    # --- Future-analytics hook --------------------------------------

    def record_outcome(
        self,
        campaign_id: int,
        *,
        actual_outcome: str | None = None,
        observed_metrics: dict[str, Any] | None = None,
        revised_confidence: int | None = None,
        status: str = "validated",
    ) -> dict[str, Any]:
        """Fold a real-world result back into a prediction.

        This is the seam for a future analytics layer: when real metrics
        arrive, it calls this to record what actually happened, optionally
        revise the confidence, and mark the prediction validated/revised. No
        analytics is computed here — this only persists the update.

        Returns the updated knowledge record.

        Raises:
            ValueError: if the campaign has no knowledge record.
        """
        existing = self.db.get_knowledge_for_campaign(campaign_id)
        if existing is None:
            raise ValueError(f"No knowledge for campaign {campaign_id}")

        fields: dict[str, Any] = {"status": status}
        if actual_outcome is not None:
            fields["actual_outcome"] = actual_outcome
        if observed_metrics is not None:
            fields["observed_metrics"] = observed_metrics
        if revised_confidence is not None:
            fields["confidence"] = _clamp_confidence(revised_confidence)

        self.db.update_knowledge(existing["id"], fields)
        log.info("Recorded outcome for campaign #%s (status=%s)", campaign_id, status)
        updated = self.db.get_knowledge(existing["id"])
        assert updated is not None
        return updated

    # --- Helpers ----------------------------------------------------

    def _build_prompt(self, campaign: dict[str, Any], brief: dict[str, Any]) -> str:
        keywords = ", ".join(brief.get("keywords", []))
        return f"""Form your prediction for this campaign.

CAMPAIGN
- Name: {campaign.get('name', '')}
- Theme: {campaign.get('theme', '')}
- Story: {campaign.get('story', '')}

STRATEGY CONTEXT (from the brief)
- Concept: {brief.get('concept', '')}
- Audience: {brief.get('audience', '')}
- Objective: {brief.get('objective', '')}
- Season: {brief.get('season', '')}
- Keywords: {keywords}

Produce:
- `hypothesis`: ONE falsifiable sentence on why this campaign will perform
  (or underperform), and against what alternative.
- `variables`: the 3-6 things this campaign effectively tests (themes,
  formats, moods, hooks).
- `predicted_outcome`: what you expect to happen, concretely.
- `confidence`: an integer 0-100, honestly calibrated.
- `success_metrics`: 3-6 specific, measurable signals to monitor
  (e.g. Pinterest saves, outbound clicks, Instagram shares).
- `recommendation`: one next action tied to a concrete metric threshold
  (e.g. "If Pinterest saves beat the rolling average by 20%, generate three
  more campaigns around slow dining culture.").
"""
