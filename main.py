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
from onassis.daily_cycle import DailyCycle
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
        "--report", action="store_true",
        help="Daily scoreboard: Revenue / Profit / Best / Worst / Recommendation.",
    )
    parser.add_argument(
        "--market", action="store_true",
        help="Build a Market Intelligence report (demand vs competition per keyword).",
    )
    parser.add_argument(
        "--ceo", action="store_true",
        help="CEO money scoreboard: Revenue/Profit yesterday, Visitors, Conversion, "
             "Pinterest clicks, Best/Worst, Launched/Retired, Cash, AI cost, ROI.",
    )
    parser.add_argument(
        "--etsy-sync", action="store_true", help="Import orders/listings from Etsy (read-only)."
    )
    parser.add_argument(
        "--etsy-login", action="store_true",
        help="Start Etsy OAuth: print the authorisation URL to open in a browser.",
    )
    parser.add_argument(
        "--etsy-callback", metavar="CODE_OR_URL",
        help="Finish Etsy OAuth: paste the redirect URL (or the ?code= value).",
    )
    parser.add_argument(
        "--etsy-auth-status", action="store_true",
        help="Show the Etsy OAuth authorisation status (no secrets).",
    )
    parser.add_argument(
        "--collect-analytics", action="store_true",
        help="Collect a fresh snapshot of performance metrics (append-only).",
    )
    parser.add_argument(
        "--experiments", action="store_true", help="List experiments and their results."
    )
    parser.add_argument(
        "--daily-run", action="store_true",
        help="Run the daily cycle via the Operations Manager (use --mode dry_run).",
    )
    parser.add_argument(
        "--ops-check", action="store_true",
        help="Run operational pre-flight health checks (no cycle).",
    )
    parser.add_argument(
        "--readiness", action="store_true",
        help="Print the Production Readiness Report (modules, blockers, checklist).",
    )
    parser.add_argument(
        "--optimise", action="store_true",
        help="Recommend the single highest-value action for an existing product.",
    )
    parser.add_argument(
        "--opportunities", action="store_true",
        help="Show the product development backlog (ranked by commercial value).",
    )
    parser.add_argument(
        "--generate-opportunities", type=int, nargs="?", const=0, metavar="COUNT",
        help="Discover new product opportunities (no images/mock-ups/listings).",
    )
    parser.add_argument(
        "--build-design-package", metavar="OPPORTUNITY_ID",
        help="Turn one CEO-approved opportunity into a print-ready design package.",
    )
    parser.add_argument(
        "--catalogue", action="store_true",
        help="Show the Phase-1 product catalogue (Revenue Expansion).",
    )
    parser.add_argument(
        "--expansion", type=int, metavar="CAMPAIGN_ID",
        help="Score the catalogue for a design and launch the profitable set (CEO).",
    )
    parser.add_argument(
        "--generate-artwork", metavar="OPPORTUNITY_ID",
        help="Generate the REAL master artwork + print file for a built design package.",
    )
    parser.add_argument(
        "--build-listing", type=int, metavar="CAMPAIGN_ID",
        help="Build & export an upload-ready Etsy listing package for a campaign.",
    )
    parser.add_argument(
        "--publish", type=int, metavar="CAMPAIGN_ID",
        help="Publish a campaign's listing package (Draft mode).",
    )
    parser.add_argument(
        "--approve-launch", type=int, metavar="CAMPAIGN_ID",
        help="One approval — launch the master design and every CEO-approved product.",
    )
    parser.add_argument(
        "--pending-launches", action="store_true",
        help="List designs awaiting a launch approval (Launch Ready).",
    )
    parser.add_argument(
        "--mode", default=None, help="Publishing mode (dry_run | draft).",
    )
    parser.add_argument(
        "--blog-rewrite", action="store_true",
        help="One-off: rewrite existing live blog articles in place (Shopify "
             "product link, featured image, HTML body).",
    )
    parser.add_argument(
        "--fix-product-descriptions", action="store_true",
        help="One-off: reformat existing live Shopify product descriptions as HTML.",
    )
    parser.add_argument(
        "--attach-product-videos", action="store_true",
        help="One-off: attach each product's slideshow video to its Shopify "
             "product page (idempotent).",
    )
    parser.add_argument(
        "--attach-etsy-videos", action="store_true",
        help="One-off: upload each product's slideshow video to its Etsy listing "
             "(idempotent; build clips first).",
    )
    parser.add_argument(
        "--post-facebook", action="store_true",
        help="Queue + post blogs (as links) and videos to the Facebook Page.",
    )
    parser.add_argument(
        "--facebook-token", metavar="SHORT_LIVED_TOKEN", default=None,
        help="Mint a never-expiring Page token from a short-lived user token "
             "(needs App ID + Secret set on Integrations → Facebook) and save it.",
    )
    parser.add_argument(
        "--facebook-check", action="store_true",
        help="Diagnose the saved Facebook token: type, scopes, expiry, and "
             "whether it can publish.",
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
        outcome = (r.get("outcome") or r["verdict"]).upper()
        print(f"#{r['id']}  {scope}  {outcome}  score {r['compliance_score']}  "
              f"(TM {r['trademark_risk']} / CR {r['copyright_risk']} / "
              f"PL {r['platform_risk']} / brand {r['brand_consistency_score']})")
        print(f"  subject: {r['subject']}")
        print(f"  {r['reasoning']}")
        for b in r.get("blocking_issues", []):
            print(f"  BLOCK [{b.get('category')}]: {b.get('detail')}")
        for a in r.get("advisories", []):
            print(f"  • advisory: {a}")
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


def _show_report(report: dict) -> None:
    """Render the daily scoreboard: Revenue / Profit / Best / Worst / Recommendation."""
    rev, prof = report["revenue"], report["profit"]
    print(f"\nONASSIS DAILY REPORT — {report['date']}\n")
    print(f"  {report['headline']}\n")
    print(f"  Revenue   today {rev['today']:>10.2f}   month {rev['month']:>10.2f}   "
          f"company {rev['company']:>10.2f}")
    print(f"  Net Profit today {prof['today_net']:>9.2f}   month {prof['month_net']:>10.2f}   "
          f"company {prof['company_net']:>10.2f}  (margin {prof['company_margin']:.0%})")
    best, worst = report["best_seller"], report["worst_seller"]
    print(f"\n  Best seller : {best['name']} — net {best['net_profit']:.2f} "
          f"({best['units']} sold)" if best else "\n  Best seller : —")
    print(f"  Worst seller: {worst['name']} — {worst['recommendation']} "
          f"(net {worst['net_profit']:.2f}, {worst['views']} views)"
          if worst else "  Worst seller: —")
    if report["products"]:
        print(f"\n  {'PRODUCT':<20} {'UNITS':>5} {'NET':>9} {'VIEWS':>6}  RECOMMENDATION")
        print("  " + "-" * 68)
        for p in report["products"]:
            print(f"  {p['name'][:20]:<20} {p['units']:>5} {p['net_profit']:>9.2f} "
                  f"{p['views']:>6}  {p['recommendation']} — {p['reason']}")
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
    cycle = DailyCycle(config, db)
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

    if args.report:
        from onassis.reporting import DailyReport

        _show_report(DailyReport(config, db).build())
        return 0

    if args.ceo:
        from onassis.dashboard import CEODashboard

        board = CEODashboard(config, db).build()
        print("\n=== CEO DASHBOARD (money, and nothing else) ===")
        print(board["headline"])
        for label, key in (("Revenue yesterday", "revenue_yesterday"),
                           ("Profit yesterday", "profit_yesterday"),
                           ("Visitors", "visitors"), ("Conversion", "conversion"),
                           ("Pinterest clicks", "pinterest_clicks"),
                           ("Cash generated", "cash_generated"),
                           ("AI cost", "ai_cost"), ("ROI", "roi")):
            print(f"  {label:20s}: {board[key]}")
        print(f"  {'Products launched':20s}: {board['products_launched']['yesterday']}")
        print(f"  {'Products retired':20s}: {board['products_retired']['yesterday']}\n")
        return 0

    if args.market:
        from onassis.market_intelligence import MarketIntelligence

        mi = MarketIntelligence(config, db)
        mi.research()
        print("\n" + mi.format_report() + "\n"
              "The CEO builds opportunities from the highest-opportunity keywords.\n")
        return 0

    if args.etsy_login:
        from onassis.connectors.etsy_oauth import EtsyAuthError, build_etsy_oauth

        try:
            auth = build_etsy_oauth(config).create_authorization_url()
        except EtsyAuthError as exc:
            print(f"Cannot start Etsy OAuth: {exc}")
            return 1
        print("\n1. Open this URL in your browser and approve access:\n")
        print(f"   {auth['url']}\n")
        print("2. Etsy redirects to your redirect URI with ?code=...&state=...")
        print("3. Finish with:\n")
        print('   python main.py --etsy-callback "<paste the full redirect URL>"\n')
        return 0

    if args.etsy_callback:
        from urllib.parse import parse_qs, urlparse

        from onassis.connectors.etsy_oauth import EtsyAuthError, build_etsy_oauth

        raw = args.etsy_callback.strip()
        # Accept either the full redirect URL or the bare code.
        code, state = raw, None
        if "code=" in raw:
            qs = parse_qs(urlparse(raw).query)
            code = (qs.get("code") or [raw])[0]
            state = (qs.get("state") or [None])[0]
        try:
            build_etsy_oauth(config).exchange_code(code, state=state)
        except EtsyAuthError as exc:
            print(f"Etsy authorisation failed: {exc}")
            return 1
        print("Etsy authorisation complete — tokens stored securely. "
              "Try: python main.py --etsy-auth-status")
        return 0

    if args.etsy_auth_status:
        from onassis.connectors.etsy_oauth import build_etsy_oauth

        st = build_etsy_oauth(config).status()
        print("\nETSY OAUTH STATUS")
        print(f"  configured : {st['configured']}")
        print(f"  authorised : {st['authorised']}")
        if st["authorised"]:
            exp = st["expires_in"]
            print(f"  access tok : {'present' if st['has_access_token'] else 'none'} "
                  f"({'expired' if st['expired'] else f'{exp}s left'})")
        print(f"  scopes     : {', '.join(st['scopes'])}\n")
        return 0

    if args.etsy_sync:
        from onassis.connectors.etsy import EtsyConnector

        result = EtsyConnector(config, db).sync()
        if not result.get("configured"):
            print("Etsy is not configured. Set ETSY_CLIENT_ID and ETSY_SHOP_ID, then "
                  "authorise: python main.py --etsy-login")
            return 1
        print(f"Etsy sync complete: {result['imported_orders']} new order(s), "
              f"{result['imported_listings']} listing(s). "
              f"Net profit now {result['metrics']['net_profit']:.2f}.")
        return 0

    if args.optimise:
        from onassis.optimiser import ProductOptimiser

        rec = ProductOptimiser(config, db).top_recommendation()
        if rec is None:
            print("No live products to analyse.")
            return 0
        print(f"\nPRODUCT OPTIMISER\n")
        print(f"  Product    : {rec['product']} ({rec['product_name']})")
        print(f"  Recommend  : {rec['recommendation']}")
        print(f"  Est. cost  : {rec['estimated_cost']:.2f}  "
              f"Expected +profit: {rec['expected_increase_in_profit']:.2f}  "
              f"ROI: {rec['expected_roi']:.2f}")
        print(f"  Confidence : {rec['confidence']}%   CEO: {rec['ceo']['verdict']}")
        print(f"  Reasoning  : {rec['reasoning']}\n")
        return 0

    if args.generate_opportunities is not None:
        from onassis.opportunities import OpportunityEngine

        count = args.generate_opportunities or None  # 0/None -> config default
        result = OpportunityEngine(config, db).generate(count)
        print(f"\nGenerated {result['generated']} opportunity(ies) "
              f"({result['duplicates_skipped']} duplicate(s) skipped):\n")
        for o in result["opportunities"]:
            print(f"  [{o['expected_value']:5.1f}] {o['opportunity_id']}  "
                  f"{o['product_name']} — {o['product_type']} / {o['theme']}")
        print()
        return 0

    if args.opportunities:
        from onassis.opportunities import OpportunityEngine

        rows = OpportunityEngine(config, db).top(limit=20)
        if not rows:
            print("Product backlog is empty. Generate ideas with "
                  "--generate-opportunities.")
            return 0
        print("\nPRODUCT DEVELOPMENT BACKLOG (top, ranked by commercial value)\n")
        for o in rows:
            print(f"  [{o['expected_value']:5.1f}] {o['opportunity_id']}  "
                  f"{o['product_name']}")
            print(f"           {o['product_type']} · {o['theme']} · "
                  f"demand {o['estimated_demand']} / competition "
                  f"{o['estimated_competition']} / conf {o['confidence']}")
        print()
        return 0

    if args.catalogue:
        from onassis.expansion import RevenueExpansionEngine

        cat = RevenueExpansionEngine(config, db).catalogue()
        print(f"\nPRODUCT CATALOGUE (Phase 1) — {len(cat)} product(s)\n")
        for p in cat:
            print(f"  {p['name']:22} cost {p['production_cost']:6.2f}  "
                  f"retail {p['retail_price']:6.2f}  ({p['gelato_uid']})")
        print()
        return 0

    if args.expansion is not None:
        from onassis.expansion import RevenueExpansionEngine

        eng = RevenueExpansionEngine(config, db)
        eng.learn_from_sales()
        plan = eng.plan(args.expansion)
        print(f"\nEXPANSION PLAN — campaign #{plan['campaign_id']} "
              f"(threshold {plan['threshold']:.0f}/100)\n")
        print(f"  Launched {plan['products_launched']} of {plan['products_scored']} "
              f"product(s):\n")
        for s in plan["scored"]:
            flag = "LAUNCH" if s["launched"] else "  -   "
            print(f"  [{flag}] {s['product_name']:22} {s['composite_score']:5.1f}/100  "
                  f"(profit {s['expected_profit']:.2f})")
        if plan["skipped_unavailable"]:
            print(f"\n  Skipped (unavailable): {', '.join(plan['skipped_unavailable'])}")
        print()
        return 0

    if args.build_design_package is not None:
        from onassis.design_package import DesignPackageBuilder, DesignPackageError

        try:
            pkg = DesignPackageBuilder(config, db).build(args.build_design_package)
        except DesignPackageError as exc:
            print(f"Error: {exc}")
            return 1
        if pkg.get("status") != "ready":
            print(f"\nDesign package blocked: {pkg.get('reason')}")
            if pkg.get("ceo"):
                print(f"  CEO: {pkg['ceo']['verdict']} — {pkg['ceo']['reasoning']}")
            if pkg.get("compliance"):
                print(f"  Compliance: {pkg['compliance']['verdict']} "
                      f"(score {pkg['compliance']['compliance_score']})")
            print()
            return 1
        b = pkg["design_brief"]
        print(f"\nDESIGN PACKAGE READY — {args.build_design_package}\n")
        print(f"  Product    : {b['product_name']}")
        print(f"  Garment    : {b['shirt_colour']} · print {b['print_colour']}")
        print(f"  Placement  : {b['print_placement']} ({b['print_size_guidance']})")
        print(f"  Format     : {b['file_format_requirements']}")
        print(f"  Transparent: {b['transparent_background_required']}  "
              f"DPI: {b['dpi_requirement']}")
        print(f"  Files      : {', '.join(pkg['files'])}")
        print(f"  Path       : {pkg['path']}\n")
        return 0

    if args.generate_artwork is not None:
        from pathlib import Path

        from onassis.artwork import ArtworkStudio
        from onassis.design_package import DesignPackageBuilder

        pkg = DesignPackageBuilder(config, db).get_package(args.generate_artwork)
        if pkg is None:
            print(f"No design package for {args.generate_artwork}. Build it first with "
                  f"--build-design-package {args.generate_artwork}.")
            return 1
        studio = ArtworkStudio(config, db)
        result = studio.generate_master(pkg, Path(pkg["path"]))
        print(f"\nMASTER ARTWORK GENERATED — {args.generate_artwork} "
              f"(backend: {result['backend']})\n")
        print(f"  Files      : {', '.join(result['files'])}")
        print(f"  Path       : {result['path']}")
        print(f"  Master QC  : {'PASS' if result['master_review']['accepted'] else 'BEST-EFFORT'}"
              f" (score {result['master_review']['score']:.0f})")
        print(f"  Print QC   : {'PASS' if result['print_review']['accepted'] else 'BEST-EFFORT'}"
              f" (score {result['print_review']['score']:.0f})\n")
        return 0

    if args.build_listing is not None:
        from onassis.listing_factory import ListingError, ListingFactory

        try:
            pkg = ListingFactory(config, db).export(args.build_listing)
        except ListingError as exc:
            print(f"Error: {exc}")
            return 1
        if pkg.get("status") != "ready":
            print(f"Listing blocked: {pkg.get('reason')}")
            return 1
        v = pkg["validation"]
        print(f"\nLISTING PACKAGE READY — campaign #{pkg['campaign_id']}\n")
        print(f"  Path     : {pkg['path']}")
        print(f"  Title    : {pkg['listing']['title']}")
        print(f"  Tags     : {len(pkg['listing']['tags'])}  Price: "
              f"{pkg['listing']['price']} {pkg['listing']['currency']}")
        print(f"  Images   : {v['present_images']}/{v['required_images']} present  "
              f"(all present: {v['all_images_present']})\n")
        return 0

    if args.collect_analytics:
        from onassis.analytics import AnalyticsEngine

        result = AnalyticsEngine(config, db).collect()
        print(f"Collected {result['collected']} metric snapshot(s): {result['by_source']}")
        return 0

    if args.ops_check:
        from onassis.operations import OperationsManager

        mode = args.mode or "production"
        result = OperationsManager(config, db).check(mode)
        print(f"\nOPERATIONS PRE-FLIGHT ({mode}) — "
              f"{'HEALTHY' if result['healthy'] else 'NOT HEALTHY'}\n")
        for c in result["checks"]:
            flag = "*" if c["critical"] else " "
            print(f"  [{c['status']:<4}]{flag} {c['label']:<22} {c['detail']}")
        print("\n  (* = critical: failure aborts the cycle)\n")
        return 0

    if args.readiness:
        from onassis.production_readiness import ProductionReadiness

        report = ProductionReadiness(config, db).report()
        s = report["summary"]
        print(f"\nPRODUCTION READINESS ({report['environment']}) — "
              f"{'PRODUCTION READY' if report['production_ready'] else 'NOT READY'}\n")
        print(f"  Modules: {s['production_ready']} ready / "
              f"{s['partially_ready']} partial / {s['development_only']} dev-only\n")
        for m in report["modules"]:
            print(f"  [{m['status']:<16}] {m['name']}")
        print("\n  SUBSYSTEMS")
        for sub in report["subsystems"]:
            print(f"    [{sub['status']:<16}] {sub['subsystem']}")
        if report["blockers"]:
            print(f"\n  BLOCKERS ({len(report['blockers'])})")
            for b in report["blockers"]:
                print(f"    ({b['priority']}) {b['module']}: {b['description']}")
                print(f"        fix: {b['recommended_fix']}  [{b['estimated_effort']}]")
        print("\n  CHECKLIST")
        for c in report["checklist"]:
            print(f"    {c['mark']} {c['item']}")
        print()
        return 0

    if args.daily_run:
        from onassis.operations import OperationsManager

        mode = args.mode or "production"
        summary = OperationsManager(config, db).run(mode=mode)
        if summary.get("aborted"):
            print(f"\nDAILY CYCLE ABORTED ({mode}) by Operations Manager:")
            for r in summary["operations_report"]["recommendations"]:
                print(f"  - {r}")
            print()
            return 1
        rep = summary["operations_report"]
        print(f"\nDAILY CYCLE — run #{summary['run_id']} ({summary['mode']}) "
              f"— {summary['status']} in {summary['duration_seconds']}s "
              f"(health: {rep['system']['overall_health']})\n")
        for s in summary["stages"]:
            line = f"  {s['stage']:<22} {s['status']}"
            if s.get("error"):
                line += f"  [error: {s['error']}]"
            print(line)
        print()
        return 0

    if args.experiments:
        from onassis.experiments import ExperimentEngine

        rows = ExperimentEngine(config, db).list()
        if not rows:
            print("No experiments yet.")
            return 0
        print(f"\nEXPERIMENTS — {len(rows)}\n")
        for e in rows:
            tail = (f"result {e['result']}"
                    + (f" @ {e['confidence']}% conf" if e.get("confidence") else "")
                    ) if e["status"] == "completed" else e["status"]
            print(f"  #{e['id']}  {e['product_id']}/{e['variable']:<16}  {tail}")
            if e.get("learning"):
                print(f"        learning: {e['learning']}")
        print()
        return 0

    if args.publish is not None:
        from onassis.publishing import PublisherService

        result = PublisherService(config, db).publish(args.publish, mode=args.mode)
        print(f"Publish campaign #{args.publish}: {result['status']}"
              + (f" — {result['reason']}" if result.get("reason") else "")
              + (f" (listing {result['publication']['listing_id']})"
                 if result.get("publication", {}).get("listing_id") else ""))
        return 0 if result["status"] in ("draft", "dry_run") else 1

    if args.approve_launch is not None:
        from onassis.publishing import PublisherService

        result = PublisherService(config, db).approve_launch(args.approve_launch)
        if result["status"] == "blocked":
            print(f"Launch approval blocked for campaign #{args.approve_launch}: "
                  f"{result['reason']}")
            return 1
        products = result.get("products", [])
        print(f"\nLAUNCH APPROVED — campaign #{args.approve_launch} "
              f"(by {result['approved_by']})")
        print(f"  {len(products)} product(s) ready to publish: "
              f"{', '.join(products) if products else '—'}\n")
        return 0

    if args.pending_launches:
        from onassis.publishing import PublisherService

        rows = PublisherService(config, db).pending_launches()
        if not rows:
            print("No designs awaiting launch approval.")
            return 0
        print(f"\nPENDING LAUNCHES — {len(rows)} design(s) awaiting approval\n")
        for r in rows:
            print(f"  campaign #{r['campaign_id']}  policy {r.get('policy', '-'):<9}  "
                  f"{r.get('products', 0)} product(s)  (since {(r.get('created_at') or '')[:10]})")
        print()
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

    if args.blog_rewrite:
        from onassis.content_engine import ContentEngine
        from onassis.integrations import apply_integration_overrides

        # Overlay the operator's UI-saved settings (Blog ID, tokens) — otherwise
        # the CLI reads config.yaml only and targets the wrong/empty blog.
        apply_integration_overrides(config, db)
        res = ContentEngine(config, db).rewrite_live_blog_articles()
        if not res.get("ok"):
            print(f"Blog rewrite skipped: {res.get('reason')}")
            return 1
        print(f"\nBLOG REWRITE — blog {res.get('blog_id')}, "
              f"{res.get('live_count', 0)} live article(s), "
              f"{res.get('products', 0)} product(s) in catalogue.")
        print(f"  {res['rewritten']} updated, {res['skipped']} left unchanged.")
        if res.get("reason"):
            print(f"  {res['reason']}")
        for d in res.get("details", []):
            mark = "✓" if d.get("ok") else "•"
            note = "" if d.get("ok") else f"  ({d.get('reason', '')})"
            print(f"  {mark} {str(d.get('title'))[:60]}{note}")
        return 0

    if args.fix_product_descriptions:
        from onassis.content_engine import ContentEngine
        from onassis.integrations import apply_integration_overrides

        apply_integration_overrides(config, db)
        res = ContentEngine(config, db).reformat_shopify_descriptions()
        if not res.get("ok"):
            print(f"Description reformat skipped: {res.get('reason')}")
            return 1
        print(f"\nPRODUCT DESCRIPTIONS — {res['updated']} reformatted, "
              f"{res['skipped']} unchanged (of {res['checked']} product(s)).")
        for d in res.get("details", []):
            mark = "✓ changed" if d.get("changed") else f"· {d.get('reason', '')}"
            print(f"  [{d.get('id')}] {mark}")
            if d.get("before"):
                print(f"      now: {d['before']}")
        return 0

    if args.attach_product_videos:
        from onassis.content_engine import ContentEngine
        from onassis.integrations import apply_integration_overrides

        apply_integration_overrides(config, db)
        res = ContentEngine(config, db).attach_videos_to_shopify()
        if not res.get("ok"):
            print(f"Attach videos skipped: {res.get('reason')}")
            return 1
        print(f"\nPRODUCT VIDEOS — {res['added']} attached, {res['skipped']} "
              f"already had one / skipped (of {res['checked']} product(s)).")
        return 0

    if args.attach_etsy_videos:
        from onassis.content_engine import ContentEngine
        from onassis.integrations import apply_integration_overrides

        apply_integration_overrides(config, db)
        res = ContentEngine(config, db).attach_videos_to_etsy()
        if not res.get("ok"):
            print(f"Attach Etsy videos skipped: {res.get('reason')}")
            return 1
        print(f"\nETSY VIDEOS — {res['added']} uploaded, {res['skipped']} "
              f"already had one / skipped (of {res['checked']} listing(s)).")
        return 0

    if args.facebook_check:
        from datetime import datetime, timezone

        from onassis.connectors.social import MetaGraphClient
        from onassis.integrations import apply_integration_overrides

        apply_integration_overrides(config, db)
        meta = config.meta or {}
        token = meta.get("page_access_token")
        app_id, secret = meta.get("app_id"), meta.get("app_secret")
        if not token:
            print("No Facebook token saved. Add one on Integrations → Facebook.")
            return 1
        if not app_id or not secret:
            print("Set App ID + App Secret on Integrations → Facebook to inspect "
                  "the token.")
            return 1
        try:
            info = MetaGraphClient(access_token=token).debug_token(token, app_id, secret)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not inspect the token: {exc}")
            return 1
        scopes = info.get("scopes") or []
        exp = info.get("expires_at")
        exp_txt = ("never" if not exp else
                   datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d"))
        print(f"\nFACEBOOK TOKEN — type {info.get('type', '?')}, "
              f"valid {'yes' if info.get('is_valid') else 'NO'}, expires {exp_txt}.")
        print(f"  scopes: {', '.join(scopes) or '(none)'}")
        need = [s for s in ("pages_show_list", "pages_read_engagement",
                            "pages_manage_posts") if s not in scopes]
        if info.get("type") != "PAGE":
            print("  ✗ This is a USER token — you must use the PAGE token. Run "
                  "--facebook-token to derive it (or use a System User token).")
        if need:
            print(f"  ✗ MISSING scope(s): {', '.join(need)} — add these to the "
                  "token; without pages_manage_posts you cannot publish.")
        elif info.get("type") == "PAGE":
            print("  ✓ Looks good — type PAGE with publish scope. --post-facebook "
                  "should work.")
        return 0

    if args.facebook_token:
        from onassis.connectors.social import mint_page_token
        from onassis.integrations import IntegrationManager, apply_integration_overrides

        apply_integration_overrides(config, db)
        meta = config.meta or {}
        app_id, secret = meta.get("app_id"), meta.get("app_secret")
        page_id = meta.get("facebook_page_id")
        if not app_id or not secret:
            print("Set App ID + App Secret on Integrations → Facebook first "
                  "(needed to mint a long-lived token).")
            return 1
        try:
            res = mint_page_token(app_id, secret, args.facebook_token, page_id)
        except Exception as exc:  # noqa: BLE001 — turn Meta errors into guidance
            print(f"Facebook rejected the request: {exc}")
            print("\nThe token needs these scopes — regenerate it in Graph API "
                  "Explorer with ALL of them ticked:")
            print("  pages_show_list, pages_read_engagement, pages_manage_posts")
            return 1
        if not res.get("ok"):
            print(f"Could not mint a Page token: {res.get('error')}")
            if res.get("pages"):
                print("  Pages you manage:")
                for p in res["pages"]:
                    print(f"    id {p['id']}  —  {p['name']}")
            return 1
        IntegrationManager(config, db).save(
            "facebook", {"page_access_token": res["page_token"],
                         "facebook_page_id": res["page_id"]})
        print(f"\n✓ Saved a never-expiring Page token for '{res['page_name']}' "
              f"(id {res['page_id']}). Facebook posting is ready — run "
              f"--post-facebook.")
        return 0

    if args.post_facebook:
        from onassis.content_engine import ContentEngine
        from onassis.distribution import ChannelDistributor
        from onassis.integrations import apply_integration_overrides

        apply_integration_overrides(config, db)
        dist = ChannelDistributor(config, db)
        if not dist.can_distribute("facebook"):
            print("Facebook isn't connected — set the Page Access Token + Page ID "
                  "on Integrations → Facebook, then re-run.")
            return 1
        q = ContentEngine(config, db).queue_facebook_posts()
        # Re-queue anything the daily run skipped/failed earlier (e.g. the toggle
        # was off) so an explicit push always ships everything outstanding.
        db.reset_failed_marketing_assets("facebook", include_skipped=True)
        print(f"\nFACEBOOK — queued {q.get('blogs', 0)} blog link(s) + "
              f"{q.get('videos', 0)} video(s).")
        out = dist.distribute(channels=["facebook"], force=True)
        fb = (out.get("by_channel") or {}).get("facebook", {})
        print(f"  posted {fb.get('posted', 0)}, failed {fb.get('failed', 0)}, "
              f"skipped {fb.get('skipped', 0)}.")
        return 0

    if args.serve:
        import uvicorn

        from onassis.api import create_app

        log.info("Serving ONASSIS API on %s:%s (docs at /docs)", args.host, args.port)
        uvicorn.run(create_app(config), host=args.host, port=args.port)
        return 0

    if args.once:
        result = cycle.run("production")
        print(f"\nProduction cycle #{result['run_id']} — {result['status']} "
              f"in {result['duration_seconds']}s")
        if result.get("opportunity_id"):
            print(f"  Product opportunity : {result['opportunity_id']}")
        if result.get("campaign_id"):
            print(f"  Campaign            : #{result['campaign_id']}")
        # Revenue-first streaming: show each product's outcome as it was published.
        stream = result.get("stream") or []
        if stream:
            print("\n  PRODUCTS (streamed — first sellable product live first)\n")
            for i, p in enumerate(stream, start=1):
                line = f"  {i}. {p['product_name'][:24]:<24} {p['status'].upper()}"
                if p.get("listing_id"):
                    line += (f"  listing {p['listing_id']}  "
                             f"images {p.get('images_uploaded', 0)}/{p.get('images', 0)}")
                    if p.get("published_at"):
                        line += f"  {p['published_at'][11:19]}"
                elif p["status"] == "failed":
                    line += f"  (failed at {p.get('stage', '?')}: {p.get('reason', '')[:50]})"
                print(line)
                for advisory in p.get("advisories", []):
                    print(f"       • advisory: {advisory}")
            if result.get("first_draft_at"):
                print(f"\n  First Etsy draft at : {result['first_draft_at'][11:19]}  "
                      f"(live: {result.get('products_live', 0)})")
        print()
        return 0

    DailyScheduler(config, cycle).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
