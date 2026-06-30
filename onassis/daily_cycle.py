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
    9. Create Product Campaign        (campaign + product from the opportunity)
   10. Build Etsy Listing Package     (the upload-ready Etsy product)
   11. Generate Marketing Content     (Pinterest/Instagram/Facebook — promotes the product)
   12. Publish Draft                  (the product becomes a real Etsy draft)
   13. Record Results

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
from onassis.config import Config
from onassis.connectors.etsy import EtsyConnector
from onassis.connectors.pinterest import PinterestConnector
from onassis.database import Database
from onassis.design_package import DesignPackageBuilder
from onassis.listing_factory import ListingFactory
from onassis.logger import get_logger
from onassis.opportunities import OpportunityEngine
from onassis.optimiser import ProductOptimiser
from onassis.orchestrator import Orchestrator
from onassis.profit import ProfitEngine
from onassis.proposals import APPROVE
from onassis.publishing import PublisherService
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
        self.orchestrator = Orchestrator(config, db)
        self.listing_factory = ListingFactory(config, db)
        self.publisher = PublisherService(config, db)
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
        self._stage(stages, "Create Product Campaign", self._create_campaign, ctx)
        self._stage(stages, "Build Etsy Listing Package", self._build_listing, ctx)
        self._stage(stages, "Generate Marketing Content", self._generate_content, ctx)
        self._stage(stages, "Publish Draft", self._publish, ctx)
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
        }

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
        ctx["campaign_approved"] = review["verdict"] == APPROVE
        if not ctx["campaign_approved"]:
            return {"status": "blocked", "detail": "campaign failed compliance review"}
        return {"status": "ok", "detail": {"campaign_id": campaign["id"],
                                           "product": opp["product_name"]}}

    def _build_listing(self, ctx: dict[str, Any]) -> dict[str, Any]:
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        if not cid or not ctx.get("campaign_approved"):
            return {"status": "skipped", "detail": "no approved campaign"}
        pkg = self.listing_factory.export(cid)
        if pkg.get("status") != "ready":
            return {"status": "blocked", "detail": pkg.get("reason")}
        ctx["listing_ready"] = True
        return {"status": "ok", "detail": {"path": pkg["path"]}}

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
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        if not cid:
            return {"status": "skipped", "detail": "no campaign to publish"}
        result = self.publisher.publish(cid, mode="draft")
        st = result["status"]
        mapped = "ok" if st in ("draft", "dry_run") else (
            "failed" if st == "failed" else "skipped")
        return {"status": mapped, "detail": result}

    # --- Reads ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        latest = self.db.get_latest_daily_run()
        return latest or {"message": "No daily runs yet."}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.list_daily_runs(limit)
