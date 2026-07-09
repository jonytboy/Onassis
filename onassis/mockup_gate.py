"""Mockup Quality Gate (P1).

A hard, reusable rule shared by the Etsy and Shopify publishers and the
Operations Centre: **a product may only be published if at least one of its
gallery images is a real, quality-passed product mockup** — never a
placeholder/fallback image, never the output of a failed generation.

This closes the production hole where an image-backend failure (missing key,
exhausted credit, rate limit, API error) was silently substituted with the
dependency-light *local* renderer, whose flat placeholder mockups then reached
Etsy. Those images are now tagged ``fallback_used`` at generation time; this
gate refuses to publish them.

Pure and deterministic — it only inspects the image records the Artwork Studio
already writes into ``listing.json`` (``provider``, ``fallback_used``,
``generation_ok``, ``quality_pass``/``quality_score``).
"""

from __future__ import annotations

from typing import Any

# The single operator-facing message when a product is blocked by this gate.
BLOCK_MESSAGE = ("Product blocked: mockup quality failed. "
                 "Regenerate mockups before publishing.")

# Below this QC score an image counts as failing when no explicit pass flag
# is present (older packages that predate quality_pass).
_MIN_SCORE = 60.0


def image_publishable(img: dict[str, Any]) -> bool:
    """True when one gallery image is a real, quality-passed product mockup.

    A fallback/placeholder or a failed generation is never publishable. When an
    explicit ``quality_pass`` verdict is present (every package the Artwork
    Studio builds now carries one) it is authoritative. Images with no quality
    metadata at all are legacy/foreign and get the benefit of the doubt — the
    gate never retroactively blocks data produced before this feature."""
    if img.get("fallback_used"):
        return False                         # placeholder substitute — never publish
    if img.get("generation_ok") is False:
        return False                         # upstream generation error
    if "quality_pass" in img:
        return bool(img["quality_pass"])     # explicit QC verdict (authoritative)
    return True                              # legacy image, no verdict — allow


def _has_quality_metadata(images: list[dict[str, Any]]) -> bool:
    keys = ("quality_pass", "fallback_used", "generation_ok")
    return any(any(k in i for k in keys) for i in images)


def evaluate_listing(listing: dict[str, Any]) -> dict[str, Any]:
    """Assess a listing's gallery. Returns
    ``{ok, passing, total, fallback, reason, message}``.

    A **modern** package (its images carry quality metadata) is publishable only
    if at least one image is a real, quality-passed mockup. A legacy listing with
    no image quality metadata is not retroactively blocked."""
    images = listing.get("images") or listing.get("mockup_manifest") or []
    passing = [i for i in images if image_publishable(i)]
    fallback = [i for i in images if i.get("fallback_used")]
    failed_gen = [i for i in images if i.get("generation_ok") is False]
    if not _has_quality_metadata(images):
        # Legacy / foreign listing — nothing to judge, don't block.
        return {"ok": True, "passing": len(passing), "total": len(images),
                "fallback": 0, "reason": "", "message": ""}
    ok = len(passing) >= 1
    if ok:
        reason = ""
    elif fallback or failed_gen:
        reason = ("Mockup generation failed — placeholder images were produced "
                  "instead of real product mockups.")
    else:
        reason = "No mockup passed quality validation."
    return {"ok": ok, "passing": len(passing), "total": len(images),
            "fallback": len(fallback), "reason": reason,
            "message": "" if ok else BLOCK_MESSAGE}


def listing_mockup_status(listing: dict[str, Any] | None) -> dict[str, Any]:
    """Convenience for the UI/cards: the gate result plus a short status word
    (``ok`` | ``failed`` | ``none``)."""
    if not listing:
        return {"ok": False, "status": "none", "passing": 0, "total": 0,
                "fallback": 0, "reason": "No listing package built yet.",
                "message": ""}
    r = evaluate_listing(listing)
    r["status"] = "ok" if r["ok"] else ("failed" if r["total"] else "none")
    return r
