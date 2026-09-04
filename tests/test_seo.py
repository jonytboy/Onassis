"""Tests for onassis.seo — Etsy title/tag normalisation and build_seo."""

from __future__ import annotations

import json

from onassis.seo import (_SCHEMA, ETSY_TAG_MAX, ETSY_TITLE_MAX, build_seo,
                         clip_title, normalize_tags)


def test_schema_has_no_maxitems():
    """Anthropic's structured-output schema rejects maxItems on arrays (400).
    The tag cap is enforced in normalize_tags instead — keep the schema clean."""
    assert "maxItems" not in json.dumps(_SCHEMA)


def test_clip_title_keeps_short_titles():
    assert clip_title("Greek Island Print") == "Greek Island Print"


def test_clip_title_trims_to_limit_on_a_phrase_boundary():
    t = clip_title("Mediterranean Wall Art, " * 20)          # way over 140
    assert len(t) <= ETSY_TITLE_MAX
    assert not t.endswith(",")                                # no dangling comma
    assert t.endswith("Art")                                  # cut on a boundary


def test_clip_title_collapses_whitespace():
    assert clip_title("Greek    Island\n Print") == "Greek Island Print"


def test_normalize_tags_dedupes_caps_and_length():
    tags = normalize_tags([
        "Greek Island Art", "greek island art",               # dup (case)
        "x" * (ETSY_TAG_MAX + 5),                             # too long -> dropped
        "  coastal  wall  decor  ",                           # whitespace collapsed
        "", "  ",                                             # empties dropped
    ])
    assert tags == ["greek island art", "coastal wall decor"]


def test_normalize_tags_caps_at_thirteen():
    tags = normalize_tags([f"tag number {i}" for i in range(20)])
    assert len(tags) == 13


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_json(self, *, system, prompt, schema):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        return self.payload


def test_build_seo_normalises_model_output():
    llm = _FakeLLM({
        "title": "Mediterranean Wall Art, Greek Island Print, " + "Extra Phrase, " * 20,
        "tags": ["greek island art", "GREEK ISLAND ART", "coastal decor",
                 "z" * 40] + [f"filler tag {i}" for i in range(20)],
    })
    out = build_seo({"product_type": "Wall Art Print",
                     "subject": "Greek island coastal scene"}, llm)
    assert len(out["title"]) <= ETSY_TITLE_MAX
    assert out["title"].startswith("Mediterranean Wall Art")
    assert len(out["tags"]) <= 13
    assert out["tags"][0] == "greek island art"
    assert "greek island art" == out["tags"][0] and out["tags"].count("greek island art") == 1
    # The product context reached the model prompt.
    assert "Wall Art Print" in llm.calls[0]["prompt"]
