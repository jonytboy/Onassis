"""ONASSIS entry point.

Usage::

    python main.py                     # start the daily scheduler (long-running)
    python main.py --once              # run the pipeline a single time and exit
    python main.py --show-last         # print the most recent brief + its content
    python main.py --campaigns         # list all campaigns (the dashboard)
    python main.py --campaign <id>     # show everything for one campaign
    python main.py --set-status <id> <status>   # change a campaign's status

This file is intentionally thin: it loads config, wires up logging, the
database, the orchestrator, and the campaign manager, then hands off. All
real logic lives in the `onassis` package.
"""

from __future__ import annotations

import argparse
import json
import sys

from onassis.campaign_manager import STATUSES, CampaignError, CampaignManager
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
    parser.add_argument(
        "--campaigns",
        action="store_true",
        help="Show the campaign dashboard (all campaigns) and exit.",
    )
    parser.add_argument(
        "--campaign",
        type=int,
        metavar="ID",
        help="Show everything created for the given campaign id and exit.",
    )
    parser.add_argument(
        "--set-status",
        nargs=2,
        metavar=("ID", "STATUS"),
        help=f"Set a campaign's status. STATUS one of: {', '.join(STATUSES)}.",
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


def _list_campaigns(campaigns: CampaignManager) -> None:
    """Render the campaign dashboard as a simple table."""
    rows = campaigns.list_campaigns()
    if not rows:
        print("No campaigns yet. Run `python main.py --once` to create one.")
        return

    print(f"\nCAMPAIGN DASHBOARD — {len(rows)} campaign(s)\n")
    print(f"{'ID':>3}  {'STATUS':<10}  {'CREATED':<10}  {'#':>3}  NAME")
    print("-" * 72)
    for c in rows:
        created = (c.get("created_at") or "")[:10]
        print(
            f"{c['id']:>3}  {c['status']:<10}  {created:<10}  "
            f"{c.get('content_count', 0):>3}  {c['name']}"
        )
    print()


def _show_campaign(campaigns: CampaignManager, campaign_id: int) -> None:
    """Print the full campaign view (metadata, story, and all content)."""
    campaign = campaigns.get_campaign(campaign_id)
    if campaign is None:
        print(f"No campaign with id {campaign_id}.")
        return
    print(json.dumps(campaign, indent=2))


def main() -> int:
    args = _parse_args()

    config = load_config()
    setup_logging(config)
    log = get_logger("onassis.main")
    log.info("Starting %s v%s (env=%s)", config.app_name, config.version, config.environment)

    db = Database(config.db_path)
    orchestrator = Orchestrator(config, db)
    campaigns = CampaignManager(config, db)

    if args.show_last:
        _show_last(db)
        return 0

    if args.campaigns:
        _list_campaigns(campaigns)
        return 0

    if args.campaign is not None:
        _show_campaign(campaigns, args.campaign)
        return 0

    if args.set_status is not None:
        cid, status = args.set_status
        try:
            updated = campaigns.set_status(int(cid), status)
        except (ValueError, CampaignError) as exc:
            print(f"Error: {exc}")
            return 1
        print(f"Campaign #{updated['id']} is now '{updated['status']}'.")
        return 0

    if args.once:
        orchestrator.run_daily()
        return 0

    DailyScheduler(config, orchestrator).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
