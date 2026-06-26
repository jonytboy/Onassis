"""Smoke test for the ONASSIS v0.1 daily pipeline.

Runs the whole pipeline against a throwaway SQLite file in a temp dir and
asserts the configured amount of content was produced and stored. Run with::

    python -m pytest        (if pytest is installed)
    python tests/test_pipeline.py   (no dependencies needed)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onassis.config import load_config  # noqa: E402
from onassis.database import Database  # noqa: E402
from onassis.orchestrator import Orchestrator  # noqa: E402


def _run(tmp_db: Path) -> dict:
    config = load_config()
    config.db_path = tmp_db  # point at a throwaway db
    db = Database(config.db_path)
    orchestrator = Orchestrator(config, db)
    summary = orchestrator.run_daily()

    targets = config.content_targets
    expected = sum(targets.values())
    assert summary["items_created"] == expected, (
        f"expected {expected} items, got {summary['items_created']}"
    )
    stored = db.get_content_for_brief(summary["brief_id"])
    assert len(stored) == expected

    # Verify the per-platform breakdown matches the config targets.
    by_platform: dict[str, int] = {}
    for item in stored:
        by_platform[item["platform"]] = by_platform.get(item["platform"], 0) + 1
    assert by_platform["pinterest"] == targets["pinterest_posts"]
    assert by_platform["instagram"] == targets["instagram_captions"]
    assert by_platform["facebook"] == targets["facebook_posts"]
    assert by_platform["image"] == targets["image_prompts"]
    return summary


def test_daily_pipeline(tmp_path) -> None:  # pytest entry point
    _run(tmp_path / "test.db")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run(Path(d) / "test.db")
        print("OK — pipeline produced:", result)
