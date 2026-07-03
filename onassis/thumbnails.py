"""The Thumbnail Optimiser — win the click before the sale.

The hero image is the whole ballgame on Etsy and Pinterest: it decides whether a
shopper clicks at all. So for every product this module generates **four**
competing hero shots, scores them, and ships the strongest one:

* **white_background** — the product clean and straight-on on a seamless studio
  ground (the safe, high-converting default).
* **lifestyle** — the product styled in a real, aspirational setting.
* **close_crop** — a tight macro crop that sells material and print quality.
* **in_use** — the product shown being used, so the shopper pictures owning it.

Scoring blends the deterministic **quality gate** (is the image fit to sell?)
with an **appeal prior** that starts from sensible defaults and is then replaced,
variant by variant, by the **real click-through rate** the system observes — so
ONASSIS learns which STYLE of hero actually earns clicks and lets that win next
time. It is deterministic (no AI agent) and reuses the Artwork Studio's backend
and quality gate; only the choice is new.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from onassis.artwork import ArtworkStudio
from onassis.config import Config
from onassis.connectors.image_backend import GALLERY, MOCKUP, ImageSpec
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# The four competing hero styles, each mapped to a distinct studio composition
# so they are genuinely different shots (not re-renders of one scene).
VARIANTS = [
    {"variant": "white_background", "scene": "hero", "kind": MOCKUP,
     "hint": "on a pure white seamless studio background"},
    {"variant": "lifestyle", "scene": "lifestyle", "kind": MOCKUP,
     "hint": "styled in an aspirational real-life setting"},
    {"variant": "close_crop", "scene": "closeup", "kind": MOCKUP,
     "hint": "a tight macro crop of the printed surface"},
    {"variant": "in_use", "scene": "room", "kind": GALLERY,
     "hint": "shown in use in its natural setting"},
]

# Appeal priors before any CTR is observed (a clean white hero is the safe
# default; these are progressively replaced by learned CTR per variant).
_DEFAULT_PRIORS = {"white_background": 0.90, "in_use": 0.75,
                   "lifestyle": 0.70, "close_crop": 0.65}


class ThumbnailOptimiser:
    """Generates 4 hero candidates per product, scores them, and picks the best."""

    def __init__(self, config: Config, db: Database | None = None,
                 studio: ArtworkStudio | None = None) -> None:
        self.config = config
        self.db = db
        self.studio = studio or ArtworkStudio(config, db)
        cfg = getattr(config, "thumbnails", None) or {}
        self.enabled = bool(cfg.get("optimise", True))
        self.ctr_ceiling = float(cfg.get("ctr_ceiling", 0.08)) or 0.08
        self.quality_weight = float(cfg.get("quality_weight", 0.4))
        self.appeal_weight = float(cfg.get("appeal_weight", 0.6))
        self.priors = {**_DEFAULT_PRIORS, **(cfg.get("priors") or {})}
        self.size = int(cfg.get("size", 0)) or int((getattr(config, "image", {}) or {})
                                                    .get("gallery_px", 1200))

    # --- Selection ---------------------------------------------------

    def choose(self, design_package: dict[str, Any], product: dict[str, Any],
               images_dir: str | Path, *, campaign_id: int | None = None,
               store: bool = True) -> dict[str, Any]:
        """Generate, score and choose the strongest hero for one product."""
        images_dir = Path(images_dir)
        images_dir.mkdir(parents=True, exist_ok=True)
        brief = design_package.get("design_brief", design_package)
        ctx = self.studio._design_context(brief)
        product_type = product.get("product_name") or product.get("product_key") or ""
        product_key = product.get("product_key") or "product"
        sku = product.get("sku")
        learned = self.db.learned_ctr_by_variant() if self.db else {}

        candidates: list[dict[str, Any]] = []
        for spec_def in VARIANTS:
            variant = spec_def["variant"]
            filename = f"hero_{variant}.jpg"
            prompt = self._prompt(brief, product_type, spec_def)
            spec = ImageSpec(kind=spec_def["kind"], width=self.size, height=self.size,
                             product_type=product_type, product_key=product_key,
                             scene=spec_def["scene"], prompt=prompt, **ctx)
            data, qc = self.studio.produce(spec)
            (images_dir / filename).write_bytes(data)
            prior = self._appeal(variant, learned)
            quality = float(qc.get("score", 0)) / 100.0
            score = round(100.0 * (self.quality_weight * quality
                                   + self.appeal_weight * prior), 1)
            candidates.append({
                "variant": variant, "filename": filename, "scene": spec_def["scene"],
                "quality_score": qc.get("score", 0.0), "prior": round(prior, 4),
                "score": score, "learned_ctr": learned.get(variant),
                "review": qc, "chosen": False,
            })

        winner = max(candidates, key=lambda c: (c["score"], c["quality_score"]))
        winner["chosen"] = True
        # The chosen hero also becomes the canonical hero.jpg the listing embeds.
        (images_dir / "hero.jpg").write_bytes((images_dir / winner["filename"]).read_bytes())

        if store and self.db:
            for c in candidates:
                self.db.insert_thumbnail({
                    "campaign_id": campaign_id, "product_key": product_key, "sku": sku,
                    "variant": c["variant"], "filename": c["filename"],
                    "quality_score": c["quality_score"], "prior": c["prior"],
                    "score": c["score"], "chosen": c["chosen"]})

        log.info("Thumbnail optimiser chose '%s' hero for %s (score %.0f of %d candidates).",
                 winner["variant"], product_key, winner["score"], len(candidates))
        return {"product_key": product_key, "chosen": winner["variant"],
                "hero_filename": "hero.jpg", "candidates": candidates,
                "learned_ctr": learned}

    def _appeal(self, variant: str, learned: dict[str, float]) -> float:
        """Appeal in [0,1]: learned CTR (normalised) once known, else the prior."""
        ctr = learned.get(variant)
        if ctr is not None:
            return max(0.0, min(1.0, ctr / self.ctr_ceiling))
        return float(self.priors.get(variant, 0.6))

    def _prompt(self, brief: dict[str, Any], product_type: str,
                spec_def: dict[str, str]) -> str:
        base = self.studio.prompts.scene(brief, product_type, spec_def["scene"])
        return f"{base} Hero thumbnail composition — {spec_def['hint']}."

    # --- CTR learning ------------------------------------------------

    def record_ctr(self, thumbnail_id: int, *, impressions: int, clicks: int) -> bool:
        """Accrue observed impressions/clicks so the winning style is learned."""
        if not self.db:
            return False
        return self.db.record_thumbnail_metrics(
            thumbnail_id, impressions=impressions, clicks=clicks)

    def learned_ctr(self) -> dict[str, float]:
        return self.db.learned_ctr_by_variant() if self.db else {}
