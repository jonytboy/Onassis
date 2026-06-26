"""Publisher agent — PLACEHOLDER for a future version.

In a later version this agent will push approved content to Pinterest,
Instagram, and Facebook via their APIs. For v0.1 it deliberately does
nothing but log its intent, so the daily pipeline has a clearly defined
slot to extend without any half-working network code shipping early.

The interface (``run``) is already in place, so wiring in real
publishing later won't change how the orchestrator calls it.
"""

from __future__ import annotations

from typing import Any

from onassis.agents.base import BaseAgent


class Publisher(BaseAgent):
    """No-op publisher. Returns how many items it *would* publish."""

    name = "Publisher"

    def run(self, *, brief_id: int | None = None, **_: Any) -> dict[str, Any]:
        """Pretend to publish; in v0.1 publishing is intentionally disabled."""
        pending = 0
        if brief_id is not None:
            pending = len(self.db.get_content_for_brief(brief_id))

        self.log.info(
            "Publishing is disabled in v0.1 — %d draft item(s) left untouched.", pending
        )
        return {"published": 0, "skipped": pending, "enabled": False}
