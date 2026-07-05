"""Tests for the Product Status Engine (Sprint 40, Objective 1)."""

from __future__ import annotations

from onassis.product_status import (
    derive_product_status, is_valid_listing_id, matches_filter)


# --- Valid listing id -------------------------------------------------

def test_valid_listing_id_rejects_sentinels():
    for bad in (None, "None", "none", "", "  ", "null", "0", "false", 0):
        assert not is_valid_listing_id(bad)
    for good in (555, "555", "abc123"):
        assert is_valid_listing_id(good)


# --- Lifecycle derivation ---------------------------------------------

def _product(active=1):
    return {"active": active, "sku": "1-mug", "product_key": "mug", "campaign_id": 1}


def test_launched_product_defaults_to_awaiting_not_published():
    """The core Sprint 40 fix: a launched product with no draft is Awaiting
    Approval — never 'published'."""
    st = derive_product_status(product=_product())
    assert st.status == "awaiting_approval"
    assert st.label == "Awaiting Approval"


def test_draft_created_only_with_valid_listing_id():
    ok = derive_product_status(product=_product(),
                               publication={"status": "draft", "listing_id": "555"})
    assert ok.status == "draft_created" and ok.listing_id == "555"

    # A draft record with no real id is a broken publish, not a Draft Created.
    broken = derive_product_status(product=_product(),
                                   publication={"status": "draft", "listing_id": None})
    assert broken.status == "failed" and broken.retryable


def test_failed_publish_surfaces_reason_and_retry():
    st = derive_product_status(
        product=_product(),
        publication={"status": "failed", "failure_reason": "transient Etsy error"})
    assert st.status == "failed"
    assert st.reason == "transient Etsy error"
    assert st.retryable is True


def test_live_then_marketing_then_tracking():
    pub = {"status": "live", "listing_id": "9"}
    assert derive_product_status(product=_product(), publication=pub).status == "live"
    assert derive_product_status(product=_product(), publication=pub,
                                 marketing_count=2).status == "marketing"
    assert derive_product_status(product=_product(), publication=pub,
                                 marketing_count=2, units_sold=3).status == "tracking"


def test_operator_decisions_without_publish():
    approved = derive_product_status(product=_product(),
                                     approval={"decision": "approved"})
    assert approved.status == "approved"
    rejected = derive_product_status(product=_product(),
                                     approval={"decision": "rejected"})
    assert rejected.status == "rejected"


def test_archived_when_inactive_and_no_live_listing():
    st = derive_product_status(product=_product(active=0))
    assert st.status == "archived"


def test_live_listing_beats_archived_flag():
    # A retired flag never hides a real live listing.
    st = derive_product_status(product=_product(active=0),
                               publication={"status": "live", "listing_id": "9"})
    assert st.status == "live"


# --- Filters ----------------------------------------------------------

def test_filter_buckets():
    assert matches_filter("awaiting_approval", "awaiting_approval")
    assert matches_filter("draft_created", "draft_created")
    assert matches_filter("publishing", "draft_created")
    assert matches_filter("live", "live")
    assert matches_filter("tracking", "live")
    assert not matches_filter("live", "failed")
    assert matches_filter("anything", None)      # no filter = all
    assert matches_filter("anything", "all")
