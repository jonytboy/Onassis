"""Content Creator agent.

The Creator is the producer. Given a brief from the Content Director it
generates platform-ready content and persists it to SQLite. The exact
counts come from ``config.yaml -> content_targets`` so editing the config
changes the output volume with no code change.

For v0.1 the generation is template-based and deterministic — it needs no
API key and runs instantly. The :meth:`_generate_*` helpers are the only
place that "writes copy", so swapping in a real LLM later means replacing
those methods and nothing else.
"""

from __future__ import annotations

from typing import Any

from onassis.agents.base import BaseAgent


class ContentCreator(BaseAgent):
    """Turns a content brief into Pinterest/Instagram/Facebook/image content."""

    name = "ContentCreator"

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

        items: list[dict[str, Any]] = []
        items += self._generate_pinterest(brief, targets.get("pinterest_posts", 5))
        items += self._generate_instagram(brief, targets.get("instagram_captions", 3))
        items += self._generate_facebook(brief, targets.get("facebook_posts", 2))
        items += self._generate_image_prompts(brief, targets.get("image_prompts", 3))

        self.db.insert_content_items(brief_id, items)
        self.log.info("Generated %d item(s) for brief #%s", len(items), brief_id)
        return items

    # --- Generators -------------------------------------------------
    # Each returns a list of content-item dicts in the shape the database
    # layer expects: platform, content_type, title, body, metadata.

    def _hashtags(self, brief: dict[str, Any]) -> list[str]:
        """Derive hashtags from the brief's keywords."""
        tags = ["#" + kw.replace(" ", "").lower() for kw in brief.get("keywords", [])]
        brand = brief.get("brand", "Onassis").replace(" ", "")
        return [f"#{brand.lower()}", *tags]

    def _generate_pinterest(self, brief: dict[str, Any], count: int) -> list[dict[str, Any]]:
        theme = brief["theme"]
        keywords = brief.get("keywords", ["lifestyle"])
        angles = [
            "A step-by-step guide to",
            "5 ideas for",
            "The minimalist's take on",
            "How to style",
            "A cozy checklist for",
            "Before & after:",
            "The little luxuries of",
        ]
        items = []
        for i in range(count):
            angle = angles[i % len(angles)]
            kw = keywords[i % len(keywords)]
            items.append(
                {
                    "platform": "pinterest",
                    "content_type": "post",
                    "title": f"{angle} {theme.lower()}",
                    "body": (
                        f"{angle} {theme.lower()} — featuring {kw}. "
                        f"Save this pin for later and make every day feel a little more intentional."
                    ),
                    "metadata": {"hashtags": self._hashtags(brief), "board": theme},
                }
            )
        return items

    def _generate_instagram(self, brief: dict[str, Any], count: int) -> list[dict[str, Any]]:
        theme = brief["theme"]
        tone = brief.get("tone", "warm")
        hooks = [
            "Pause for a second.",
            "Here's your reminder:",
            "Some days call for this.",
            "We've been thinking about this all week.",
        ]
        items = []
        for i in range(count):
            hook = hooks[i % len(hooks)]
            items.append(
                {
                    "platform": "instagram",
                    "content_type": "caption",
                    "title": f"{theme} — caption {i + 1}",
                    "body": (
                        f"{hook} {theme} isn't about doing more — it's about doing it beautifully. "
                        f"What's one small thing making your day feel elevated? Tell us below. 🤍\n\n"
                        + " ".join(self._hashtags(brief))
                    ),
                    "metadata": {"tone": tone, "hashtags": self._hashtags(brief)},
                }
            )
        return items

    def _generate_facebook(self, brief: dict[str, Any], count: int) -> list[dict[str, Any]]:
        theme = brief["theme"]
        objective = brief.get("objective", "")
        items = []
        for i in range(count):
            items.append(
                {
                    "platform": "facebook",
                    "content_type": "post",
                    "title": f"{theme} — post {i + 1}",
                    "body": (
                        f"{theme}: our favourite way to bring a little more ease into the everyday. "
                        f"Tap through for the full story, and tell us how you make it your own. "
                        f"\n\n({objective})"
                    ),
                    "metadata": {"objective": objective},
                }
            )
        return items

    def _generate_image_prompts(self, brief: dict[str, Any], count: int) -> list[dict[str, Any]]:
        theme = brief["theme"]
        keywords = brief.get("keywords", ["lifestyle"])
        styles = [
            "soft natural light, film grain, muted earthy palette",
            "bright airy editorial, shallow depth of field, minimalist composition",
            "warm golden hour, cozy textures, lived-in elegance",
        ]
        items = []
        for i in range(count):
            kw = keywords[i % len(keywords)]
            style = styles[i % len(styles)]
            items.append(
                {
                    "platform": "image",
                    "content_type": "image_prompt",
                    "title": f"Image prompt {i + 1} — {theme}",
                    "body": (
                        f"A lifestyle photograph evoking '{theme}', featuring {kw}. "
                        f"{style}. No text, no logos, 4:5 aspect ratio, photorealistic."
                    ),
                    "metadata": {"style": style, "aspect_ratio": "4:5"},
                }
            )
        return items
