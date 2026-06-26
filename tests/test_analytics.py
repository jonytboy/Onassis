"""Tests for the Analytics agent (placeholder — local counts only in v0.1)."""

from __future__ import annotations

from onassis.agents.analytics import AnalyticsAgent


def test_analytics_empty_database(config, db):
    result = AnalyticsAgent(config, db).run(brief_id=None)
    assert result["items_this_brief"] == 0
    assert result["items_total"] == 0
    assert result["external_metrics"] == "not available in v0.1"


def test_analytics_counts_for_brief_and_total(config, db):
    b1 = db.insert_brief({"brief_date": "2026-06-25", "theme": "A", "keywords": []})
    b2 = db.insert_brief({"brief_date": "2026-06-26", "theme": "B", "keywords": []})
    db.insert_content_items(
        b1, [{"platform": "pinterest", "content_type": "post", "body": "x"}]
    )
    db.insert_content_items(
        b2,
        [
            {"platform": "instagram", "content_type": "caption", "body": "y"},
            {"platform": "facebook", "content_type": "post", "body": "z"},
        ],
    )

    result = AnalyticsAgent(config, db).run(brief_id=b2)
    assert result["items_this_brief"] == 2
    assert result["items_total"] == 3


def test_analytics_does_not_require_llm(config, db):
    config.anthropic_api_key = None
    result = AnalyticsAgent(config, db).run(brief_id=None)
    assert result["external_metrics"] == "not available in v0.1"
