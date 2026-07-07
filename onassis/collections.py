"""Product categorisation, category-balanced selection, and collections.

Sprint 42 Phase 4. Investigation (Obj 10): apparel *is* in the catalogue and CEO-
approved, but as the Learning Engine raises the score of what already sells
(mugs, posters, totes), apparel slips out of the top-N and stops being launched.

The fix is a **category-diversity guard** (Obj 8): when the naturally top-scoring
set omits a category we want represented (apparel), swap the lowest-scoring,
over-represented pick for the best available apparel candidate. It is a no-op
when the set is already diverse, so it never disturbs a healthy selection — it
only rescues under-represented categories.

Plus lightweight **collection** naming/description (Obj 9): the launched set for a
concept is a branded collection spanning categories, not isolated products.
"""

from __future__ import annotations

from typing import Any

# Substring → category. Order matters (first match wins).
_CATEGORY_RULES = [
    (("tshirt", "t-shirt", "tee", "hoodie", "sweat", "shirt", "apparel", "clothing"), "apparel"),
    (("poster", "print", "canvas", "framed", "art"), "print"),
    (("mug", "cup", "tumbler", "drink"), "drinkware"),
    (("tote", "bag", "pouch", "cushion", "towel", "apron", "board"), "homeware"),
    (("notebook", "card", "journal", "stationery", "sticker"), "stationery"),
]
# Categories the selection tries to keep represented, most important first.
ENSURE_CATEGORIES = ("apparel",)
MIN_CATEGORIES = 3


def categorise(product_key: str | None) -> str:
    key = (product_key or "").lower()
    for needles, category in _CATEGORY_RULES:
        if any(n in key for n in needles):
            return category
    return "other"


def _cat(score: dict[str, Any]) -> str:
    return categorise(score.get("product_key"))


def balance_selection(chosen: list[dict[str, Any]], pool: list[dict[str, Any]],
                      *, min_categories: int = MIN_CATEGORIES,
                      ensure: tuple[str, ...] = ENSURE_CATEGORIES) -> list[dict[str, Any]]:
    """Rebalance ``chosen`` (top-N, score-desc) using the wider ``pool`` (all
    CEO-approved, score-desc) so under-represented categories get a slot.

    * The single highest-scoring pick is always kept.
    * A required category missing from ``chosen`` is added by swapping the
      lowest-scoring pick from the most over-represented category.
    * No-op when ``chosen`` already has ``min_categories`` and every ``ensure``
      category present.
    """
    if not chosen:
        return chosen
    result = list(chosen)
    chosen_keys = {s["product_key"] for s in result}

    def categories() -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in result:
            counts[_cat(s)] = counts.get(_cat(s), 0) + 1
        return counts

    def swap_in(candidate: dict[str, Any]) -> bool:
        counts = categories()
        # Never drop the top-scoring pick (index 0). Choose the lowest-scoring
        # victim from the most over-represented category.
        victim_idx = None
        for i in range(len(result) - 1, 0, -1):
            cat = _cat(result[i])
            if counts.get(cat, 0) > 1:      # over-represented → safe to drop one
                victim_idx = i
                break
        if victim_idx is None:
            return False
        result[victim_idx] = candidate
        chosen_keys.discard(result[victim_idx]["product_key"])
        chosen_keys.add(candidate["product_key"])
        return True

    for cat in ensure:
        if any(_cat(s) == cat for s in result):
            continue
        candidate = next((p for p in pool if _cat(p) == cat
                          and p["product_key"] not in chosen_keys), None)
        if candidate is not None:
            swap_in(candidate)

    # Top-up diversity if still below the floor.
    while len({_cat(s) for s in result}) < min_categories:
        present = {_cat(s) for s in result}
        candidate = next((p for p in pool if _cat(p) not in present
                          and p["product_key"] not in chosen_keys), None)
        if candidate is None or not swap_in(candidate):
            break
    return result


def collection_name(opportunity: dict[str, Any] | None, campaign: dict[str, Any] | None = None) -> str:
    theme = ((opportunity or {}).get("theme") or (opportunity or {}).get("product_name")
             or (campaign or {}).get("name") or "Signature")
    theme = str(theme).strip().title()
    return theme if theme.lower().endswith("collection") else f"{theme} Collection"


def describe_collection(launched: list[dict[str, Any]],
                        opportunity: dict[str, Any] | None = None,
                        campaign: dict[str, Any] | None = None) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for s in launched:
        c = _cat(s)
        counts[c] = counts.get(c, 0) + 1
    return {
        "name": collection_name(opportunity, campaign),
        "product_keys": [s["product_key"] for s in launched],
        "categories": counts,
        "category_count": len(counts),
        "has_apparel": "apparel" in counts,
        "size": len(launched),
    }
