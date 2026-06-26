"""Tests for the SQLite persistence layer."""

from __future__ import annotations


def _brief(theme: str = "Theme", brief_date: str = "2026-06-26") -> dict:
    return {
        "brief_date": brief_date,
        "theme": theme,
        "tone": "warm",
        "audience": "travellers",
        "objective": "saves",
        "keywords": ["a", "b"],
        "campaign_name": f"Campaign {theme}",
    }


def test_insert_and_get_brief_roundtrip(db):
    brief_id = db.insert_brief(_brief("Coastal mornings"))
    assert isinstance(brief_id, int)

    stored = db.get_brief(brief_id)
    assert stored is not None
    assert stored["theme"] == "Coastal mornings"
    assert stored["keywords"] == ["a", "b"]  # decoded from JSON
    assert stored["brief_date"] == "2026-06-26"


def test_get_brief_missing_returns_none(db):
    assert db.get_brief(999) is None


def test_insert_content_items_and_fetch(db):
    brief_id = db.insert_brief(_brief())
    items = [
        {
            "platform": "pinterest",
            "content_type": "post",
            "title": "T",
            "body": "B",
            "metadata": {"hashtags": ["#x"]},
        },
        {
            "platform": "instagram",
            "content_type": "caption",
            "body": "caption body",
        },
    ]
    n = db.insert_content_items(brief_id, items)
    assert n == 2

    stored = db.get_content_for_brief(brief_id)
    assert len(stored) == 2
    assert stored[0]["platform"] == "pinterest"
    assert stored[0]["metadata"] == {"hashtags": ["#x"]}  # decoded from JSON
    assert stored[0]["status"] == "draft"  # default
    assert stored[1]["metadata"] == {}  # default when omitted


def test_count_content(db):
    assert db.count_content() == 0
    brief_id = db.insert_brief(_brief())
    db.insert_content_items(
        brief_id, [{"platform": "facebook", "content_type": "post", "body": "x"}]
    )
    assert db.count_content() == 1


def test_get_recent_briefs_newest_first(db):
    db.insert_brief(_brief("First", "2026-06-24"))
    db.insert_brief(_brief("Second", "2026-06-25"))
    db.insert_brief(_brief("Third", "2026-06-26"))

    recent = db.get_recent_briefs(limit=2)
    assert len(recent) == 2
    assert recent[0]["theme"] == "Third"  # newest first
    assert recent[1]["theme"] == "Second"


def test_get_recent_briefs_empty(db):
    assert db.get_recent_briefs() == []


def test_content_for_brief_is_isolated(db):
    b1 = db.insert_brief(_brief("One"))
    b2 = db.insert_brief(_brief("Two"))
    db.insert_content_items(b1, [{"platform": "image", "content_type": "image_prompt", "body": "p"}])
    assert len(db.get_content_for_brief(b1)) == 1
    assert db.get_content_for_brief(b2) == []
