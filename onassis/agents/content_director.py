"""Content Director agent.

The Director is the strategist. Once a day it produces a single **campaign
brief**: the unifying concept, theme, tone, audience, objective, visual
direction, and keywords that the Content Creator builds everything from.

In v0.1 this is LLM-generated. The Director composes the brief from:

* the **brand** identity (Local Celebrity — premium Mediterranean lifestyle)
* the brand's **content pillars** (from ``config.yaml``)
* the **current season** (so content feels timely)
* **previous campaigns** pulled from SQLite (so it never repeats itself)

The returned dict keeps the same core fields the database expects
(theme, tone, audience, objective, keywords, …) so persistence is
unchanged; richer fields (campaign concept, visual direction) ride along
in the stored JSON payload and feed the Creator.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from onassis.agents.base import BaseAgent
from onassis.llm import LLMClient

# JSON schema the brief is constrained to. (Structured outputs disallow
# length/array-size constraints, so we keep it to types + required fields.)
_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "campaign_name": {"type": "string"},
        "theme": {"type": "string"},
        "concept": {"type": "string"},
        "tone": {"type": "string"},
        "audience": {"type": "string"},
        "objective": {"type": "string"},
        "visual_direction": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "campaign_name",
        "theme",
        "concept",
        "tone",
        "audience",
        "objective",
        "visual_direction",
        "keywords",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the Content Director for a premium Mediterranean lifestyle brand. "
    "You think like the editor of a high-end travel and design magazine, not a "
    "marketer. Your daily campaign briefs are evocative, specific, and tasteful "
    "— they set a single strong creative direction the content team can run with. "
    "You never write like an advertisement: no hype, no hard selling, no clichés."
)


def _season(for_date: date) -> str:
    """Northern-hemisphere (Mediterranean) season for the given date."""
    month = for_date.month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


class ContentDirector(BaseAgent):
    """Produces the daily, LLM-generated campaign brief."""

    name = "ContentDirector"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        """Lazily-built LLM client (so a missing key only errors when used)."""
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    def run(self, *, for_date: date | None = None, **_: Any) -> dict[str, Any]:
        """Build (and persist) today's campaign brief.

        Args:
            for_date: Date the brief is for. Defaults to today.

        Returns:
            The brief dict, with ``id`` set to the new database row id.
        """
        for_date = for_date or date.today()
        brand = self.config.brand
        season = _season(for_date)
        pillars: list[str] = brand.get("content_pillars") or ["Mediterranean lifestyle"]
        recent = self._recent_campaign_summary()

        prompt = self._build_prompt(brand, season, pillars, recent, for_date)
        self.log.info("Requesting brief for %s (season=%s)", for_date.isoformat(), season)
        generated = self.llm.generate_json(
            system=_SYSTEM, prompt=prompt, schema=_BRIEF_SCHEMA
        )

        # Merge the LLM output with metadata into the brief dict. Core columns
        # (theme/tone/audience/objective/keywords) map straight to the DB;
        # the extra fields are preserved in the stored JSON payload.
        brief: dict[str, Any] = {
            "brief_date": for_date.isoformat(),
            "season": season,
            "brand": brand.get("name", "Local Celebrity"),
            "tagline": brand.get("tagline", ""),
            "campaign_name": generated["campaign_name"],
            "theme": generated["theme"],
            "concept": generated["concept"],
            "tone": generated["tone"],
            "audience": generated["audience"],
            "objective": generated["objective"],
            "visual_direction": generated["visual_direction"],
            "keywords": generated["keywords"],
        }

        self.log.info("Brief: %r (%s)", brief["campaign_name"], brief["theme"])
        brief["id"] = self.db.insert_brief(brief)
        return brief

    def _recent_campaign_summary(self) -> str:
        """A compact list of recent campaigns, for the 'avoid repetition' rule."""
        recent = self.db.get_recent_briefs(limit=30)
        if not recent:
            return "None yet — this is the first campaign."
        lines = []
        for b in recent:
            payload_name = b.get("theme", "")
            # campaign_name lives in the JSON payload; fall back to theme.
            name = ""
            try:
                name = json.loads(b.get("payload", "{}")).get("campaign_name", "")
            except (TypeError, ValueError):
                pass
            label = name or payload_name
            lines.append(f"- {b.get('brief_date', '?')}: {label} (theme: {b.get('theme', '?')})")
        return "\n".join(lines)

    def _build_prompt(
        self,
        brand: dict[str, Any],
        season: str,
        pillars: list[str],
        recent: str,
        for_date: date,
    ) -> str:
        pillars_block = "\n".join(f"- {p}" for p in pillars)
        return f"""Create today's content campaign brief for the brand below.

BRAND
- Name: {brand.get('name', 'Local Celebrity')}
- Positioning: {brand.get('positioning', 'premium Mediterranean lifestyle')}
- Tagline: {brand.get('tagline', '')}
- Voice: {brand.get('tone', 'aspirational, warm, refined')}
- Audience: {brand.get('target_audience', '')}

CONTENT PILLARS (choose ONE to anchor today, or blend two thoughtfully):
{pillars_block}

CONTEXT
- Date: {for_date.isoformat()}
- Season: {season} on the Mediterranean — let the season shape the mood, light, and references.

RECENT CAMPAIGNS (do NOT repeat these themes, concepts, or angles — go somewhere fresh):
{recent}

REQUIREMENTS
- Land on ONE distinctive campaign concept with a strong point of view.
- It must feel like premium Mediterranean lifestyle storytelling — editorial and emotive, never an advert.
- `campaign_name`: a short, evocative title.
- `theme`: the core subject in a few words.
- `concept`: 2-3 sentences describing the creative idea and the feeling it should evoke.
- `tone`: the voice for today's content.
- `audience`: who this speaks to.
- `objective`: the strategic goal (e.g. saves, profile visits, brand affinity) in one line.
- `visual_direction`: the look — light, palette, settings, styling — to guide image creation.
- `keywords`: 5-8 concrete, on-theme keywords.
"""
