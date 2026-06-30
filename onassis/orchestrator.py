"""Content agents + the marketing-content step.

The :class:`Orchestrator` owns the shared resources (config + database) and
instantiates the content agents once (Director, Creator, Brain, Compliance,
Campaign Manager, Publisher, Analytics). It exposes the **marketing-content**
step of the single product-first production workflow.

There is exactly one production workflow — :class:`~onassis.daily_cycle.DailyCycle`
— and it is product-first: a sellable product (opportunity → design package →
Etsy listing) is created *before* any marketing content. This module provides
the last creative step of that workflow, :meth:`Orchestrator.generate_marketing_content`,
which promotes an already-created, approved product. There is no separate
content-first pipeline.
"""

from __future__ import annotations

from typing import Any

from onassis.agents import AnalyticsAgent, ContentCreator, ContentDirector, Publisher
from onassis.brain import OnassisBrain
from onassis.campaign_manager import CampaignManager
from onassis.compliance import ComplianceDirector
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.profit import ProfitEngine

log = get_logger(__name__)


class Orchestrator:
    """Builds the agents and runs the daily content pipeline."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        # Agents are cheap and stateless; build them once and reuse.
        self.director = ContentDirector(config, db)
        self.creator = ContentCreator(config, db)
        self.publisher = Publisher(config, db)
        self.analytics = AnalyticsAgent(config, db)
        # Campaigns are the central object — every run produces one.
        self.campaigns = CampaignManager(config, db)
        # The Brain predicts and remembers — every campaign generates knowledge.
        self.brain = OnassisBrain(config, db)
        # The Compliance Director reviews every campaign (veto authority).
        self.compliance = ComplianceDirector(config, db)
        # The Profit Engine records the economics of each run.
        self.profit = ProfitEngine(config, db)

    def generate_marketing_content(self, brief: dict[str, Any]) -> dict[str, Any]:
        """Generate and persist the marketing content for a product's brief.

        There is a single, product-first production workflow
        (:class:`~onassis.daily_cycle.DailyCycle`): a sellable product is created
        first, and this is the **last** creative step — marketing exists only to
        promote a product/campaign that already exists and was approved. Returns
        the created content items and the (placeholder) publish result.
        """
        items = self.creator.execute(brief=brief)
        publish_result = self.publisher.execute(brief_id=brief["id"])
        return {"items": items, "published": publish_result["published"]}
