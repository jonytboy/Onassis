"""Tests for the Production Readiness audit (read-only; no decisions)."""

from __future__ import annotations

import pytest

from onassis.production_readiness import (
    DEV_ONLY,
    PARTIAL,
    READY,
    ProductionReadiness,
)


@pytest.fixture
def pr(config, db):
    # config fixture: AI key present, no Etsy/Pinterest credentials.
    return ProductionReadiness(config, db)


def test_credential_detection(pr):
    assert pr.has_ai is True            # test-key present in the config fixture
    assert pr.has_etsy is False         # no Etsy credentials in tests
    assert pr.has_pinterest is False


def test_report_shape(pr):
    report = pr.report()
    assert set(report) >= {
        "environment", "production_ready", "summary", "credentials",
        "modules", "subsystems", "blockers", "checklist",
    }
    s = report["summary"]
    assert s["modules_audited"] == len(report["modules"])
    assert (s["production_ready"] + s["partially_ready"]
            + s["development_only"] == s["modules_audited"])


def test_every_module_has_a_valid_status(pr):
    valid = {READY, PARTIAL, DEV_ONLY}
    for m in pr.audit_modules():
        assert m["status"] in valid
        # Anything not fully ready must explain itself with at least one blocker.
        if m["status"] != READY:
            assert m["blockers"], f"{m['name']} is {m['status']} but has no blocker"


def test_blockers_are_fully_specified(pr):
    for b in pr.report()["blockers"]:
        assert b["description"] and b["recommended_fix"]
        assert b["priority"] and b["estimated_effort"]
        assert "module" in b


def test_pinterest_is_development_only(pr):
    by = {m["module"]: m for m in pr.audit_modules()}
    pin = by["onassis/connectors/pinterest.py"]
    assert pin["status"] == DEV_ONLY
    assert any("fetch_metrics" in b["description"] for b in pin["blockers"])


def test_artwork_studio_generates_real_images(pr):
    by = {m["module"]: m for m in pr.audit_modules()}
    studio = by["onassis/artwork.py"]
    assert studio["status"] == READY
    assert "real" in studio["summary"].lower()
    # The Listing Factory no longer ships placeholder mock-up imagery.
    lf = by["onassis/listing_factory.py"]
    assert "placeholder" not in lf["summary"].lower()
    assert not any("placeholder" in b["description"].lower() for b in lf["blockers"])


def test_missing_etsy_credentials_blocks_etsy_modules(pr):
    by = {m["module"]: m for m in pr.audit_modules()}
    assert by["onassis/connectors/etsy.py"]["status"] == DEV_ONLY
    assert by["onassis/publishing.py"]["status"] == PARTIAL  # draft offline; live needs creds


def test_etsy_modules_ready_when_configured(config, db):
    config.etsy = {**config.etsy, "api_key": "k", "access_token": "t", "shop_id": "1"}
    pr = ProductionReadiness(config, db)
    by = {m["module"]: m for m in pr.audit_modules()}
    assert pr.has_etsy is True
    assert by["onassis/connectors/etsy.py"]["status"] == READY


def test_missing_ai_key_marks_llm_modules_dev_only(config, db):
    config.anthropic_api_key = None
    pr = ProductionReadiness(config, db)
    by = {m["module"]: m for m in pr.audit_modules()}
    assert by["onassis/llm.py"]["status"] == DEV_ONLY
    assert any(b["priority"] == "Critical" for b in by["onassis/llm.py"]["blockers"])


def test_subsystems_cover_the_nine_named(pr):
    names = {s["subsystem"] for s in pr.verify_subsystems()}
    assert names == {
        "Etsy reading", "Etsy publishing", "Pinterest publishing",
        "Analytics collection", "Revenue collection", "Compliance",
        "CEO", "Daily Cycle", "Operations Manager",
    }


def test_checklist_marks_match_readiness(pr):
    checklist = pr.checklist()
    for c in checklist:
        assert c["mark"] == ("✅" if c["ready"] else "❌")
    by = {c["item"]: c for c in checklist}
    assert by["AI provider (Anthropic API key)"]["ready"] is True
    assert by["Etsy read credentials"]["ready"] is False
    assert by["Pinterest OAuth credentials"]["ready"] is False


def test_not_production_ready_without_etsy(pr):
    # AI is present but Etsy is not -> the company cannot operate end-to-end.
    assert pr.report()["production_ready"] is False


def test_makes_no_commercial_decision(pr):
    # The audit only reports readiness; it must not emit commercial verdicts.
    report = pr.report()
    assert "verdict" not in report and "decision" not in report
    for m in report["modules"]:
        assert "verdict" not in m and "decision" not in m
    for s in report["subsystems"]:
        assert "verdict" not in s and "decision" not in s
