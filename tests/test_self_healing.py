"""Tests for workflow self-healing / reconciliation (Sprint 41.2)."""

from __future__ import annotations

from onassis.self_healing import reconcile


def test_removes_orphan_approvals(config, db):
    db.insert_product({"sku": "1-mug", "name": "Mug", "campaign_id": 1, "product_key": "mug"})
    db.set_product_approval({"sku": "1-mug", "product_key": "mug", "campaign_id": 1,
                             "decision": "approved"})
    db.set_product_approval({"sku": "9-ghost", "product_key": "ghost", "campaign_id": 9,
                             "decision": "approved"})   # no product row
    r = reconcile(config, db)
    assert r["orphan_approvals_removed"] == 1
    skus = {a["sku"] for a in db.list_product_approvals()}
    assert skus == {"1-mug"}


def test_clamps_impossible_confidence(config, db):
    db.insert_product_score({"campaign_id": 1, "product_key": "mug", "product_name": "Mug",
                             "composite_score": 250})
    db.insert_product_score({"campaign_id": 1, "product_key": "poster", "product_name": "P",
                             "composite_score": -20})
    r = reconcile(config, db)
    assert r["confidence_clamped"] == 2
    scores = {s["product_key"]: s["composite_score"] for s in db.list_product_scores()}
    assert scores["mug"] == 100.0 and scores["poster"] == 0.0


def test_fixes_broken_publication(config, db):
    # A "draft" with no valid listing id is an impossible state -> becomes failed.
    db.insert_publication({"platform": "etsy", "product_id": "1-mug", "campaign_id": 1,
                           "listing_id": "None", "mode": "draft", "status": "draft"})
    r = reconcile(config, db)
    assert r["broken_publications_fixed"] == 1
    assert db.list_publications()[0]["status"] == "failed"


def test_dedupes_active_publications(config, db):
    for _ in range(2):
        db.insert_publication({"platform": "etsy", "product_id": "1-mug", "campaign_id": 1,
                               "listing_id": "555", "mode": "draft", "status": "draft"})
    r = reconcile(config, db)
    assert r["duplicate_publications_superseded"] == 1
    active = [p for p in db.list_publications() if p["status"] == "draft"]
    assert len(active) == 1


def test_reconcile_is_idempotent(config, db):
    db.insert_publication({"platform": "etsy", "product_id": "1-mug", "campaign_id": 1,
                           "listing_id": "None", "mode": "draft", "status": "draft"})
    reconcile(config, db)
    second = reconcile(config, db)
    assert second["repaired"] == 0        # nothing left to fix


def test_clean_system_reports_zero(config, db):
    r = reconcile(config, db)
    assert r["repaired"] == 0
