"""Content Creator agent.

The Creator is the producer. Given the Director's campaign brief it
generates platform-ready content with the LLM and persists it to SQLite.
The counts come from ``config.yaml -> content_targets``, so changing the
numbers changes the output with no code change.

Per brief it produces:
    * 5 Pinterest posts
    * 3 Instagram captions
    * 2 Facebook posts
    * 3 cinematic image prompts

Everything is written to read like premium Mediterranean lifestyle
editorial — evocative, specific, and never like an advertisement.
"""

from __future__ import annotations

from typing import Any

from onassis.agents.base import BaseAgent
from onassis.llm import LLMClient

_SYSTEM = (
    "You are the Content Creator for a premium Mediterranean lifestyle brand. "
    "You write like a top-tier travel and design magazine — sensory, specific, "
    "and effortlessly elegant. Every line should evoke sun, sea, stone, linen, "
    "and slow living. Absolutely no advertising voice: no hype, no exclamation "
    "spam, no 'shop now', no generic filler. Show, don't sell."
)


def _content_schema() -> dict[str, Any]:
    """JSON schema for the full content set the Creator returns."""
    str_array = {"type": "array", "items": {"type": "string"}}
    pinterest = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "hashtags": str_array,
        },
        "required": ["title", "description", "hashtags"],
        "additionalProperties": False,
    }
    instagram = {
        "type": "object",
        "properties": {
            "caption": {"type": "string"},
            "hashtags": str_array,
        },
        "required": ["caption", "hashtags"],
        "additionalProperties": False,
    }
    facebook = {
        "type": "object",
        "properties": {"body": {"type": "string"}},
        "required": ["body"],
        "additionalProperties": False,
    }
    image_prompt = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "aspect_ratio": {"type": "string"},
        },
        "required": ["title", "prompt", "aspect_ratio"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "pinterest_posts": {"type": "array", "items": pinterest},
            "instagram_captions": {"type": "array", "items": instagram},
            "facebook_posts": {"type": "array", "items": facebook},
            "image_prompts": {"type": "array", "items": image_prompt},
        },
        "required": [
            "pinterest_posts",
            "instagram_captions",
            "facebook_posts",
            "image_prompts",
        ],
        "additionalProperties": False,
    }


class ContentCreator(BaseAgent):
    """Turns a campaign brief into LLM-generated platform content."""

    name = "ContentCreator"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    def run(self, *, brief: dict[str, Any], **_: Any) -> list[dict[str, Any]]:
        """Generate and persist all content for ``brief``.

        Args:
            brief: A brief dict produced by :class:`ContentDirector`
                (must include an ``id``).

        Returns:
            The list of content-item dicts that were stored.
        """
        brief_id = brief["id"]
        targets = self.config.content_targets
        n_pin = targets.get("pinterest_posts", 5)
        n_ig = targets.get("instagram_captions", 3)
        n_fb = targets.get("facebook_posts", 2)
        n_img = targets.get("image_prompts", 3)

        prompt = self._build_prompt(brief, n_pin, n_ig, n_fb, n_img)
        self.log.info("Requesting content for brief #%s", brief_id)
        generated = self.llm.generate_json(
            system=_SYSTEM, prompt=prompt, schema=_content_schema()
        )

        items = self._map_to_items(generated)
        self._warn_if_off_target(generated, n_pin, n_ig, n_fb, n_img)

        self.db.insert_content_items(brief_id, items)
        self.log.info("Generated %d item(s) for brief #%s", len(items), brief_id)
        return items

    # --- Mapping ----------------------------------------------------
    # Convert the LLM's structured output into the content-item shape the
    # database layer expects (platform, content_type, title, body, metadata).

    def _map_to_items(self, generated: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        for p in generated.get("pinterest_posts", []):
            items.append(
                {
                    "platform": "pinterest",
                    "content_type": "post",
                    "title": p.get("title"),
                    "body": p.get("description", ""),
                    "metadata": {"hashtags": p.get("hashtags", [])},
                }
            )
        for c in generated.get("instagram_captions", []):
            items.append(
                {
                    "platform": "instagram",
                    "content_type": "caption",
                    "title": None,
                    "body": c.get("caption", ""),
                    "metadata": {"hashtags": c.get("hashtags", [])},
                }
            )
        for f in generated.get("facebook_posts", []):
            items.append(
                {
                    "platform": "facebook",
                    "content_type": "post",
                    "title": None,
                    "body": f.get("body", ""),
                    "metadata": {},
                }
            )
        for img in generated.get("image_prompts", []):
            items.append(
                {
                    "platform": "image",
                    "content_type": "image_prompt",
                    "title": img.get("title"),
                    "body": img.get("prompt", ""),
                    "metadata": {"aspect_ratio": img.get("aspect_ratio", "4:5")},
                }
            )
        return items

    def _warn_if_off_target(
        self, generated: dict[str, Any], n_pin: int, n_ig: int, n_fb: int, n_img: int
    ) -> None:
        """Log if the model returned a different count than requested."""
        for key, want in (
            ("pinterest_posts", n_pin),
            ("instagram_captions", n_ig),
            ("facebook_posts", n_fb),
            ("image_prompts", n_img),
        ):
            got = len(generated.get(key, []))
            if got != want:
                self.log.warning("%s: requested %d, got %d", key, want, got)

    # --- Prompt -----------------------------------------------------

    def _build_prompt(
        self, brief: dict[str, Any], n_pin: int, n_ig: int, n_fb: int, n_img: int
    ) -> str:
        keywords = ", ".join(brief.get("keywords", []))
        return f"""Create today's social content from this campaign brief.

CAMPAIGN BRIEF
- Campaign: {brief.get('campaign_name', '')}
- Theme: {brief.get('theme', '')}
- Concept: {brief.get('concept', '')}
- Tone: {brief.get('tone', '')}
- Audience: {brief.get('audience', '')}
- Objective: {brief.get('objective', '')}
- Season: {brief.get('season', '')}
- Visual direction: {brief.get('visual_direction', '')}
- Keywords: {keywords}

PRODUCE EXACTLY:
- {n_pin} Pinterest posts — each a saveable idea. `title` is a crisp,
  search-friendly hook; `description` is 1-2 evocative sentences; `hashtags`
  are 4-6 relevant, lowercase tags.
- {n_ig} Instagram captions — each `caption` opens with a strong first line,
  tells a small story or shares a feeling, and ends with a gentle, genuine
  invitation to engage (not a hard CTA). Include 5-8 tasteful `hashtags`.
- {n_fb} Facebook posts — each `body` is a slightly longer, warmer micro-story
  (2-4 sentences) that suits the Facebook audience.
- {n_img} cinematic image prompts — each `prompt` is a vivid, photographic
  brief for an image generator (composition, light, subject, mood, lens feel),
  faithful to the visual direction, with no text or logos; set a fitting
  `aspect_ratio` (e.g. "4:5" or "9:16") and a short `title`.

STYLE RULES
- Premium Mediterranean lifestyle editorial. Sensory and specific.
- Never sound like an advertisement. No "shop now", no hype, no emoji spam
  (at most one tasteful emoji per caption, and only if it truly fits).
- Vary openings and structure across pieces — no repetition.
"""
