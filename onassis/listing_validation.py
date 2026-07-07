"""Etsy pre-publish validation + safe sanitisation (Sprint 41.2, Obj 4/5).

Before ONASSIS ever calls the Etsy API it validates a listing locally, fixes the
issues it safely can, and — if a genuine problem remains — returns a clear,
human-readable operator message *instead of* a raw HTTP 400. This turns "HTTP 400
too_many_invalid_characters" into "Title contains multiple '&' characters — retry
after correction", and quietly repairs the common cases (e.g. ``Coffee & Tea &
Home`` → ``Coffee & Tea and Home``).

Pure and deterministic — no I/O — so it is trivially testable and reused by the
publisher and the Operations Centre.
"""

from __future__ import annotations

import re
from typing import Any

# Etsy limits.
TITLE_MAX = 140
TAG_MAX = 13
TAG_LEN_MAX = 20
DESC_MIN = 20            # a listing this short is almost certainly broken

_MULTISPACE = re.compile(r"\s{2,}")
_REPEAT_PUNCT = re.compile(r"([!?.,])\1{1,}")


def _issue(field: str, code: str, message: str, *, cause: str, suggestion: str,
           fixed: bool = False, blocking: bool = False) -> dict[str, Any]:
    return {"field": field, "code": code, "message": message, "cause": cause,
            "suggestion": suggestion, "fixed": fixed, "blocking": blocking}


def _sanitise_title(title: str, issues: list) -> str:
    original = title
    title = title.strip()
    # Collapse runs of whitespace and repeated punctuation.
    title = _MULTISPACE.sub(" ", title)
    title = _REPEAT_PUNCT.sub(r"\1", title)
    # Etsy dislikes multiple "&" — keep the first, turn the rest into "and".
    if title.count("&") > 1:
        parts = [p.strip() for p in title.split("&")]
        title = parts[0] + " & " + " and ".join(parts[1:])
        title = _MULTISPACE.sub(" ", title).strip()
        issues.append(_issue(
            "title", "multiple_ampersands",
            "Title contained multiple '&' characters.",
            cause="Etsy rejects titles with repeated '&'.",
            suggestion="Replaced repeated '&' with 'and'.", fixed=True))
    if title != original and not issues:
        issues.append(_issue("title", "normalised", "Title whitespace/punctuation tidied.",
                             cause="Cosmetic formatting.", suggestion="Auto-tidied.",
                             fixed=True))
    return title


def validate_listing(listing: dict[str, Any], *, min_description: int = DESC_MIN,
                     ) -> dict[str, Any]:
    """Validate + safely sanitise a listing before publishing.

    Returns ``{ok, listing (sanitised copy), issues, blocking}``. ``ok`` is False
    only when a **blocking** issue remains that ONASSIS cannot safely auto-fix.
    """
    out = dict(listing)
    issues: list[dict[str, Any]] = []

    # --- Required fields --------------------------------------------
    title = str(out.get("title") or "").strip()
    if not title:
        issues.append(_issue("title", "missing_title", "The listing has no title.",
                             cause="Title is required by Etsy.",
                             suggestion="Regenerate the listing package.", blocking=True))
    if not str(out.get("description") or "").strip():
        issues.append(_issue("description", "missing_description",
                             "The listing has no description.",
                             cause="Description is required by Etsy.",
                             suggestion="Regenerate the listing package.", blocking=True))
    price = out.get("price") or out.get("retail_price") or 0
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        issues.append(_issue("price", "invalid_price", "The listing has no valid price.",
                             cause="A positive price is required.",
                             suggestion="Set the product's retail price.", blocking=True))
    tags = [str(t).strip() for t in (out.get("tags") or []) if str(t).strip()]
    if not tags:
        issues.append(_issue("tags", "missing_tags", "The listing has no tags.",
                             cause="At least one tag is required for discoverability.",
                             suggestion="Regenerate SEO tags.", blocking=True))

    # --- Sanitise the title -----------------------------------------
    if title:
        title = _sanitise_title(title, issues)
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX].rsplit(" ", 1)[0]
            issues.append(_issue(
                "title", "title_too_long",
                f"Title exceeded {TITLE_MAX} characters.",
                cause="Etsy titles are capped at 140 characters.",
                suggestion=f"Trimmed to {len(title)} characters.", fixed=True))
        out["title"] = title

    # --- Sanitise tags (dedupe, cap at 13, drop over-long) ----------
    if tags:
        seen, clean = set(), []
        dropped_long = 0
        for t in tags:
            if len(t) > TAG_LEN_MAX:
                dropped_long += 1
                continue
            k = t.lower()
            if k not in seen:
                seen.add(k)
                clean.append(t)
        if dropped_long:
            issues.append(_issue("tags", "tag_too_long",
                                 f"{dropped_long} tag(s) exceeded {TAG_LEN_MAX} characters.",
                                 cause="Etsy tags are capped at 20 characters.",
                                 suggestion="Dropped the over-long tags.", fixed=True))
        if len(clean) > TAG_MAX:
            clean = clean[:TAG_MAX]
            issues.append(_issue("tags", "too_many_tags",
                                 f"More than {TAG_MAX} tags supplied.",
                                 cause="Etsy allows at most 13 tags.",
                                 suggestion="Kept the first 13.", fixed=True))
        if not clean:  # sanitising removed them all → now blocking
            issues.append(_issue("tags", "no_valid_tags", "No valid tags remain.",
                                 cause="All tags were empty or over-long.",
                                 suggestion="Regenerate SEO tags.", blocking=True))
        out["tags"] = clean

    # --- Non-blocking warnings --------------------------------------
    desc = str(out.get("description") or "")
    if desc and len(desc) < min_description:
        issues.append(_issue("description", "description_short",
                             f"Description is only {len(desc)} characters.",
                             cause="Very short descriptions convert poorly.",
                             suggestion="Consider a richer description.", blocking=False))

    blocking = any(i["blocking"] for i in issues)
    return {"ok": not blocking, "listing": out, "issues": issues, "blocking": blocking}


def operator_summary(result: dict[str, Any]) -> str:
    """A one-line human summary of a validation result (for the operator)."""
    blocking = [i for i in result["issues"] if i["blocking"]]
    if blocking:
        first = blocking[0]
        return f"{first['message']} {first['suggestion']}"
    fixed = [i for i in result["issues"] if i["fixed"]]
    if fixed:
        return f"Auto-corrected {len(fixed)} issue(s) before publishing."
    return "Listing passed validation."
