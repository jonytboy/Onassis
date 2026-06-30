"""Campaign Manager — the central object of ONASSIS.

A **campaign** is the spine everything hangs off: a named, themed body of
work with a story, a status, and the content that belongs to it. Each daily
brief produced by the Content Director becomes exactly one campaign (1:1),
and the content the Creator generates for that brief belongs to the campaign.

This module owns campaign *logic* — lifecycle, status rules, and the
dashboard view. It does not touch SQL directly; all persistence goes through
:class:`onassis.database.Database`, keeping storage in one place (consistent
with the rest of the system).

Campaign lifecycle::

    Draft -> Scheduled -> Live -> Complete

(Publishing is intentionally NOT built — status is tracked, not acted on.)
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# The allowed campaign statuses, in lifecycle order.
STATUSES: tuple[str, ...] = ("Draft", "Scheduled", "Live", "Complete")


class CampaignError(ValueError):
    """Raised for invalid campaign operations (bad status, missing campaign)."""


class CampaignManager:
    """Creates, lists, and inspects campaigns — the hub of ONASSIS."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # --- Creation ---------------------------------------------------

    def create_from_brief(self, brief: dict[str, Any]) -> dict[str, Any]:
        """Create the campaign for a brief (idempotent, 1:1 with the brief).

        The brief's campaign concept becomes the campaign's story, so the
        campaign carries the human-readable narrative independent of the
        brief's internal structure.

        Args:
            brief: A brief dict from the Content Director (must include ``id``).

        Returns:
            The campaign record (existing one if it already exists).
        """
        brief_id = brief["id"]
        existing = self.db.get_campaign_by_brief(brief_id)
        if existing:
            return existing

        campaign = {
            "name": brief.get("campaign_name") or brief.get("theme") or "Untitled campaign",
            "theme": brief.get("theme", ""),
            "story": brief.get("concept", ""),
            "status": "Draft",
            "brief_id": brief_id,
        }
        campaign["id"] = self.db.insert_campaign(campaign)
        log.info("Campaign #%s created from brief #%s", campaign["id"], brief_id)
        return campaign

    def create_from_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        design: dict[str, Any] | None = None,
        for_date: date | None = None,
    ) -> dict[str, Any]:
        """Create the marketing campaign **for a product opportunity**.

        This is the product-first bridge: the campaign (and therefore the
        marketing content generated from it) is driven by an approved product
        opportunity rather than a generic brief. It derives a brief from the
        opportunity (and its design package), creates the campaign, and
        registers a Product linked to it so the listing factory can price it
        and future Etsy orders link back.

        Returns ``{campaign, brief, product_id}``.
        """
        opp = opportunity
        design_brief = (design or {}).get("design_brief", {}) if design else {}
        visual = design_brief.get("artwork_description") or ", ".join(
            x for x in (
                opp.get("photography_style"),
                opp.get("illustration_style"),
                ", ".join(opp.get("colour_palette", []) or []),
            ) if x
        )
        keywords = list(dict.fromkeys(
            k for k in (
                [opp.get("product_type", "")]
                + [s.strip() for s in re.split(r"[,/]", opp.get("search_intent", "")) if s.strip()]
            ) if k
        ))

        brief: dict[str, Any] = {
            "brief_date": (for_date or date.today()).isoformat(),
            "season": opp.get("seasonal_relevance", ""),
            "brand": opp.get("brand") or self.config.brand.get("name", "Local Celebrity"),
            "campaign_name": opp.get("product_name") or "New product",
            "theme": opp.get("theme", ""),
            "concept": opp.get("concept", ""),
            "tone": opp.get("emotional_angle", ""),
            "audience": opp.get("target_customer", ""),
            "objective": f"Promote and sell {opp.get('product_name', 'the product')} on Etsy.",
            "visual_direction": visual,
            "keywords": keywords,
        }
        brief["id"] = self.db.insert_brief(brief)
        campaign = self.create_from_brief(brief)

        production_cost = float((self.config.listing or {}).get("default_production_cost", 12.0))
        product_id = self.db.insert_product({
            "sku": opp.get("opportunity_id"),
            "name": opp.get("product_name", ""),
            "campaign_id": campaign["id"],
            "brand": brief["brand"],
            "marketplace": "etsy",
            "production_cost": production_cost,
        })
        log.info(
            "Campaign #%s created from opportunity %s (product #%s)",
            campaign["id"], opp.get("opportunity_id"), product_id,
        )
        return {"campaign": campaign, "brief": brief, "product_id": product_id}

    def sync_from_briefs(self) -> int:
        """Backfill campaigns for any briefs that don't have one yet.

        Guarantees the 'everything belongs to a campaign' invariant even for
        briefs created before campaigns existed. Returns how many were created.
        """
        orphans = self.db.get_briefs_without_campaign()
        for brief in orphans:
            # get_brief returns the stored payload; merge it so we recover
            # campaign_name/concept that live in the JSON payload.
            merged = self._brief_with_payload(brief)
            self.create_from_brief(merged)
        if orphans:
            log.info("Backfilled %d campaign(s) from existing briefs", len(orphans))
        return len(orphans)

    # --- Status lifecycle -------------------------------------------

    def set_status(self, campaign_id: int, status: str) -> dict[str, Any]:
        """Update a campaign's status (validated against the lifecycle).

        Company Law: a campaign may not progress beyond ``Draft`` without an
        approving Compliance report — the Compliance Director's veto applies
        here too.
        """
        if status not in STATUSES:
            raise CampaignError(
                f"Invalid status {status!r}. Must be one of: {', '.join(STATUSES)}"
            )
        if status != "Draft":  # advancing requires Compliance approval
            report = self.db.get_compliance_for_campaign(campaign_id)
            if report is None or report.get("verdict") != "APPROVE":
                raise CampaignError(
                    "Compliance approval required before a campaign can progress "
                    f"beyond Draft (campaign #{campaign_id})."
                )
        if not self.db.update_campaign_status(campaign_id, status):
            raise CampaignError(f"No campaign with id {campaign_id}")
        log.info("Campaign #%s -> %s", campaign_id, status)
        campaign = self.db.get_campaign(campaign_id)
        assert campaign is not None  # just updated it
        return campaign

    # --- Views ------------------------------------------------------

    def list_campaigns(self) -> list[dict[str, Any]]:
        """All campaigns (newest first), each enriched with a content count."""
        self.sync_from_briefs()  # keep the dashboard complete
        campaigns = self.db.list_campaigns()
        for c in campaigns:
            c["content_count"] = len(self.db.get_content_for_brief(c["brief_id"]))
        return campaigns

    def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        """A full campaign view: metadata, the brief, and all its content."""
        self.sync_from_briefs()
        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            return None
        content = self.db.get_content_for_brief(campaign["brief_id"])
        campaign["content_ids"] = [item["id"] for item in content]
        campaign["content"] = content
        campaign["brief"] = self.db.get_brief(campaign["brief_id"])
        return campaign

    # --- Helpers ----------------------------------------------------

    @staticmethod
    def _brief_with_payload(brief: dict[str, Any]) -> dict[str, Any]:
        """Merge a brief row's JSON payload up to the top level."""
        import json

        merged = dict(brief)
        try:
            payload = json.loads(brief.get("payload") or "{}")
            for key, value in payload.items():
                merged.setdefault(key, value)
        except (TypeError, ValueError):
            pass
        return merged
