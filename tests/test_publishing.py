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


class UploadingDraftClient(StubDraftClient):
    """A draft client that also supports uploadListingImage — records uploads."""

    def __init__(self, listing_id=555, fail_on=None):
        super().__init__(listing_id)
        self.uploaded: list[tuple[str, int]] = []
        self.fail_on = set(fail_on or ())

    def upload_listing_image(self, listing_id, image_path, *, rank=1,
                             alt_text=None, overwrite=False):
        from pathlib import Path

        name = Path(image_path).name
        if name in self.fail_on:
            raise RuntimeError("image upload boom")
        self.uploaded.append((name, rank))
        return {"listing_image_id": len(self.uploaded)}


def _write_package_with_images(tmp_path, cid, product_key=None, n=3):
    folder = tmp_path / "exports" / str(cid)
    if product_key:
        folder = folder / product_key
    images_dir = folder / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    images = []
    for i in range(n):
        fn = f"gallery_{i:02d}.jpg"
        (images_dir / fn).write_bytes(b"\xff\xd8\xff\xe0-real-jpeg-" + bytes([i]) * 32)
        images.append({"order": i + 1, "filename": fn, "alt_text": f"alt {i}",
                       "mockup_type": "hero"})
    (folder / "listing.json").write_text(json.dumps({
        "campaign_id": cid,
        "product_id": f"{cid}-{product_key}" if product_key else "SKU1",
        "product_key": product_key, "title": "T", "description": "d", "tags": ["a"],
        "price": 30.0, "quantity": 50, "images": images,
        "image_order": [im["filename"] for im in images],
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


# --- Image upload to Etsy (every generated image attached) ----------

def test_publish_uploads_every_generated_image(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    client = UploadingDraftClient()
    pub = PublisherService(config, db, draft_client=client)
    cid = _approved_campaign(db)
    _write_package_with_images(tmp_path, cid, "ceramic_mug", n=3)
    _launch(db, cid, "ceramic_mug")

    result = pub.publish(cid, mode="draft", product_key="ceramic_mug")
    assert result["status"] == "draft"
    assert result["publication"]["images_uploaded"] == 3
    assert result["publication"]["images_failed"] == 0
    # Uploaded in gallery order (rank 1..3).
    assert [r for _, r in client.uploaded] == [1, 2, 3]
    assert {name for name, _ in client.uploaded} == {
        "gallery_00.jpg", "gallery_01.jpg", "gallery_02.jpg"}


def test_mockup_gate_blocks_publish_when_all_images_are_fallback(config, db, tmp_path):
    """P1 — a listing whose gallery is only placeholder/fallback images is
    blocked before any Etsy call, with the operator message."""
    import json

    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    pub = PublisherService(config, db, draft_client=UploadingDraftClient())
    cid = _approved_campaign(db)
    _launch(db, cid, "ceramic_mug")
    folder = tmp_path / "exports" / str(cid) / "ceramic_mug"
    (folder / "images").mkdir(parents=True, exist_ok=True)
    (folder / "images" / "hero.jpg").write_bytes(b"\xff\xd8\xff\xe0x" * 40)
    (folder / "listing.json").write_text(json.dumps({
        "campaign_id": cid, "product_id": f"{cid}-ceramic_mug", "product_key": "ceramic_mug",
        "title": "Mug", "description": "d", "tags": ["a"], "price": 22.0, "quantity": 50,
        "images": [{"order": 1, "filename": "hero.jpg", "alt_text": "x",
                    "quality_pass": True, "fallback_used": True, "generation_ok": False}],
    }))
    result = pub.publish(cid, mode="draft", product_key="ceramic_mug")
    assert result["status"] == "failed" and result["mockup_blocked"] is True
    assert "Regenerate mockups" in result["reason"]
    # Nothing was drafted on Etsy.
    stored = [p for p in db.list_publications() if p["platform"] == "etsy"][0]
    assert stored["status"] == "failed" and stored["listing_id"] is None


def test_image_upload_failure_does_not_fail_the_draft(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    client = UploadingDraftClient(fail_on={"gallery_01.jpg"})
    pub = PublisherService(config, db, draft_client=client)
    cid = _approved_campaign(db)
    _write_package_with_images(tmp_path, cid, "ceramic_mug", n=3)
    _launch(db, cid, "ceramic_mug")

    result = pub.publish(cid, mode="draft", product_key="ceramic_mug")
    # The draft survives; the failed image is counted, not fatal.
    assert result["status"] == "draft"
    assert result["publication"]["images_uploaded"] == 2
    assert result["publication"]["images_failed"] == 1


def test_client_without_image_support_is_skipped_cleanly(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _write_package_with_images(tmp_path, cid, product_key=None, n=3)  # StubDraftClient
    result = publisher.publish(cid, mode="draft")
    assert result["status"] == "draft"
    assert result["publication"]["images_uploaded"] == 0   # stub can't upload
    assert result["images"]["skipped"] == 3


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


# --- Launch policy (single approval → whole approved set) -----------

def _launched_campaign(publisher, db, tmp_path, keys=("ceramic_mug", "premium_poster")):
    cid = _approved_campaign(db)
    for key in keys:
        _launch(db, cid, key)
        _write_product_package(tmp_path, cid, key)
    return cid


def test_manual_policy_reaches_launch_ready_and_waits(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    config.launch = {"policy": "manual"}
    pub = PublisherService(config, db, draft_client=StubDraftClient())
    cid = _launched_campaign(pub, db, tmp_path)

    result = pub.launch(cid, mode="draft")
    # Manual mode drafts everything but waits for a single approval.
    assert result["status"] == "launch_ready"
    assert result["policy"] == "manual"
    assert result["drafts"]["published"] == 2
    assert db.get_launch(cid)["status"] == "launch_ready"
    assert cid in {r["campaign_id"] for r in pub.pending_launches()}


def test_automatic_policy_launches_immediately(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    config.launch = {"policy": "automatic"}
    pub = PublisherService(config, db, draft_client=StubDraftClient())
    cid = _launched_campaign(pub, db, tmp_path)

    result = pub.launch(cid, mode="draft")
    assert result["status"] == "launched"
    assert result["policy"] == "automatic"
    assert db.get_launch(cid)["status"] == "launched"
    assert db.get_launch(cid)["approved_by"] == "automatic"
    assert pub.pending_launches() == []  # nothing left waiting


def test_single_approval_covers_every_product(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    config.launch = {"policy": "manual"}
    pub = PublisherService(config, db, draft_client=StubDraftClient())
    cid = _launched_campaign(pub, db, tmp_path, keys=("ceramic_mug", "premium_poster",
                                                       "premium_tshirt"))
    pub.launch(cid, mode="draft")

    # One approval action launches the master design + every approved product.
    approval = pub.approve_launch(cid)
    assert approval["status"] == "launched"
    assert set(approval["products"]) == {"ceramic_mug", "premium_poster", "premium_tshirt"}
    assert db.get_launch(cid)["status"] == "launched"
    assert db.get_launch(cid)["approved_by"] == "owner"


def test_approve_launch_blocked_when_not_launch_ready(publisher, db):
    cid = _approved_campaign(db)
    result = publisher.approve_launch(cid)
    assert result["status"] == "blocked"
    assert "not Launch Ready" in result["reason"]


def test_launch_blocked_without_approved_products(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 3}
    config.launch = {"policy": "manual"}
    pub = PublisherService(config, db, draft_client=StubDraftClient())
    cid = _approved_campaign(db)  # no launched products
    result = pub.launch(cid, mode="draft")
    assert result["status"] == "blocked"
    assert db.get_launch(cid) is None


# --- Live publishing (go-live) --------------------------------------

class LiveDraftClient(UploadingDraftClient):
    """A draft client that can also activate listings LIVE."""

    def __init__(self, listing_id=555):
        super().__init__(listing_id)
        self.activated: list[str] = []
        self._counter = listing_id

    def create_draft(self, listing):
        self._counter += 1
        self.calls += 1
        return {"listing_id": self._counter}

    def publish_listing(self, listing_id):
        self.activated.append(str(listing_id))
        return {"listing_id": listing_id, "state": "active"}


def _priced(db, cid, key, retail, cost, launched=1):
    db.insert_product_score({"campaign_id": cid, "product_key": key, "product_name": key,
                             "launched": launched, "composite_score": 85,
                             "retail_price": retail, "production_cost": cost,
                             "ceo_verdict": "APPROVE"})


def _live_publisher(config, db, tmp_path, *, auto_go_live=True, floor=0.10):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["dry_run", "draft", "live"],
                         "max_retries": 3, "min_go_live_margin": floor}
    config.launch = {"policy": "automatic", "auto_go_live": auto_go_live}
    return PublisherService(config, db, draft_client=LiveDraftClient())


def test_automatic_launch_takes_products_live(config, db, tmp_path):
    pub = _live_publisher(config, db, tmp_path)
    cid = _approved_campaign(db)
    for key in ("ceramic_mug", "premium_poster"):
        _priced(db, cid, key, retail=22.0, cost=7.5)
        _write_product_package(tmp_path, cid, key)

    result = pub.launch(cid, mode="draft")
    assert result["status"] == "launched"
    go_live = result["launch"]["go_live"]
    assert go_live["live"] == 2                          # both activated on Etsy
    assert len(pub._draft_client.activated) == 2
    # The publications are now LIVE, not merely drafts.
    live = [p for p in db.list_publications() if p["status"] == "live"]
    assert len(live) == 2


def test_go_live_margin_guard_never_lists_a_loss(config, db, tmp_path):
    pub = _live_publisher(config, db, tmp_path, floor=0.10)
    cid = _approved_campaign(db)
    _priced(db, cid, "ceramic_mug", retail=22.0, cost=7.5)      # healthy margin
    _priced(db, cid, "greeting_card", retail=10.0, cost=9.0)    # loses money after fees
    for key in ("ceramic_mug", "greeting_card"):
        _write_product_package(tmp_path, cid, key)

    go_live = pub.launch(cid, mode="draft")["launch"]["go_live"]
    by = {r["product_key"]: r for r in go_live["results"]}
    assert by["ceramic_mug"]["status"] == "live"
    assert by["greeting_card"]["status"] == "held"       # guarded, not listed at a loss
    assert go_live["live"] == 1
    assert "greeting_card" not in pub._draft_client.activated


def test_go_live_is_idempotent(config, db, tmp_path):
    pub = _live_publisher(config, db, tmp_path)
    cid = _approved_campaign(db)
    _priced(db, cid, "ceramic_mug", retail=22.0, cost=7.5)
    _write_product_package(tmp_path, cid, "ceramic_mug")

    pub.launch(cid, mode="draft")
    again = pub.go_live(cid)
    assert again["results"][0]["status"] == "already_live"
    assert len(pub._draft_client.activated) == 1          # activated exactly once


def test_no_go_live_when_live_mode_disabled(config, db, tmp_path):
    # Live not in enabled_modes -> auto go-live is off; drafts stay drafts.
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["dry_run", "draft"], "max_retries": 3}
    config.launch = {"policy": "automatic", "auto_go_live": True}
    pub = PublisherService(config, db, draft_client=LiveDraftClient())
    cid = _approved_campaign(db)
    _priced(db, cid, "ceramic_mug", retail=22.0, cost=7.5)
    _write_product_package(tmp_path, cid, "ceramic_mug")

    result = pub.launch(cid, mode="draft")
    assert result["status"] == "launched"
    assert "go_live" not in result.get("launch", {})      # never attempted
    assert pub._draft_client.activated == []
    assert all(p["status"] == "draft" for p in db.list_publications())


# --- Sprint 40: Etsy draft validation -------------------------------

class NoIdDraftClient:
    """Returns a response with a missing/invalid listing id (Etsy hiccup)."""

    def __init__(self, listing_id=None):
        self.listing_id = listing_id
        self.calls = 0

    def create_draft(self, listing):
        self.calls += 1
        return {"listing_id": self.listing_id, "state": "draft"}


def test_missing_listing_id_is_a_failure_not_a_draft(config, db, tmp_path):
    """A publish that returns no valid listing id must be recorded FAILED —
    never stored as a draft with id 'None'."""
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 2}
    pub = PublisherService(config, db, draft_client=NoIdDraftClient(listing_id=None))
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)

    result = pub.publish(cid, mode="draft")
    assert result["status"] == "failed"
    assert "valid listing id" in result["reason"]
    stored = db.list_publications()[0]
    assert stored["status"] == "failed"
    assert stored["listing_id"] is None
    # No active (draft/live) publication exists, so a retry is still possible.
    assert db.get_active_publication(cid) is None


def test_string_none_listing_id_is_rejected(config, db, tmp_path):
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 1}
    pub = PublisherService(config, db, draft_client=NoIdDraftClient(listing_id="None"))
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    assert pub.publish(cid, mode="draft")["status"] == "failed"


def test_publish_products_counts_are_honest(config, db, tmp_path):
    """drafts_created counts only real drafts; a failure is not 'published'."""
    config.listing = {"exports_dir": str(tmp_path / "exports")}
    config.publishing = {"enabled_modes": ["draft"], "max_retries": 1}
    pub = PublisherService(config, db, draft_client=StubDraftClient())
    cid = _approved_campaign(db)
    _launch(db, cid, "ceramic_mug")
    _write_product_package(tmp_path, cid, "ceramic_mug")
    _launch(db, cid, "premium_poster")  # launched but NO package -> blocked

    result = pub.publish_products(cid, mode="draft")
    assert result["drafts_created"] == 1
    assert result["published"] == 1        # only the real draft counts
    assert result["count"] == 2


# --- Status ---------------------------------------------------------

def test_status_summary(publisher, db, tmp_path):
    cid = _approved_campaign(db)
    _write_package(tmp_path, cid)
    publisher.publish(cid, mode="draft")
    status = publisher.status()
    assert status["total"] == 1
    assert status["by_status"]["draft"] == 1
    assert "recent" in status
