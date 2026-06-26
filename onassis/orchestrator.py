"""Pipeline orchestration.

The :class:`Orchestrator` owns the shared resources (config + database),
instantiates the agents once, and defines the **daily pipeline** that
wires them together. This is the single place that knows the *order* of
operations, which keeps the agents themselves decoupled and unaware of
one another.

Daily pipeline::

    Content Director    -> brief
    Campaign Manager    -> campaign (the brief becomes a campaign; content belongs to it)
    Content Creator     -> content (stored in SQLite)
    ONASSIS Brain       -> knowledge (a prediction the campaign will be measured against)
    Compliance Director -> compliance report (risk/brand review of the campaign)
    Publisher           -> no-op (placeholder)
    Analytics Agent     -> local counts (placeholder)
"""

from __future__ import annotations

from datetime import date
from typing import Any

from onassis.agents import AnalyticsAgent, ContentCreator, ContentDirector, Publisher
from onassis.brain import OnassisBrain
from onassis.campaign_manager import CampaignManager
from onassis.compliance import ComplianceDirector
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

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

    def run_daily(self, *, for_date: date | None = None) -> dict[str, Any]:
        """Run one full pass of the daily pipeline.

        Returns a small summary dict that's handy for logs and tests.
        """
        log.info("=== ONASSIS daily pipeline START ===")

        brief = self.director.execute(for_date=for_date)
        campaign = self.campaigns.create_from_brief(brief)
        items = self.creator.execute(brief=brief)
        knowledge = self.brain.generate_for_campaign(campaign, brief)
        compliance = self.compliance.review_campaign(campaign, items)
        publish_result = self.publisher.execute(brief_id=brief["id"])
        analytics_result = self.analytics.execute(brief_id=brief["id"])

        summary = {
            "campaign_id": campaign["id"],
            "campaign_name": campaign["name"],
            "brief_id": brief["id"],
            "theme": brief["theme"],
            "items_created": len(items),
            "knowledge_id": knowledge["id"],
            "confidence": knowledge["confidence"],
            "compliance_score": compliance["compliance_score"],
            "compliance_verdict": compliance["verdict"],
            "published": publish_result["published"],
            "analytics": analytics_result,
        }
        log.info("=== ONASSIS daily pipeline DONE: %s ===", summary)
        return summary
