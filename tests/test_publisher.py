"""Tests for the Publisher agent (placeholder — must not publish in v0.1)."""

from __future__ import annotations

from onassis.agents.publisher import Publisher


def test_publisher_is_disabled_and_publishes_nothing(config, db):
    pub = Publisher(config, db)
    result = pub.run(brief_id=None)
    assert result == {"published": 0, "skipped": 0, "enabled": False}


def test_publisher_reports_pending_drafts_without_publishing(config, db):
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    db.insert_content_items(
        brief_id,
        [
            {"platform": "pinterest", "content_type": "post", "body": "a"},
            {"platform": "facebook", "content_type": "post", "body": "b"},
        ],
    )

    result = Publisher(config, db).run(brief_id=brief_id)
    assert result["published"] == 0
    assert result["enabled"] is False
    assert result["skipped"] == 2  # counted but left untouched

    # Nothing should have been marked published.
    stored = db.get_content_for_brief(brief_id)
    assert all(item["status"] == "draft" for item in stored)


def test_publisher_does_not_require_llm(config, db):
    # Placeholder agents must work with no API key configured.
    config.anthropic_api_key = None
    assert Publisher(config, db).run(brief_id=None)["enabled"] is False
