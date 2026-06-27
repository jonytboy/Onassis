"""REST API for ONASSIS — the service interface for external systems (e.g. Make).

This is a **thin** layer. It owns no business logic: every endpoint simply
calls an existing service (:class:`~onassis.orchestrator.Orchestrator`,
:class:`~onassis.campaign_manager.CampaignManager`,
:class:`~onassis.brain.OnassisBrain`, :class:`~onassis.database.Database`).
The existing architecture is untouched — this module only exposes it over HTTP.

Run it::

    python main.py --serve            # or: uvicorn onassis.api:app

Interactive docs (Swagger) are served at ``/docs``; the OpenAPI schema at
``/openapi.json``. All responses are JSON. No authentication yet (Sprint 5
scope), and no publishing.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from onassis.config import Config, load_config
from onassis.connectors.etsy import EtsyConnector
from onassis.database import Database
from onassis.logger import get_logger, setup_logging
from onassis.orchestrator import Orchestrator
from onassis.profit import ProfitEngine
from onassis.revenue import RevenueEngine

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
    orchestrator = Orchestrator(config, Database(config.db_path))
    # Reuse the orchestrator's own services so the whole app shares one set.
    db = orchestrator.db
    campaigns = orchestrator.campaigns
    brain = orchestrator.brain
    profit = ProfitEngine(config, db)
    revenue = RevenueEngine(config, db)
    etsy = EtsyConnector(config, db)

    app = FastAPI(
        title="ONASSIS API",
        version=config.version,
        description=(
            "Service interface for ONASSIS — the autonomous content engine. "
            "Generate campaigns and read everything they produce. JSON only."
        ),
    )
    # Expose services for tests / introspection.
    app.state.config = config
    app.state.orchestrator = orchestrator
    app.state.db = db
    app.state.campaigns = campaigns
    app.state.brain = brain
    app.state.profit = profit
    app.state.revenue = revenue
    app.state.etsy = etsy

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

    @app.post(
        "/campaign/create", response_model=CampaignCreateResponse, tags=["campaigns"]
    )
    def create_campaign() -> dict[str, Any]:
        """Run the full campaign generation pipeline and return a summary."""
        started = time.monotonic()
        try:
            summary = orchestrator.run_daily()
        except Exception as exc:  # surface generation failures as 500s
            log.exception("Campaign generation failed")
            raise HTTPException(status_code=500, detail=f"Campaign generation failed: {exc}")
        return {
            "campaign_id": summary["campaign_id"],
            "status": "completed",
            "assets_created": summary["items_created"],
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
