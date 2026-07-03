"""Tests for the Operations Manager (health gate + reporting; no decisions)."""

from __future__ import annotations

import pytest

from onassis.operations import OperationsManager


@pytest.fixture
def ops(config, db):
    # config fixture provides an API key and healthy cash/budget defaults.
    return OperationsManager(config, db)


# --- Pre-flight checks ----------------------------------------------

def test_check_healthy_production(ops):
    result = ops.check("production")
    assert result["healthy"] is True
    names = {c["name"] for c in result["checks"]}
    assert {"etsy", "pinterest", "revenue", "analytics", "database", "exports",
            "ai_provider", "ai_budget", "cash_reserve"} <= names


def test_etsy_pinterest_down_is_warning_not_abort(ops):
    # No Etsy/Pinterest credentials in tests -> warn, never critical.
    result = ops.check("production")
    by = {c["name"]: c for c in result["checks"]}
    assert by["etsy"]["status"] == "warn" and by["etsy"]["critical"] is False
    assert by["pinterest"]["status"] == "warn" and by["pinterest"]["critical"] is False
    assert result["healthy"] is True


def test_revenue_engine_failure_is_critical(ops):
    class _Boom:
        def company_profit(self):
            raise RuntimeError("db gone")
    ops.revenue = _Boom()
    result = ops.check("production")
    by = {c["name"]: c for c in result["checks"]}
    assert by["revenue"]["status"] == "fail" and by["revenue"]["critical"] is True
    assert result["healthy"] is False


def test_missing_ai_key_critical_in_production_only(config, db):
    config.anthropic_api_key = None
    ops = OperationsManager(config, db)
    assert ops.check("production")["healthy"] is False    # AI provider critical
    assert ops.check("dry_run")["healthy"] is True        # AI not needed for a dry run


def test_low_cash_is_critical_in_production(config, db):
    config.policy = {**config.policy, "available_cash": 1000, "cash_reserve": 5000}
    ops = OperationsManager(config, db)
    by = {c["name"]: c for c in ops.check("production")["checks"]}
    assert by["cash_reserve"]["status"] == "fail"
    assert ops.check("production")["healthy"] is False


# --- Abort path -----------------------------------------------------

def test_run_aborts_on_critical_failure_and_notifies_ceo(config, db):
    config.policy = {**config.policy, "available_cash": 1000, "cash_reserve": 5000}
    ops = OperationsManager(config, db)
    result = ops.run("production")

    assert result["aborted"] is True
    assert result["status"] == "aborted"
    assert result["stages"] == []                        # cycle never ran
    report = result["operations_report"]
    assert report["ceo_notified"] is True
    assert report["system"]["overall_health"] == "critical"
    assert any("Cash reserve" in r for r in report["recommendations"])
    # The aborted report is persisted.
    assert db.get_latest_operations_report()["status"] == "aborted"


# --- Healthy run + report -------------------------------------------

def test_dry_run_produces_report(ops, db):
    result = ops.run("dry_run")
    assert result["aborted"] is False
    assert result["mode"] == "dry_run"
    assert len(result["stages"]) == 20

    report = result["operations_report"]
    assert set(report) >= {"system", "business", "recommendations", "preflight"}
    assert set(report["business"]) == {
        "campaigns_generated", "listings_built", "listings_published",
        "revenue_imported", "orders_imported", "profit_today", "ai_spend_today"}
    assert "Revenue sync successful." in report["recommendations"]
    # Persisted and retrievable.
    assert db.get_latest_operations_report()["mode"] == "dry_run"


def test_stage_failure_marks_report_degraded(ops):
    class _Boom:
        def top_recommendation(self):
            raise RuntimeError("optimiser down")
    ops.cycle.optimiser = _Boom()   # not a pre-flight check -> cycle runs, stage fails
    result = ops.run("dry_run")

    assert result["status"] == "completed_with_failures"
    report = result["operations_report"]
    assert report["system"]["errors"] >= 1
    assert report["system"]["overall_health"] == "degraded"


# --- Reads ----------------------------------------------------------

def test_status_and_report_reads(ops, db):
    assert ops.status()["status"] == "never_run"
    ops.run("dry_run")
    assert ops.status()["status"] in ("completed", "completed_with_failures")
    assert "business" in ops.report()


def test_check_makes_no_commercial_decision(ops):
    # The Operations Manager only reports health; it must not emit verdicts.
    result = ops.check("production")
    blob = str(result).upper()
    assert "APPROVE" not in blob and "REJECT" not in blob
