"""Tests for the Design Package Builder (CEO + compliance gated; no assets)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from onassis.design_package import DesignPackageBuilder, DesignPackageError
from onassis.opportunities import OpportunityEngine
from tests.conftest import FakeLLM, make_compliance_response


def _opp_item(**overrides: Any) -> dict[str, Any]:
    base = {
        "theme": "Slow coastal mornings", "target_customer": "design-loving travellers",
        "emotional_angle": "calm, unhurried luxury", "product_type": "t-shirt",
        "search_intent": "mediterranean lifestyle tee", "seasonal_relevance": "summer",
        "commercial_score": 85, "originality_score": 80, "brand_fit_score": 90,
        "estimated_demand": 80, "estimated_competition": 30, "confidence": 80,
        "product_name": "Amalfi Morning Tee",
        "concept": "A linen-soft tee evoking slow Amalfi mornings.",
        "colour_palette": ["citrus", "whitewash", "sea blue"],
        "typography_style": "elegant serif", "illustration_style": "watercolour",
        "photography_style": "morning light", "mockup_style": "terrace flatlay",
    }
    base.update(overrides)
    return base


_DESIGN = {
    "shirt_colour": "natural/ecru", "print_colour": "terracotta + sea blue",
    "typography_direction": "humanist serif, lowercase, relaxed spacing",
    "layout_direction": "centred stacked lockup with a fine line motif",
    "print_placement": "centre chest", "print_size_guidance": "approx 25cm wide, centred",
    "artwork_description": "A minimal line-drawn lemon branch over a calm horizon.",
    "mockup_scene": "folded tee on sunlit linen beside fresh lemons",
    "design_rationale": "Quiet, premium and on-theme for slow Mediterranean mornings.",
    "listing_title_seed": "Amalfi Morning Lemon Tee — Slow Coastal Living",
    "listing_tags_seed": ["mediterranean", "lemon", "coastal", "slow living"],
    "listing_description_seed": "A calm, sun-warmed tee for unhurried mornings.",
}


def _seed_opportunity(config, db, *, item=None) -> str:
    """Create one opportunity and return its id."""
    eng = OpportunityEngine(config, db)
    eng._llm = FakeLLM({"opportunities": [item or _opp_item()]})
    return eng.generate(1)["opportunities"][0]["opportunity_id"]


def _builder(config, db, *, compliance=None) -> DesignPackageBuilder:
    b = DesignPackageBuilder(config, db)
    b._llm = FakeLLM(_DESIGN)
    b.compliance._llm = FakeLLM(compliance or make_compliance_response())
    return b


# --- Happy path -----------------------------------------------------

def test_build_writes_all_six_files(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    result = _builder(config, db).build(oid)

    assert result["status"] == "ready"
    folder = Path(result["path"])
    for name in ("design_brief.json", "print_spec.json", "artwork_prompt.txt",
                 "mockup_prompt.txt", "listing_seed.json", "compliance_report.json"):
        assert (folder / name).exists(), f"missing {name}"
    # Lives under exports/opportunities/<id>/
    assert folder.parent.name == "opportunities"
    assert folder.name == oid


def test_design_brief_has_every_required_field(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    brief = _builder(config, db).build(oid)["design_brief"]

    for field in (
        "product_name", "target_customer", "emotional_angle", "shirt_colour",
        "print_colour", "typography_direction", "layout_direction",
        "print_placement", "print_size_guidance", "file_format_requirements",
        "transparent_background_required", "dpi_requirement", "safe_margin_guidance",
        "gelato_compatibility_notes",
    ):
        assert field in brief and brief[field] not in (None, ""), f"missing {field}"
    # Carried from the opportunity.
    assert brief["product_name"] == "Amalfi Morning Tee"
    assert brief["target_customer"] == "design-loving travellers"
    # Deterministic print spec.
    assert brief["transparent_background_required"] is True
    assert brief["dpi_requirement"] == 300


def test_print_spec_and_seed_and_prompts_are_consistent(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    result = _builder(config, db).build(oid)

    spec = result["print_spec"]
    assert spec["dpi_requirement"] == 300 and spec["transparent_background_required"]
    assert spec["shirt_colour"] == "natural/ecru"
    # listing_seed is SEED material, not a listing.
    seed = result["listing_seed"]
    assert seed["title_seed"] and seed["tags_seed"]
    assert "future listing" in seed["note"].lower()
    # The mock-up file is a PROMPT, explicitly not a mock-up.
    assert "future mock-up generator" in result["mockup_prompt"].lower()
    assert "transparent background" in result["artwork_prompt"].lower()


# --- Gates ----------------------------------------------------------

def test_ceo_rejection_blocks_the_package(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    # A poor opportunity the CEO will reject (negative net profit).
    poor = _opp_item(product_name="Poor Tee", commercial_score=5, originality_score=5,
                     brand_fit_score=5, estimated_demand=5, estimated_competition=95,
                     confidence=20)
    oid = _seed_opportunity(config, db, item=poor)
    result = _builder(config, db).build(oid)

    assert result["status"] == "blocked"
    assert "not CEO-approved" in result["reason"]
    assert result["ceo"]["verdict"] != "APPROVE"
    # Nothing was written.
    assert not (Path(tmp_path) / "opportunities" / oid).exists()


def test_compliance_rejection_blocks_export(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    # High trademark risk -> compliance veto.
    builder = _builder(config, db, compliance=make_compliance_response(trademark=95))
    result = builder.build(oid)

    assert result["status"] == "blocked"
    assert "compliance" in result["reason"].lower()
    assert not (Path(tmp_path) / "opportunities" / oid).exists()


def test_compliance_reviews_before_export(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    builder = _builder(config, db)
    builder.build(oid)
    # The brief (not just the opportunity) was the compliance subject.
    assert builder.compliance._llm.calls, "compliance was not consulted"
    assert "Amalfi Morning Tee" in builder.compliance._llm.last_prompt


# --- Autonomous compliance remediation (no human in the loop) -------

def test_compliance_amendments_are_applied_then_approved(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    builder = _builder(config, db)
    # First review needs changes; the amended design is clean.
    builder.compliance._llm = FakeLLM([
        make_compliance_response(platform=55,
                                 corrections=["Remove the lemon-brand reference"]),
        make_compliance_response(),
    ])
    result = builder.build(oid)

    assert result["status"] == "ready"
    assert result["compliance_attempts"] == 2
    # The design was REGENERATED with the required amendment fed back in.
    assert len(builder._llm.calls) == 2
    assert "Remove the lemon-brand reference" in builder._llm.calls[1]["prompt"]
    assert (Path(tmp_path) / "opportunities" / oid / "design_brief.json").exists()


def test_compliance_regeneration_is_bounded_then_blocks(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    config.compliance = {**(config.compliance or {}), "max_remediation_attempts": 2}
    oid = _seed_opportunity(config, db)
    # Every pass still needs changes — the loop is bounded, then blocks.
    builder = _builder(config, db, compliance=make_compliance_response(
        platform=55, corrections=["still too close to a trademark"]))
    result = builder.build(oid)

    assert result["status"] == "blocked"
    assert result["compliance_attempts"] == 2                 # bounded, never hangs
    assert result["missing"] == ["still too close to a trademark"]
    assert "changes" in result["reason"].lower()
    assert not (Path(tmp_path) / "opportunities" / oid).exists()   # nothing written


# --- Reads + errors -------------------------------------------------

def test_get_package_round_trip(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    _builder(config, db).build(oid)

    pkg = DesignPackageBuilder(config, db).get_package(oid)
    assert pkg is not None and pkg["status"] == "ready"
    assert pkg["design_brief"]["product_name"] == "Amalfi Morning Tee"
    assert pkg["print_spec"]["dpi_requirement"] == 300


def test_get_package_missing_returns_none(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    assert DesignPackageBuilder(config, db).get_package("OPP-none") is None


def test_unknown_opportunity_raises(config, db):
    with pytest.raises(DesignPackageError):
        _builder(config, db).build("OPP-does-not-exist")


def test_selected_opportunity_marked_after_build(config, db, tmp_path):
    config.design = {**config.design, "exports_dir": str(tmp_path)}
    oid = _seed_opportunity(config, db)
    _builder(config, db).build(oid)
    assert db.get_opportunity(oid)["status"] == "selected"
