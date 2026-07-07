"""Human-friendly failure recovery (Sprint 41.2, Obj 5).

Turns a raw publish error — ``HTTP 400 too_many_invalid_characters`` — into an
operator-readable explanation with a cause, a suggested fix and whether a retry
is worthwhile. Used to wrap every publish/Shopify failure so the Operations
Centre never shows a bare API error.
"""

from __future__ import annotations

import re
from typing import Any

# (regex over the raw error, cause, suggestion, retryable)
_PATTERNS: list[tuple[str, str, str, bool]] = [
    (r"too_many_invalid_characters|multiple.*&|invalid_char",
     "The title contains characters Etsy rejects (often repeated '&').",
     "Replace repeated '&' with 'and', then retry.", True),
    (r"title.*(long|length|140)|too_long",
     "The title is longer than Etsy allows (140 characters).",
     "Shorten the title and retry.", True),
    (r"tag.*(invalid|long|limit|13|20)",
     "One or more tags are invalid (over 20 chars, or more than 13 tags).",
     "Trim the tags and retry.", True),
    (r"description.*(required|missing|short)",
     "The description is missing or too short.",
     "Add a fuller description and retry.", True),
    (r"shipping.*(profile|required)|readiness",
     "Etsy needs a shipping profile before a listing can be created.",
     "Set a shipping profile on the shop, then retry.", True),
    (r"401|unauthori|invalid_token|token.*(expired|invalid)",
     "Authentication with the platform failed (token missing or expired).",
     "Reconnect the integration (Integrations → Test Connection), then retry.", True),
    (r"403|forbidden|scope|permission",
     "The connected app lacks permission for this action.",
     "Grant the required scopes to the app and reconnect.", False),
    (r"429|rate.?limit|too_many_requests",
     "The platform is rate-limiting requests.",
     "Wait a few minutes, then retry.", True),
    (r"credentials are not set|not_configured|not configured",
     "The channel is not connected.",
     "Add credentials on the Integrations page and Test Connection.", False),
    (r"no valid listing id|no.*product id|did not.*confirm",
     "The platform did not confirm the listing/product was created.",
     "Retry; if it persists, check the platform status.", True),
    (r"5\d\d|server error|timeout|timed out|connection",
     "The platform had a temporary server/network problem.",
     "Retry shortly — this is usually transient.", True),
]


def explain(error: str | None, *, status: str | None = None) -> dict[str, Any]:
    """Map a raw error (and/or a status like 'not_configured') to guidance."""
    raw = (error or status or "").strip()
    low = raw.lower()
    for pattern, cause, suggestion, retryable in _PATTERNS:
        if re.search(pattern, low):
            return {"cause": cause, "suggestion": suggestion, "retryable": retryable,
                    "raw": raw}
    if not raw:
        return {"cause": "Unknown issue.", "suggestion": "Retry, or check the logs.",
                "retryable": True, "raw": ""}
    return {"cause": raw, "suggestion": "Review the detail and retry.",
            "retryable": True, "raw": raw}


def annotate(result: dict[str, Any]) -> dict[str, Any]:
    """Attach a ``help`` block to a failed publish result (in place)."""
    if result.get("status") in ("failed", "not_configured", "invalid", "blocked"):
        result["help"] = explain(result.get("reason"), status=result.get("status"))
    return result
