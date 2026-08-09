"""The Daily Cycle — ONASSIS's single execution entry point.

This module **only orchestrates** existing modules in a fixed order. It adds no
new agents and makes no decisions of its own — every stage delegates to a
module that already owns that responsibility.

The cycle is **product-first**: marketing is generated only after a commercially
viable product exists. After observation and CEO/Compliance approval it creates
the product, then promotes it.

Order:
    1. Sync Etsy                 2. Sync Pinterest        3. Import Revenue
    4. Fulfil Orders                  (paid Etsy orders -> Gelato production, true cost)
    5. Import Analytics
    6. Learn & Review Portfolio       (what sold/didn't/why; 30-day KEEP/IMPROVE/RETIRE)
    7. Run Product Optimiser     8. CEO Decision
    9. Market Research                (score keywords; build FROM the report, not a vacuum)
   10. Create Product Opportunity     (the product idea, drawn from the market, CEO-approved)
   11. Build Design Package           (design brief + artwork prompt, compliance-gated)
   12. Generate Master Artwork        (the REAL master artwork + print file, QC-gated)
   13. Create Product Campaign        (campaign + product from the opportunity)
   14. Expand Products                (score the catalogue; CEO launches the profitable set)
   15. Publish Products (streaming)   (per product: listing -> artwork -> compliance ->
                                       draft -> upload images -> go live -> next; failures
                                       isolated, never rolls back a published product; a
                                       daily portfolio cap stops ONASSIS flooding Etsy)
   16. Generate Marketing Content     (campaign content + a full per-product marketing kit:
                                       Pinterest/Instagram/Facebook/Blog/Email -> the listing)
   17. Promote on Pinterest           (Traffic Engine: schedule 5-10 pins/day, distribute,
                                       log the funnel Impressions->Clicks->Visits->Sales)
   18. Daily Report                   (Revenue / Profit / Best / Worst / Recommendation)
   19. CEO Dashboard                  (money, and nothing else — the morning scoreboard)
   20. Record Results

Revenue beats completeness: the first sellable product reaches Etsy as early as
possible, and one product's failure never cancels the others.

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
from onassis.connectors.gelato import GelatoConnector
from onassis.connectors.pinterest import PinterestConnector
from onassis.dashboard import CEODashboard
from onassis.database import Database
from onassis.design_package import DesignPackageBuilder
from onassis.etsy_automation import EtsyAutomationEngine
from onassis.etsy_intelligence import EtsyIntelligence
from onassis.expansion import RevenueExpansionEngine
from onassis.learning import LearningEngine
from onassis.listing_factory import ListingFactory
from onassis.logger import get_logger
from onassis.market_intelligence import MarketIntelligence
from onassis.distribution import ChannelDistributor
from onassis.marketing import MarketingEngine
from onassis.opportunities import OpportunityEngine
from onassis.shopify_publisher import ShopifyPublisher
from onassis.optimiser import ProductOptimiser
from onassis.orchestrator import Orchestrator
from onassis.portfolio import PortfolioManager
from onassis.protection import FinancialProtectionEngine
from onassis.profit import ProfitEngine
from onassis.proposals import APPROVE, is_compliant
from onassis.publishing import PublisherService
from onassis.reporting import DailyReport
from onassis.revenue import RevenueEngine
from onassis.traffic import TrafficEngine

log = get_logger(__name__)


class DailyCycle:
    """Coordinates the existing modules into one daily operating cycle."""

    def __init__(self, config: Config, db: Database,
                 content_studio: Any | None = None) -> None:
        self.config = config
        self.db = db
        # Optional injected reel studio (tests pass a stub encoder so the cycle
        # never shells out to ffmpeg); None → ContentEngine uses the real one.
        self.content_studio = content_studio
        # Reuse the existing, independent modules — coordinate, don't replace.
        self.etsy = EtsyConnector(config, db)
        self.gelato = GelatoConnector(config, db)
        self.pinterest = PinterestConnector(config)
        self.revenue = RevenueEngine(config, db)
        self.profit = ProfitEngine(config, db)
        self.analytics = AnalyticsEngine(config, db)
        self.optimiser = ProductOptimiser(config, db)
        self.market = MarketIntelligence(config, db)
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
        # Revenue-optimisation engines (learn, promote, distribute, report money).
        self.learning = LearningEngine(config, db)
        self.portfolio = PortfolioManager(config, db)
        # Financial Protection sits above pricing: it decides whether a commercial
        # action is safe, and has final authority to hold it.
        self.protection = FinancialProtectionEngine(config, db)
        self.etsy_automation = EtsyAutomationEngine(config, db, protection=self.protection)
        self.etsy_intelligence = EtsyIntelligence(config, db)
        self.marketing = MarketingEngine(config, db)
        self.traffic = TrafficEngine(config, db, pinterest=self.pinterest)
        # Sprint 41 — commerce reach: Shopify as a second sales channel, and
        # distribution of the IG/FB/Blog/Email marketing assets.
        self.shopify = ShopifyPublisher(config, db)
        self.distribution = ChannelDistributor(config, db)
        # Sprint 43 — Make.com is the single distribution engine when configured.
        from onassis.campaign_distributor import CampaignDistributor
        self.campaign_distributor = CampaignDistributor(config, db)
        from onassis.cmo import CMOManager
        from onassis.marketing_learning import MarketingLearning
        self.cmo = CMOManager(config, db)
        self.marketing_learning = MarketingLearning(config, db)
        self.dashboard = CEODashboard(config, db)
        # Campaign/Brain/Compliance are owned by the orchestrator — reuse them.
        self.campaigns = self.orchestrator.campaigns
        self.brain = self.orchestrator.brain
        self.compliance = self.orchestrator.compliance
        # AI cost accounting (Sprint 42.2): every LLM/image call records its cost.
        from onassis.ai_accounting import set_recorder
        set_recorder(db, config)

    # --- Entry point ------------------------------------------------

    def run_marketing(self, today: str | None = None) -> dict[str, Any]:
        """Run ONLY the promotion push — decoupled from product creation — so
        marketing goes out DAILY even on days with no production run.

        Cheap and safe to run often: it schedules + evergreen-cycles + posts pins
        (rotating the whole catalogue), ships any due channel assets, imports
        traffic metrics and logs the funnel. It creates NO products and spends no
        LLM/image credit."""
        log.info("=== ONASSIS marketing push START ===")
        # Keep the content pool growing with the catalogue: generate the ($0)
        # marketing kit (incl. blog) for any active product that has none yet, so
        # content keeps flowing even with no new products.
        self.backfill_marketing()
        # Keep a rolling forward blog schedule: give freshly-generated articles a
        # future date so the blog DRIPS (a steady daily trickle you can see on the
        # calendar) instead of dumping the whole backlog in one run.
        try:
            from onassis.business_settings import BusinessSettings
            from onassis.content_engine import ContentEngine

            engine = ContentEngine(self.config, self.db, studio=self.content_studio)
            settings = BusinessSettings(self.db, self.config)
            evergreen = bool(settings.get("evergreen_enabled"))
            # Evergreen recycling covers BLOGS + videos + reels together. When it's
            # off we still launch new products, but nothing recycles.
            if evergreen:
                blog_sched = engine.refill_blog_schedule()
                log.info("Blog schedule: +%d created, %d queued ahead (next %s → %s).",
                         blog_sched.get("created", 0), blog_sched.get("scheduled", 0),
                         blog_sched.get("next"), blog_sched.get("last"))
            else:
                blog_sched = {}
            # Build any missing per-product slideshow clips (idempotent — only
            # products without clips yet, so steady-state this is a no-op).
            clips = engine.build_batch(limit=int(self.config.content.get("clip_daily_limit", 12)
                                                 if isinstance(self.config.content, dict) else 12))
            log.info("Clips: +%d built, %d already had them, %d missing a package.",
                     clips.get("built", 0), clips.get("skipped", 0), clips.get("no_package", 0))
            # Attach each product's slideshow to its Shopify product page + Etsy
            # listing (both idempotent — only products without a video yet).
            vids = engine.attach_videos_to_shopify()
            if vids.get("ok"):
                log.info("Shopify videos: +%d attached, %d already had one.",
                         vids.get("added", 0), vids.get("skipped", 0))
            evids = engine.attach_videos_to_etsy()
            if evids.get("ok"):
                log.info("Etsy videos: +%d uploaded, %d already had one.",
                         evids.get("added", 0), evids.get("skipped", 0))
            # Queue Facebook posts for new blogs + videos (distributor ships them).
            fb = engine.queue_facebook_posts()
            tt = engine.queue_tiktok_posts()
            if evergreen:
                ev = engine.queue_evergreen_facebook()  # rotate videos, cycle + loop
                er = engine.queue_evergreen_reels(       # rotate reels, cycle + loop
                    per_run=int(settings.get("evergreen_reels_per_run")))
            else:
                ev = er = {"queued": 0}
            log.info("Social queue: FB +%d link(s)/+%d video(s)/+%d re-share(s), "
                     "TikTok/Reels +%d new/+%d evergreen.",
                     fb.get("blogs", 0), fb.get("videos", 0), ev.get("queued", 0),
                     tt.get("videos", 0), er.get("queued", 0))
        except Exception:  # scheduling/clip build is best-effort, never fail the push
            blog_sched = {}
            log.debug("blog scheduling / clip build skipped", exc_info=True)
        traffic = self.traffic.run(today)
        # Native channel distribution (IG/FB/Blog/Email) ships what's due; when
        # Make is the distribution path, per-product campaigns already went out at
        # launch, so we only publish the first-party blog directly here.
        if self.campaign_distributor.make.is_configured:
            # Blog publishes first-party (capped per run so a backlog drips instead
            # of dumping); reels drip out via Make (→ FB Reels / TikTok). New-product
            # FB/IG campaigns already went out at launch.
            from onassis.business_settings import BusinessSettings
            blogs_cap = int(BusinessSettings(self.db, self.config).get("blogs_per_run"))
            channels = {"posted": 0, "failed": 0, "skipped": 0, "by_channel": {}}
            if blogs_cap > 0:
                b = self.distribution.distribute(channels=["blog"], limit=blogs_cap)
                for k in ("posted", "failed", "skipped"):
                    channels[k] += b.get(k, 0)
                channels["by_channel"].update(b.get("by_channel", {}))
            r = self.distribution.distribute(channels=["tiktok"])
            for k in ("posted", "failed", "skipped"):
                channels[k] += r.get(k, 0)
            channels["by_channel"].update(r.get("by_channel", {}))
        else:
            channels = self.distribution.distribute()
        # Adaptive price discovery — walks each product's price on the sales signal
        # (self-gated to once per window, so calling every run is safe/cheap).
        try:
            if str((self.config.pricing or {}).get("strategy", "")).lower() == "adaptive":
                from onassis.adaptive_pricing import AdaptivePricer
                ap = AdaptivePricer(self.config, self.db).reprice(apply=True, today=today)
                if ap.get("changed"):
                    log.info("Adaptive pricing: %d product price(s) moved.", ap["changed"])
        except Exception:  # pricing is best-effort, never fail the push
            log.debug("adaptive pricing skipped", exc_info=True)
        funnel = self.traffic.snapshot(today)
        try:
            self.marketing_learning.record()
        except Exception:  # learning is best-effort, never fail the push
            log.debug("marketing learning skipped", exc_info=True)
        posted = (traffic.get("distribute") or {}).get("posted", 0)
        ever = (traffic.get("evergreen") or {}).get("scheduled", 0)
        log.info("=== marketing push DONE — %d pin(s) posted, %d queued evergreen ===",
                 posted, ever)
        return {"status": "ok", "traffic": traffic, "channels": channels,
                "funnel": funnel, "pins_posted": posted, "evergreen_scheduled": ever,
                "blog_schedule": blog_sched}

    def backfill_apparel(self, *, limit: int = 5,
                         garments: list[str] | None = None) -> dict[str, Any]:
        """Turn an existing mug/tote/poster catalogue into a clothing range: for
        each existing design, reload its artwork and build the apparel products
        (t-shirt / hoodie / sweatshirt) as DRAFT Shopify/Etsy listings for review.
        Bounded by ``limit`` designs (a test batch). Idempotent — a garment a
        design already has is skipped. Returns per-design results."""
        garments = garments or ["premium_tshirt", "heavyweight_hoodie", "sweatshirt"]
        cat = {c["key"]: c for c in (self.config.expansion or {}).get("catalogue", [])}
        specs = [{"product_key": g, "product_name": cat[g].get("name", g),
                  "gelato_uid": cat[g].get("gelato_uid"),
                  "production_cost": float(cat[g].get("production_cost", 0) or 0),
                  "retail_price": float(cat[g].get("retail_price", 0) or 0)}
                 for g in garments
                 if g in cat and cat[g].get("available", True) is not False]
        if not specs:
            return {"ok": False, "designs": 0, "built": 0,
                    "reason": "No available apparel products in the catalogue config."}
        results: list[dict[str, Any]] = []
        designs = built = 0
        for camp in self.campaigns.list_campaigns():
            if designs >= limit:
                break
            cid = camp["id"]
            opp_id = self.db.opportunity_for_campaign(cid)
            if not opp_id:
                continue
            pkg = self.design.get_package(opp_id)
            if not pkg:                       # design package not on disk — don't
                results.append({"campaign_id": cid, "name": camp.get("name"),
                                "skipped": "design package missing"})
                continue
            made: list[str] = []
            for spec in specs:
                sku = f"{cid}-{spec['product_key']}"
                if self.db.get_product_by_sku(sku):
                    continue                  # already has this garment
                try:
                    p = self.listing_factory.export_product(
                        cid, spec, design_package=pkg)
                    if p.get("status") != "ready":
                        continue
                    self.db.insert_product({
                        "sku": sku, "name": spec["product_name"], "campaign_id": cid,
                        "marketplace": "gelato", "product_key": spec["product_key"],
                        "production_cost": spec["production_cost"]})
                    self.publisher.publish(cid, product_key=spec["product_key"])
                    try:
                        self.shopify.publish(
                            cid, spec["product_key"], p.get("listing", {}),
                            images_dir=self._product_images_dir(cid, spec["product_key"]))
                    except Exception:  # Shopify is best-effort; Etsy draft still stands
                        log.debug("apparel Shopify publish failed", exc_info=True)
                    made.append(spec["product_key"])
                    built += 1
                except Exception as exc:  # one garment failing never stops the run
                    log.warning("[apparel] %s/%s failed: %s",
                                cid, spec["product_key"], exc)
            if made:
                designs += 1
                results.append({"campaign_id": cid, "name": camp.get("name"),
                                "built": made})
        return {"ok": True, "designs": designs, "built": built, "results": results[:50]}

    def _launch_new_products(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Launch burst: immediately post the marketing for each product actually
        published this run (blog live now, clips, video on Shopify/Etsy, Facebook)
        — the 'it's news' path. Evergreen recycling handles the rest later."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        from onassis.content_engine import ContentEngine

        cid = ctx.get("campaign_id")
        launched = 0
        engine = ContentEngine(self.config, self.db, studio=self.content_studio)
        for p in (ctx.get("stream") or []):
            if not p.get("listing_id") or not p.get("product_key"):
                continue                       # only products that really published
            try:
                engine.launch_product(cid, p["product_key"])
                launched += 1
            except Exception:  # best-effort — never fail the cycle on a launch
                log.debug("launch marketing skipped for %s",
                          p.get("product_key"), exc_info=True)
        return {"status": "ok", "detail": f"{launched} product(s) launched"}

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
        self._stage(stages, "Fulfil Orders", self._fulfil_orders, ctx)
        self._stage(stages, "Import Analytics", self._import_analytics, ctx)
        self._stage(stages, "Learn & Review Portfolio", self._learn_and_review, ctx)
        self._stage(stages, "Run Product Optimiser", self._run_optimiser, ctx)
        self._stage(stages, "CEO Decision", self._ceo_decision, ctx)
        # --- Product first: research the market, then build FROM it. ---
        self._stage(stages, "Market Research", self._market_research, ctx)
        self._stage(stages, "Create Product Opportunity", self._create_opportunity, ctx)
        self._stage(stages, "Build Design Package", self._build_design_package, ctx)
        self._stage(stages, "Generate Master Artwork", self._generate_master_artwork, ctx)
        self._stage(stages, "Create Product Campaign", self._create_campaign, ctx)
        self._stage(stages, "Expand Products", self._expand_products, ctx)
        # Revenue-first: stream each product to a live Etsy draft independently —
        # Product 1 is published while Product 2 is still generating.
        self._stage(stages, "Publish Products", self._stream_products, ctx)
        self._stage(stages, "Generate Marketing Content", self._generate_content, ctx)
        self._stage(stages, "Launch New Products", self._launch_new_products, ctx)
        self._stage(stages, "Promote on Pinterest", self._promote, ctx)
        self._stage(stages, "Daily Report", self._daily_report, ctx)
        self._stage(stages, "CEO Dashboard", self._ceo_dashboard, ctx)
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
            # Revenue-first streaming: per-product timeline + the primary KPI
            # (time from opportunity to the FIRST live Etsy draft).
            "stream": ctx.get("stream", []),
            "first_draft_at": ctx.get("first_draft_at"),
            "report": ctx.get("report"),
            # Revenue-optimisation outputs (learning, funnel, CEO scoreboard).
            "learning": ctx.get("learning"),
            "funnel": ctx.get("funnel"),
            "dashboard": ctx.get("dashboard"),
            "fulfilment": ctx.get("fulfilment"),
            # CFO end-of-run AI-spend optimisation report (Sprint 42.2, Obj 12).
            "ai_optimisation": self._ai_optimisation_report(),
        }

    def _ai_optimisation_report(self) -> dict[str, Any]:
        try:
            from onassis.cfo import CFOManager
            return CFOManager(self.config, self.db).optimisation_report()
        except Exception as exc:  # never let reporting break the cycle result
            log.warning("CFO optimisation report failed: %s", exc)
            return {}

    @staticmethod
    def _go_live_result(ctx: dict[str, Any]) -> dict[str, Any]:
        """The go-live outcome from the streaming publish stage."""
        launch = ctx.get("launch") or {}
        return (launch.get("go_live")
                or (launch.get("launch") or {}).get("go_live") or {})

    # --- Stage runner -----------------------------------------------

    def _stage(
        self, stages: list[dict[str, Any]], name: str,
        fn: Callable[[dict[str, Any]], dict[str, Any]], ctx: dict[str, Any],
    ) -> None:
        from onassis.ai_accounting import cost_context

        t0 = time.monotonic()
        log.info("[daily] %s: start", name)
        status, detail, error = "ok", None, None
        try:
            # Tag every AI call in this stage with the stage + current campaign.
            with cost_context(stage=name, campaign_id=ctx.get("campaign_id")):
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

    def _fulfil_orders(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Turn paid Etsy orders into Gelato production orders, then poll status +
        tracking and record the ACTUAL production cost. Safe no-op until Gelato is
        configured. Runs in both modes — fulfilment is a real customer obligation."""
        if not self.gelato.can_fulfil:
            return {"status": "skipped", "detail": "Gelato not configured for fulfilment"}
        submit = self.gelato.fulfil_new_orders()
        sync = self.gelato.sync_status()
        ctx["fulfilment"] = {"submitted": submit.get("submitted", 0),
                             "failed": submit.get("failed", 0),
                             "status_updated": sync.get("updated", 0)}
        status = "ok" if (submit.get("submitted") or sync.get("updated")) else "skipped"
        if submit.get("failed"):
            status = "ok"  # failures are recorded per-order, not a stage failure
        return {"status": status, "detail": ctx["fulfilment"]}

    def _import_analytics(self, ctx: dict[str, Any]) -> dict[str, Any]:
        collected = self.analytics.collect()
        # Etsy intelligence: import search terms (provider-gated) and compute REAL
        # per-product conversion from synced views + orders, to feed learning.
        terms = self.etsy_intelligence.import_search_terms()
        report = self.etsy_intelligence.conversion_report()
        ctx["conversion_lookup"] = {p["sku"]: p["conversion"]
                                    for p in report["products"]}
        ctx["etsy_intelligence"] = {"shop_conversion": report["shop"]["conversion"],
                                    "search_terms_imported": terms["imported"]}
        return {"status": "ok", "detail": {**collected,
                                           "shop_conversion": report["shop"]["conversion"],
                                           "search_terms": terms}}

    def _run_optimiser(self, ctx: dict[str, Any]) -> dict[str, Any]:
        rec = self.optimiser.top_recommendation()
        ctx["recommendation"] = rec
        if rec is None:
            return {"status": "skipped", "detail": "no live products"}
        return {"status": "ok", "detail": {
            "product": rec["product"], "recommendation": rec["recommendation"],
            "expected_roi": rec["expected_roi"], "confidence": rec["confidence"]}}

    def _learn_and_review(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Every morning: what sold, what didn't, why — then act. Learn from
        sales, run the 30-day portfolio lifecycle (KEEP/IMPROVE/RETIRE, archive),
        and revive an archived type only if the market trend has turned."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run (no archiving/writes)"}
        # Feed the Learning Engine the REAL per-listing conversion (from synced
        # Etsy views + orders), computed in the Import Analytics stage.
        conversion = ctx.get("conversion_lookup") or {}
        digest = self.learning.run(ctr_lookup=lambda sku: float(conversion.get(sku, 0.0)))
        revived = self.portfolio.reconsider_archived()
        ctx["learning"] = digest
        # Act on the decisions: reflect reprice/retire back onto live Etsy listings
        # (safe no-op + audited until Etsy is configured for writes).
        actuation = self.etsy_automation.apply_learning_actions(digest)
        ctx["etsy_automation"] = actuation
        return {"status": "ok", "detail": {
            "headline": digest["headline"],
            "scaled": len(digest["actions"]["increase"]),
            "adjusted": len(digest["actions"]["adjust"]),
            "retired": len(digest["actions"]["retire"]),
            "revived": len(revived["reactivated"]),
            "etsy_changes_applied": actuation.get("applied", 0)}}

    def _ceo_decision(self, ctx: dict[str, Any]) -> dict[str, Any]:
        rec = ctx.get("recommendation")
        if not rec:
            ctx["approved"] = False
            return {"status": "skipped", "detail": "no recommendation to decide on"}
        verdict = rec["ceo"]["verdict"]  # reuse the CEO's decision — no duplication
        ctx["approved"] = verdict == "APPROVE"
        return {"status": "ok", "detail": {"verdict": verdict, "approved": ctx["approved"]}}

    def _market_research(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Research the market BEFORE inventing anything — the CEO builds from the
        highest-opportunity keywords, never at random."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        # Only research when we need fresh ideas (the backlog is empty).
        if self.opportunities.top(limit=1):
            return {"status": "skipped", "detail": "backlog not empty — using it"}
        report = self.market.research()
        ctx["market"] = report
        top = self.market.top(5, min_band=self.opportunities.min_opportunity_band)
        return {"status": "ok", "detail": {
            "keywords_scored": report["count"],
            "top": [{"keyword": k["keyword"], "demand": k["demand"],
                     "competition": k["competition"], "opportunity": k["opportunity"]}
                    for k in top]}}

    def _create_opportunity(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Pick (or generate) a CEO-approved product opportunity to build."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        if not self.opportunities.top(limit=1):
            self.opportunities.generate()  # backlog empty — discover ideas from the market
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

    def _daily_listing_cap(self) -> int:
        """The effective daily new-listing cap: the operator's Business Setting
        if set, otherwise the config default."""
        try:
            from onassis.business_settings import BusinessSettings
            return int(BusinessSettings(self.db, self.config).get("max_campaigns_per_day"))
        except Exception:  # fall back to raw config on any lookup problem
            return int((self.config.portfolio or {}).get("max_new_listings_per_day", 2))

    def _stream_products(self, ctx: dict[str, Any], *, ignore_cap: bool = False,
                         go_live_override: bool | None = None) -> dict[str, Any]:
        """Stream each approved product to a live Etsy draft, independently.

        Revenue beats completeness: for every product we run the *whole* tail —
        build listing (real artwork + gallery + autonomous compliance) → create
        the Etsy draft → upload its images → (per policy) activate LIVE → record —
        and only THEN move to the next product. The first sellable product reaches
        Etsy as early as possible. A product that fails is isolated: it is logged
        and skipped, never rolled back, and the remaining products still run.

        ``ignore_cap`` skips the daily portfolio cap (the Catalogue Compiler is an
        explicit bulk build-out, bounded by its own budget cap). ``go_live_override``
        forces draft-only (False) or live (True) regardless of the launch policy.
        """
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        if not cid or not ctx.get("campaign_approved"):
            return {"status": "skipped", "detail": "no approved campaign"}
        specs = (ctx.get("expansion") or {}).get("launched", [])
        if not specs:
            return {"status": "skipped", "detail": "no approved products to publish"}

        # Portfolio guard — never flood Etsy. Cap NEW live/draft listings per day.
        # Honour the operator's Business Setting (DB override) over the config
        # default, so raising "Max Campaigns / Day" in the UI actually takes effect.
        capped = 0
        if not ignore_cap:
            cap = int(self._daily_listing_cap())
            already = self.db.count_new_listings_today()
            remaining = max(0, cap - already)
            if len(specs) > remaining:
                capped = len(specs) - remaining
                log.info("[stream] Portfolio cap: %d/%d listing(s) already today; "
                         "publishing %d, deferring %d.", already, cap, remaining, capped)
                specs = specs[:remaining]
            if not specs:
                return {"status": "skipped",
                        "detail": f"daily listing cap reached ({already}/{cap})"}

        design_package = ctx.get("design_package")
        go_live = (self.publisher.auto_go_live if go_live_override is None
                   else bool(go_live_override))
        stream: list[dict[str, Any]] = []
        live_results: list[dict[str, Any]] = []
        published = live = 0
        first_draft_at: str | None = None

        for i, spec in enumerate(specs, start=1):
            key = spec["product_key"]
            name = spec.get("product_name") or key
            rec: dict[str, Any] = {"product_key": key, "product_name": name,
                                   "status": "generating"}
            log.info("[stream] Product %d/%d (%s): generating…", i, len(specs), name)
            try:
                from onassis.ai_accounting import cost_context
                with cost_context(product_id=f"{cid}-{key}", campaign_id=cid):
                    pkg = self.listing_factory.export_product(cid, spec, design_package=design_package)
                if pkg.get("status") != "ready":
                    rec.update(status="failed", stage="listing", reason=pkg.get("reason"))
                    log.warning("[stream] Product %s FAILED at listing: %s", name, pkg.get("reason"))
                    stream.append(rec)
                    continue
                rec["images"] = len(pkg.get("listing", {}).get("images", []))
                rec["advisories"] = pkg.get("advisories", [])   # logged, not blocking
                if rec["advisories"]:
                    log.info("[stream] Product %s advisories: %s",
                             name, "; ".join(rec["advisories"]))

                pub = self.publisher.publish(cid, product_key=key)
                if pub.get("status") != "draft":
                    rec.update(status="failed", stage="publish",
                               reason=pub.get("reason", pub.get("status")))
                    log.warning("[stream] Product %s FAILED at publish: %s", name, rec["reason"])
                    stream.append(rec)
                    continue
                publication = pub.get("publication", {})
                listing_id = publication.get("listing_id")
                rec.update(status="draft", listing_id=listing_id,
                           images_uploaded=publication.get("images_uploaded", 0))
                published += 1
                first_draft_at = first_draft_at or datetime.now(timezone.utc).isoformat()

                # Publish to Shopify at the same time (second sales channel).
                # Safe no-op until Shopify is credentialled; never blocks the
                # Etsy draft — a Shopify failure is isolated and recorded.
                try:
                    shop_dir = self._product_images_dir(cid, key)
                    shop = self.shopify.publish(cid, key, pkg.get("listing", {}),
                                                images_dir=shop_dir)
                    rec["shopify"] = shop.get("status")
                    if shop.get("status") not in ("draft", "live", "skipped", "not_configured"):
                        log.warning("[stream] Shopify publish for %s: %s",
                                    name, shop.get("reason"))
                except Exception as exc:  # never let Shopify break the Etsy flow
                    rec["shopify"] = "failed"
                    log.warning("[stream] Shopify publish crashed for %s: %s", name, exc)

                # Financial Protection has final authority: never take a product
                # LIVE below protected profitability. A rejected product stays a
                # draft (not lost), the reason is audited, and the CEO is alerted.
                price = float(pkg.get("listing", {}).get("price")
                              or spec.get("retail_price") or 0)
                shipping = float((self.config.pricing or {}).get("shipping_cost", 0) or 0)
                verdict = self.protection.guard_launch(
                    price, production_cost=spec.get("production_cost"),
                    shipping_cost=shipping, product_key=key, listing_id=listing_id)
                rec["protected_profit"] = verdict["protected_profit"]
                if not verdict["approved"]:
                    rec.update(status="held", protection="rejected",
                               protection_reason=verdict["reason"])
                    log.warning("[stream] Product %s HELD by Financial Protection: %s",
                                name, verdict["reason"])
                elif go_live:
                    gl = self.publisher.go_live(cid, [spec])
                    r0 = (gl.get("results") or [{}])[0]
                    rec["status"] = r0.get("status", rec["status"])  # live | held | failed
                    if r0.get("status") == "live":
                        live += 1
                        live_results.append({"product_key": key, "listing_id": listing_id,
                                             "status": "live"})
                rec["published_at"] = datetime.now(timezone.utc).isoformat()
                log.info("[stream] Product %s: %s (listing %s, images %s/%s) at %s",
                         name, rec["status"].upper(), listing_id,
                         rec.get("images_uploaded", 0), rec.get("images", 0),
                         rec["published_at"][11:19])
            except Exception as exc:  # isolate the failure — the others still run
                rec.update(status="failed", stage="exception", reason=str(exc))
                log.exception("[stream] Product %s crashed — isolated, continuing.", name)
            stream.append(rec)

        # Record the launch (so /launch/status + promotion see the live set).
        self.db.upsert_launch({"campaign_id": cid, "policy": self.publisher.launch_policy,
                               "status": "launched" if (go_live and live) else "launch_ready",
                               "products": published})
        ctx["listing_ready"] = published > 0
        ctx["stream"] = stream
        ctx["first_draft_at"] = first_draft_at
        ctx["launch"] = {"status": "launched" if (go_live and live) else "launch_ready",
                         "policy": self.publisher.launch_policy,
                         "go_live": {"live": live, "results": live_results}}

        failed = [r for r in stream if r["status"] == "failed"]
        status = "ok" if published else ("failed" if failed else "skipped")
        return {"status": status, "detail": {
            "products": len(specs), "published": published, "live": live,
            "failed": len(failed), "deferred_by_cap": capped,
            "first_draft_at": first_draft_at, "timeline": stream}}

    def build_unit(self, *, go_live: bool | None = None,
                   ignore_cap: bool = False) -> dict[str, Any]:
        """Build ONE product unit end-to-end (opportunity → design → master
        artwork → campaign → expand → publish drafts) and return a summary.

        This is exactly the daily cycle's product-creation tail (stages 10–15),
        packaged as one call so the Catalogue Compiler can loop it to build a
        whole catalogue in one event — without duplicating the pipeline. ``go_live``
        forces draft-only (False) or live (True); ``ignore_cap`` skips the daily
        listing cap (the compiler is bounded by its own budget instead)."""
        ctx: dict[str, Any] = {"dry": False}
        opp = self._create_opportunity(ctx)
        if opp["status"] != "ok":
            return {"status": "skipped", "reason": opp.get("detail", "no opportunity"),
                    "products": []}
        for stage in (self._build_design_package, self._generate_master_artwork,
                      self._create_campaign, self._expand_products):
            res = stage(ctx)
            if res["status"] == "blocked":
                return {"status": "blocked", "reason": res.get("detail"),
                        "campaign_id": ctx.get("campaign_id"), "products": []}
        stream = self._stream_products(ctx, ignore_cap=ignore_cap, go_live_override=go_live)
        # Generate the per-product marketing kit (blog/pins/IG/FB/email, $0) so
        # compiler-built products aren't left without any marketing to distribute.
        try:
            self._marketing_kits(ctx)
        except Exception:  # marketing is best-effort — never fail the build on it
            log.debug("marketing kit generation skipped", exc_info=True)
        from onassis.catalogue import category_of
        launched = (ctx.get("expansion") or {}).get("launched", [])
        products = [{"product_key": s["product_key"],
                     "product_name": s.get("product_name") or s["product_key"],
                     "category": category_of(s["product_key"], s.get("product_name"))}
                    for s in launched]
        return {"status": stream["status"],
                "campaign_id": ctx.get("campaign_id"),
                "opportunity": (ctx.get("opportunity") or {}).get("opportunity_id"),
                "published": (stream.get("detail") or {}).get("published", 0),
                "products": products}

    def _generate_content(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Generate marketing LAST — only to promote the new product. Produces the
        campaign content AND a full per-product marketing kit (Pinterest /
        Instagram / Facebook / Blog / Email), every asset linking to the listing."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        brief = ctx.get("brief")
        if not brief or not ctx.get("campaign_approved"):
            return {"status": "skipped", "detail": "no approved product campaign"}
        content = self.orchestrator.generate_marketing_content(brief)
        ctx["content_items"] = len(content["items"])
        kits = self._marketing_kits(ctx)
        ctx["marketing_kits"] = kits
        return {"status": "ok", "detail": {"items": len(content["items"]),
                                           "marketing_kits": kits}}

    def _marketing_kits(self, ctx: dict[str, Any]) -> int:
        """Build the deterministic ($0) per-product marketing kit — Pinterest /
        Instagram / Facebook / Blog / Email — for every BUILT product (draft or
        live). Previously this only ran for live products, so content-first drafts
        (and everything built by the Catalogue Compiler) never got a kit."""
        cid = ctx.get("campaign_id")
        if not cid:
            return 0
        kits = 0
        for s in (ctx.get("expansion") or {}).get("launched", []):
            key = s.get("product_key")
            listing = self._listing_json(cid, key) if key else None
            if not listing:
                continue
            url = listing.get("listing_url") or self._marketing_link(cid, key)
            self.marketing.build(listing, listing_url=url, campaign_id=cid, product_key=key,
                                 image_url=self._hero_url(cid, key))
            kits += 1
        return kits

    def _hero_url(self, campaign_id: int, product_key: str) -> str | None:
        base = ((getattr(self.config, "content", None) or {}).get("public_base")
                or (self.config.gelato or {}).get("file_base_url") or "").rstrip("/")
        return f"{base}/{campaign_id}/{product_key}/images/hero.jpg" if base else None

    def backfill_marketing(self, limit: int = 100) -> int:
        """Generate the ($0) marketing kit for active products that have no blog
        articles yet — so content (blog/pins/IG/FB/email) keeps flowing as the
        catalogue grows, without a production run. Idempotent."""
        have = {a.get("product_key") for a in self.db.list_marketing_assets(channel="blog")}
        made = 0
        for p in self.db.list_products():
            if made >= limit:
                break
            key, cid = p.get("product_key"), p.get("campaign_id")
            if not p.get("active", 1) or not key or not cid or key in have:
                continue
            listing = self._listing_json(cid, key)
            if not listing:
                continue
            url = listing.get("listing_url") or self._marketing_link(cid, key)
            try:
                self.marketing.build(listing, listing_url=url, campaign_id=cid,
                                     product_key=key, image_url=self._hero_url(cid, key))
                have.add(key)
                made += 1
            except Exception:  # one product never blocks the rest
                log.debug("marketing backfill failed for %s", key, exc_info=True)
        if made:
            log.info("Marketing backfill: generated kits for %d product(s).", made)
        return made

    def _marketing_link(self, campaign_id: int, product_key: str) -> str:
        """Best available public link for a built product (Etsy/Shopify), or ''."""
        for platform in ("etsy", "shopify"):
            pub = self.db.get_latest_publication(campaign_id, platform,
                                                 product_id=f"{campaign_id}-{product_key}")
            if pub:
                url = pub.get("listing_url") or pub.get("url")
                if url:
                    return url
                lid = pub.get("listing_id")
                if platform == "etsy" and lid and str(lid).isdigit():
                    return f"https://www.etsy.com/listing/{lid}"
        return ""

    def _promote(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Distribute the marketing via the Traffic Engine — schedule 5-10 pins/day
        across boards/keywords/seasons (each linking to the Etsy listing), post the
        due ones (safe no-op until Pinterest is configured), and log the funnel
        Impressions -> Clicks -> Visits -> Sales."""
        if ctx["dry"]:
            return {"status": "skipped", "detail": "dry run"}
        cid = ctx.get("campaign_id")
        sched = self.traffic.schedule(campaign_id=cid)
        # Evergreen: keep pinning daily, cycling the WHOLE catalogue (least-
        # recently-pinned first) so promotion never dries up after launch.
        self.traffic.backfill_evergreen()
        dist = self.traffic.distribute()
        # The CMO schedules new marketing assets across the campaign calendar,
        # then distribute ships only what is due today (Sprint 42).
        self.cmo.schedule_pending()
        # Distribution: Make.com (one webhook per product) when configured;
        # otherwise the native per-channel distributor (Sprint 43).
        if self.campaign_distributor.make.is_configured:
            channels = self._distribute_via_make(cid)
        else:
            channels = self.distribution.distribute()
        metrics = self.traffic.import_metrics()   # impressions/clicks -> attributed
        funnel = self.traffic.snapshot()
        # Marketing learning loop: record channel effectiveness so next run can
        # compare and improve (Sprint 42 Phase 5).
        self.marketing_learning.record()
        posted = int(dist.get("posted", 0) or 0)
        scheduled = int(sched.get("scheduled", 0) or 0)
        ctx["promotion"] = {"posted": posted, "scheduled": scheduled,
                            "impressions": metrics.get("impressions", 0),
                            "clicks": metrics.get("clicks", 0),
                            "channels_posted": channels.get("posted", 0)}
        ctx["distribution"] = channels
        ctx["funnel"] = funnel
        return {"status": "ok" if (scheduled or posted or channels.get("processed")) else "skipped",
                "detail": {"pins_scheduled": scheduled, "pins_posted": posted,
                           "queued": int(dist.get("queued", 0) or 0),
                           "season": sched.get("season"),
                           "channels": {"processed": channels.get("processed", 0),
                                        "posted": channels.get("posted", 0),
                                        "skipped": channels.get("skipped", 0),
                                        "failed": channels.get("failed", 0)},
                           "impressions": metrics.get("impressions", 0),
                           "clicks": metrics.get("clicks", 0), "funnel": funnel}}

    def _distribute_via_make(self, campaign_id: int | None) -> dict[str, Any]:
        """Send each launched product's complete campaign to Make.com in one
        webhook (Sprint 43). Returns a native-distribute-shaped summary."""
        launched = [s for s in self.db.list_product_scores(campaign_id)
                    if s.get("launched")] if campaign_id else []
        posted = failed = 0
        for spec in launched:
            key = spec.get("product_key")
            res = self.campaign_distributor.distribute(
                campaign_id, key, product_id=f"{campaign_id}-{key}")
            if res.get("ok"):
                posted += 1
            else:
                failed += 1
        # The Shopify Blog is a FIRST-PARTY Shopify action, not a social platform
        # Make distributes — always publish it directly via ONASSIS (Sprint 44.2).
        blog = self.distribution.distribute(channels=["blog"])
        return {"processed": len(launched), "posted": posted, "skipped": 0,
                "failed": failed, "provider": "make",
                "blog": {"posted": blog.get("posted", 0), "failed": blog.get("failed", 0),
                         "skipped": blog.get("skipped", 0)}}

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

    def _ceo_dashboard(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """The CEO scoreboard — money, and nothing else. Read-only; runs always."""
        board = self.dashboard.build()
        ctx["dashboard"] = board
        log.info("[daily] CEO — %s", board["headline"])
        return {"status": "ok", "detail": {
            "headline": board["headline"],
            "revenue_yesterday": board["revenue_yesterday"],
            "profit_yesterday": board["profit_yesterday"],
            "visitors": board["visitors"], "conversion": board["conversion"],
            "pinterest_clicks": board["pinterest_clicks"],
            "products_launched": board["products_launched"]["yesterday"],
            "products_retired": board["products_retired"]["yesterday"],
            "ai_cost": board["ai_cost"], "roi": board["roi"]}}

    # --- Promotion helpers ------------------------------------------

    def _listing_json(self, campaign_id: int, product_key: str) -> dict[str, Any] | None:
        """Load a published product's listing.json (title/description/tags/etc)."""
        import json
        from pathlib import Path

        from onassis.config import ROOT_DIR

        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        path = base / str(campaign_id) / product_key / "listing.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _product_images_dir(self, campaign_id: int, product_key: str):
        """The gallery-images folder for a product's package (for Shopify upload)."""
        from pathlib import Path

        from onassis.config import ROOT_DIR

        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        return base / str(campaign_id) / product_key / "images"

    def _live_products(self, ctx: dict[str, Any]) -> list[tuple[str, str]]:
        go_live = self._go_live_result(ctx)
        return [(r["product_key"], r["listing_id"])
                for r in (go_live.get("results") or [])
                if r.get("status") == "live" and r.get("listing_id")]

    # --- Reads ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        latest = self.db.get_latest_daily_run()
        return latest or {"message": "No daily runs yet."}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.list_daily_runs(limit)
