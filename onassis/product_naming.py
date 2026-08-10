"""Storefront product naming — a single source of truth.

A product carries a DESIGN (its campaign) and a TYPE (from the stable
``product_key``). The catch: the design engine names campaigns after physical
product *concepts* — "Persiana Sun-Stripe Cushion", "Tavola Lunga Linen Tea
Towel" — but ONASSIS prints them on mugs / totes / posters / apparel. So the
full campaign name would mislabel a tote as a "Cushion".

We therefore take only the **collection** — the leading brand/theme words of the
campaign name ("Persiana Sun-Stripe", "Tavola Lunga", "Salt & Olive") — and
append the **actual** product type: ``'{collection} — {type}'``. A tote is
'Persiana Sun-Stripe — Tote Bag', the mug 'Persiana Sun-Stripe — Ceramic Mug':
correct type, distinct listing, never a conflicting concept.

Used at BOTH ends so they can't drift: listing creation (Expansion Engine) and
the rename tool (Content Engine). Deterministic and idempotent.
"""

from __future__ import annotations

_TYPE_LABELS = {
    "ceramic_mug": "Ceramic Mug", "premium_poster": "Premium Poster",
    "tote_bag": "Tote Bag", "premium_tshirt": "Premium T-Shirt",
    "heavyweight_hoodie": "Heavyweight Hoodie", "sweatshirt": "Sweatshirt",
}

# Small connector words that shouldn't be the last word of a collection.
_CONNECTORS = {"&", "and", "of", "the", "de", "di", "la", "le"}


def type_label(product_key: str | None) -> str:
    """The product's TYPE label from its stable ``product_key``."""
    pk = str(product_key or "").lower()
    return _TYPE_LABELS.get(pk, pk.replace("_", " ").title())


def collection(design: str | None) -> str:
    """The leading brand/theme of a campaign name — the part worth keeping — so a
    product-concept campaign ('Persiana Sun-Stripe Cushion') yields the collection
    ('Persiana Sun-Stripe'), never the concept ('Cushion'). Heuristic: the first
    two words, extended across connectors like '&' ('Salt & Olive')."""
    words = (design or "").strip().split()
    if not words:
        return ""
    n = min(2, len(words))
    # Extend while we'd otherwise end on a connector (or the next word is one).
    while n < len(words) and (words[n - 1].lower() in _CONNECTORS
                              or words[n].lower() in _CONNECTORS):
        n += 1
    return " ".join(words[:n])


def display_name(design: str | None, *, product_key: str | None = None,
                 type_name: str | None = None) -> str:
    """The storefront name ``'{collection} — {type}'`` — correct product type,
    distinct per design. Idempotent: re-deriving yields the same name."""
    tn = (type_name if type_name is not None else type_label(product_key)).strip()
    col = collection(design)
    if not col:
        return tn or "Product"
    if not tn:
        return col
    # If a previously-composed name is fed back in, don't double the type.
    if col.lower().endswith(tn.lower()):
        return col
    return f"{col} — {tn}"
