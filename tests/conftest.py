"""Shared pytest fixtures and test doubles for the ONASSIS suite.

The agents call the Anthropic API through :class:`onassis.llm.LLMClient`.
For unit tests we never touch the network — instead we inject a
:class:`FakeLLM` into each agent (the agents build their client lazily via a
``llm`` property backed by ``self._llm``, so setting ``_llm`` swaps it out).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from onassis.config import load_config
from onassis.database import Database


class FakeLLM:
    """Stand-in for :class:`onassis.llm.LLMClient`.

    Returns a canned response and records every call so tests can assert on
    the prompt/schema the agent built.
    """

    def __init__(self, response: dict[str, Any] | list[dict[str, Any]]) -> None:
        # A single dict is returned every call; a list is consumed in order and
        # then sticks on the last item (for testing multi-pass remediation loops).
        self._sequence = response if isinstance(response, list) else None
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self._sequence is not None:
            i = min(len(self.calls) - 1, len(self._sequence) - 1)
            return self._sequence[i]
        return self.response

    @property
    def last_prompt(self) -> str:
        assert self.calls, "generate_json was never called"
        return self.calls[-1]["prompt"]


@pytest.fixture
def config(tmp_path: Path):
    """A real Config (from config.yaml) pointed at a throwaway SQLite file."""
    cfg = load_config()
    cfg.db_path = tmp_path / "test.db"
    cfg.anthropic_api_key = "test-key"  # present so LLMClient construction wouldn't fail
    # Keep artwork generation tiny & fast in tests (real files, small pixels).
    cfg.image = {**(cfg.image or {}), "backend": "local", "master_px": 96,
                 "print_px": 96, "gallery_px": 96, "gallery_count": 9,
                 "max_attempts": 2}
    return cfg


@pytest.fixture
def db(config) -> Database:
    return Database(config.db_path)


@pytest.fixture
def sample_brief() -> dict[str, Any]:
    """A brief dict shaped exactly like ContentDirector produces."""
    return {
        "campaign_name": "Salt & Citrus Mornings",
        "theme": "Slow coastal breakfasts",
        "concept": "Unhurried mornings on a sun-warmed terrace.",
        "tone": "warm, editorial",
        "audience": "design-loving travellers",
        "objective": "Grow saves with genuinely useful, beautiful ideas.",
        "visual_direction": "soft morning light, linen, citrus, whitewashed stone",
        "keywords": ["slow mornings", "mediterranean", "citrus", "linen", "terrace"],
    }


def make_compliance_response(
    *, trademark=10, copyright=10, platform=10, brand=90,
    reasoning="Original, on-brand, low risk.", corrections=None,
    blocking_issues=None, advisories=None,
) -> dict[str, Any]:
    """Build a fake Compliance LLM response.

    ``corrections`` (legacy) are treated as FIXABLE prohibited-claim blocking
    issues — amendments that must be applied (they drive the remediation loop).
    Pass ``advisories`` for non-blocking notes, or ``blocking_issues`` explicitly.
    """
    corr = corrections or []
    if blocking_issues is None:
        blocking_issues = [{"category": "prohibited_claim", "detail": c, "fixable": True}
                           for c in corr]
    return {
        "trademark_risk": trademark,
        "copyright_risk": copyright,
        "platform_risk": platform,
        "brand_consistency_score": brand,
        "reasoning": reasoning,
        "corrections": corr,
        "blocking_issues": blocking_issues,
        "advisories": advisories or [],
    }


def compliance_advisory(*advisories, **scores) -> dict[str, Any]:
    """A PASS response carrying only advisories (no blocking issues)."""
    return make_compliance_response(advisories=list(advisories), **scores)


def compliance_block(category="copyright", detail="Copies a protected work.",
                     fixable=False, **scores) -> dict[str, Any]:
    """A response with one concrete blocking issue."""
    return make_compliance_response(
        blocking_issues=[{"category": category, "detail": detail, "fixable": fixable}],
        **scores)


def make_content_response(n_pin: int, n_ig: int, n_fb: int, n_img: int) -> dict[str, Any]:
    """Build a fake Content Creator response with the given counts."""
    return {
        "pinterest_posts": [
            {"title": f"Pin {i}", "description": f"desc {i}", "hashtags": ["#a", "#b"]}
            for i in range(n_pin)
        ],
        "instagram_captions": [
            {"caption": f"caption {i}", "hashtags": ["#x", "#y"]} for i in range(n_ig)
        ],
        "facebook_posts": [{"body": f"fb post {i}"} for i in range(n_fb)],
        "image_prompts": [
            {"title": f"Img {i}", "prompt": f"prompt {i}", "aspect_ratio": "4:5"}
            for i in range(n_img)
        ],
    }
