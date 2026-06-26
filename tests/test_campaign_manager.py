"""Tests for the Campaign Manager and the campaign persistence layer."""

from __future__ import annotations

import pytest

from onassis.campaign_manager import STATUSES, CampaignError, CampaignManager


def _make_brief(db, *, campaign_name="Salt & Citrus", theme="Coastal mornings",
                concept="A long, golden afternoon.") -> dict:
    """Insert a brief and return the in-memory dict with its id."""
    brief = {
        "brief_date": "2026-06-26",
        "theme": theme,
        "campaign_name": campaign_name,
        "concept": concept,
        "tone": "warm",
        "audience": "travellers",
        "objective": "saves",
        "keywords": ["a", "b"],
    }
    brief["id"] = db.insert_brief(brief)
    return brief


@pytest.fixture
def manager(config, db) -> CampaignManager:
    return CampaignManager(config, db)


# --- Database-layer campaign methods --------------------------------

def test_insert_and_get_campaign_roundtrip(db):
    brief = _make_brief(db)
    cid = db.insert_campaign(
        {"name": "C", "theme": "T", "story": "S", "status": "Draft", "brief_id": brief["id"]}
    )
    stored = db.get_campaign(cid)
    assert stored["name"] == "C"
    assert stored["status"] == "Draft"
    assert stored["brief_id"] == brief["id"]


def test_list_campaigns_newest_first(db):
    b1, b2 = _make_brief(db, campaign_name="One"), _make_brief(db, campaign_name="Two")
    db.insert_campaign({"name": "One", "brief_id": b1["id"]})
    db.insert_campaign({"name": "Two", "brief_id": b2["id"]})
    rows = db.list_campaigns()
    assert [r["name"] for r in rows] == ["Two", "One"]


def test_update_campaign_status_returns_bool(db):
    brief = _make_brief(db)
    cid = db.insert_campaign({"name": "C", "brief_id": brief["id"]})
    assert db.update_campaign_status(cid, "Live") is True
    assert db.get_campaign(cid)["status"] == "Live"
    assert db.update_campaign_status(999, "Live") is False


def test_get_briefs_without_campaign(db):
    b1 = _make_brief(db, campaign_name="Has campaign")
    _make_brief(db, campaign_name="No campaign")
    db.insert_campaign({"name": "x", "brief_id": b1["id"]})
    orphans = db.get_briefs_without_campaign()
    assert len(orphans) == 1
    assert orphans[0]["id"] != b1["id"]


# --- CampaignManager.create_from_brief ------------------------------

def test_create_from_brief_maps_fields(manager, db):
    brief = _make_brief(db, campaign_name="Salt & Citrus", theme="Mornings",
                        concept="The story.")
    campaign = manager.create_from_brief(brief)
    assert campaign["name"] == "Salt & Citrus"
    assert campaign["theme"] == "Mornings"
    assert campaign["story"] == "The story."   # concept becomes the story
    assert campaign["status"] == "Draft"
    assert campaign["brief_id"] == brief["id"]
    assert "id" in campaign


def test_create_from_brief_is_idempotent(manager, db):
    brief = _make_brief(db)
    first = manager.create_from_brief(brief)
    second = manager.create_from_brief(brief)
    assert first["id"] == second["id"]
    assert len(db.list_campaigns()) == 1  # no duplicate


def test_create_from_brief_falls_back_to_theme_for_name(manager, db):
    brief = _make_brief(db, campaign_name="")
    brief["campaign_name"] = ""  # missing name
    campaign = manager.create_from_brief(brief)
    assert campaign["name"] == brief["theme"]


# --- Status lifecycle -----------------------------------------------

def test_set_status_valid(manager, db):
    brief = _make_brief(db)
    campaign = manager.create_from_brief(brief)
    updated = manager.set_status(campaign["id"], "Scheduled")
    assert updated["status"] == "Scheduled"


def test_set_status_rejects_invalid_status(manager, db):
    brief = _make_brief(db)
    campaign = manager.create_from_brief(brief)
    with pytest.raises(CampaignError, match="Invalid status"):
        manager.set_status(campaign["id"], "Published")


def test_set_status_rejects_unknown_campaign(manager):
    with pytest.raises(CampaignError, match="No campaign"):
        manager.set_status(999, "Live")


def test_statuses_are_the_required_four():
    assert STATUSES == ("Draft", "Scheduled", "Live", "Complete")


# --- Views ----------------------------------------------------------

def test_list_campaigns_includes_content_count(manager, db):
    brief = _make_brief(db)
    manager.create_from_brief(brief)
    db.insert_content_items(
        brief["id"],
        [
            {"platform": "pinterest", "content_type": "post", "body": "a"},
            {"platform": "facebook", "content_type": "post", "body": "b"},
        ],
    )
    rows = manager.list_campaigns()
    assert rows[0]["content_count"] == 2


def test_get_campaign_full_view(manager, db):
    brief = _make_brief(db)
    campaign = manager.create_from_brief(brief)
    db.insert_content_items(
        brief["id"], [{"platform": "instagram", "content_type": "caption", "body": "x"}]
    )
    view = manager.get_campaign(campaign["id"])
    assert view["content_ids"] == [c["id"] for c in view["content"]]
    assert len(view["content"]) == 1
    assert view["brief"]["id"] == brief["id"]


def test_get_campaign_missing_returns_none(manager):
    assert manager.get_campaign(999) is None


# --- Backfill (everything belongs to a campaign) --------------------

def test_sync_backfills_orphan_briefs(manager, db):
    # Briefs created before campaigns existed.
    _make_brief(db, campaign_name="Provence in Bloom", concept="Lavender story")
    _make_brief(db, campaign_name="Blue Hour")
    created = manager.sync_from_briefs()
    assert created == 2
    names = {c["name"] for c in db.list_campaigns()}
    assert {"Provence in Bloom", "Blue Hour"} <= names


def test_list_campaigns_auto_backfills(manager, db):
    _make_brief(db, campaign_name="Orphan")
    rows = manager.list_campaigns()  # should backfill before listing
    assert any(c["name"] == "Orphan" for c in rows)
