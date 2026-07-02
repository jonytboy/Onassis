"""The Daily Cycle — ONASSIS's single execution entry point.

This module **only orchestrates** existing modules in a fixed order. It adds no
new agents and makes no decisions of its own — every stage delegates to a
module that already owns that responsibility.

The cycle is **product-first**: marketing is generated only after a commercially
viable product exists. After observation and CEO/Compliance approval it creates
the product, then promotes it.

Order:
    1. Sync Etsy                 2. Sync Pinterest        3. Import Revenue
    4. Import Analytics          5. Run Product Optimiser 6. CEO Decision
    7. Create Product Opportunity     (the product idea, CEO-approved)
    8. Build Design Package           (design brief + artwork prompt, compliance-gated)
    9. Generate Master Artwork        (the REAL master artwork + print file, QC-gated)
   10. Create Product Campaign        (campaign + product from the opportunity)
   11. Expand Products                (score the catalogue; CEO launches the profitable set)
   12. Build Etsy Listing Package     (real artwork, mockups & 8-10 image gallery per product)
   13. Generate Marketing Content     (Pinterest/Instagram/Facebook — promotes the product)
   14. Publish Live                   (draft + activate LIVE on Etsy, margin-guarded)
   15. Promote on Pinterest           (pins that link back to each live listing)
   16. Daily Report                   (Revenue / Profit / Best / Worst / Recommendation)
   17. Record Results

Every stage logs start/finish, records its duration, captures failures, and the
cycle continues safely past a failed stage. Two modes are supported: ``dry_run``
(observation + decision only — no product creation, content, or publishing) and
``production`` (the full cycle). No scheduling/cron/timers — just coordination.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from onassis.analytics import AnalyticsEngine
from onassis.artwork import ArtworkStudio
from onassis.config import Config
from onassis.connectors.etsy import EtsyConnector
from onassis.connectors.pinterest import PinterestConnector
from onassis.database import Database
from onassis.design_package import DesignPackageBuilder
from onassis.expansion import RevenueExpansionEngine
from onassis.listing_factory import ListingFactory
from onassis.logger import get_logger
from onassis.opportunities import OpportunityEngine
from onassis.optimiser import ProductOptimiser
from onassis.orchestrator import Orchestrator
from onassis.profit import ProfitEngine
from onassis.proposals import APPROVE, is_compliant
from onassis.publishing import PublisherService
from onassis.reporting import DailyReport
from onassis.revenue import RevenueEngine

log = get_logger(__name__)


class DailyCycle:
    """Coordinates the existing modules into one daily operating cycle."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        # Reuse the existing, independent modules — coordinate, don't replace.
        self.etsy = EtsyConnector(config, db)
        self.pinterest = PinterestConnector(config)
        self.revenue = RevenueEngine(config, db)
        self.profit = ProfitEngine(config, db)
        self.analytics = AnalyticsEngine(config, db)
        self.optimiser = ProductOptimiser(config, db)
        self.opportunities = OpportunityEngine(config, db)
        self.design = DesignPackageBuilder(config, db)
        self.expansion = RevenueExpansionEngine(config, db)
        self.orchestrator = Orchestrator(config, db)
        # One Artwork Studio produces every real image; the Listing Factory
        # reuses it so master artwork and product galleries share a backend.
        self.artwork = ArtworkStudio(config, db)
        self.listing_factory = ListingFactory(config, db, studio=self.artwork)
        self.publisher = PublisherService(config, db)
        self.report = DailyReport(config, db)
        # Campaign/Brain/Compliance are owned by the orchestrator — reuse them.
        self.campaigns = self.orchestrator.campaigns
        self.brain = self.orchestrator.brain
        self.compliance = self.orchestrator.compliance

    # --- Entry point ------------------------------------------------

    def run(self, mode: str = "production") -> dict[str, Any]:
        """Run the full cycle. ``mode`` is 'production' or 'dry_run'."""
        dry = mode == "dry_run"
        ctx: dict[str, Any] = {"dry": dry}
        stages: list[dict[str, Any]] = []
        started = datetime.now(timezone.utc).isoformat()
        log.info("=== ONASSIS daily cycle START (mode=%s) ===", mode)

        self._stage(stages, "Sync Etsy", self._sync_etsy, ctx)
        self._stage(stages, "Sync Pinterest", self._sync_pinterest, ctx)
        self._stage(stages, "Import Revenue", self._import_revenue, ctx)
        self._stage(stages, "Import Analytics", self._import_analytics, ctx)
        self._stage(stages, "Run Product Optimiser", self._run_optimiser, ctx)
        self._stage(stages, "CEO Decision", self._ceo_decision, ctx)
        # --- Product first: create the product, then promote it. ---
        self._stage(stages, "Create Product Opportunity", self._create_opportunity, ctx)
        self._stage(stages, "Build Design Package", self._build_design_package, ctx)
        self._stage(stages, "Generate Master Artwork", self._generate_master_artwork, ctx)
        self._stage(stages, "Create Product Campaign", self._create_campaign, ctx)
        self._stage(stages, "Expand Products", self._expand_products, ctx)
        self._stage(stages, "Build Etsy Listing Package", self._build_listing, ctx)
        self._stage(stages, "Generate Marketing Content", self._generate_content, ctx)
        self._stage(stages, "Publish Live", self._publish, ctx)
        self._stage(stages, "Promote on Pinterest", self._promote, ctx)
        self._stage(stages, "Daily Report", self._daily_report, ctx)
        # Final stage — Record Results — is the persistence below.
        stages.append({"stage": "Record Results", "status": "ok",
                       "duration_seconds": 0.0, "detail": None, "error": None})

        status = ("completed_with_failures"
                  if any(s["status"] == "failed" for s in stages) else "completed")
        duration = round(sum(s["duration_seconds"] for s in stages), 3)
        record = {"mode": mode, "status": status, "duration_seconds": duration,
                  "stages": stages}
        run_id = self.db.insert_daily_run(record)
        log.info("=== ONASSIS daily cycle DONE (run #%s, %s) ===", run_id, status)
        return {
            "run_id": run_id, "mode": mode, "status": status,
            "started_at": started, "duration_seconds": duration, "stages": stages,
            # The product this cycle created (the workflow's primary output).
            "opportunity_id": (ctx.get("opportunity") or {}).get("opportunity_id"),
            "campaign_id": ctx.get("campaign_id"),
            "listing_ready": ctx.get("listing_ready", False),
            "assets_created": ctx.get("content_items", 0),
            "products_launched": (ctx.get("expansion") or {}).get("products_launched", 0),
            "launch_status": (ctx.get("launch") or {}).get("status"),
            "products_live": self._go_live_result(ctx).get("live", 0),
            "pins_posted": (ctx.get("promotion") or {}).get("posted", 0),
            "report": ctx.get("report"),
        }

    @staticmethod
    def _go_live_result(ctx: dict[str, Any]) -> dict[str, Any]:
        """The go-live outcome, whether returned inline or nested under the
        automatic-policy approval."""
        launch = ctx.get("launch") or {}
        return (launch.get("go_live")
                or (launch.get("launch") or {}).get("go_live") or {})

    # --- Stage runner -----------------------------------------------

    def _stage(
        self, stages: list[dict[str, Any]], name: str,
        fn: Callable[[dict[str, Any]], dict[str, Any]], ctx: dict[str, Any],
    ) -> None:
        t0 = time.monotonic()
        log.info("[daily] %s: start", name)
        status, detail, error = "ok", None, None
        try:
            result = fn(ctx) or {}
            status = result.get("status", "ok")
            detail = result.get("detail")
        except Exception as exc:  # continue safely past a failed stage
            status, error = "failed", str(exc)
            log.exception("[daily] %s failed", name)
        duration = round(time.monotonic() - t0, 3)
        log.info("[daily] %s: %s (%.3fs)", name, status, duration)
        stages.append({"stage": name, "status": status,
                       "duration_seconds": duration, "detail": detail, "error": error})

    # --- Stages (each delegates to an existing module) --------------

    def _sync_etsy(self, ctx: dict[str, Any]) -> dict[str, Any]:
        if not self.etsy.is_configured:
            return {"status": "skipped", "detail": "Etsy not configured"}
        r = self.etsy.sync()
        if not r.get("configured", True):
            return {"status": "skipped", "detail": r.get("message")}
        return {"status": "ok", "detail": {
            "imported_orders": r.get("imported_orders"),
            "imported_listings": r.get("imported_listings")}}

    def _sync_pinterest(self, ctx: dict[str, Any]) -> dict[str, Any]:
        if not self.pinterest.is_configured:
            return {"status": "skipped", "detail": "Pinterest not configured"}
        rows = self.pinterest.fetch_metrics()
        return {"status": "ok", "detail": {"metrics": len(rows)}}

    def _import_revenue(self, ctx: dict[str, Any]) -> dict[str, Any]:
        profit = self.revenue.company_profit()
        ctx["revenue"] = profit
        return {"status": "ok", "detail": profit}

    def _import_analytics(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "detail": self.analytics.collect()}

    def _run_optimiser(self, ctx: dict[str, Any]) -> dict[str, Any]:
        rec = self.optimiser.top_recommendation()
        ctx["recommendation"] = rec
        if rec is None:
            return {"status": "skipped", "detail": "no live products"}
        return {"status": "ok", "detail": {
            "product": rec["product"], "recommendation": rec["recommendation"],
            "expected_roi": rec["expected_roi"], "confidence": rec["confidence"]}}

    def _ceo_decision(self, ctx: dict[str, Any]) -> dict[str, Any]:
        rec = ctx.get("recommendation")
        if not rec:
            ctx["approved"] = False
            return {"status": "skipped", "detail": "no recommendation to decide on"}
        verdict = rec["ceo"]["verdict"]  # reuse the CEO's decision — no duplication
        ctx["approved"] = verdict == "APPROVE"
        return {"status": "ok", "detail": {"verdict": verdict, "approved": ctx["approved"]}}

    def _create_opportunity(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Pick (or generate) a CEO-approved product opportunity to build."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        if not self.opportunities.top(limit=1):
            self.opportunities.generate()  # backlog empty — discover ideas
        choice = self.opportunities.select_next(agent_name="DailyCycle")
        if choice is None:
            return {"status": "skipped", "detail": "no product opportunity available"}
        opp = choice["opportunity"]
        ctx["opportunity"] = opp
        if choice["ceo"]["verdict"] != APPROVE:
            return {"status": "blocked",
                    "detail": f"CEO did not approve opportunity {opp['opportunity_id']}"}
        return {"status": "ok", "detail": {
            "opportunity_id": opp["opportunity_id"], "product": opp["product_name"]}}

    def _build_design_package(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Turn the approved opportunity into a print-ready design package."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        opp = ctx.get("opportunity")
        if not opp:
            return {"status": "skipped", "detail": "no opportunity"}
        pkg = self.design.build(opp["opportunity_id"])
        if pkg.get("status") != "ready":
            return {"status": "blocked", "detail": pkg.get("reason")}
        ctx["design_package"] = pkg
        return {"status": "ok", "detail": {"path": pkg["path"]}}

    def _generate_master_artwork(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Generate the REAL master artwork + print file from the design, and
        run the artwork quality gate before it feeds product production."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        pkg = ctx.get("design_package")
        if not pkg:
            return {"status": "skipped", "detail": "no design package"}
        from pathlib import Path

        master = self.artwork.generate_master(pkg, Path(pkg["path"]))
        ctx["master_artwork"] = master
        return {"status": "ok", "detail": {
            "backend": master["backend"],
            "files": master["files"],
            "master_quality": master["master_review"]["score"],
            "print_quality": master["print_review"]["score"],
            "master_accepted": master["master_review"]["accepted"],
            "print_accepted": master["print_review"]["accepted"]}}

    def _create_campaign(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Create the campaign + product FROM the opportunity (product-driven)."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        opp = ctx.get("opportunity")
        if not opp:
            return {"status": "skipped", "detail": "no opportunity"}
        result = self.campaigns.create_from_opportunity(opp, design=ctx.get("design_package"))
        campaign, brief = result["campaign"], result["brief"]
        ctx["campaign_id"] = campaign["id"]
        ctx["brief"] = brief
        # Record the campaign's estimated AI cost in the profit ledger.
        ai_cost = float((self.config.profit or {}).get("ai_cost_per_campaign", 0) or 0)
        if ai_cost:
            self.profit.record_campaign_ai_cost(campaign["id"], ai_cost)
        # Governance for the marketing campaign: predict + compliance review.
        self.brain.generate_for_campaign(campaign, brief)
        review = self.compliance.review_campaign(campaign, [])
        # Cleared to proceed on APPROVE or APPROVE_WITH_CHANGES; only REJECT blocks
        # (the autonomous pipeline never stalls waiting for a human).
        ctx["campaign_approved"] = is_compliant(review["verdict"])
        if not ctx["campaign_approved"]:
            return {"status": "blocked", "detail": "campaign REJECTED by compliance"}
        return {"status": "ok", "detail": {"campaign_id": campaign["id"],
                                           "product": opp["product_name"]}}

    def _expand_products(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Score the catalogue for this design; the CEO launches the profitable
        set. Learns from sales first so scores adapt over time."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        if not cid or not ctx.get("campaign_approved"):
            return {"status": "skipped", "detail": "no approved product campaign"}
        self.expansion.learn_from_sales()  # sales continually adjust the scores
        plan = self.expansion.plan(cid, ctx.get("opportunity"))
        ctx["expansion"] = plan
        return {"status": "ok", "detail": {
            "products_launched": plan["products_launched"],
            "products_scored": plan["products_scored"],
            "launched": [s["product_key"] for s in plan["launched"]]}}

    def _build_listing(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Build one Etsy listing package per CEO-approved product."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        if not cid or not ctx.get("campaign_approved"):
            return {"status": "skipped", "detail": "no approved campaign"}
        pkg = self.listing_factory.export_products(
            cid, design_package=ctx.get("design_package"))
        if pkg.get("status") != "ready":
            return {"status": "blocked", "detail": pkg.get("reason")}
        ctx["listing_ready"] = True
        images = sum(len(p.get("listing", {}).get("images", []))
                     for p in pkg["products"] if p.get("status") == "ready")
        return {"status": "ok", "detail": {"products_built": pkg["count"],
                                           "images_generated": images}}

    def _generate_content(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Generate marketing content LAST — only to promote the new product."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        brief = ctx.get("brief")
        if not brief or not ctx.get("campaign_approved"):
            return {"status": "skipped", "detail": "no approved product campaign"}
        content = self.orchestrator.generate_marketing_content(brief)
        ctx["content_items"] = len(content["items"])
        return {"status": "ok", "detail": {"items": len(content["items"])}}

    def _publish(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Draft each approved product, then (per launch policy) take the approved
        set LIVE on Etsy so the products are actually buyable."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        if not cid:
            return {"status": "skipped", "detail": "no campaign to publish"}
        result = self.publisher.launch(cid, mode="draft")
        if result.get("status") == "blocked":
            return {"status": "skipped", "detail": result.get("reason")}
        ctx["launch"] = result
        drafts = result.get("drafts", {})
        statuses = [r["status"] for r in drafts.get("results", [])]
        if any(s in ("draft", "dry_run") for s in statuses):
            mapped = "ok"
        elif any(s == "failed" for s in statuses):
            mapped = "failed"
        else:
            mapped = "skipped"
        go_live = (result.get("launch") or {}).get("go_live") or result.get("go_live") or {}
        return {"status": mapped, "detail": {
            "launch_status": result["status"], "policy": result.get("policy"),
            "published": drafts.get("published", 0),
            "products_live": go_live.get("live", 0),
            "results": drafts.get("results", [])}}

    def _promote(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Promote each LIVE product on Pinterest — pins that link back to the
        Etsy listing (free, high-intent traffic). Safe no-op until configured."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        if not self.pinterest.can_publish:
            return {"status": "skipped", "detail": "Pinterest not configured for publishing"}
        live = self._live_products(ctx)
        if not live:
            return {"status": "skipped", "detail": "no live listings to promote"}
        cid = ctx["campaign_id"]
        posted, per_product = 0, []
        for product_key, listing_id in live:
            pins = self._build_pins(cid, product_key, listing_id)
            res = self.pinterest.publish_pins(pins)
            posted += res["posted"]
            per_product.append({"product_key": product_key, "posted": res["posted"]})
        ctx["promotion"] = {"posted": posted, "products": len(live)}
        return {"status": "ok" if posted else "skipped",
                "detail": {"pins_posted": posted, "products_promoted": len(live),
                           "per_product": per_product}}

    def _daily_report(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Build the daily Revenue / Profit / Best / Worst / Recommendation report."""
        report = self.report.build()
        ctx["report"] = report
        log.info("[daily] REPORT — %s", report["headline"])
        return {"status": "ok", "detail": {
            "headline": report["headline"],
            "revenue_today": report["revenue"]["today"],
            "profit_today": report["profit"]["today_net"],
            "best_seller": report["best_seller"],
            "worst_seller": report["worst_seller"],
            "recommendations": report["recommendations"]}}

    # --- Promotion helpers ------------------------------------------

    def _live_products(self, ctx: dict[str, Any]) -> list[tuple[str, str]]:
        go_live = self._go_live_result(ctx)
        return [(r["product_key"], r["listing_id"])
                for r in (go_live.get("results") or [])
                if r.get("status") == "live" and r.get("listing_id")]

    def _build_pins(self, campaign_id: int, product_key: str,
                    listing_id: str) -> list[dict[str, Any]]:
        import json
        from pathlib import Path

        from onassis.config import ROOT_DIR

        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        folder = base / str(campaign_id) / product_key
        listing_path = folder / "listing.json"
        if not listing_path.exists():
            return []
        listing = json.loads(listing_path.read_text(encoding="utf-8"))
        url = f"https://www.etsy.com/listing/{listing_id}"
        title = listing.get("title", "")
        desc = (listing.get("description", "") or "")[:480]
        pins: list[dict[str, Any]] = []
        for img in listing.get("images", []):
            pins.append({
                "title": title, "description": desc, "link": url,
                "image_path": str(folder / "images" / img["filename"]),
                "alt_text": img.get("alt_text", title),
            })
        return pins

    # --- Reads ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        latest = self.db.get_latest_daily_run()
        return latest or {"message": "No daily runs yet."}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.list_daily_runs(limit)
