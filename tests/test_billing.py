"""Tests for the Billing monitor (per-provider spend + credit remaining)."""

from __future__ import annotations

from onassis.billing import BillingMonitor


def _spend(db, provider, cost, date):
    db.insert_ai_request({"request_date": date, "provider": provider, "model": "m",
                          "kind": "llm", "cost_usd": cost, "ok": True})


def test_summary_rolls_up_spend_per_provider(config, db):
    _spend(db, "openai", 2.0, "2026-08-01")
    _spend(db, "openai", 1.0, "2026-08-09")
    _spend(db, "anthropic", 0.5, "2026-08-09")
    s = BillingMonitor(config, db).summary()
    by = {r["provider"]: r for r in s["providers"]}
    assert by["openai"]["spend_total"] == 3.0
    assert by["anthropic"]["spend_total"] == 0.5
    assert s["spend_total"] == 3.5
    # Both known providers always appear, even with no credit set.
    assert by["openai"]["remaining"] is None


def test_set_credit_then_shows_remaining(config, db):
    _spend(db, "openai", 4.0, "2026-08-09")
    bm = BillingMonitor(config, db)
    bm.db.set_setting("billing.openai.since", "2026-08-01")   # count spend from here
    s = bm.set_credit("openai", 20.0, as_of="2026-08-01")
    openai = next(r for r in s["providers"] if r["provider"] == "openai")
    assert openai["credit"] == 20.0
    assert openai["remaining"] == 16.0        # 20 topped up − 4 spent since


def test_credit_is_persisted(config, db):
    BillingMonitor(config, db).set_credit("anthropic", 50.0)
    assert float(db.get_setting("billing.anthropic.credit", 0)) == 50.0
