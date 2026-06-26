"""Content Director agent.

The Director is the strategist. Once a day it produces a single
**content brief**: the theme, tone, audience, objective, and keywords
that the Content Creator will build everything else from.

v0.1 keeps this deterministic and dependency-free. The Director rotates
through the brand's content pillars (configured in ``config.yaml``) using
the day of the year, so successive days get different themes without any
external input. The whole brief is returned as a plain dict and also
persisted to SQLite, so a future LLM-backed Director can drop in by
simply changing how this dict is built.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from onassis.agents.base import BaseAgent

# Keyword seeds per pillar keep briefs concrete. Falls back to a generic
# set if a pillar isn't listed here, so adding pillars never breaks.
_PILLAR_KEYWORDS: dict[str, list[str]] = {
    "Slow mornings & mindful rituals": ["morning ritual", "slow living", "mindfulness", "coffee"],
    "Elevated home & interior design": ["interior design", "home decor", "minimalism", "styling"],
    "Travel for the soul": ["slow travel", "hidden gems", "wanderlust", "boutique stays"],
    "Conscious wardrobe staples": ["capsule wardrobe", "quiet luxury", "timeless style", "linen"],
    "Hosting & the art of the table": ["tablescape", "entertaining", "dinner party", "hosting"],
    "Wellness without the hustle": ["wellness", "balance", "self care", "rest"],
    "Small luxuries worth it": ["everyday luxury", "small joys", "quality", "treat yourself"],
}

_OBJECTIVES = [
    "Grow saves and shares by leading with genuinely useful, beautiful ideas.",
    "Deepen brand affinity through warm, aspirational storytelling.",
    "Drive profile visits with a clear, single call to action.",
    "Spark comments by asking the audience one thoughtful question.",
]


class ContentDirector(BaseAgent):
    """Produces the daily content brief."""

    name = "ContentDirector"

    def run(self, *, for_date: date | None = None, **_: Any) -> dict[str, Any]:
        """Build (and persist) today's content brief.

        Args:
            for_date: Date the brief is for. Defaults to today.

        Returns:
            The brief dict, with ``id`` set to the new database row id.
        """
        for_date = for_date or date.today()
        brand = self.config.brand
        pillars: list[str] = brand.get("content_pillars") or ["Lifestyle"]

        # Rotate deterministically so each day differs but is reproducible.
        day_index = for_date.toordinal()
        theme = pillars[day_index % len(pillars)]
        objective = _OBJECTIVES[day_index % len(_OBJECTIVES)]
        keywords = _PILLAR_KEYWORDS.get(theme, ["lifestyle", "inspiration", "design"])

        brief: dict[str, Any] = {
            "brief_date": for_date.isoformat(),
            "brand": brand.get("name", "Onassis"),
            "tagline": brand.get("tagline", ""),
            "theme": theme,
            "tone": brand.get("tone", "aspirational, warm"),
            "audience": brand.get("target_audience", "lifestyle enthusiasts"),
            "objective": objective,
            "keywords": keywords,
        }

        self.log.info("Brief for %s -> theme=%r", brief["brief_date"], theme)
        brief["id"] = self.db.insert_brief(brief)
        return brief
