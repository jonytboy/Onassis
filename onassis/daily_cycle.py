"""The Daily Cycle — ONASSIS's single execution entry point.

This module **only orchestrates** existing modules in a fixed order. It adds no
business logic, no new agents, and makes no decisions of its own — every stage
delegates to a module that already owns that responsibility.

Order:
    1. Sync Etsy            2. Sync Pinterest      3. Import Revenue
    4. Import Analytics     5. Run Product Optimiser
    6. CEO Decision         7. Generate Campaign (if approved)
    8. Build Listing Package 9. Publish Draft (if approved)
    10. Record Results

Every stage logs start/finish, records its duration, captures failures, and the
cycle continues safely past a failed stage. Two modes are supported: ``dry_run``
(observation + decision only — no generation, listing, or publishing) and
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
from onassis.listing_factory import ListingFactory
from onassis.logger import get_logger
from onassis.optimiser import ProductOptimiser
from onassis.orchestrator import Orchestrator
from onassis.profit import ProfitEngine
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
        self.orchestrator = Orchestrator(config, db)
        self.listing_factory = ListingFactory(config, db)
        self.publisher = PublisherService(config, db)

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
        self._stage(stages, "Generate Campaign", self._generate_campaign, ctx)
        self._stage(stages, "Build Listing Package", self._build_listing, ctx)
        self._stage(stages, "Publish Draft", self._publish, ctx)
        # Stage 10 — Record Results — is the persistence below.
        stages.append({"stage": "Record Results", "status": "ok",
                       "duration_seconds": 0.0, "detail": None, "error": None})

        status = ("completed_with_failures"
                  if any(s["status"] == "failed" for s in stages) else "completed")
        duration = round(sum(s["duration_seconds"] for s in stages), 3)
        record = {"mode": mode, "status": status, "duration_seconds": duration,
                  "stages": stages}
        run_id = self.db.insert_daily_run(record)
        log.info("=== ONASSIS daily cycle DONE (run #%s, %s) ===", run_id, status)
        return {"run_id": run_id, "mode": mode, "status": status,
                "started_at": started, "duration_seconds": duration, "stages": stages}

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

    def _generate_campaign(self, ctx: dict[str, Any]) -> dict[str, Any]:
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        if not ctx.get("approved"):
            return {"status": "skipped", "detail": "CEO did not approve"}
        summary = self.orchestrator.run_daily()
        ctx["campaign_id"] = summary["campaign_id"]
        return {"status": "ok", "detail": {
            "campaign_id": summary["campaign_id"], "items": summary["items_created"]}}

    def _build_listing(self, ctx: dict[str, Any]) -> dict[str, Any]:
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        if not cid:
            return {"status": "skipped", "detail": "no campaign generated"}
        pkg = self.listing_factory.export(cid)
        if pkg.get("status") != "ready":
            return {"status": "blocked", "detail": pkg.get("reason")}
        ctx["listing_ready"] = True
        return {"status": "ok", "detail": {"path": pkg["path"]}}

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
