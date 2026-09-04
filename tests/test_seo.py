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


def test_type_conflict_flags_wrong_garment():
    from onassis.seo import type_conflict
    # A hoodie the model relabelled a sweatshirt/crewneck (the real bug we saw).
    assert type_conflict("Embroidered Sunset Sweatshirt, Cozy Crewneck",
                         "heavyweight_hoodie")
    # A sweatshirt titled as a hoodie.
    assert type_conflict("Cozy Hoodie Pullover", "sweatshirt")
    # A hoodie titled as a tee.
    assert type_conflict("Mediterranean Tee, Soft Cotton Shirt", "heavyweight_hoodie")


def test_type_conflict_passes_correct_garment():
    from onassis.seo import type_conflict
    assert type_conflict("Heavyweight Hoodie, Sunset Pullover", "heavyweight_hoodie") == ""
    assert type_conflict("Mediterranean Sweatshirt, Cozy Crewneck", "sweatshirt") == ""
    assert type_conflict("Lemon T-Shirt, Cotton Tee", "premium_tshirt") == ""


def test_type_conflict_ignores_non_apparel():
    from onassis.seo import type_conflict
    assert type_conflict("Anything at all here", "ceramic_mug") == ""
    assert type_conflict("Coastal Print Wall Art", "premium_poster") == ""


def test_has_word_is_whole_word():
    from onassis.seo import _has_word
    assert _has_word("tee", "cotton tee shirt") is True
    assert _has_word("tee", "canteen menu") is False
    assert _has_word("t-shirt", "lemon t-shirt, cotton") is True


def test_type_conflict_uses_old_title_when_key_unknown():
    from onassis.seo import type_conflict
    # A duplicate listing whose product_key drifted (not a known apparel key),
    # but the ORIGINAL title says "Heavyweight Hoodie" — a new "Sweatshirt /
    # Crewneck" title must STILL be caught via the old-title fallback.
    assert type_conflict(
        "Sunset Embroidered Sweatshirt, Muted Stone Pullover, Brushed Cotton Crewneck",
        "legacy-riviera-123",
        old_title="Riviera Sunset Heavyweight Hoodie in Muted Stone") != ""
    # A correct hoodie title passes even with the drifted key.
    assert type_conflict(
        "Heavyweight Hoodie, Sunset Pullover Hoodie",
        "legacy-riviera-123",
        old_title="Riviera Sunset Heavyweight Hoodie") == ""
