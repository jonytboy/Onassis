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

import re
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


# Garment types are DISTINCT products on the store (a hoodie is not a
# sweatshirt is not a tee). The LLM occasionally relabels one as another, which
# mislabels the listing (buyer searches "crewneck", lands on a hoodie) — the
# rename-disaster failure mode. `type_conflict` enforces the true type from the
# stable product_key: the right garment word must appear, a wrong one must not.
_APPAREL_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "heavyweight_hoodie": {
        "need": ("hoodie", "hooded"),
        "avoid": ("crewneck", "crew neck", "t-shirt", "t shirt", "tshirt", "tee")},
    "sweatshirt": {
        "need": ("sweatshirt", "crewneck", "crew neck", "jumper"),
        "avoid": ("hoodie", "hooded", "t-shirt", "t shirt", "tshirt", "tee")},
    "premium_tshirt": {
        "need": ("t-shirt", "t shirt", "tshirt", "tee"),
        "avoid": ("hoodie", "hooded", "sweatshirt", "crewneck", "crew neck")},
}


def _has_word(term: str, text: str) -> bool:
    """Whole-word (case-insensitive) containment, so 'tee' does not match
    'canteen' and 't-shirt' is found next to punctuation."""
    return re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])",
                     text, re.IGNORECASE) is not None


def type_conflict(title: str, product_key: str | None) -> str:
    """Reason string if ``title`` mislabels the garment type for this product,
    else "". Only apparel (where the model actually confuses types) is guarded;
    mugs/posters/totes return "". A row with a conflict must NOT be applied."""
    spec = _APPAREL_TERMS.get(str(product_key or "").lower())
    if not spec:
        return ""
    title = title or ""
    if not any(_has_word(w, title) for w in spec["need"]):
        return f"title never says '{spec['need'][0]}' (wrong or missing garment type)"
    bad = [w for w in spec["avoid"] if _has_word(w, title)]
    if bad:
        return f"title calls it a {'/'.join(bad)} — not a {spec['need'][0]}"
    return ""


def _prompt(context: dict[str, Any]) -> str:
    ptype = context.get("product_type") or "product"
    subject = context.get("subject") or context.get("theme") or ""
    current = context.get("current_title") or ""
    lines = [
        f"Product type: {ptype}",
        f"Design subject / style: {subject}" if subject else "",
        f"Current (weak) title: {current}" if current else "",
        "",
        f"This item is a {ptype}. The title and tags MUST describe it as a "
        f"{ptype} and MUST NOT call it any other product (never say 'hoodie' for "
        "a sweatshirt, 'sweatshirt' or 'crewneck' for a hoodie, 'tee'/'t-shirt' "
        "for either, etc.). Keep the exact garment/product type.",
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
