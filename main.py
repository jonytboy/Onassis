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
    python main.py --serve             # run the REST API (Swagger at /docs)

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
from onassis.profit import ProfitEngine
from onassis.revenue import RevenueEngine
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
    parser.add_argument(
        "--proposals", action="store_true", help="List submitted proposals and their status."
    )
    parser.add_argument(
        "--decisions", action="store_true", help="List the decision log (CEO + Compliance)."
    )
    parser.add_argument(
        "--compliance", action="store_true", help="List compliance reports."
    )
    parser.add_argument(
        "--dashboard", action="store_true", help="Show the profit dashboard."
    )
    parser.add_argument(
        "--orders", action="store_true", help="List recorded orders with economics."
    )
    parser.add_argument(
        "--revenue", action="store_true", help="Show today/month/company revenue & profit."
    )
    parser.add_argument(
        "--etsy-sync", action="store_true", help="Import orders/listings from Etsy (read-only)."
    )
    parser.add_argument(
        "--serve", action="store_true", help="Run the REST API (Swagger at /docs)."
    )
    parser.add_argument("--host", default="0.0.0.0", help="API host (with --serve).")
    parser.add_argument("--port", type=int, default=8000, help="API port (with --serve).")
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


def _list_proposals(db: Database) -> None:
    rows = db.list_proposals()
    if not rows:
        print("No proposals submitted yet.")
        return
    print(f"\nPROPOSALS — {len(rows)}\n")
    print(f"{'ID':>3}  {'STATUS':<10}  {'AGENT':<16}  ACTION")
    print("-" * 72)
    for p in rows:
        print(f"{p['id']:>3}  {p['status']:<10}  {p['agent_name'][:16]:<16}  "
              f"{p['requested_action'][:40]}")
    print()


def _list_decisions(db: Database) -> None:
    rows = db.list_decisions()
    if not rows:
        print("No decisions recorded yet.")
        return
    print(f"\nDECISION LOG — {len(rows)}\n")
    for d in rows:
        print(f"#{d['id']}  proposal #{d['proposal_id']}  [{d['authority']}]  {d['verdict']}")
        print(f"  {d['reasoning']}")
        print("-" * 72)
    print()


def _list_compliance(db: Database) -> None:
    rows = db.list_compliance_reports()
    if not rows:
        print("No compliance reports yet.")
        return
    print(f"\nCOMPLIANCE REPORTS — {len(rows)}\n")
    for r in rows:
        scope = f"campaign #{r['campaign_id']}" if r.get("campaign_id") else (
            f"proposal #{r['proposal_id']}" if r.get("proposal_id") else "—")
        print(f"#{r['id']}  {scope}  {r['verdict']}  score {r['compliance_score']}  "
              f"(TM {r['trademark_risk']} / CR {r['copyright_risk']} / "
              f"PL {r['platform_risk']} / brand {r['brand_consistency_score']})")
        print(f"  subject: {r['subject']}")
        print(f"  {r['reasoning']}")
        if r.get("corrections"):
            print(f"  corrections: {'; '.join(r['corrections'])}")
        print("-" * 72)
    print()


def _show_dashboard(profit: ProfitEngine) -> None:
    d = profit.dashboard()
    print("\nONASSIS PROFIT DASHBOARD\n")
    print(f"  1. Net Profit          : {d['net_profit']:.2f}")
    print(f"  2. ROI                 : {d['roi']:.2%}")
    print(f"  3. Cash Balance        : {d['cash_balance']:.2f}")
    print(f"  4. AI Cost             : {d['ai_cost']:.2f}")
    print(f"  5. Advertising Cost    : {d['advertising_cost']:.2f}")
    print(f"  6. Active Products     : {d['active_products']}")
    print(f"  7. Profit Per Product  : {d['profit_per_product'] or '—'}")
    print(f"  8. Profit Per Campaign : {d['profit_per_campaign'] or '—'}")
    print(f"\n  (today: AI spend {d['ai_spend_today']:.2f} / "
          f"budget remaining {d['remaining_ai_budget_today']:.2f})\n")


def _list_orders(revenue: RevenueEngine) -> None:
    rows = revenue.list_orders()
    if not rows:
        print("No orders recorded yet.")
        return
    print(f"\nORDERS — {len(rows)}\n")
    print(f"{'ID':>3}  {'DATE':<10}  {'PLATFORM':<10}  {'PRODUCT':<10}  "
          f"{'REVENUE':>9}  {'NET':>9}  {'MARGIN':>7}")
    print("-" * 72)
    for o in rows:
        print(f"{o['id']:>3}  {o['sale_date']:<10}  {(o['platform'] or '-'):<10}  "
              f"{(o['product_id'] or '-'):<10}  {o['gross_revenue']:>9.2f}  "
              f"{o['net_profit']:>9.2f}  {o['profit_margin']:>6.1%}")
    print()


def _show_revenue(revenue: RevenueEngine) -> None:
    today = revenue.revenue_today()
    month = revenue.revenue_month()
    company = revenue.company_profit()
    print("\nREVENUE INTELLIGENCE\n")
    print(f"  Today ({today['period']}):  {today['orders']} order(s)  "
          f"revenue {today['gross_revenue']:.2f}  net {today['net_profit']:.2f}")
    print(f"  Month ({month['period']}):  {month['orders']} order(s)  "
          f"revenue {month['gross_revenue']:.2f}  net {month['net_profit']:.2f}")
    print(f"  Company total:        revenue {company['gross_revenue']:.2f}  "
          f"net {company['net_profit']:.2f}  cash {company['cash_balance']:.2f}")
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

    if args.proposals:
        _list_proposals(db)
        return 0

    if args.decisions:
        _list_decisions(db)
        return 0

    if args.compliance:
        _list_compliance(db)
        return 0

    if args.dashboard:
        _show_dashboard(ProfitEngine(config, db))
        return 0

    if args.orders:
        _list_orders(RevenueEngine(config, db))
        return 0

    if args.revenue:
        _show_revenue(RevenueEngine(config, db))
        return 0

    if args.etsy_sync:
        from onassis.connectors.etsy import EtsyConnector

        result = EtsyConnector(config, db).sync()
        if not result.get("configured"):
            print("Etsy is not configured. Set ETSY_API_KEY, ETSY_ACCESS_TOKEN, "
                  "ETSY_SHOP_ID.")
            return 1
        print(f"Etsy sync complete: {result['imported_orders']} new order(s), "
              f"{result['imported_listings']} listing(s). "
              f"Net profit now {result['metrics']['net_profit']:.2f}.")
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

    if args.serve:
        import uvicorn

        from onassis.api import create_app

        log.info("Serving ONASSIS API on %s:%s (docs at /docs)", args.host, args.port)
        uvicorn.run(create_app(config), host=args.host, port=args.port)
        return 0

    if args.once:
        orchestrator.run_daily()
        return 0

    DailyScheduler(config, orchestrator).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
