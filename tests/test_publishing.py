"""Tests for the Autonomous Publisher (Draft mode; stub Etsy write client)."""

from __future__ import annotations

import json

import pytest

from onassis.publishing import PublisherService


class StubDraftClient:
    """Creates a fake draft and returns a listing id."""

    def __init__(self, listing_id=555):
        self.listing_id = listing_id
        self.calls = 0

    def create_draft(self, listing):
        self.calls += 1
        return {"listing_id": self.listing_id, "state": "draft"}


class FlakyDraftClient:
    """Fails ``fail_times`` then succeeds — to test safe retries."""

    def __init__(self, fail_times=1, listing_id=777):
        self.fail_times = fail_times
        self.listing_id = listing_id
        self.calls = 0

    def create_draft(self, listing):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("transient Etsy error")
        return {"listing_id": self.listing_id}


def _approved_campaign(db, *, verdict="APPROVE") -> int:
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    cid = db.insert_campaign({"name": "Salt", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": cid, "verdict": verdict,
                                 "reasoning": "ok", "compliance_score": 90})
    return cid


def _write_package(tmp_path, cid, product_id="SKU1"):
    folder = tmp_path / "exports" / str(cid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "listing.json").write_text(json.dumps({
        "campaign_id": cid, "product_id": product_id, "title": "Linen Throw",
        "description": "Lovely.", "tags": ["a"], "price": 30.0, "quantity": 50,
    }))


@pytest.fixture
def publisher(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"default_mode": "draft", "enabled_modes": ["dry_run", "draft"],
                         "max_retries": 3}
    return PublisherService(config, db, draft_client=StubDraftClient())


# --- Draft publishing -----------------------------------------------

def _write_product_package(tmp_path, cid, product_key):
    folder = tmp_path / "exports" / str(cid) / product_key
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "listing.json").write_text(json.dumps({
        "campaign_id": cid, "product_id": f"{cid}-{product_key}", "product_key": product_key,
        "title": f"Design on {product_key}", "description": "Lovely.", "tags": ["a"],
        "price": 22.0, "quantity": 50,
    }))


def _launch(db, cid, key, launched=1):
    db.insert_product_score({"campaign_id": cid, "product_key": key, "product_name": key,
                             "launched": launched, "composite_score": 85,
                             "ceo_verdict": "APPROVE" if launched else "REJECT"})


def test_publish_products_publishes_each_approved_product(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    for key in ("ceramic_mug", "premium_poster"):
        _launch(db, cid, key)
        _write_product_package(tmp_path, cid, key)
    _launch(db, cid, "hardcover_notebook", launched=0)  # rejected — never published

    result = publisher.publish_products(cid, mode="draft")
    assert result["status"] == "ok" and result["count"] == 2 and result["published"] == 2
    assert all(r["status"] == "draft" for r in result["results"])
    pubs = db.list_publications()
    assert {p["product_id"] for p in pubs} == {f"{cid}-ceramic_mug", f"{cid}-premium_poster"}


def test_publish_products_never_duplicates_per_product(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _launch(db, cid, "ceramic_mug")
    _write_product_package(tmp_path, cid, "ceramic_mug")
    publisher.publish_products(cid, mode="draft")
    again = publisher.publish_products(cid, mode="draft")
    assert again["results"][0]["status"] == "skipped"  # already published, not duplicated


def test_publish_products_blocked_without_approved_set(publisher, db):
    cid = _approved_campaign(db)
    assert publisher.publish_products(cid, mode="draft")["status"] == "blocked"


# --- Draft publishing -----------------------------------------------

def test_publish_draft_success(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    result = publisher.publish(cid, mode="draft")
    assert result["status"] == "draft"
    assert result["publication"]["listing_id"] == "555"
    assert db.list_publications()[0]["status"] == "draft"


def test_publication_is_logged_with_required_fields(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid, product_id="SKU9")
    publisher.publish(cid, mode="draft")
    pub = db.list_publications()[0]
    for field in ("platform", "product_id", "campaign_id", "created_at",
                  "listing_id", "status"):
        assert field in pub
    assert pub["platform"] == "etsy"
    assert pub["product_id"] == "SKU9"


# --- Gates & duplicates ---------------------------------------------

def test_unapproved_campaign_blocked(publisher, db, tmp_path):
    cid = _approved_campaign(db, verdict="REJECT")
    _write_package(tmp_path, cid)
    assert publisher.publish(cid, mode="draft")["status"] == "blocked"


def test_missing_package_blocked(publisher, db):
    cid = _approved_campaign(db)
    result = publisher.publish(cid, mode="draft")
    assert result["status"] == "blocked"
    assert "build it first" in result["reason"]


def test_never_creates_duplicate(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    first = publisher.publish(cid, mode="draft")
    assert first["status"] == "draft"
    second = publisher.publish(cid, mode="draft")
    assert second["status"] == "skipped"
    # only one real publication exists
    drafts = [p for p in db.list_publications() if p["status"] == "draft"]
    assert len(drafts) == 1


# --- Retries & failures ---------------------------------------------

def test_retries_then_succeeds(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    client = FlakyDraftClient(fail_times=2)
    pub = PublisherService(config, db, draft_client=client)
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)

    result = pub.publish(cid, mode="draft")
    assert result["status"] == "draft"
    assert result["publication"]["attempts"] == 3   # 2 failures + 1 success
    assert client.calls == 3


def test_records_failure_after_exhausting_retries(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 2}
    pub = PublisherService(config, db, draft_client=FlakyDraftClient(fail_times=99))
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)

    result = pub.publish(cid, mode="draft")
    assert result["status"] == "failed"
    assert "transient" in result["reason"]
    assert db.list_publications()[0]["status"] == "failed"
    # A failure must not block a future retry (no active draft created).
    assert db.get_active_publication(cid) is None


# --- Dry run & live --------------------------------------------------

def test_dry_run_needs_no_client(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["dry_run", "draft"], "max_retries": 3}
    pub = PublisherService(config, db)  # no draft client
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    result = pub.publish(cid, mode="dry_run")
    assert result["status"] == "dry_run"
    assert db.list_publications()[0]["status"] == "dry_run"


def test_live_mode_is_blocked(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    assert publisher.publish(cid, mode="live")["status"] == "blocked"


def test_draft_not_configured_without_client_or_creds(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    config.etsy = {}
    pub = PublisherService(config, db)  # no client, no creds
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    assert pub.publish(cid, mode="draft")["status"] == "not_configured"


# --- Status ---------------------------------------------------------

def test_status_summary(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    publisher.publish(cid, mode="draft")
    status = publisher.status()
    assert status["total"] == 1
    assert status["by_status"]["draft"] == 1
    assert "recent" in status
