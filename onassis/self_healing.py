"""Workflow self-healing / reconciliation (Sprint 41.2, Obj 1 & 2).

On every startup ONASSIS scans its own state and repairs the safe, deterministic
inconsistencies that otherwise force manual database editing — so the operator
can leave it running unattended and return to a fully reconciled system:

* Orphaned approval records (product deleted) are removed.
* Impossible confidence scores are clamped into [0, 100].
* Broken publications — status ``draft``/``live`` but *no valid listing id* — are
  corrected to ``failed`` so they surface as Failed/Retry instead of pretending
  to be live.
* Duplicate active publications for the same product are de-duplicated (newest
  kept; the rest marked ``superseded``).

It reports (without regenerating) products missing artwork/thumbnails so the UI
can show a placeholder rather than a broken image. Every action is logged and the
summary is returned for the Operations Centre.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.product_status import is_valid_listing_id

log = get_logger(__name__)

_ACTIVE = {"draft", "published", "live"}


def reconcile(config: Config, db: Database) -> dict[str, Any]:
    """Run the full repair pass. Idempotent and safe to run at every startup."""
    summary: dict[str, Any] = {
        "orphan_approvals_removed": 0,
        "confidence_clamped": 0,
        "broken_publications_fixed": 0,
        "duplicate_publications_superseded": 0,
        "missing_thumbnails": 0,
    }

    # 1. Orphaned approvals + impossible confidence — one query each.
    try:
        summary["orphan_approvals_removed"] = db.delete_orphan_product_approvals()
        summary["confidence_clamped"] = db.clamp_product_score_confidence()
    except Exception as exc:  # noqa: BLE001
        log.warning("reconcile: approval/score repair failed: %s", exc)

    # 2. Publications: fix broken ones + de-duplicate actives.
    try:
        pubs = db.list_publications()
        seen_active: dict[tuple, int] = {}       # (campaign, platform, product) -> pub id kept
        for p in pubs:                            # newest first (id DESC)
            status = (p.get("status") or "").lower()
            if status not in _ACTIVE:
                continue
            if not is_valid_listing_id(p.get("listing_id")):
                db.set_publication_status(p["id"], "failed")
                summary["broken_publications_fixed"] += 1
                continue
            key = (p.get("campaign_id"), p.get("platform"), p.get("product_id"))
            if key in seen_active:
                db.set_publication_status(p["id"], "superseded")
                summary["duplicate_publications_superseded"] += 1
            else:
                seen_active[key] = p["id"]
    except Exception as exc:  # noqa: BLE001
        log.warning("reconcile: publication repair failed: %s", exc)

    # 3. Report products missing a chosen thumbnail (UI shows a placeholder;
    #    regeneration is deferred to the next cycle, not forced at boot).
    try:
        chosen = {t.get("product_key") for t in db.list_thumbnails()
                  if t.get("chosen")} if hasattr(db, "list_thumbnails") else set()
        for prod in db.list_products():
            if prod.get("active", 1) and prod.get("product_key") not in chosen:
                summary["missing_thumbnails"] += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("reconcile: thumbnail scan skipped: %s", exc)

    total = (summary["orphan_approvals_removed"] + summary["confidence_clamped"]
             + summary["broken_publications_fixed"]
             + summary["duplicate_publications_superseded"])
    if total:
        log.info("Self-healing reconciled %d issue(s): %s", total, summary)
    summary["repaired"] = total
    return summary
