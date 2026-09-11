"""REST API for ONASSIS — the service interface for external systems (e.g. Make).

This is a **thin** layer. It owns no business logic: every endpoint simply
calls an existing service (:class:`~onassis.orchestrator.Orchestrator`,
:class:`~onassis.campaign_manager.CampaignManager`,
:class:`~onassis.brain.OnassisBrain`, :class:`~onassis.database.Database`).
The existing architecture is untouched — this module only exposes it over HTTP.

Run it::

    python main.py --serve            # or: uvicorn onassis.api:app

Interactive docs (Swagger) are served at ``/docs``; the OpenAPI schema at
``/openapi.json``. All responses are JSON.

In **production** (``ONASSIS_ENV=production``) the security middleware
(:mod:`onassis.security`) protects every route except the public ones
(``/``, ``/health``, ``/exports/*``) with an API key, applies per-IP rate
limiting and security headers, and locks Swagger behind the key. In development
it is inert, so this module still owns no business logic.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from onassis.config import Config, load_config
from onassis.connectors.etsy_oauth import EtsyAuthError
from onassis.daily_cycle import DailyCycle
from onassis.database import Database
from onassis.design_package import DesignPackageError
from onassis.experiments import ExperimentEngine, ExperimentError
from onassis.listing_factory import ListingError
from onassis.logger import get_logger, setup_logging
from onassis.operations import OperationsManager
from onassis.production_readiness import ProductionReadiness

log = get_logger(__name__)


# --- Response models (drive the Swagger docs) -----------------------

class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    app: str
    version: str
    environment: str
    campaigns: int = Field(description="Number of campaigns stored.")
    knowledge: int = Field(description="Number of Brain predictions stored.")


class CampaignCreateResponse(BaseModel):
    campaign_id: int
    status: str = Field(examples=["completed"])
    assets_created: int = Field(description="Content items generated for the campaign.")
    duration_seconds: int


class CampaignSummary(BaseModel):
    id: int
    name: str
    theme: str | None = None
    status: str
    created_at: str
    content_count: int


def create_app(config: Config | None = None) -> FastAPI:
    """Build the FastAPI app over a set of ONASSIS services.

    Accepting an optional ``config`` lets tests point the app at a throwaway
    database (and inject fake LLMs via ``app.state.orchestrator``).
    """
    config = config or load_config()
    db = Database(config.db_path)
    # Overlay operator-saved integration credentials (Integration Manager) onto
    # the live config BEFORE connectors are built, so they use the latest keys.
    from onassis.integrations import IntegrationManager, apply_integration_overrides

    apply_integration_overrides(config, db)
    # Self-healing: reconcile workflow state on startup so the operator always
    # returns to a consistent system (Sprint 41.2). Never fatal.
    from onassis.self_healing import reconcile

    try:
        _reconcile_summary = reconcile(config, db)
    except Exception:  # noqa: BLE001
        _reconcile_summary = {"repaired": 0, "error": "reconcile failed"}
    # One product-first workflow (the Daily Cycle). Everything the API exposes
    # reuses the cycle's own module instances, so there is a single shared set
    # and a single behaviour — no separate content-first path.
    daily = DailyCycle(config, db)
    orchestrator = daily.orchestrator
    campaigns = daily.campaigns
    brain = daily.brain
    profit = daily.profit
    revenue = daily.revenue
    etsy = daily.etsy
    optimiser = daily.optimiser
    listing_factory = daily.listing_factory
    publisher = daily.publisher
    analytics = daily.analytics
    opportunities = daily.opportunities
    design_builder = daily.design
    expansion = daily.expansion
    report = daily.report
    market = daily.market
    learning = daily.learning
    portfolio = daily.portfolio
    traffic = daily.traffic
    ceo = daily.dashboard
    gelato = daily.gelato
    etsy_automation = daily.etsy_automation
    etsy_intelligence = daily.etsy_intelligence
    protection = daily.protection
    experiments = ExperimentEngine(config, db)
    operations = OperationsManager(config, db, cycle=daily)
    readiness = ProductionReadiness(config, db)

    app = FastAPI(
        title="ONASSIS API",
        version=config.version,
        description=(
            "Service interface for ONASSIS — the autonomous content engine. "
            "Generate campaigns and read everything they produce. JSON only."
        ),
    )
    # Serve the generated artwork over HTTPS so Gelato can fetch print files
    # directly (GELATO_FILE_BASE_URL = https://<server>/exports). Image files
    # only — internal JSON manifests are never exposed. Replaceable by an object
    # store later with no change to the Gelato connector.
    from onassis.exports_static import mount_exports

    mount_exports(app, config)

    # Production security (auth + rate limiting + headers + access logging).
    # Inert in development; auto-enabled in production. No business logic.
    from onassis.security import install_security

    install_security(app, config)

    # The Operations Centre — the browser control room (Jinja + HTMX + Alpine).
    # Adds no business logic; reuses the engines below. Serves /operations.
    from onassis.operations_centre import (
        OperationsState, build_operations_router,
    )

    app.state.ops_state = OperationsState()
    app.include_router(build_operations_router(lambda a: a.state.ops_state))

    # The Personaliser — buyer self-serve personalised products, unlocked by an
    # Etsy order number. Serves /make. Reuses the engines; no business logic.
    from onassis.personaliser_web import build_personaliser_router

    app.include_router(build_personaliser_router(config, db))

    # Expose services for tests / introspection.
    app.state.config = config
    app.state.orchestrator = orchestrator
    app.state.db = db
    app.state.campaigns = campaigns
    app.state.brain = brain
    app.state.profit = profit
    app.state.revenue = revenue
    app.state.etsy = etsy
    app.state.optimiser = optimiser
    app.state.listing_factory = listing_factory
    app.state.publisher = publisher
    app.state.analytics = analytics
    app.state.experiments = experiments
    app.state.daily = daily
    app.state.operations = operations
    app.state.readiness = readiness
    app.state.opportunities = opportunities
    app.state.report = report
    app.state.market = market
    app.state.design_builder = design_builder
    app.state.expansion = expansion
    app.state.learning = learning
    app.state.portfolio = portfolio
    app.state.traffic = traffic
    app.state.ceo = ceo
    app.state.gelato = gelato
    app.state.etsy_automation = etsy_automation
    app.state.etsy_intelligence = etsy_intelligence
    app.state.protection = protection

    from onassis.deployment import DeploymentService
    app.state.deployment = DeploymentService(config, db)
    app.state.integrations = IntegrationManager(config, db)
    app.state.reconcile_summary = _reconcile_summary

    from onassis.commercial import CommercialIntelligence
    app.state.commercial = CommercialIntelligence(config, db)
    app.state.cmo = daily.cmo
    app.state.marketing_learning = daily.marketing_learning
    from onassis.cfo import CFOManager
    app.state.cfo = CFOManager(config, db)
    from onassis.campaign_distributor import CampaignDistributor
    app.state.distribution = CampaignDistributor(config, db)
    from onassis.catalogue import CatalogueManager
    app.state.catalogue = CatalogueManager(config, db)
    from onassis.content_engine import ContentEngine
    app.state.content = ContentEngine(config, db)

    def _full_campaign(campaign_id: int) -> dict[str, Any] | None:
        """Assemble a campaign with its content and the Brain's prediction."""
        view = campaigns.get_campaign(campaign_id)
        if view is None:
            return None
        view["knowledge"] = brain.get_for_campaign(campaign_id)
        return view

    @app.get("/", tags=["meta"])
    def root() -> dict[str, str]:
        """Service banner with a pointer to the docs."""
        return {"service": "ONASSIS API", "version": config.version, "docs": "/docs"}

    @app.post("/daily/run", tags=["daily"])
    def daily_run(mode: str = "production") -> dict[str, Any]:
        """Run the daily cycle through the Operations Manager (pre-flight + report)."""
        return operations.run(mode=mode)

    @app.post("/operations/check", tags=["operations"])
    def operations_check(mode: str = "production") -> dict[str, Any]:
        """Run pre-flight health checks (no cycle, no decisions)."""
        return operations.check(mode=mode)

    @app.get("/operations/status", tags=["operations"])
    def operations_status() -> dict[str, Any]:
        """Latest operational status (or a live health snapshot if never run)."""
        return operations.status()

    @app.get("/operations/report", tags=["operations"])
    def operations_report() -> dict[str, Any]:
        """The latest full Operations Report (SYSTEM / BUSINESS / RECOMMENDATIONS)."""
        return operations.report()

    @app.get("/production/readiness", tags=["operations"])
    def production_readiness() -> dict[str, Any]:
        """Production Readiness Report: per-module status, blockers, checklist."""
        return readiness.report()

    @app.get("/daily/status", tags=["daily"])
    def daily_status() -> dict[str, Any]:
        """The most recent daily run."""
        return daily.status()

    @app.get("/daily/history", tags=["daily"])
    def daily_history() -> list[dict[str, Any]]:
        """Past daily runs (newest first)."""
        return daily.history()

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    def health() -> dict[str, Any]:
        """System status — confirms the service and database are reachable."""
        return {
            "status": "ok",
            "app": config.app_name,
            "version": config.version,
            "environment": config.environment,
            "campaigns": len(db.list_campaigns()),
            "knowledge": len(db.list_knowledge()),
        }

    @app.get("/dashboard", tags=["profit"])
    def dashboard() -> dict[str, Any]:
        """The profit-first company dashboard (net profit, ROI, cash, costs…)."""
        return profit.dashboard()

    @app.get("/revenue/today", tags=["revenue"])
    def revenue_today() -> dict[str, Any]:
        """Revenue and profit from today's orders."""
        return revenue.revenue_today()

    @app.get("/revenue/month", tags=["revenue"])
    def revenue_month() -> dict[str, Any]:
        """Revenue and profit from this month's orders."""
        return revenue.revenue_month()

    @app.get("/profit", tags=["revenue"])
    def profit_endpoint() -> dict[str, Any]:
        """Company-wide profit — the financial bottom line."""
        return revenue.company_profit()

    @app.get("/orders", tags=["revenue"])
    def list_orders() -> list[dict[str, Any]]:
        """All recorded orders (newest first), each with computed economics."""
        return revenue.list_orders()

    @app.get("/orders/{order_id}", tags=["revenue"])
    def get_order(order_id: int) -> dict[str, Any]:
        """One order with its full economics."""
        order = revenue.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail=f"No order with id {order_id}.")
        return order

    @app.get("/etsy/orders", tags=["etsy"])
    def etsy_orders() -> list[dict[str, Any]]:
        """Orders imported from Etsy."""
        return etsy.imported_orders()

    @app.get("/etsy/listings", tags=["etsy"])
    def etsy_listings() -> list[dict[str, Any]]:
        """Imported Etsy listings, each linked to its revenue and profit."""
        return etsy.listings()

    @app.get("/etsy/stats", tags=["etsy"])
    def etsy_stats() -> list[dict[str, Any]]:
        """Imported listing statistics (views, favourites, conversion)."""
        return etsy.stats()

    @app.get("/etsy/sync", tags=["etsy"])
    def etsy_sync() -> dict[str, Any]:
        """Run a read-only Etsy import; updates the Revenue Engine and metrics."""
        return etsy.sync()

    @app.get("/etsy/oauth/login", tags=["etsy"])
    def etsy_oauth_login() -> dict[str, Any]:
        """Start Etsy OAuth — returns the consent URL to open in a browser."""
        try:
            auth = etsy.oauth.create_authorization_url()
        except EtsyAuthError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"authorization_url": auth["url"], "state": auth["state"]}

    @app.get("/etsy/oauth/callback", tags=["etsy"])
    def etsy_oauth_callback(code: str, state: str | None = None) -> dict[str, Any]:
        """OAuth redirect target — exchanges the code for tokens (stored securely)."""
        try:
            etsy.oauth.exchange_code(code, state=state)
        except EtsyAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "authorised", **etsy.oauth.status()}

    @app.get("/etsy/oauth/status", tags=["etsy"])
    def etsy_oauth_status() -> dict[str, Any]:
        """Etsy OAuth authorisation status (no secrets)."""
        return etsy.oauth.status()

    @app.get("/pinterest/oauth/login", tags=["pinterest"])
    def pinterest_oauth_login() -> Any:
        """Start Pinterest OAuth — redirect to consent, requesting pins:write."""
        from starlette.responses import RedirectResponse

        from onassis.connectors.pinterest_oauth import authorize_url
        resolved = app.state.integrations.resolve("pinterest")
        app_id, redirect_uri = resolved.get("app_id"), resolved.get("redirect_uri")
        if not (app_id and redirect_uri):
            raise HTTPException(status_code=409,
                                detail="Set the Pinterest App ID + Redirect URI first.")
        return RedirectResponse(authorize_url(app_id, redirect_uri))

    @app.get("/pinterest/oauth/callback", tags=["pinterest"])
    def pinterest_oauth_callback(code: str | None = None, state: str | None = None,
                                 error: str | None = None) -> Any:
        """OAuth redirect target — exchange the code for a WRITE-scoped token and
        save it to the Pinterest integration (applied live, no restart)."""
        from starlette.responses import RedirectResponse

        from onassis.connectors.pinterest_oauth import (
            PinterestOAuthError, exchange_code,
        )
        if error or not code:
            raise HTTPException(status_code=400,
                                detail=f"Pinterest authorisation was not granted ({error or 'no code'}).")
        resolved = app.state.integrations.resolve("pinterest")
        base_url = (app.state.config.pinterest or {}).get(
            "base_url", "https://api.pinterest.com/v5")
        try:
            tok = exchange_code(resolved.get("app_id"), resolved.get("app_secret"),
                                code, resolved.get("redirect_uri"), base_url=base_url)
        except PinterestOAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        access = tok.get("access_token")
        if not access:
            raise HTTPException(status_code=400, detail="Pinterest returned no access token.")
        app.state.integrations.save("pinterest", {"access_token": access},
                                    operator="pinterest-oauth")
        return RedirectResponse("/operations?connected=pinterest")

    @app.get("/listing/{campaign_id}", tags=["listing"])
    def listing(campaign_id: int) -> dict[str, Any]:
        """Build & export a complete, upload-ready Etsy listing package."""
        try:
            package = listing_factory.export(campaign_id)
        except ListingError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if package.get("status") != "ready":
            # Approved gate or compliance blocked the export — report why.
            raise HTTPException(status_code=409, detail=package)
        return package

    @app.get("/listing/{campaign_id}/products", tags=["listing"])
    def listing_products(campaign_id: int) -> dict[str, Any]:
        """Build one listing package per CEO-approved product for a design."""
        try:
            result = listing_factory.export_products(campaign_id)
        except ListingError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if result.get("status") != "ready":
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/publish/{campaign_id}", tags=["publishing"])
    def publish(campaign_id: int, mode: str | None = None) -> dict[str, Any]:
        """Publish a campaign's listing package (Draft mode) — never duplicates."""
        return publisher.publish(campaign_id, mode=mode)

    @app.post("/publish/{campaign_id}/products", tags=["publishing"])
    def publish_products(campaign_id: int, mode: str | None = None) -> dict[str, Any]:
        """Publish each CEO-approved product's listing as an Etsy draft."""
        return publisher.publish_products(campaign_id, mode=mode)

    @app.post("/launch/approve/{campaign_id}", tags=["publishing"])
    def approve_launch(campaign_id: int) -> dict[str, Any]:
        """One approval — approve the master design and every approved product."""
        return publisher.approve_launch(campaign_id, by="owner")

    @app.get("/launch/status/{campaign_id}", tags=["publishing"])
    def launch_status(campaign_id: int) -> dict[str, Any]:
        """A design's launch status (none | launch_ready | launched)."""
        return publisher.launch_status(campaign_id)

    @app.get("/launch/pending", tags=["publishing"])
    def pending_launches() -> list[dict[str, Any]]:
        """Designs at Launch Ready awaiting a single approval."""
        return publisher.pending_launches()

    @app.get("/report/daily", tags=["reporting"])
    def daily_report() -> dict[str, Any]:
        """The daily scoreboard — Revenue, Profit, Best/Worst seller, and a
        per-product Expand / Hold / Kill recommendation."""
        return report.build()

    @app.get("/ceo/dashboard", tags=["reporting"])
    def ceo_dashboard() -> dict[str, Any]:
        """The CEO money scoreboard — Revenue/Profit yesterday, Visitors,
        Conversion, Pinterest clicks, Best/Worst seller, Products launched/retired,
        Cash generated, AI cost, ROI."""
        return ceo.build()

    @app.get("/learning/daily", tags=["reporting"])
    def learning_daily() -> dict[str, Any]:
        """The morning learning digest — what sold, what didn't, why — plus the
        actions taken: increase winners, retire losers, adjust the rest."""
        return learning.run()

    @app.get("/portfolio/reviews", tags=["portfolio"])
    def portfolio_reviews(sku: str | None = None) -> list[dict[str, Any]]:
        """The 30-day lifecycle verdict history (KEEP / IMPROVE / RETIRE)."""
        return portfolio.history(sku)

    @app.get("/portfolio/archived", tags=["portfolio"])
    def portfolio_archived() -> list[dict[str, Any]]:
        """Products that have been retired (archived)."""
        return portfolio.archived()

    @app.get("/traffic/schedule", tags=["traffic"])
    def traffic_schedule(date: str | None = None) -> list[dict[str, Any]]:
        """The Pinterest posting schedule (optionally for one YYYY-MM-DD)."""
        return db.list_pin_schedule(scheduled_date=date)

    @app.get("/traffic/funnel", tags=["traffic"])
    def traffic_funnel(date: str | None = None) -> dict[str, Any]:
        """The traffic funnel — Impressions -> Clicks -> Visits -> Sales."""
        return traffic.funnel(date)

    @app.post("/traffic/run", tags=["traffic"])
    def traffic_run() -> dict[str, Any]:
        """Schedule pins, post the due ones, and import Pinterest metrics."""
        return traffic.run()

    @app.get("/marketing/{product_key}", tags=["marketing"])
    def marketing_assets(product_key: str) -> list[dict[str, Any]]:
        """The stored marketing kit (per channel) for a product."""
        return db.list_marketing_assets(product_key=product_key)

    @app.get("/fulfilment/status", tags=["fulfilment"])
    def fulfilment_status() -> dict[str, Any]:
        """Gelato fulfilment summary — counts by status + recent orders."""
        return gelato.status()

    @app.post("/fulfilment/run", tags=["fulfilment"])
    def fulfilment_run() -> dict[str, Any]:
        """Submit new paid orders to Gelato and poll production status/tracking."""
        return {"submit": gelato.fulfil_new_orders(), "sync": gelato.sync_status()}

    @app.get("/etsy/changes", tags=["etsy"])
    def etsy_changes(listing_id: str | None = None) -> list[dict[str, Any]]:
        """The audit log of automated changes ONASSIS wrote to Etsy listings."""
        return etsy_automation.audit(listing_id)

    @app.get("/etsy/intelligence", tags=["etsy"])
    def etsy_intelligence_report() -> dict[str, Any]:
        """Real conversion (per product + shop) and keyword performance from
        synced Etsy data."""
        return etsy_intelligence.intelligence()

    @app.get("/etsy/search-terms", tags=["etsy"])
    def etsy_search_terms() -> list[dict[str, Any]]:
        """Aggregated search-term performance (impressions/clicks/orders/CTR)."""
        return etsy_intelligence.keyword_performance()

    @app.get("/protection/audit", tags=["protection"])
    def protection_audit(decision: str | None = None) -> list[dict[str, Any]]:
        """The commercial audit trail — every protection decision, approve or reject."""
        return protection.audit(decision)

    @app.get("/protection/alerts", tags=["protection"])
    def protection_alerts() -> list[dict[str, Any]]:
        """Actions the Financial Protection Engine stopped (CEO alerts)."""
        return protection.alerts()

    @app.get("/market/report", tags=["market"])
    def market_report(limit: int = 20) -> list[dict[str, Any]]:
        """The latest Market Intelligence report — keywords scored by demand vs
        competition, best opportunity first."""
        return market.latest(limit=limit)

    @app.post("/market/research", tags=["market"])
    def market_research() -> dict[str, Any]:
        """Run market research and return the scored keyword report."""
        return market.research()

    @app.get("/publishing/status", tags=["publishing"])
    def publishing_status() -> dict[str, Any]:
        """Publication log summary (counts by status + recent publications)."""
        return publisher.status()

    @app.get("/experiments/active", tags=["experiments"])
    def experiments_active() -> list[dict[str, Any]]:
        """All currently-running experiments."""
        return experiments.active()

    @app.get("/experiments", tags=["experiments"])
    def experiments_list() -> list[dict[str, Any]]:
        """All experiments (newest first)."""
        return experiments.list()

    @app.get("/experiments/{experiment_id}", tags=["experiments"])
    def experiments_get(experiment_id: int) -> dict[str, Any]:
        """One experiment by id."""
        exp = experiments.get(experiment_id)
        if exp is None:
            raise HTTPException(status_code=404, detail=f"No experiment {experiment_id}.")
        return exp

    @app.post("/experiments", tags=["experiments"])
    def experiments_start(payload: dict[str, Any]) -> dict[str, Any]:
        """Start an experiment (rejects a duplicate active test on the variable)."""
        try:
            return experiments.start(**payload)
        except (ExperimentError, TypeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/experiments/{experiment_id}/complete", tags=["experiments"])
    def experiments_complete(experiment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Complete an experiment: store result, confidence, and learning."""
        try:
            return experiments.complete(experiment_id, **payload)
        except (ExperimentError, TypeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/analytics", tags=["analytics"])
    def analytics_overall() -> dict[str, Any]:
        """Collected-analytics summary (snapshot counts, products, platforms)."""
        return analytics.overall()

    @app.get("/analytics/product/{product_id}", tags=["analytics"])
    def analytics_product(product_id: str) -> dict[str, Any]:
        """Historical metrics and trends for one product."""
        return analytics.product_analytics(product_id)

    @app.get("/analytics/campaign/{campaign_id}", tags=["analytics"])
    def analytics_campaign(campaign_id: int) -> dict[str, Any]:
        """Historical metrics and trends for one campaign."""
        return analytics.campaign_analytics(campaign_id)

    @app.get("/optimiser", tags=["optimiser"])
    def optimiser_recommendation() -> dict[str, Any]:
        """The single highest-value action for an existing product (CEO-reviewed)."""
        rec = optimiser.top_recommendation()
        if rec is None:
            return {"message": "No live products to analyse."}
        return {
            "product_analysed": rec["product"],
            "recommendation": rec["recommendation"],
            "expected_roi": rec["expected_roi"],
            "reasoning": rec["reasoning"],
            "confidence": rec["confidence"],
            "estimated_cost": rec["estimated_cost"],
            "expected_increase_in_profit": rec["expected_increase_in_profit"],
            "ceo_verdict": rec["ceo"]["verdict"],
            "metrics": rec["metrics"],
        }

    @app.get("/opportunities", tags=["opportunities"])
    def list_opportunities(status: str | None = None) -> list[dict[str, Any]]:
        """The product development backlog, ranked by expected commercial value."""
        return opportunities.list_opportunities(status=status)

    @app.get("/opportunities/top", tags=["opportunities"])
    def top_opportunities(limit: int = 5) -> list[dict[str, Any]]:
        """The highest expected-value opportunities still in the backlog."""
        return opportunities.top(limit=limit)

    @app.post("/opportunities/generate", tags=["opportunities"])
    def generate_opportunities(
        count: int | None = None, season: str | None = None, focus: str | None = None
    ) -> dict[str, Any]:
        """Discover new product opportunities (no images/mock-ups/listings)."""
        return opportunities.generate(count, season=season, focus=focus)

    @app.post("/opportunities/{opportunity_id}/build-design-package", tags=["opportunities"])
    def build_design_package(opportunity_id: str) -> dict[str, Any]:
        """Turn one CEO-approved opportunity into a print-ready design package."""
        try:
            package = design_builder.build(opportunity_id)
        except DesignPackageError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if package.get("status") != "ready":
            # CEO or compliance gate blocked the build — report why.
            raise HTTPException(status_code=409, detail=package)
        return package

    @app.get("/expansion/catalogue", tags=["expansion"])
    def expansion_catalogue() -> list[dict[str, Any]]:
        """The Phase-1 product catalogue (available products)."""
        return expansion.catalogue()

    @app.post("/expansion/plan/{campaign_id}", tags=["expansion"])
    def expansion_plan(campaign_id: int) -> dict[str, Any]:
        """Score the catalogue for a design and launch the profitable set (CEO)."""
        expansion.learn_from_sales()
        return expansion.plan(campaign_id)

    @app.get("/expansion/plan/{campaign_id}", tags=["expansion"])
    def expansion_plan_read(campaign_id: int) -> list[dict[str, Any]]:
        """The recorded product scores for a campaign (best first)."""
        return expansion.plan_for(campaign_id)

    @app.get("/expansion/performance", tags=["expansion"])
    def expansion_performance() -> list[dict[str, Any]]:
        """Learned per-product-type performance from real sales."""
        return expansion.performance()

    @app.get("/opportunities/{opportunity_id}/design-package", tags=["opportunities"])
    def get_design_package(opportunity_id: str) -> dict[str, Any]:
        """Read back a previously built design package."""
        package = design_builder.get_package(opportunity_id)
        if package is None:
            raise HTTPException(
                status_code=404,
                detail=f"No design package for opportunity {opportunity_id}.",
            )
        return package

    @app.post(
        "/campaign/create", response_model=CampaignCreateResponse, tags=["campaigns"]
    )
    def create_campaign() -> dict[str, Any]:
        """Run the one product-first production workflow and return its campaign.

        Same workflow as ``POST /daily/run``: a sellable product is created
        first (opportunity → design → Etsy listing); marketing content is
        generated only afterwards to promote it.
        """
        started = time.monotonic()
        try:
            result = daily.run("production")
        except Exception as exc:  # surface generation failures as 500s
            log.exception("Production workflow failed")
            raise HTTPException(status_code=500, detail=f"Production workflow failed: {exc}")
        if result.get("campaign_id") is None:
            raise HTTPException(
                status_code=409,
                detail="The cycle produced no product/campaign (see /daily/status).",
            )
        return {
            "campaign_id": result["campaign_id"],
            "status": "completed",
            "assets_created": result.get("assets_created", 0),
            "duration_seconds": round(time.monotonic() - started),
        }

    @app.get("/campaigns", response_model=list[CampaignSummary], tags=["campaigns"])
    def list_campaigns() -> list[dict[str, Any]]:
        """Return all campaigns (newest first), each with a content count."""
        return campaigns.list_campaigns()

    @app.get("/campaign/latest", tags=["campaigns"])
    def latest_campaign() -> dict[str, Any]:
        """Return the most recently generated campaign, with all its assets."""
        rows = campaigns.list_campaigns()  # newest first
        if not rows:
            raise HTTPException(status_code=404, detail="No campaigns yet.")
        full = _full_campaign(rows[0]["id"])
        assert full is not None  # it exists — we just listed it
        return full

    @app.get("/campaign/{campaign_id}", tags=["campaigns"])
    def get_campaign(campaign_id: int) -> dict[str, Any]:
        """Return one campaign with all generated assets and its prediction."""
        full = _full_campaign(campaign_id)
        if full is None:
            raise HTTPException(status_code=404, detail=f"No campaign with id {campaign_id}.")
        return full

    return app


# Module-level app for `uvicorn onassis.api:app`.
_config = load_config()
setup_logging(_config)
app = create_app(_config)
