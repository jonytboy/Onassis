"""Analytics agent — PLACEHOLDER for a future version.

Eventually this agent will pull engagement metrics (impressions, saves,
clicks) back from each platform and feed insights to the Content Director
so briefs improve over time — closing the loop into a true autonomous
engine.

For v0.1 it only reports basic counts from the local database, which is
enough to prove the pipeline ran and to give the future implementation a
concrete interface to grow into.
"""

from __future__ import annotations

from typing import Any

from onassis.agents.base import BaseAgent


class AnalyticsAgent(BaseAgent):
    """Reports local content counts. External metrics come in a later version."""

    name = "AnalyticsAgent"

    def run(self, *, brief_id: int | None = None, **_: Any) -> dict[str, Any]:
        """Summarize what's in the database for this brief (and overall)."""
        per_brief = 0
        if brief_id is not None:
            per_brief = len(self.db.get_content_for_brief(brief_id))
        total = self.db.count_content()

        self.log.info(
            "Analytics (v0.1, local only): %d item(s) this brief, %d total stored.",
            per_brief,
            total,
        )
        return {
            "items_this_brief": per_brief,
            "items_total": total,
            "external_metrics": "not available in v0.1",
        }
