"""ONASSIS entry point.

Usage::

    python main.py                     # start the daily scheduler (long-running)
    python main.py --once              # run the pipeline a single time and exit
    python main.py --show-last         # print the most recent brief + its content
    python main.py --campaigns         # list all campaigns (the dashboard)
    python main.py --campaign <id>     # show everything for one campaign (+ knowledge)
    python main.py --set-status <id> <status>   # change a campaign's status
    python main.py --knowledge         # the Brain's memory — all predictions
    python main.py --learn [id]        # generate knowledge for a campaign (or all missing)

This file is intentionally thin: it loads config, wires up logging, the
database, the orchestrator, the campaign manager, and the Brain, then hands
off. All real logic lives in the `onassis` package.
"""

from __future__ import annotations

import argparse
import json
import sys

from onassis.brain import OnassisBrain
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
    parser.add_argument(
        "--knowledge",
        action="store_true",
        help="Show the Brain's memory — all knowledge records (predictions).",
    )
    parser.add_argument(
        "--learn",
        nargs="?",
        type=int,
        const=0,  # 0 = sentinel meaning "all campaigns missing knowledge"
        metavar="ID",
        help="Generate knowledge for campaign ID (calls the LLM). With no id, "
        "backfill every campaign that's missing it.",
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


def _show_campaign(
    campaigns: CampaignManager, brain: OnassisBrain, campaign_id: int
) -> None:
    """Print the full campaign view (metadata, story, content, and knowledge)."""
    campaign = campaigns.get_campaign(campaign_id)
    if campaign is None:
        print(f"No campaign with id {campaign_id}.")
        return
    # Attach the Brain's prediction (read-only — never generates here).
    campaign["knowledge"] = brain.get_for_campaign(campaign_id)
    print(json.dumps(campaign, indent=2))


def _list_knowledge(brain: OnassisBrain) -> None:
    """Render the Brain's memory — one prediction per campaign."""
    records = brain.list_knowledge()
    if not records:
        print("The Brain has no knowledge yet. Run `python main.py --once` "
              "or `python main.py --learn`.")
        return

    print(f"\nONASSIS BRAIN — {len(records)} prediction(s)\n")
    for k in records:
        print(f"Campaign #{k['campaign_id']}  •  confidence {k['confidence']}%  "
              f"•  status: {k['status']}")
        print(f"  Hypothesis : {k['hypothesis']}")
        print(f"  Variables  : {', '.join(k['variables'])}")
        print(f"  Predicted  : {k['predicted_outcome']}")
        print(f"  Metrics    : {', '.join(k['success_metrics'])}")
        print(f"  Recommend  : {k['recommendation']}")
        print("-" * 72)
    print()


def _learn(brain: OnassisBrain, db: Database, campaign_arg: int) -> int:
    """Generate knowledge for one campaign, or backfill all missing (arg == 0)."""
    if campaign_arg == 0:
        created = brain.generate_missing()
        print(f"Generated knowledge for {created} campaign(s).")
        return 0

    campaign = db.get_campaign(campaign_arg)
    if campaign is None:
        print(f"No campaign with id {campaign_arg}.")
        return 1
    knowledge = brain.generate_for_campaign(campaign)
    print(
        f"Campaign #{campaign_arg}: knowledge #{knowledge['id']} "
        f"(confidence {knowledge['confidence']}%)."
    )
    return 0


def main() -> int:
    args = _parse_args()

    config = load_config()
    setup_logging(config)
    log = get_logger("onassis.main")
    log.info("Starting %s v%s (env=%s)", config.app_name, config.version, config.environment)

    db = Database(config.db_path)
    orchestrator = Orchestrator(config, db)
    campaigns = CampaignManager(config, db)
    brain = OnassisBrain(config, db)

    if args.show_last:
        _show_last(db)
        return 0

    if args.campaigns:
        _list_campaigns(campaigns)
        return 0

    if args.campaign is not None:
        _show_campaign(campaigns, brain, args.campaign)
        return 0

    if args.knowledge:
        _list_knowledge(brain)
        return 0

    if args.learn is not None:
        return _learn(brain, db, args.learn)

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
