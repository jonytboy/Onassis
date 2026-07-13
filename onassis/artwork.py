"""The Artwork Studio — commercial asset production.

This is the missing production stage: it turns an approved **master design**
into the **actual image files** required to sell a product — not prompts, not
descriptions, not placeholders. For a design (and, per product, the approved
product set) it produces:

* ``master_artwork.png`` — the original commercial artwork.
* ``print_file.png`` — the print file for Gelato (correct size/resolution,
  transparent background where appropriate).
* product artwork — the artwork adapted for the specific product.
* mock-ups — real commercial scenes (hero, lifestyle, close-up, scale, room).
* a complete Etsy gallery of 8-10 conversion-focused images.

Every generated image passes a **deterministic quality gate** before it is
accepted (valid, correctly sized, not blank/flat, readable content); a failing
image is **regenerated** (a fresh composition) up to a bounded number of
attempts, keeping the best result. The quality gate is a measurable check, not
a new AI agent.

Image generation is delegated to a replaceable :class:`ImageBackend`
(:mod:`onassis.connectors.image_backend`). The Studio never talks to an image
provider directly, so ONASSIS is not locked to one. If the configured backend
fails on an image, the Studio falls back to the local renderer so a real file
is always produced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from onassis.config import Config
from onassis.connectors.image_backend import (
    GALLERY, MASTER, MOCKUP, PRINT, PRODUCT, ImageBackend, ImageSpec,
    LocalRenderBackend, build_image_backend, build_upscaler,
)
from onassis.logger import get_logger

log = get_logger(__name__)

# The commercial image plan for a product's Etsy gallery: an ordered set of
# mock-up scenes, padded with gallery variants to hit the configured target.
_SCENES = ["hero", "lifestyle", "closeup", "scale", "room"]

# Map an expansion product name to a compositional family. A poster, a mug and a
# tee must NOT receive the same image — each has its own natural shot and setting.
_FAMILY_KEYWORDS = [
    (("tote", "shopper"), "tote"),
    (("mug",), "mug"),
    (("hoodie",), "hoodie"),
    (("sweatshirt",), "sweatshirt"),
    (("t-shirt", "tshirt", "tee", "shirt"), "tshirt"),
    (("notebook", "journal"), "notebook"),
    (("greeting", "card"), "card"),
    (("framed",), "framed_poster"),
    (("canvas",), "canvas"),
    (("poster", "print", "wall art"), "poster"),
]

# Per-family composition: how the product is shot and where it naturally lives —
# so the model photographs the RIGHT scene for each product, not a generic one.
_PRODUCT_SHOTS = {
    "poster": {
        "noun": "a fine-art giclée poster print",
        "hero": "a single unframed art print shown flat and perfectly straight-on, "
                "filling the frame on a soft neutral studio background",
        "setting": "a light-filled, minimally styled modern living room with a linen "
                   "sofa, oak floor and a trailing plant",
        "use": "framed on a feature wall"},
    "framed_poster": {
        "noun": "a framed art print in a slim oak frame",
        "hero": "the framed print shown straight-on against a warm plaster wall, "
                "filling the frame",
        "setting": "a calm Scandinavian-Mediterranean interior with soft daylight",
        "use": "hung above a console table"},
    "canvas": {
        "noun": "a gallery-wrapped canvas print",
        "hero": "the canvas shown at a slight three-quarter angle to reveal its depth, "
                "on a clean studio background",
        "setting": "a sunlit bedroom with linen bedding and a rattan chair",
        "use": "mounted above the bed"},
    "mug": {
        "noun": "an 11oz ceramic mug",
        "hero": "the mug shown straight-on with the artwork wrapping its face, on a "
                "clean seamless studio background",
        "setting": "a cosy kitchen or sunlit breakfast table with coffee, linen and "
                   "fresh citrus",
        "use": "wrapped around morning coffee"},
    "tshirt": {
        "noun": "a premium relaxed-fit cotton t-shirt",
        "hero": "the t-shirt on an invisible mannequin (ghost-mannequin), straight-on, "
                "the artwork printed centre-chest, on a soft studio background",
        "setting": "a bright coastal scene on a relaxed model, natural candid styling",
        "use": "worn on easy summer days"},
    "sweatshirt": {
        "noun": "a heavyweight premium sweatshirt",
        "hero": "the sweatshirt flat-lay from above on natural linen, artwork "
                "centre-chest, neatly styled",
        "setting": "a cosy autumn interior on a relaxed model",
        "use": "worn on cool evenings"},
    "hoodie": {
        "noun": "a heavyweight premium hoodie",
        "hero": "the hoodie on a ghost-mannequin, straight-on, artwork centre-chest, "
                "soft studio background",
        "setting": "a coastal boardwalk at golden hour on a relaxed model",
        "use": "worn layered by the sea"},
    "tote": {
        "noun": "a natural canvas tote bag",
        "hero": "the tote shown flat and straight-on with the artwork printed on the "
                "front panel, on a clean studio background",
        "setting": "a sunlit market or café scene, carried on the shoulder",
        "use": "carried to the market"},
    "notebook": {
        "noun": "a hardcover notebook",
        "hero": "the notebook shown straight-on with the artwork on its cover, on a "
                "clean studio background",
        "setting": "a writer's desk with coffee, a pen and soft morning light",
        "use": "kept on a writing desk"},
    "card": {
        "noun": "a folded greeting card",
        "hero": "the greeting card standing upright with the artwork on its front, on "
                "a clean styled surface",
        "setting": "a gift setting on a mantel with dried flowers and ribbon",
        "use": "given as a thoughtful gift"},
}


def product_family(name: str | None) -> str:
    """Classify an expansion product name into a compositional family."""
    n = (name or "").lower()
    for keys, fam in _FAMILY_KEYWORDS:
        if any(k in n for k in keys):
            return fam
    return "poster"


class CommercialPromptBuilder:
    """Builds sales-optimised image prompts from ONASSIS's **commercial brief**.

    This is where ONASSIS becomes valuable: the model is given the whole
    context — customer, emotion, lifestyle, colours, mood, intended use/room and
    why someone would buy — plus product-specific composition, not merely the
    product title. Prompts are optimised for **click-through and conversion**,
    not artistic merit.
    """

    def master(self, brief: dict[str, Any]) -> str:
        c = self._context(brief)
        return (
            "Original commercial surface artwork for a premium Mediterranean "
            "lifestyle brand — this is the ARTWORK itself, not a photo of a product. "
            f"Subject: {c['artwork']}. Theme: {c['theme']}. "
            f"Colour palette: {c['palette']}. Mood: {c['mood']}. "
            f"Created to attract {c['customer']} seeking {c['emotion']}. {c['rationale']} "
            "Balanced, print-ready composition with a clear focal point and generous "
            "negative space; refined, gallery-quality, cohesive enough to adapt across "
            "posters, apparel and homeware. Flat artwork on a clean ground — no mockup, "
            "no product, no photography, no text, no watermark, no signature."
        )

    def print_file(self, brief: dict[str, Any]) -> str:
        c = self._context(brief)
        return (
            "Isolated original artwork on a fully transparent background for "
            "print-on-demand / direct-to-garment production. "
            f"Subject: {c['artwork']}. Palette: {c['palette']}. {c['mood']}. "
            "Centred, high-contrast, crisp clean edges, no background, no drop shadow, "
            "no text, no watermark — production-ready at high resolution."
        )

    def scene(self, brief: dict[str, Any], product_type: str, scene: str) -> str:
        c = self._context(brief)
        p = _PRODUCT_SHOTS.get(product_family(product_type), _PRODUCT_SHOTS["poster"])
        return (
            f"Professional Etsy product photograph — {p['noun']} featuring an original "
            f"{c['theme']} design. {self._shot(scene, p)} "
            f"The printed artwork: {c['artwork']}, in a {c['palette']} palette; {c['mood']}. "
            f"Styled for {c['customer']} who want {c['emotion']} — compose the shot so they "
            f"instantly picture it {p['use']} in their own life. {c['season_line']}"
            "Commercial catalogue photography: natural soft directional light, shallow "
            "depth of field, crisp focus on the product, aspirational yet authentic "
            "styling, high resolution. Optimised to maximise click-through and conversion "
            "as an Etsy listing image. No text, captions, watermarks, logos or borders; "
            "realistic proportions and true-to-life materials."
        )

    def _shot(self, scene: str, p: dict[str, str]) -> str:
        noun = self._bare(p["noun"])  # drop the leading article for "the {noun}"
        if scene in ("hero", "product"):
            return f"Primary hero thumbnail: {p['hero']}."
        if scene == "lifestyle":
            return (f"Lifestyle scene: the {noun} shown {p['use']} within "
                    f"{p['setting']}, with tasteful natural props and a sense of real "
                    f"daily life.")
        if scene == "closeup":
            return (f"Extreme macro close-up of the printed surface of the {noun}, "
                    f"revealing texture, print crispness and material quality.")
        if scene == "scale":
            return (f"The {noun} photographed beside an everyday object for a clear "
                    f"sense of scale, on a clean neutral surface.")
        if scene == "room":
            return (f"The {noun} styled in situ within {p['setting']}, shown as part "
                    f"of a beautifully decorated space.")
        return f"{p['hero']}."

    @staticmethod
    def _bare(noun: str) -> str:
        for article in ("an ", "a "):
            if noun.lower().startswith(article):
                return noun[len(article):]
        return noun

    def _context(self, brief: dict[str, Any]) -> dict[str, str]:
        theme = brief.get("theme") or "Mediterranean coastal lifestyle"
        customer = (brief.get("target_customer")
                    or "design-loving travellers who value calm, premium living")
        emotion = (brief.get("emotional_angle")
                   or "a feeling of calm, unhurried Mediterranean luxury")
        artwork = (brief.get("artwork_description")
                   or f"an elegant, original {theme} illustration")
        rationale = (brief.get("design_rationale") or "").strip()
        if rationale and not rationale.endswith("."):
            rationale += "."
        season = brief.get("seasonal_relevance")
        return {
            "theme": theme, "customer": customer, "emotion": emotion,
            "artwork": artwork, "rationale": rationale,
            "palette": self._palette_words(brief),
            "mood": f"warm, editorial and aspirational, evoking {emotion}",
            "season_line": f"Evoke {season}. " if season else "",
        }

    @staticmethod
    def _palette_words(brief: dict[str, Any]) -> str:
        words: list[str] = []
        for key in ("shirt_colour", "print_colour", "primary_colour", "secondary_colour"):
            val = brief.get(key)
            if val and str(val).lower() not in [w.lower() for w in words]:
                words.append(str(val))
        for c in brief.get("colour_palette", []) or []:
            if c and str(c).lower() not in [w.lower() for w in words]:
                words.append(str(c))
        return ", ".join(words) or "warm Mediterranean neutrals with a terracotta accent"


class ArtworkReview:
    """A deterministic quality gate for generated artwork (not an AI agent).

    It answers, with measurable proxies, whether an image is fit to sell: is it a
    valid image, not blank or flat (has real content), and does it carry visible,
    readable detail? Anything below the bar is regenerated by the Studio.

    It deliberately does **not** judge pixel dimensions. Image models return a
    fixed native size (e.g. GPT Image = 1024²); a smaller-than-target image is not
    a quality failure and re-requesting it would just burn API calls. The Studio
    upscales the accepted image to print resolution instead. QC only regenerates
    for genuine quality problems (blank / flat / invalid / unreadable).
    """

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or {}
        self.min_coverage = float(cfg.get("min_coverage", 0.015))
        self.max_coverage = float(cfg.get("max_coverage", 0.995))
        self.min_stddev = float(cfg.get("min_stddev", 6.0))
        self.min_bytes = int(cfg.get("min_bytes", 800))

    def evaluate(self, data: bytes, spec: ImageSpec) -> dict[str, Any]:
        import io

        from PIL import Image, ImageStat

        reasons: list[str] = []
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:  # not a valid image
            return {"accepted": False, "score": 0.0,
                    "reasons": [f"invalid image: {exc}"], "checks": {}}

        # NB: dimensions are NOT an accept/reject criterion — the model's native
        # size is upscaled to the target later. We only record it.
        bytes_ok = len(data) >= self.min_bytes

        # Content coverage: fraction of pixels that carry the design (ink), vs a
        # flat background. Transparent print files use the alpha channel.
        if spec.transparent and "A" in img.getbands():
            raw = img.getchannel("A").resize((96, 96)).tobytes()  # 1 byte/px, downsampled
            coverage = sum(1 for p in raw if p > 16) / max(1, len(raw))
        else:
            coverage = self._ink_coverage(img.convert("RGB"))
        coverage_ok = self.min_coverage <= coverage <= self.max_coverage

        # Visible detail / contrast (typography + composition readability).
        stddev = max(ImageStat.Stat(img.convert("L")).stddev or [0.0])
        detail_ok = stddev >= self.min_stddev

        if not bytes_ok:
            reasons.append("image too small / empty")
        if not coverage_ok:
            reasons.append(f"content coverage {coverage:.3f} out of range")
        if not detail_ok:
            reasons.append(f"too flat (stddev {stddev:.1f})")

        checks = {"source_size": [img.width, img.height], "bytes_ok": bytes_ok,
                  "coverage": round(coverage, 4), "coverage_ok": coverage_ok,
                  "stddev": round(stddev, 2), "detail_ok": detail_ok}
        accepted = bytes_ok and coverage_ok and detail_ok
        score = round(100.0 * (coverage_ok + detail_ok + bytes_ok) / 3.0, 1)
        return {"accepted": accepted, "score": score, "reasons": reasons, "checks": checks}

    @staticmethod
    def _ink_coverage(rgb) -> float:
        """Fraction of pixels that differ noticeably from the background colour."""
        raw = rgb.resize((64, 64)).tobytes()  # 3 bytes/px (RGB), downsampled
        bg = (raw[0], raw[1], raw[2])         # background ≈ top-left corner
        n = len(raw) // 3
        diff = sum(1 for i in range(0, len(raw), 3)
                   if abs(raw[i] - bg[0]) + abs(raw[i + 1] - bg[1])
                   + abs(raw[i + 2] - bg[2]) > 40)
        return diff / max(1, n)


class ArtworkStudio:
    """Produces the real commercial image files for a design and its products."""

    # The DISTINCT premium gallery scenes, priority-ordered (Sprint 42.2, Obj 7).
    # No scene repeats, so no image is ever rendered twice. gallery_count picks
    # how many to generate from the top: the first four are the required set
    # (Hero, Lifestyle, Detail, Secondary/Scale).
    _GALLERY_SCENES: list[tuple[str, str, str]] = [
        ("hero.jpg", MOCKUP, "hero"),
        ("mockup_01.jpg", MOCKUP, "lifestyle"),
        ("mockup_02.jpg", MOCKUP, "closeup"),
        ("mockup_03.jpg", MOCKUP, "scale"),
        ("gallery_01.jpg", GALLERY, "room"),
        ("gallery_02.jpg", PRODUCT, "hero"),
    ]

    def __init__(self, config: Config, db: Any | None = None,
                 backend: ImageBackend | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "image", None) or {}
        self._backend = backend or build_image_backend(config)
        self._fallback = LocalRenderBackend()
        # A successful image is never silently replaced. The DEV local renderer is
        # used ONLY when the production backend genuinely fails, and only if
        # explicitly allowed — the exact exception is always surfaced.
        self.fallback_to_local = bool(self.cfg.get("fallback_to_local", True))
        self.upscaler = build_upscaler(config)
        self.prompts = CommercialPromptBuilder()
        self.review = ArtworkReview(self.cfg.get("quality_gate"))
        self.max_attempts = int(self.cfg.get("max_attempts", 3))
        self.master_px = int(self.cfg.get("master_px", 2048))
        self.print_px = int(self.cfg.get("print_px", 3600))  # ~300 DPI over A3-ish
        self.gallery_px = int(self.cfg.get("gallery_px", 1200))
        # Cost scales with image count (Sprint 42.2, Obj 6/7): generate only the
        # required DISTINCT premium mockups — never duplicate renders. Tunable
        # from the minimum saleable set (4) up to the full distinct palette.
        self.gallery_count = max(4, min(len(self._GALLERY_SCENES),
                                        int(self.cfg.get("gallery_count", 6))))

    @property
    def backend_name(self) -> str:
        return self._backend.name

    # --- Generation with the quality gate ---------------------------

    def produce(self, spec: ImageSpec) -> tuple[bytes, dict[str, Any]]:
        """Public entry: generate one QC-passed, finalised image for a spec.

        Used by the Thumbnail Optimiser to render competing hero candidates
        through the same backend + quality gate the gallery uses."""
        return self._produce(spec)

    def _produce(self, spec: ImageSpec) -> tuple[bytes, dict[str, Any]]:
        """Generate one image, regenerating only on genuine QC failure, then
        upscale the accepted image to the target print resolution.

        The image model returns its native size; we never re-request a larger
        image (that just burns API calls). We regenerate only for real quality
        problems (blank / flat / invalid), then upscale the winner."""
        best: tuple[bytes, dict[str, Any]] | None = None
        for attempt in range(1, self.max_attempts + 1):
            spec.variant = attempt - 1
            data, gen = self._generate(spec)
            verdict = self.review.evaluate(data, spec)
            verdict["attempts"] = attempt
            # Provenance travels with every image (P1): provider/model/prompt and
            # whether a fallback placeholder was substituted for a real generation.
            verdict.update(provider=gen["provider"], model=gen["model"],
                           prompt=spec.prompt, fallback_used=gen["fallback_used"],
                           generation_ok=gen["generation_ok"],
                           generation_error=gen["generation_error"])
            # A fallback/placeholder image is NEVER a real product mockup — it must
            # never be treated as publishable, however "clean" it renders.
            if gen["fallback_used"]:
                verdict["accepted"] = False
                verdict["reasons"] = [*verdict.get("reasons", []),
                                      "placeholder/fallback image — real image "
                                      "generation failed; not publishable"]
            if verdict["accepted"]:
                return self._finalise(data, spec), verdict
            if best is None or verdict["score"] > best[1]["score"]:
                best = (data, verdict)
            log.info("Artwork QC rejected %s/%s (attempt %d): %s — regenerating.",
                     spec.kind, spec.scene, attempt, "; ".join(verdict["reasons"]))
        assert best is not None
        log.warning("Artwork QC accepted best-effort %s/%s after %d attempts (score %.0f).",
                    best[1].get("kind", spec.kind), spec.scene, self.max_attempts,
                    best[1]["score"])
        return self._finalise(best[0], spec), best[1]

    def _finalise(self, data: bytes, spec: ImageSpec) -> bytes:
        """Upscale the accepted native image to the target size + canonical format
        (PNG for master/print, JPEG for product photos). No re-request to the model."""
        fmt = "PNG" if (spec.transparent or spec.kind in (MASTER, PRINT)) else "JPEG"
        try:
            return self.upscaler.upscale(data, spec.width, spec.height, fmt=fmt)
        except Exception as exc:  # never lose the asset over an encode/resize hiccup
            log.warning("Upscale failed for %s/%s (%s); using the native image.",
                        spec.kind, spec.scene, exc)
            return data

    def _generate(self, spec: ImageSpec) -> tuple[bytes, dict[str, Any]]:
        """Generate one image, returning ``(bytes, provenance)``. A successful
        backend image is returned verbatim — it is never replaced. Only a genuine
        backend failure (an exception) can fall back; the exact exception is
        always surfaced, and the returned provenance records ``fallback_used`` so
        the placeholder can never be published (Mockup Quality Gate, P1)."""
        import time as _time

        from onassis.ai_accounting import record_image

        meta = {"provider": self._backend.name,
                "model": getattr(self._backend, "model", "") or "",
                "fallback_used": False, "generation_ok": True, "generation_error": ""}
        started = _time.monotonic()
        try:
            data = self._backend.generate(spec)
            # Cost accounting (Sprint 42.2) — the local dev renderer is free.
            if self._backend.name != "local":
                record_image(provider=self._backend.name,
                             model=getattr(self._backend, "model", "") or "",
                             quality=getattr(self._backend, "quality", "") or "",
                             images=1, duration_ms=int((_time.monotonic() - started) * 1000),
                             ok=True)
            return data, meta
        except Exception as exc:
            if self._backend.name == "local":
                raise  # the dev renderer failing is a real bug — don't mask it
            record_image(provider=self._backend.name,
                         model=getattr(self._backend, "model", "") or "",
                         quality=getattr(self._backend, "quality", "") or "", images=0,
                         duration_ms=int((_time.monotonic() - started) * 1000),
                         ok=False, detail=str(exc)[:200])
            # Surface the EXACT exception (full traceback) — never silent.
            log.error("Image backend '%s' FAILED for %s/%s: %s",
                      self._backend.name, spec.kind, spec.scene, exc, exc_info=True)
            meta["generation_ok"] = False
            meta["generation_error"] = str(exc)
            if not self.fallback_to_local:
                raise
            log.error("Substituting the DEV local renderer for this image "
                      "(image.fallback_to_local=true) — this image is a PLACEHOLDER "
                      "and is not publishable.")
            data = self._fallback.generate(spec)
            meta["provider"] = self._fallback.name
            meta["fallback_used"] = True
            return data, meta

    # --- Master design assets ---------------------------------------

    def generate_master(self, design_package: dict[str, Any], out_dir: Path) -> dict[str, Any]:
        """Produce ``master_artwork.png`` + ``print_file.png`` for a design."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        brief = design_package.get("design_brief", design_package)
        spec_common = self._design_context(brief)

        master_spec = ImageSpec(kind=MASTER, width=self.master_px, height=self.master_px,
                                transparent=False, prompt=self.prompts.master(brief),
                                **spec_common)
        master_bytes, master_qc = self._produce(master_spec)
        (out_dir / "master_artwork.png").write_bytes(master_bytes)

        transparent = bool(brief.get("transparent_background_required", True))
        print_spec = ImageSpec(kind=PRINT, width=self.print_px, height=self.print_px,
                               transparent=transparent,
                               prompt=self.prompts.print_file(brief),
                               **spec_common)
        print_bytes, print_qc = self._produce(print_spec)
        (out_dir / "print_file.png").write_bytes(print_bytes)

        log.info("Master artwork ready at %s (backend=%s).", out_dir, self.backend_name)
        return {
            "status": "ready",
            "backend": self.backend_name,
            "path": str(out_dir),
            "files": ["master_artwork.png", "print_file.png"],
            "master_review": master_qc,
            "print_review": print_qc,
            "dpi": int(brief.get("dpi_requirement", 300)),
        }

    # --- Per-product commercial gallery -----------------------------

    def build_product_gallery(
        self, design_package: dict[str, Any], product: dict[str, Any],
        images_dir: Path, *, alt_texts: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Produce the ordered commercial gallery for one product as real files.

        Writes into ``images_dir`` and returns an ordered image manifest —
        ``[{order, filename, kind, scene, alt_text, review}]`` — that the Listing
        Factory embeds and the Publisher uploads. 8-10 images, conversion-first.
        """
        images_dir = Path(images_dir)
        images_dir.mkdir(parents=True, exist_ok=True)
        brief = design_package.get("design_brief", design_package)
        ctx = self._design_context(brief)
        product_type = product.get("product_name") or product.get("product_key") or ""
        product_key = product.get("product_key") or "product"

        plan = self._gallery_plan()
        manifest: list[dict[str, Any]] = []
        for order, (filename, kind, scene) in enumerate(plan, start=1):
            spec = ImageSpec(
                kind=kind, width=self.gallery_px, height=self.gallery_px,
                product_type=product_type, product_key=product_key, scene=scene,
                prompt=self.prompts.scene(brief, product_type, scene),
                **ctx,
            )
            data, qc = self._produce(spec)
            (images_dir / filename).write_bytes(data)
            alt = (alt_texts[order - 1] if alt_texts and order - 1 < len(alt_texts)
                   else self._alt(brief, product_type, scene))
            manifest.append({"order": order, "filename": filename, "kind": kind,
                             "scene": scene, "alt_text": alt, "review": qc,
                             "source": qc.get("provider") or self.backend_name,
                             "provider": qc.get("provider") or self.backend_name,
                             "model": qc.get("model", ""),
                             "prompt": qc.get("prompt", spec.prompt),
                             "fallback_used": bool(qc.get("fallback_used")),
                             "generation_ok": bool(qc.get("generation_ok", True)),
                             "generation_error": qc.get("generation_error", ""),
                             "quality_pass": bool(qc.get("accepted"))})
        log.info("Built %d commercial image(s) for %s at %s.",
                 len(manifest), product_key, images_dir)
        return manifest

    def _gallery_plan(self) -> list[tuple[str, str, str]]:
        """(filename, kind, scene) — the ordered DISTINCT commercial gallery.

        Each entry is a different scene, so no image is rendered twice
        (Sprint 42.2, Obj 6/7). ``gallery_count`` selects how many of the
        required premium mockups to generate, cheapest-first coverage:
        Hero → Lifestyle → Detail → Scale → Room → Product-front."""
        return list(self._GALLERY_SCENES[:self.gallery_count])

    # --- Design context / prompts -----------------------------------

    def _design_context(self, brief: dict[str, Any]) -> dict[str, Any]:
        palette = self._palette(brief)
        return {
            "palette": palette,
            "title": brief.get("listing_title_seed") or brief.get("product_name") or "",
            "subtitle": brief.get("brand") or (self.config.brand or {}).get("name", ""),
            "motif": brief.get("theme", "coastal"),
        }

    def _palette(self, brief: dict[str, Any]) -> list[str]:
        colours = [brief.get("shirt_colour"), brief.get("print_colour"),
                   brief.get("primary_colour"), brief.get("secondary_colour")]
        colours = [c for c in colours if c]
        return colours or ["ecru", "terracotta", "sea", "olive"]

    def _alt(self, brief: dict[str, Any], product_type: str, scene: str) -> str:
        name = brief.get("product_name") or product_type
        label = {"hero": "hero image", "lifestyle": "lifestyle scene",
                 "closeup": "close-up of the artwork", "scale": "scale reference",
                 "room": "styled room scene"}.get(scene, scene)
        return f"{name} on a {product_type} — {label}"[:250]
