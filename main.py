"""ONASSIS entry point.

Usage::

    python main.py            # start the daily scheduler (long-running)
    python main.py --once     # run the pipeline a single time and exit
    python main.py --show-last # print the most recent brief + its content

This file is intentionally thin: it loads config, wires up logging, the
database, the orchestrator, and the scheduler, then hands off. All real
logic lives in the `onassis` package.
"""

from __future__ import annotations

import argparse
import json
import sys

from onassis.config import load_config
from onassis.database import Database
from onassis.logger import get_logger, setup_logging
from onassis.orchestrator import Orchestrator
from onassis.scheduler import DailyScheduler


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ONASSIS — autonomous content engine (v0.1)")
    parser.add_argument(
        "--once", action="store_true", help="Run the daily pipeline once and exit."
    )
    parser.add_argument(
        "--show-last",
        action="store_true",
        help="Print the most recently generated brief and its content, then exit.",
    )
    return parser.parse_args()


def _show_last(db: Database) -> None:
    """Pretty-print the latest brief and its content for quick inspection."""
    # The latest brief id == current max; reuse count via a tiny query.
    with db._connect() as conn:  # noqa: SLF001 - intentional internal use for a CLI helper
        row = conn.execute("SELECT id FROM briefs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        print("No briefs found yet. Run `python main.py --once` first.")
        return

    brief = db.get_brief(row["id"])
    content = db.get_content_for_brief(row["id"])
    print(json.dumps({"brief": brief, "content": content}, indent=2))


def main() -> int:
    args = _parse_args()

    config = load_config()
    setup_logging(config)
    log = get_logger("onassis.main")
    log.info("Starting %s v%s (env=%s)", config.app_name, config.version, config.environment)

    db = Database(config.db_path)
    orchestrator = Orchestrator(config, db)

    if args.show_last:
        _show_last(db)
        return 0

    if args.once:
        orchestrator.run_daily()
        return 0

    DailyScheduler(config, orchestrator).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
