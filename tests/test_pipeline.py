"""Smoke test for the ONASSIS production workflow (product-first).

Runs the one production workflow (the Daily Cycle) against a throwaway SQLite
file in a temp dir and asserts it produced a product (an upload-ready Etsy
listing) before any marketing content.

This is a LIVE test: it calls the real Anthropic API (network + credits). It
is **opt-in** so the normal suite stays fast and offline — set
``ONASSIS_RUN_LIVE_TESTS=1`` (and have a valid key) to run it::

    ONASSIS_RUN_LIVE_TESTS=1 python -m pytest tests/test_pipeline.py
    ONASSIS_RUN_LIVE_TESTS=1 python tests/test_pipeline.py

Without the flag it skips, regardless of whether a key is configured — so a
key in `.env` never silently triggers paid API calls during `pytest`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the repo root importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onassis.config import load_config  # noqa: E402
from onassis.daily_cycle import DailyCycle  # noqa: E402
from onassis.database import Database  # noqa: E402

# Opt-in only: a configured key must NOT be enough to trigger paid calls.
_RUN_LIVE = bool(os.environ.get("ONASSIS_RUN_LIVE_TESTS"))


def _run(tmp_db: Path) -> dict:
    config = load_config()
    config.db_path = tmp_db  # point at a throwaway db
    db = Database(config.db_path)
    result = DailyCycle(config, db).run("production")

    by_stage = {s["stage"]: s for s in result["stages"]}
    # Product-first: a product (opportunity -> design -> Etsy listing) is created
    # before any marketing content.
    assert by_stage["Create Product Opportunity"]["status"] == "ok"
    assert by_stage["Build Etsy Listing Package"]["status"] == "ok"
    assert result["listing_ready"] is True
    assert result["campaign_id"] is not None

    order = [s["stage"] for s in result["stages"]]
    assert order.index("Build Etsy Listing Package") < order.index("Generate Marketing Content")
    return result


def test_daily_pipeline(tmp_path) -> None:  # pytest entry point
    if not _RUN_LIVE:
        import pytest

        pytest.skip("live test — set ONASSIS_RUN_LIVE_TESTS=1 to run it")
    _run(tmp_path / "test.db")


if __name__ == "__main__":
    import tempfile

    if not _RUN_LIVE:
        print("SKIP — set ONASSIS_RUN_LIVE_TESTS=1 to run the live pipeline test.")
        sys.exit(0)

    with tempfile.TemporaryDirectory() as d:
        result = _run(Path(d) / "test.db")
        print("OK — pipeline produced:", result)
