"""The Product Status Engine — a single source of truth for a product's real
commercial lifecycle.

The Operations Centre used to derive a product's status from the ``active``
flag (``active`` -> "published", ``not active`` -> "archived"). That was
**misleading**: a product row is created the moment the Revenue Expansion
Engine launches a score — long before any Etsy draft exists — so every launched
product showed as "published" even though only one had a real Etsy listing.

This module replaces that with a **true, deterministic lifecycle** derived from
real state: the operator's approval decision, the recorded Etsy publication, and
the product's marketing/sales history. The cardinal rule of Sprint 40:

    A product only reaches **Draft Created** once Etsy has returned a *valid*
    draft/listing id — never before, and never on a failed publish.

The engine is pure (no I/O): callers pass the already-loaded records and it
returns a status, so it is trivially testable and reused by the API and the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- Canonical lifecycle statuses (id -> operator-facing label) --------
# Ordered from earliest to latest so a status can be ranked/sorted.
LIFECYCLE: list[tuple[str, str]] = [
    ("opportunity", "Opportunity"),
    ("researching", "Research"),
    ("ceo_approved", "CEO Approved"),
    ("compliance_passed", "Compliance Passed"),
    ("artwork_generated", "Artwork Generated"),
    ("listing_generated", "Listing Generated"),
    ("awaiting_approval", "Awaiting Approval"),
    ("approved", "Approved"),
    ("publishing", "Publishing"),
    ("draft_created", "Draft Created"),
    ("live", "Live"),
    ("marketing", "Marketing Started"),
    ("tracking", "Tracking"),
    ("rejected", "Rejected"),
    ("failed", "Failed"),
    ("archived", "Archived"),
]
LABELS: dict[str, str] = dict(LIFECYCLE)
ORDER: dict[str, int] = {sid: i for i, (sid, _) in enumerate(LIFECYCLE)}

# The dashboard filter buckets the spec asks for.
FILTERS: dict[str, tuple[str, ...]] = {
    "awaiting_approval": ("awaiting_approval",),
    "ready_to_publish": ("approved",),
    "draft_created": ("draft_created", "publishing"),
    "live": ("live", "marketing", "tracking"),
    "failed": ("failed", "rejected"),
    "archived": ("archived",),
}


def is_valid_listing_id(value: Any) -> bool:
    """True only for a genuine Etsy listing/draft id.

    ``str(result.get("listing_id"))`` used to record ``"None"`` as a draft id
    when Etsy returned nothing. A valid id is a non-empty token that is not one
    of the sentinel strings a missing value stringifies to.
    """
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in {"none", "null", "0", "false"}


@dataclass(frozen=True)
class ProductStatus:
    status: str
    label: str
    reason: str = ""          # populated for the ``failed`` status
    listing_id: str = ""      # the real Etsy id, when Draft Created / Live
    retryable: bool = False   # a failed publish can be retried

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "label": self.label, "reason": self.reason,
                "listing_id": self.listing_id, "retryable": self.retryable}


def derive_product_status(
    *,
    product: dict[str, Any],
    publication: dict[str, Any] | None = None,
    approval: dict[str, Any] | None = None,
    marketing_count: int = 0,
    units_sold: int = 0,
) -> ProductStatus:
    """Derive the real lifecycle status for one product.

    Highest-truth signal wins, in this order:

    1. A live Etsy listing  -> Live / Marketing / Tracking (by real activity).
    2. A recorded draft with a **valid** listing id -> Draft Created.
    3. The latest publish attempt failed -> Failed (with reason + Retry).
    4. The operator's explicit decision -> Approved / Rejected.
    5. Archived product row -> Archived.
    6. Otherwise the launched product is Awaiting Approval.
    """
    pub = publication or {}
    pub_status = (pub.get("status") or "").lower()
    listing_id = pub.get("listing_id")
    has_real_listing = is_valid_listing_id(listing_id)
    lid = str(listing_id) if has_real_listing else ""
    decision = (approval or {}).get("decision", "").lower()

    # 1. Live on Etsy — the strongest possible signal of real state.
    if pub_status == "live" and has_real_listing:
        if units_sold > 0:
            return ProductStatus("tracking", LABELS["tracking"], listing_id=lid)
        if marketing_count > 0:
            return ProductStatus("marketing", LABELS["marketing"], listing_id=lid)
        return ProductStatus("live", LABELS["live"], listing_id=lid)

    # 2. Draft created — ONLY when Etsy actually returned a valid id.
    if pub_status in {"draft", "published"} and has_real_listing:
        if marketing_count > 0:
            return ProductStatus("marketing", LABELS["marketing"], listing_id=lid)
        return ProductStatus("draft_created", LABELS["draft_created"], listing_id=lid)

    # 3. Failed publish — surface the reason and offer a retry.
    if pub_status == "failed":
        reason = pub.get("failure_reason") or "Etsy did not confirm the draft."
        return ProductStatus("failed", LABELS["failed"], reason=reason, retryable=True)

    # A draft/published record without a valid id is a broken publish, not a draft.
    if pub_status in {"draft", "published"} and not has_real_listing:
        return ProductStatus("failed", LABELS["failed"],
                             reason="Etsy returned no valid listing id.", retryable=True)

    # 4. Operator decision (no publish attempt yet).
    if decision == "rejected":
        return ProductStatus("rejected", LABELS["rejected"])
    if decision == "approved":
        return ProductStatus("approved", LABELS["approved"])

    # 5. Archived (retired product with no live listing).
    if not product.get("active", 1):
        return ProductStatus("archived", LABELS["archived"])

    # 6. Default: a launched product waiting for the operator.
    return ProductStatus("awaiting_approval", LABELS["awaiting_approval"])


def matches_filter(status: str, bucket: str | None) -> bool:
    """Does a status fall into a dashboard filter bucket? Unknown/empty bucket
    matches everything."""
    if not bucket or bucket == "all":
        return True
    return status in FILTERS.get(bucket, ())
