"""The Autonomous Publisher.

Publishes approved listing packages from the ``exports/`` folder to Etsy as
**drafts**. It supports three modes — Dry Run, Draft, Live — but only Dry Run
and Draft are enabled; Live is intentionally not implemented yet.

Safety guarantees:

* Only **compliance-approved** campaigns are published (Company Law gate).
* Every publication is logged (platform, product, campaign, date/time, listing
  id, status).
* Failures are retried safely and the reason is recorded.
* It **never creates duplicate listings** — an existing draft/published record
  for a campaign short-circuits a re-publish.

Etsy *write* access is injected (a draft client), so this runs live with write
credentials and is fully tested offline with a stub. No advertising; products
are not modified after publication.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from onassis.config import ROOT_DIR, Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.proposals import APPROVE

log = get_logger(__name__)

DRY_RUN = "dry_run"
DRAFT = "draft"
LIVE = "live"
PLATFORM = "etsy"


class PublisherService:
    """Reads listing packages and publishes them (Draft mode) with logging."""

    def __init__(self, config: Config, db: Database, draft_client: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = config.publishing or {}
        self.listing_cfg = config.listing or {}
        self.max_retries = int(self.cfg.get("max_retries", 3))
        self.enabled_modes = set(self.cfg.get("enabled_modes", [DRY_RUN, DRAFT]))
        self.default_mode = self.cfg.get("default_mode", DRAFT)
        self._draft_client = draft_client

    # --- Configuration ----------------------------------------------

    @property
    def draft_configured(self) -> bool:
        if self._draft_client is not None:
            return True
        e = self.config.etsy or {}
        if not e.get("api_key"):
            return False
        if e.get("access_token"):
            return True
        from onassis.connectors.etsy_oauth import build_etsy_oauth

        return build_etsy_oauth(self.config).is_authorised

    # --- Publish ----------------------------------------------------

    def publish(self, campaign_id: int, mode: str | None = None) -> dict[str, Any]:
        """Publish a campaign's listing package. Returns a result dict."""
        mode = (mode or self.default_mode).lower()
        if mode == LIVE or mode not in self.enabled_modes:
            return {"status": "blocked", "campaign_id": campaign_id, "mode": mode,
                    "reason": f"Publishing mode '{mode}' is not enabled."}

        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "No such campaign."}

        # Approval gate — only compliance-approved campaigns may publish.
        approval = self.db.get_compliance_for_campaign(campaign_id)
        if not approval or approval.get("verdict") != APPROVE:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "Campaign is not compliance-approved."}

        # Never duplicate — a prior draft/published record short-circuits.
        existing = self.db.get_active_publication(campaign_id, PLATFORM)
        if existing:
            return {"status": "skipped", "campaign_id": campaign_id,
                    "reason": "Already published; not creating a duplicate.",
                    "publication": existing}

        listing = self._load_listing(campaign_id)
        if listing is None:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "No listing package found — build it first."}
        product_id = listing.get("product_id")

        if mode == DRY_RUN:
            pub = {"platform": PLATFORM, "product_id": product_id,
                   "campaign_id": campaign_id, "listing_id": None,
                   "mode": DRY_RUN, "status": DRY_RUN, "attempts": 1}
            pub["id"] = self.db.insert_publication(pub)
            return {"status": DRY_RUN, "campaign_id": campaign_id, "publication": pub}

        # DRAFT mode.
        if not self.draft_configured:
            return {"status": "not_configured", "campaign_id": campaign_id,
                    "reason": "Etsy write credentials are not set."}

        return self._publish_draft(campaign_id, product_id, listing)

    def _publish_draft(
        self, campaign_id: int, product_id: str | None, listing: dict[str, Any]
    ) -> dict[str, Any]:
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                result = self._draft_backend().create_draft(listing)
                listing_id = str(result.get("listing_id"))
                pub = {"platform": PLATFORM, "product_id": product_id,
                       "campaign_id": campaign_id, "listing_id": listing_id,
                       "mode": DRAFT, "status": DRAFT, "attempts": attempt}
                pub["id"] = self.db.insert_publication(pub)
                log.info("Published campaign #%s as Etsy draft %s", campaign_id, listing_id)
                return {"status": DRAFT, "campaign_id": campaign_id, "publication": pub}
            except Exception as exc:  # transient failure — retry safely
                last_error = str(exc)
                log.warning("Publish attempt %d for campaign #%s failed: %s",
                            attempt, campaign_id, last_error)

        pub = {"platform": PLATFORM, "product_id": product_id,
               "campaign_id": campaign_id, "listing_id": None, "mode": DRAFT,
               "status": "failed", "attempts": self.max_retries,
               "failure_reason": last_error}
        pub["id"] = self.db.insert_publication(pub)
        return {"status": "failed", "campaign_id": campaign_id,
                "reason": last_error, "publication": pub}

    # --- Status -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        pubs = self.db.list_publications()
        counts: dict[str, int] = {}
        for p in pubs:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        return {
            "total": len(pubs),
            "by_status": counts,
            "draft_configured": self.draft_configured,
            "enabled_modes": sorted(self.enabled_modes),
            "recent": pubs[:20],
        }

    # --- Helpers ----------------------------------------------------

    def _draft_backend(self) -> Any:
        if self._draft_client is None:
            from onassis.connectors.etsy_client import EtsyDraftClient

            e = self.config.etsy or {}
            token_provider = None
            if not e.get("access_token"):
                from onassis.connectors.etsy_oauth import build_etsy_oauth

                token_provider = build_etsy_oauth(self.config).valid_access_token
            self._draft_client = EtsyDraftClient(
                api_key=e.get("api_key"), shop_id=e.get("shop_id"),
                access_token=e.get("access_token"), token_provider=token_provider,
                shared_secret=e.get("client_secret"),
                base_url=e.get("base_url", "https://openapi.etsy.com/v3/application"),
            )
        return self._draft_client

    def _load_listing(self, campaign_id: int) -> dict[str, Any] | None:
        base = Path(self.listing_cfg.get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        path = base / str(campaign_id) / "listing.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
