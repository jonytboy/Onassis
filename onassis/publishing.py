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
from onassis.product_status import is_valid_listing_id
from onassis.proposals import is_compliant

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
        self.min_go_live_margin = float(self.cfg.get("min_go_live_margin", 0.10))
        launch_cfg = config.launch or {}
        self.launch_policy = launch_cfg.get("policy", "manual")
        # Live go-live requires the toggle AND 'live' in the enabled modes.
        self.auto_go_live = bool(launch_cfg.get("auto_go_live", False)) \
            and LIVE in self.enabled_modes
        from onassis.fees import FeeModel

        self.fee_model = FeeModel.from_config(config)
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

    def publish_products(self, campaign_id: int, mode: str | None = None) -> dict[str, Any]:
        """Publish every **CEO-approved** product's listing as a draft.

        Iterates only the products the Revenue Expansion Engine launched (never
        the rejected ones) and publishes each package via :meth:`publish`.
        """
        launched = [s for s in self.db.list_product_scores(campaign_id) if s.get("launched")]
        if not launched:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "No CEO-approved products to publish."}
        results = [
            self.publish(campaign_id, mode=mode, product_key=s["product_key"])
            for s in launched
        ]
        # Honest counts (Sprint 40): a real Etsy draft is only DRAFT status with
        # a listing id — never a dry_run, never a failed attempt.
        drafts_created = sum(1 for r in results if r["status"] == DRAFT)
        drafts_failed = sum(1 for r in results if r["status"] in ("failed", "not_configured"))
        dry_runs = sum(1 for r in results if r["status"] == DRY_RUN)
        # ``published`` counts real drafts (draft mode) or the dry-run simulations
        # (dry-run mode) so a dry-run still reports what it *would* publish, but
        # the two are never conflated in draft mode.
        published = drafts_created + (dry_runs if (mode or self.default_mode) == DRY_RUN else 0)
        return {"status": "ok", "campaign_id": campaign_id,
                "count": len(results), "published": published,
                "drafts_created": drafts_created, "drafts_failed": drafts_failed,
                "dry_runs": dry_runs, "results": results}

    def publish(
        self, campaign_id: int, mode: str | None = None, product_key: str | None = None
    ) -> dict[str, Any]:
        """Publish a listing package. With ``product_key``, publishes that
        specific approved product's package (one draft per product)."""
        mode = (mode or self.default_mode).lower()
        if mode == LIVE or mode not in self.enabled_modes:
            return {"status": "blocked", "campaign_id": campaign_id, "mode": mode,
                    "reason": f"Publishing mode '{mode}' is not enabled."}

        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "No such campaign."}

        # Approval gate — only compliance-cleared campaigns may publish
        # (APPROVE or APPROVE_WITH_CHANGES; a REJECT never publishes).
        approval = self.db.get_compliance_for_campaign(campaign_id)
        if not approval or not is_compliant(approval.get("verdict", "")):
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "Campaign is not compliance-approved."}

        # Per-product id (matches the Expansion Engine's Product sku) for dedup.
        product_id = f"{campaign_id}-{product_key}" if product_key else None

        # Never duplicate — a prior draft/published record short-circuits.
        existing = self.db.get_active_publication(campaign_id, PLATFORM, product_id=product_id)
        if existing:
            return {"status": "skipped", "campaign_id": campaign_id,
                    "product_key": product_key,
                    "reason": "Already published; not creating a duplicate.",
                    "publication": existing}

        listing = self._load_listing(campaign_id, product_key)
        if listing is None:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "product_key": product_key,
                    "reason": "No listing package found — build it first."}
        product_id = listing.get("product_id") or product_id

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

        # Mockup Quality Gate (P1): never publish a product whose gallery is only
        # placeholder/fallback images or failed quality — require a real mockup.
        from onassis.mockup_gate import evaluate_listing as _eval_mockups
        mq = _eval_mockups(listing)
        if not mq["ok"]:
            reason = f"{mq['message']} {mq['reason']}".strip()
            pub = {"platform": PLATFORM, "product_id": product_id,
                   "campaign_id": campaign_id, "listing_id": None, "mode": DRAFT,
                   "status": "failed", "attempts": 0, "failure_reason": reason}
            pub["id"] = self.db.insert_publication(pub)
            log.warning("Mockup gate blocked campaign #%s product %s: %s",
                        campaign_id, product_key, reason)
            return {"status": "failed", "campaign_id": campaign_id,
                    "product_key": product_key, "reason": reason,
                    "mockup_quality": mq, "mockup_blocked": True, "publication": pub}

        images_dir = self._listing_folder(campaign_id, product_key) / "images"
        return self._publish_draft(campaign_id, product_id, listing, images_dir)

    def _publish_draft(
        self, campaign_id: int, product_id: str | None, listing: dict[str, Any],
        images_dir: Path | None = None,
    ) -> dict[str, Any]:
        from onassis.failure_help import explain
        from onassis.listing_validation import operator_summary, validate_listing

        # Pre-publish validation + safe sanitisation — never call Etsy with a
        # listing we already know it will reject (Sprint 41.2).
        vr = validate_listing(
            listing, min_description=int(self.listing_cfg.get("min_description_chars", 20)))
        if not vr["ok"]:
            reason = operator_summary(vr)
            pub = {"platform": PLATFORM, "product_id": product_id,
                   "campaign_id": campaign_id, "listing_id": None, "mode": DRAFT,
                   "status": "failed", "attempts": 0, "failure_reason": reason}
            pub["id"] = self.db.insert_publication(pub)
            log.warning("Pre-publish validation blocked campaign #%s: %s", campaign_id, reason)
            return {"status": "failed", "campaign_id": campaign_id, "reason": reason,
                    "issues": vr["issues"], "help": explain(reason, status="invalid"),
                    "publication": pub, "validation": True}
        listing = vr["listing"]                 # use the sanitised listing
        sanitised = [i for i in vr["issues"] if i["fixed"]]

        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                backend = self._draft_backend()
                result = backend.create_draft(listing)
                # Etsy Draft Validation (Sprint 40): only treat this as a draft
                # when Etsy actually returned a *valid* listing id. A missing id
                # used to be stringified to "None" and stored as a real draft.
                raw_id = (result or {}).get("listing_id")
                if not is_valid_listing_id(raw_id):
                    raise ValueError(
                        "Etsy did not return a valid listing id "
                        f"(got {raw_id!r}) — draft not confirmed.")
                listing_id = str(raw_id)
                uploads = self._upload_images(backend, listing_id, listing, images_dir)
                pub = {"platform": PLATFORM, "product_id": product_id,
                       "campaign_id": campaign_id, "listing_id": listing_id,
                       "mode": DRAFT, "status": DRAFT, "attempts": attempt,
                       "images_uploaded": uploads["uploaded"],
                       "images_failed": uploads["failed"]}
                pub["id"] = self.db.insert_publication(pub)
                log.info("Published campaign #%s as Etsy draft %s (%d image(s) attached)",
                         campaign_id, listing_id, uploads["uploaded"])
                return {"status": DRAFT, "campaign_id": campaign_id, "publication": pub,
                        "images": uploads, "sanitised": sanitised}
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
                "reason": last_error, "publication": pub,
                "help": explain(last_error)}

    # --- Launch policy (single approval → whole approved set) --------

    def launch(self, campaign_id: int, mode: str | None = None) -> dict[str, Any]:
        """Draft every approved product, reach Launch Ready, apply the policy.

        The pipeline always completes to **Launch Ready** (drafts created). The
        launch policy then decides the final step:
        * ``manual`` / ``scheduled`` — wait for one approval (:meth:`approve_launch`).
        * ``automatic`` — approve the launch immediately.
        """
        drafts = self.publish_products(campaign_id, mode=mode)
        if drafts.get("status") == "blocked":
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": drafts.get("reason"), "drafts": drafts}

        self.db.upsert_launch({"campaign_id": campaign_id, "status": "launch_ready",
                               "policy": self.launch_policy,
                               "products": drafts.get("published", 0)})
        if self.launch_policy == "automatic":
            approval = self.approve_launch(campaign_id, by="automatic")
            return {"status": "launched", "campaign_id": campaign_id,
                    "policy": self.launch_policy, "drafts": drafts, "launch": approval}
        return {"status": "launch_ready", "campaign_id": campaign_id,
                "policy": self.launch_policy, "drafts": drafts,
                "launch": self.db.get_launch(campaign_id)}

    def approve_launch(self, campaign_id: int, by: str = "owner") -> dict[str, Any]:
        """Approve a design's launch in **one action** — master design + all its
        CEO-approved product drafts become ready for publication together. When
        auto go-live is enabled, the approved drafts are also **activated LIVE**
        on Etsy (subject to the margin guard)."""
        from datetime import datetime, timezone

        launch = self.db.get_launch(campaign_id)
        if launch is None:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "Nothing to approve — the design is not Launch Ready."}
        products = [s for s in self.db.list_product_scores(campaign_id) if s.get("launched")]
        self.db.upsert_launch({
            **launch, "status": "launched",
            "approved_at": datetime.now(timezone.utc).isoformat(), "approved_by": by,
        })
        result = {"status": "launched", "campaign_id": campaign_id, "approved_by": by,
                  "products": [s["product_key"] for s in products]}
        if self.auto_go_live:
            result["go_live"] = self.go_live(campaign_id, products)
        log.info("Launch approved for campaign #%s by %s — %d product(s); live=%s.",
                 campaign_id, by, len(products),
                 (result.get("go_live") or {}).get("live", 0))
        return result

    def go_live(self, campaign_id: int,
                products: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Activate approved product drafts as **LIVE** Etsy listings.

        Each product must (1) already have a draft with a listing id, and (2)
        clear the go-live **margin guard** — never take a product live if its net
        margin after real fees is below ``min_go_live_margin``. A loss-making SKU
        is skipped, not listed. Idempotent: a product already live is left alone.
        """
        if LIVE not in self.enabled_modes:
            return {"status": "disabled", "live": 0,
                    "reason": "Live publishing is not enabled."}
        if products is None:
            products = [s for s in self.db.list_product_scores(campaign_id)
                        if s.get("launched")]
        backend = None
        live, results = 0, []
        for s in products:
            product_id = f"{campaign_id}-{s['product_key']}"
            pub = self.db.get_active_publication(campaign_id, PLATFORM, product_id=product_id)
            outcome = {"product_key": s["product_key"]}
            if not pub or not pub.get("listing_id"):
                outcome.update(status="skipped", reason="no draft listing to activate")
            elif pub.get("status") == "live":
                outcome.update(status="already_live", listing_id=pub["listing_id"])
            else:
                margin = self.fee_model.net_margin(
                    s.get("retail_price", 0), s.get("production_cost", 0))
                if margin < self.min_go_live_margin:
                    outcome.update(status="held", reason=(
                        f"net margin {margin:.0%} below the {self.min_go_live_margin:.0%} "
                        f"go-live floor — not listed at a loss"), margin=margin)
                else:
                    try:
                        backend = backend or self._draft_backend()
                        backend.publish_listing(pub["listing_id"])
                        self.db.set_publication_status(pub["id"], "live")
                        live += 1
                        outcome.update(status="live", listing_id=pub["listing_id"],
                                       margin=margin)
                        log.info("LIVE: campaign #%s %s -> Etsy listing %s (margin %.0f%%)",
                                 campaign_id, s["product_key"], pub["listing_id"],
                                 margin * 100)
                    except Exception as exc:  # activation failed — keep the draft
                        outcome.update(status="failed", reason=str(exc),
                                       listing_id=pub["listing_id"])
                        log.warning("Go-live failed for %s: %s", s["product_key"], exc)
            results.append(outcome)
        return {"status": "ok", "campaign_id": campaign_id, "live": live,
                "results": results}

    def launch_status(self, campaign_id: int) -> dict[str, Any]:
        return self.db.get_launch(campaign_id) or {"campaign_id": campaign_id,
                                                   "status": "none"}

    def pending_launches(self) -> list[dict[str, Any]]:
        """Designs awaiting a launch approval (Launch Ready, not yet launched)."""
        return self.db.list_launches(status="launch_ready")

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

    def _listing_folder(self, campaign_id: int, product_key: str | None = None) -> Path:
        base = Path(self.listing_cfg.get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        folder = base / str(campaign_id)
        if product_key:  # the approved product's own package sub-folder
            folder = folder / product_key
        return folder

    def _load_listing(
        self, campaign_id: int, product_key: str | None = None
    ) -> dict[str, Any] | None:
        path = self._listing_folder(campaign_id, product_key) / "listing.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _upload_images(
        self, backend: Any, listing_id: str, listing: dict[str, Any],
        images_dir: Path | None,
    ) -> dict[str, Any]:
        """Upload every generated gallery image to the Etsy draft, in order.

        Best-effort per image: a single image failure is logged and counted but
        never aborts the publish (the draft already exists). A backend without
        image support (e.g. a dry-run stub) is skipped cleanly.
        """
        images = listing.get("images") or []
        if images_dir is None or not hasattr(backend, "upload_listing_image"):
            return {"uploaded": 0, "failed": 0, "skipped": len(images)}
        uploaded, failed = 0, 0
        for img in images:
            path = images_dir / img["filename"]
            if not path.exists():
                failed += 1
                continue
            try:
                backend.upload_listing_image(
                    listing_id, str(path), rank=img.get("order", 1),
                    alt_text=img.get("alt_text"))
                uploaded += 1
            except Exception as exc:  # keep the draft; record the miss
                failed += 1
                log.warning("Image upload failed for listing %s (%s): %s",
                            listing_id, img["filename"], exc)
        return {"uploaded": uploaded, "failed": failed, "skipped": 0}
