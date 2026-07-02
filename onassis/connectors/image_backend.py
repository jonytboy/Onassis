"""Image-generation backends — the replaceable commercial-artwork engine.

ONASSIS must generate the **actual artwork files** (not prompts). This module
defines a small, replaceable backend interface plus two implementations, so the
system is never locked to one image provider:

* :class:`LocalRenderBackend` — a dependency-light renderer (Pillow) that always
  produces **real** print-ready image files: original artwork composed from the
  design brief (palette, typography seed, motif), product-adapted layouts, and
  commercial mock-up scenes. It needs no network or API key, so the pipeline is
  complete and testable out of the box. This is the default.

* :class:`RemoteImageBackend` — an OpenAI-compatible ``/images/generations``
  client that produces AI artwork when an image-generation key is configured.
  It is a drop-in replacement selected by config; any error falls back to the
  local renderer so the cycle never dies without images.

Everything downstream (the Artwork Studio, Listing Factory, Publisher) depends
only on the :class:`ImageBackend` interface — swap the backend, nothing else
changes.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
from dataclasses import dataclass, field
from typing import Any

from onassis.logger import get_logger

log = get_logger(__name__)

# Backend "kinds" of image the studio asks for.
MASTER = "master"
PRINT = "print"
PRODUCT = "product"
MOCKUP = "mockup"
GALLERY = "gallery"

# A small brand palette so evocative design words resolve to real colours even
# when Pillow's CSS colour table doesn't know them.
_BRAND_COLOURS: dict[str, tuple[int, int, int]] = {
    "ecru": (240, 234, 214), "natural": (238, 231, 213), "cream": (245, 240, 228),
    "sand": (224, 208, 176), "bone": (236, 229, 214), "oat": (226, 214, 190),
    "terracotta": (196, 105, 74), "clay": (188, 112, 82), "rust": (176, 92, 60),
    "olive": (122, 128, 84), "sage": (156, 165, 132), "sea": (74, 128, 138),
    "azure": (86, 140, 176), "cobalt": (52, 92, 150), "coastal blue": (70, 120, 150),
    "whitewash": (248, 246, 240), "charcoal": (54, 54, 58), "ink": (38, 40, 46),
    "lemon": (226, 196, 96), "citrus": (226, 176, 72), "sun": (232, 188, 96),
    "stone": (176, 168, 152), "marble": (238, 236, 230), "midnight": (30, 40, 62),
}

_MEDITERRANEAN = [(74, 128, 138), (196, 105, 74), (240, 234, 214), (122, 128, 84)]


def resolve_colour(name: str | None, default: tuple[int, int, int] = (74, 128, 138)
                   ) -> tuple[int, int, int]:
    """Resolve a colour word/hex to RGB, robust to phrases like 'natural/ecru'."""
    if not name:
        return default
    from PIL import ImageColor

    raw = str(name).strip().lower()
    for token in [raw, *raw.replace("/", " ").replace(",", " ").split()]:
        token = token.strip()
        if not token:
            continue
        if token in _BRAND_COLOURS:
            return _BRAND_COLOURS[token]
        try:
            return ImageColor.getrgb(token)
        except (ValueError, KeyError):
            continue
    return default


@dataclass
class ImageSpec:
    """A single image the studio wants produced."""

    kind: str                       # MASTER | PRINT | PRODUCT | MOCKUP | GALLERY
    width: int
    height: int
    prompt: str = ""                # natural-language brief (for remote models)
    transparent: bool = False       # transparent background (print files)
    palette: list[str] = field(default_factory=list)
    title: str = ""                 # typography seed rendered onto the artwork
    subtitle: str = ""
    product_type: str = ""          # "t-shirt", "mug", "poster", ...
    product_key: str = ""
    scene: str = "hero"             # mock-up scene: hero|lifestyle|closeup|scale|room
    motif: str = "coastal"
    variant: int = 0                # varies composition (gallery / regeneration)

    def seed(self) -> int:
        raw = f"{self.title}|{self.product_key}|{self.scene}|{self.kind}|{self.variant}"
        return int(hashlib.sha256(raw.encode("utf-8")).hexdigest(), 16)

    def colours(self) -> list[tuple[int, int, int]]:
        resolved = [resolve_colour(c) for c in self.palette if c]
        return resolved or list(_MEDITERRANEAN)


class ImageBackend:
    """The replaceable image-generation interface. Returns encoded image bytes."""

    name = "base"

    def generate(self, spec: ImageSpec) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


# --- Local renderer (always available, real files) ------------------

class LocalRenderBackend(ImageBackend):
    """Renders real, print-ready artwork and commercial mock-ups with Pillow.

    Deterministic (seeded by the design), needs no network or key, and produces
    genuine PNG/JPEG files — never 1×1 placeholders.
    """

    name = "local"

    def generate(self, spec: ImageSpec) -> bytes:
        from PIL import Image

        if spec.kind in (MASTER, PRINT):
            img = self._artwork(spec, transparent=spec.transparent)
        elif spec.kind == PRODUCT:
            img = self._product(spec)
        else:  # MOCKUP / GALLERY are scene compositions of the product
            img = self._mockup(spec)

        fmt = "PNG" if (spec.transparent or spec.kind in (MASTER, PRINT)) else "JPEG"
        buf = io.BytesIO()
        if fmt == "JPEG" and img.mode == "RGBA":
            img = img.convert("RGB")
        img.save(buf, format=fmt, quality=92)
        return buf.getvalue()

    # -- primitives --

    def _font(self, size: int):
        from PIL import ImageFont

        try:
            return ImageFont.load_default(size=size)
        except (TypeError, AttributeError):  # very old Pillow
            return ImageFont.load_default()

    def _rng(self, spec: ImageSpec):
        import random

        return random.Random(spec.seed())

    def _artwork(self, spec: ImageSpec, transparent: bool):
        """The original commercial artwork: a composed Mediterranean motif +
        typography, on a transparent or garment-coloured ground."""
        from PIL import Image, ImageDraw

        W, H = spec.width, spec.height
        palette = spec.colours()
        ground = (0, 0, 0, 0) if transparent else (*palette[2 % len(palette)], 255)
        img = Image.new("RGBA", (W, H), ground)
        draw = ImageDraw.Draw(img)
        ink = palette[1 % len(palette)]
        accent = palette[0]
        rng = self._rng(spec)

        cx, cy = W // 2, int(H * 0.42)
        # A rising-sun / concentric-arc motif — clean, on-brand, print-safe.
        sun_r = int(min(W, H) * 0.20)
        draw.ellipse([cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r],
                     fill=(*accent, 255))
        for i in range(1, 6):
            r = sun_r + int(min(W, H) * 0.045) * i
            draw.arc([cx - r, cy - r, cx + r, cy + r], start=200, end=340,
                     fill=(*ink, 255), width=max(2, W // 260))
        # Horizon / wave lines beneath the sun.
        for i in range(3):
            y = cy + sun_r + int(min(W, H) * 0.06) * (i + 1)
            amp = int(min(W, H) * (0.018 + 0.01 * rng.random()))
            pts = [(x, y + int(amp * math.sin(x / (W / 6) + i)))
                   for x in range(int(W * 0.12), int(W * 0.88), max(4, W // 120))]
            if len(pts) > 1:
                draw.line(pts, fill=(*ink, 255), width=max(2, W // 320), joint="curve")

        # Typography seed — the readable brand line.
        title = (spec.title or "").strip()
        if title:
            self._center_text(draw, title.upper(), (cx, int(H * 0.78)),
                              W, ink, max(14, W // 16))
        if spec.subtitle:
            self._center_text(draw, spec.subtitle.strip(), (cx, int(H * 0.88)),
                              W, ink, max(10, W // 30))
        return img

    def _center_text(self, draw, text, xy, width, colour, size):
        font = self._font(size)
        # Wrap to the print-safe width.
        words, lines, line = text.split(), [], ""
        for w in words:
            trial = f"{line} {w}".strip()
            if draw.textlength(trial, font=font) <= width * 0.82 or not line:
                line = trial
            else:
                lines.append(line)
                line = w
        if line:
            lines.append(line)
        cx, cy = xy
        lh = size + max(2, size // 6)
        top = cy - (len(lines) * lh) // 2
        for i, ln in enumerate(lines):
            tw = draw.textlength(ln, font=font)
            draw.text((cx - tw / 2, top + i * lh), ln, fill=(*colour, 255), font=font)

    def _product(self, spec: ImageSpec):
        """Artwork adapted to the product's shape (garment/mug/poster/…)."""
        from PIL import Image, ImageDraw

        W, H = spec.width, spec.height
        palette = spec.colours()
        bg = self._soft_gradient(W, H, palette)
        draw = ImageDraw.Draw(bg)
        art = self._artwork(ImageSpec(kind=MASTER, width=int(W * 0.6), height=int(H * 0.6),
                                      palette=spec.palette, title=spec.title,
                                      subtitle=spec.subtitle, variant=spec.variant,
                                      transparent=True), transparent=True)
        self._blit_on_product(bg, draw, art, spec)
        return bg

    def _mockup(self, spec: ImageSpec):
        """A commercial mock-up scene: product hero on a styled background."""
        from PIL import Image, ImageDraw

        W, H = spec.width, spec.height
        palette = spec.colours()
        scene = spec.scene
        bg = self._soft_gradient(W, H, palette, scene=scene)
        draw = ImageDraw.Draw(bg)

        # Close-up = zoom on the artwork; others show the product in a scene.
        if scene == "closeup":
            art = self._artwork(ImageSpec(kind=MASTER, width=int(W * 0.82),
                                          height=int(H * 0.82), palette=spec.palette,
                                          title=spec.title, subtitle=spec.subtitle,
                                          variant=spec.variant, transparent=True),
                                transparent=True)
            bg.alpha_composite(art, (int(W * 0.09), int(H * 0.09)))
        else:
            art = self._artwork(ImageSpec(kind=MASTER, width=int(W * 0.5),
                                          height=int(H * 0.5), palette=spec.palette,
                                          title=spec.title, subtitle=spec.subtitle,
                                          variant=spec.variant, transparent=True),
                                transparent=True)
            self._blit_on_product(bg, draw, art, spec)
            if scene == "scale":  # a scale reference bar
                draw.rectangle([int(W * 0.08), int(H * 0.86), int(W * 0.32), int(H * 0.88)],
                               fill=palette[1 % len(palette)])
                self._center_text(draw, "30 cm", (int(W * 0.20), int(H * 0.915)),
                                  int(W * 0.3), palette[1 % len(palette)], max(12, W // 34))

        # A tasteful scene label (hero/lifestyle/room…) — signals a curated shot.
        label = {"hero": "", "lifestyle": "", "room": "", "scale": "",
                 "closeup": ""}.get(scene, "")
        if label:
            self._center_text(draw, label, (W // 2, int(H * 0.06)), W,
                              palette[1 % len(palette)], max(12, W // 40))
        return bg

    def _soft_gradient(self, W, H, palette, scene: str = "hero"):
        from PIL import Image

        top = palette[2 % len(palette)]
        bottom = tuple(int(c * 0.82) for c in palette[0])
        if scene == "room":  # warm interior wall
            top, bottom = (238, 230, 216), (206, 194, 176)
        elif scene == "lifestyle":
            top, bottom = palette[2 % len(palette)], palette[3 % len(palette)]
        # Build a 1×H gradient column, then stretch to width — O(H), not O(W·H).
        col = Image.new("RGBA", (1, H))
        col.putdata([
            (*tuple(int(top[i] * (1 - y / max(1, H - 1)) + bottom[i] * (y / max(1, H - 1)))
                    for i in range(3)), 255)
            for y in range(H)
        ])
        return col.resize((W, H))

    def _blit_on_product(self, bg, draw, art, spec: ImageSpec):
        """Draw a simple product silhouette and composite the artwork onto it."""
        W, H = bg.size
        palette = spec.colours()
        garment = palette[2 % len(palette)]
        ptype = (spec.product_type or "").lower()

        if any(k in ptype for k in ("shirt", "tee", "hoodie", "sweat")):
            self._garment(draw, W, H, garment, palette[1 % len(palette)])
            bg.alpha_composite(art, (W // 2 - art.width // 2, int(H * 0.34)))
        elif "mug" in ptype:
            self._mug(draw, W, H, garment, palette[1 % len(palette)])
            bg.alpha_composite(art, (int(W * 0.34) - art.width // 2 + int(W * 0.02),
                                     int(H * 0.5) - art.height // 2))
        elif "tote" in ptype or "bag" in ptype:
            self._tote(draw, W, H, garment, palette[1 % len(palette)])
            bg.alpha_composite(art, (W // 2 - art.width // 2, int(H * 0.4)))
        elif "notebook" in ptype or "card" in ptype:
            self._panel(draw, W, H, garment, palette[1 % len(palette)], ratio=0.7)
            bg.alpha_composite(art, (W // 2 - art.width // 2, H // 2 - art.height // 2))
        else:  # poster / framed / canvas / generic — a framed print
            self._panel(draw, W, H, (250, 248, 242), palette[1 % len(palette)], ratio=0.78)
            bg.alpha_composite(art, (W // 2 - art.width // 2, H // 2 - art.height // 2))

    def _garment(self, draw, W, H, colour, edge):
        cx = W // 2
        body = [(int(W * 0.30), int(H * 0.30)), (int(W * 0.70), int(H * 0.30)),
                (int(W * 0.78), int(H * 0.44)), (int(W * 0.70), int(H * 0.50)),
                (int(W * 0.70), int(H * 0.86)), (int(W * 0.30), int(H * 0.86)),
                (int(W * 0.30), int(H * 0.50)), (int(W * 0.22), int(H * 0.44))]
        draw.polygon(body, fill=colour, outline=edge)
        draw.arc([cx - int(W * 0.08), int(H * 0.26), cx + int(W * 0.08), int(H * 0.36)],
                 start=0, end=180, fill=edge, width=max(2, W // 300))

    def _mug(self, draw, W, H, colour, edge):
        draw.rounded_rectangle([int(W * 0.24), int(H * 0.30), int(W * 0.66), int(H * 0.72)],
                               radius=int(W * 0.03), fill=colour, outline=edge,
                               width=max(2, W // 320))
        draw.arc([int(W * 0.62), int(H * 0.38), int(W * 0.80), int(H * 0.64)],
                 start=300, end=60, fill=edge, width=max(4, W // 90))

    def _tote(self, draw, W, H, colour, edge):
        draw.rectangle([int(W * 0.28), int(H * 0.34), int(W * 0.72), int(H * 0.82)],
                       fill=colour, outline=edge, width=max(2, W // 320))
        for x in (0.38, 0.62):
            draw.arc([int(W * (x - 0.06)), int(H * 0.18), int(W * (x + 0.06)), int(H * 0.44)],
                     start=180, end=360, fill=edge, width=max(3, W // 160))

    def _panel(self, draw, W, H, colour, edge, ratio: float):
        m = (1 - ratio) / 2
        draw.rectangle([int(W * m), int(H * m), int(W * (1 - m)), int(H * (1 - m))],
                       fill=colour, outline=edge, width=max(3, W // 120))


# --- Production AI backends (activate with a key) -------------------

class OpenAIImageBackend(ImageBackend):
    """Production backend — OpenAI **GPT Image** (``gpt-image-1``).

    Generates commercial, sellable artwork from a rich prompt (built from
    ONASSIS's commercial brief, not the product title). Transparent print files
    request ``background=transparent`` + PNG; product/mock-up photos request
    JPEG. GPT Image always returns base64 image data.

    This class is registered as the ``openai`` provider; the pipeline never
    references it directly, so adding another provider is a one-line
    :func:`register_image_provider` call with no pipeline changes.
    """

    name = "openai"
    _SIZES = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}

    def __init__(self, api_key: str, *, base_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-image-1", quality: str = "high",
                 moderation: str = "auto", timeout: float = 180.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.quality = quality
        self.moderation = moderation
        self.timeout = timeout

    def generate(self, spec: ImageSpec) -> bytes:
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": (spec.prompt or spec.title)[:32000],
            "n": 1,
            "size": self._size(spec.width, spec.height),
            "quality": self.quality,
        }
        if spec.transparent:  # isolated print file — real transparency, PNG
            payload["background"] = "transparent"
            payload["output_format"] = "png"
        else:                 # commercial product photo — JPEG is lighter
            payload["output_format"] = "jpeg"
        if self.model == "gpt-image-1":
            payload["moderation"] = self.moderation
        resp = httpx.post(
            f"{self.base_url}/images/generations",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=self.timeout,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except (ValueError, KeyError):
                detail = resp.text
            raise RuntimeError(
                f"OpenAI image generation HTTP {resp.status_code}: {detail}")
        data = (resp.json().get("data") or [{}])[0]
        if data.get("b64_json"):
            return base64.b64decode(data["b64_json"])
        if data.get("url"):  # some compatible servers return a URL
            img = httpx.get(data["url"], timeout=self.timeout)
            img.raise_for_status()
            return img.content
        raise RuntimeError("OpenAI image backend returned no image data.")

    @classmethod
    def _size(cls, w: int, h: int) -> str:
        if abs(w - h) <= max(w, h) * 0.1:
            return cls._SIZES["square"]
        return cls._SIZES["portrait"] if h > w else cls._SIZES["landscape"]

    # Backwards-compatible alias.
    _nearest_size = _size


# Backwards-compatible name for the OpenAI-compatible production backend.
RemoteImageBackend = OpenAIImageBackend


# --- Provider registry (add providers without touching the pipeline) --

_PROVIDERS: dict[str, Any] = {}


def register_image_provider(name: str, factory: Any) -> None:
    """Register an image provider factory ``factory(cfg, api_key) -> ImageBackend``.

    New providers (Stability, Google, Replicate, …) register here and become
    selectable via ``image.provider`` with **no change to the pipeline**.
    """
    _PROVIDERS[name.lower()] = factory


def _openai_factory(cfg: dict[str, Any], api_key: str) -> ImageBackend:
    return OpenAIImageBackend(
        api_key,
        base_url=cfg.get("base_url", "https://api.openai.com/v1"),
        model=cfg.get("model", "gpt-image-1"),
        quality=cfg.get("quality", "high"),
        moderation=cfg.get("moderation", "auto"),
        timeout=float(cfg.get("timeout", 180.0)),
    )


register_image_provider("openai", _openai_factory)
register_image_provider("gpt-image-1", _openai_factory)


def _api_key(cfg: dict[str, Any]) -> str | None:
    import os

    return (cfg.get("api_key") or os.environ.get("IMAGE_API_KEY")
            or os.environ.get("OPENAI_API_KEY"))


def build_image_backend(config: Any) -> ImageBackend:
    """Select the image backend from config.

    ``image.backend``: ``auto`` (default) | ``local`` | ``remote``.
    ``image.provider``: the production provider (``openai`` by default).

    * ``local`` — always the built-in **development** renderer.
    * ``remote``/``auto`` — the configured provider when an API key is present
      (``image.api_key`` / ``IMAGE_API_KEY`` / ``OPENAI_API_KEY``). ``auto``
      falls back to the local renderer when no key is set; ``remote`` warns and
      falls back so the pipeline never crashes.

    The local renderer is **development-only**; production uses the provider.
    """
    cfg = getattr(config, "image", None) or {}
    backend = str(cfg.get("backend", "auto")).lower()
    provider = str(cfg.get("provider", "openai")).lower()
    api_key = _api_key(cfg)

    if backend == "local":
        return LocalRenderBackend()
    factory = _PROVIDERS.get(provider)
    if backend in ("remote", "auto") and api_key and factory:
        log.info("Image backend: %s (%s, quality=%s).",
                 provider, cfg.get("model", "gpt-image-1"), cfg.get("quality", "high"))
        return factory(cfg, api_key)
    if backend == "remote" or (backend == "auto" and api_key and not factory):
        log.warning("Production image backend unavailable (provider=%s, key=%s) — "
                    "falling back to the DEV local renderer.", provider, bool(api_key))
    return LocalRenderBackend()


# --- Upscaling (native model output -> print resolution) ------------

class Upscaler:
    """Turns an accepted image into print/target resolution — replaceable.

    Image models return a fixed native size (e.g. GPT Image = 1024²); the studio
    never re-requests a bigger image (that just burns API calls), it **upscales**
    the accepted master. The default resamples with Pillow; a real AI upscaler
    (Real-ESRGAN, Topaz, a Replicate model, …) can be registered and selected via
    ``image.upscaler`` with no pipeline change.
    """

    name = "base"

    def upscale(self, data: bytes, target_w: int, target_h: int,
                *, fmt: str = "JPEG") -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


class PillowUpscaler(Upscaler):
    """High-quality Lanczos resample + canonical re-encode (PNG keeps alpha)."""

    name = "pillow"

    def upscale(self, data: bytes, target_w: int, target_h: int,
                *, fmt: str = "JPEG") -> bytes:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
        if fmt.upper() == "PNG":
            if img.mode not in ("RGBA", "RGB"):
                img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if (img.width, img.height) != (target_w, target_h):
            img = img.resize((target_w, target_h), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt.upper() == "PNG":
            img.save(buf, format="PNG")
        else:
            img.save(buf, format="JPEG", quality=92)
        return buf.getvalue()


_UPSCALERS: dict[str, Any] = {}


def register_upscaler(name: str, factory: Any) -> None:
    """Register an upscaler factory ``factory(cfg) -> Upscaler`` (e.g. an AI
    upscaler provider), selectable via ``image.upscaler`` with no pipeline change."""
    _UPSCALERS[name.lower()] = factory


register_upscaler("pillow", lambda cfg: PillowUpscaler())


def build_upscaler(config: Any) -> Upscaler:
    """Select the upscaler from ``image.upscaler`` (default: pillow)."""
    cfg = getattr(config, "image", None) or {}
    name = str(cfg.get("upscaler", "pillow")).lower()
    factory = _UPSCALERS.get(name)
    if factory is None:
        log.warning("Unknown image.upscaler '%s' — using pillow.", name)
        factory = _UPSCALERS["pillow"]
    return factory(cfg)
