"""Storefront product naming — a single source of truth.

Products carry a design (the campaign name, e.g. 'Salt & Olive Bathing Bar') and
a type (from the stable ``product_key``, e.g. Ceramic Mug). The storefront name
is ``'{design} — {type}'`` so 28 mugs of different designs are 28 distinct
listings — never 28 identical 'Ceramic Mug's.

Used at BOTH ends so they can't drift: listing creation (Expansion Engine) names
products correctly from the start, and the rename tool (Content Engine) repairs
older ones the same way. The rule is idempotent — re-deriving a name yields the
same name, never dropping or duplicating the type.
"""

from __future__ import annotations

_TYPE_LABELS = {
    "ceramic_mug": "Ceramic Mug", "premium_poster": "Premium Poster",
    "tote_bag": "Tote Bag", "premium_tshirt": "Premium T-Shirt",
    "heavyweight_hoodie": "Heavyweight Hoodie", "sweatshirt": "Sweatshirt",
}


def type_label(product_key: str | None) -> str:
    """The product's TYPE label from its stable ``product_key`` (never the mutable
    name — that fed back on re-runs and dropped the type)."""
    pk = str(product_key or "").lower()
    return _TYPE_LABELS.get(pk, pk.replace("_", " ").title())


def display_name(design: str | None, *, product_key: str | None = None,
                 type_name: str | None = None) -> str:
    """The storefront name ``'{design} — {type}'``. Only drops the type when the
    design ALREADY names this exact type (e.g. a Heavyweight Hoodie whose design is
    'Riviera Sunset Heavyweight Hoodie'); a Sweatshirt with that design still gets
    '— Sweatshirt', so the type is never mislabelled. Idempotent."""
    tn = (type_name if type_name is not None else type_label(product_key)).strip()
    cn = (design or "").strip()
    if not cn:
        return tn or "Product"
    if not tn:
        return cn
    composed = f"{cn} — {tn}"
    if cn.endswith(f"— {tn}") or cn == composed:   # already final (re-run)
        return cn
    if tn.lower() in cn.lower():                    # design already names THIS type
        return cn
    return composed
