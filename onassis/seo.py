"""Etsy-first SEO for listings — buyer-searched titles and long-tail tags.

The shop's problem isn't conversion, it's discoverability: listings lead with
invented brand words ("Meridiano", "Cyclades") that nobody searches, so Etsy
search never shows them. This module rewrites a listing's **title** and **tags**
around the phrases real buyers type, under Etsy's hard limits.

`build_seo` is pure and testable: it takes a product context + an LLM client and
returns a normalised ``{"title", "tags"}``. The orchestration (which products,
pushing to Etsy/Shopify, preview vs apply) lives in
:meth:`onassis.content_engine.ContentEngine.rewrite_seo`.
"""

from __future__ import annotations

from typing import Any

# Etsy's hard limits (2024): 140-char title, up to 13 tags of 20 chars each.
ETSY_TITLE_MAX = 140
ETSY_TAG_MAX = 20
ETSY_MAX_TAGS = 13

_SYSTEM = (
    "You are an expert Etsy SEO copywriter. You write listing titles and tags "
    "that match what real buyers TYPE INTO ETSY SEARCH, so Etsy ranks and shows "
    "the listing. You follow these rules without exception:\n"
    "- The title leads with the strongest buyer search phrase (product type + "
    "subject), because Etsy weights the first words most. NEVER lead with an "
    "invented brand or collection name (e.g. 'Meridiano', 'Cyclades') — buyers "
    "do not search those.\n"
    "- The title is human-readable, comma-separated phrases — NOT a random pile "
    "of keywords. Each phrase is something a shopper would actually search.\n"
    "- Cover distinct angles across the title and tags: product type, subject/"
    "style, room/use, recipient, and gift/occasion.\n"
    "- Tags are multi-word long-tail phrases (2-3 words), never single generic "
    "words, never duplicates of each other, and each at most 20 characters.\n"
    "- No ALL-CAPS, no emoji, no quotes."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string",
                  "description": "Etsy title, <=140 chars, buyer phrase first."},
        # NB: no maxItems here — Anthropic's structured-output schema rejects it
        # on arrays. The 13-tag cap is enforced in normalize_tags instead.
        "tags": {"type": "array",
                 "items": {"type": "string"},
                 "description": "Up to 13 long-tail tags, <=20 chars each."},
    },
    "required": ["title", "tags"],
}


def clip_title(title: str, limit: int = ETSY_TITLE_MAX) -> str:
    """Trim a title to Etsy's limit without cutting a word in half — prefer to
    end on the last complete comma-separated phrase, else the last whole word."""
    title = " ".join((title or "").split())          # collapse whitespace
    if len(title) <= limit:
        return title
    head = title[:limit]
    for sep in (",", " "):
        cut = head.rfind(sep)
        if cut >= limit * 0.6:                        # don't lose most of it
            return head[:cut].rstrip(" ,")
    return head.rstrip(" ,")


def normalize_tags(raw: Any, *, max_tags: int = ETSY_MAX_TAGS,
                   tag_len: int = ETSY_TAG_MAX) -> list[str]:
    """Clean model output into a valid Etsy tag set: trimmed, de-duplicated
    (case-insensitively), each within the length limit, capped at ``max_tags``.
    Tags longer than the limit are dropped rather than truncated mid-word (a
    chopped tag matches nothing)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in (raw or []):
        tag = " ".join(str(t).split()).strip().lower()
        if not tag or len(tag) > tag_len:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= max_tags:
            break
    return out


def _prompt(context: dict[str, Any]) -> str:
    ptype = context.get("product_type") or "product"
    subject = context.get("subject") or context.get("theme") or ""
    current = context.get("current_title") or ""
    lines = [
        f"Product type: {ptype}",
        f"Design subject / style: {subject}" if subject else "",
        f"Current (weak) title: {current}" if current else "",
        "",
        f"Write a new Etsy SEO title (<= {ETSY_TITLE_MAX} characters) and up to "
        f"{ETSY_MAX_TAGS} tags (each <= {ETSY_TAG_MAX} characters). Lead the "
        "title with the phrase a buyer would search for this item, then style, "
        "then a gift/occasion angle. Return JSON only.",
    ]
    return "\n".join(ln for ln in lines if ln != "")


def build_seo(context: dict[str, Any], llm: Any) -> dict[str, Any]:
    """Generate a normalised ``{"title", "tags"}`` for one product.

    ``context`` needs at least ``product_type``; ``subject``/``theme`` and
    ``current_title`` sharpen the result. ``llm`` is anything exposing
    ``generate_json(system=, prompt=, schema=)`` (the real
    :class:`onassis.llm.LLMClient`, or a stub in tests)."""
    out = llm.generate_json(system=_SYSTEM, prompt=_prompt(context), schema=_SCHEMA)
    title = clip_title(str(out.get("title") or ""))
    tags = normalize_tags(out.get("tags"))
    return {"title": title, "tags": tags}
