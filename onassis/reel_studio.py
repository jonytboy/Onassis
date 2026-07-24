"""The Reel Studio (Sprint 48) — short-form video for TikTok / Reels.

ONASSIS produced only static images; TikTok and Reels are video-first. This turns
a product's existing assets (the diverse artwork + lifestyle mockups) into
vertical 9:16 short-form video: aesthetic "style slides" with the product woven
in, text hooks, a caption and hashtags — the format an operator approved.

Two clean layers, so it is fully offline-testable:

* :func:`compose_frames` — pure Pillow. Turns a :class:`ReelSpec` (ordered slides
  + text) into a list of 9:16 frames with Ken-Burns motion and text overlays. No
  ffmpeg, no network — the whole visual is testable.
* :class:`ReelStudio` — assembles frames into an ``.mp4`` via a replaceable
  ``encoder`` (default: ffmpeg). Tests inject a stub encoder, so no ffmpeg is
  needed to test the pipeline.

Audio is deliberately NOT burned in (attaching copyrighted trending sound
programmatically is a takedown/strike risk) — clips render silent and the
suggested sound rides along in the content package, added natively when posted.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from onassis.logger import get_logger

log = get_logger(__name__)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Georgia.ttf",
]
_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int, *, bold: bool = False):
    for path in (_FONT_BOLD_CANDIDATES if bold else []) + _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


@dataclass
class Slide:
    """One scene: an image (path or PIL), an optional on-screen hook, and motion."""
    image: Any = None                       # path str | Path | PIL.Image | None
    text: str = ""
    caption: str | None = None
    pan: tuple = (0.0, 0.15, 0.25, 0.0)     # (x0,y0)->(x1,y1) fractional pan
    frames: int = 60                        # ~2s/slide at 30fps (readable, not rushed)
    bold: bool = True
    text_y: float = 0.5


@dataclass
class ReelSpec:
    slides: list[Slide]
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    fmt: str = "style_slide"
    sound: str = ""
    product_key: str | None = None
    campaign_id: int | None = None
    listing_url: str | None = None
    size: tuple = (1080, 1920)              # production 9:16
    fps: int = 30
    xfade_frames: int = 10                  # crossfade between slides (~0.33s)


def _load(image: Any, size: tuple) -> Image.Image:
    """Load a source scene, cover-cropped to a 2× canvas for Ken-Burns headroom."""
    w, h = size
    sw, sh = w * 2, h * 2
    img: Image.Image | None = None
    try:
        if isinstance(image, Image.Image):
            img = image.convert("RGB")
        elif image and Path(str(image)).exists():
            img = Image.open(str(image)).convert("RGB")
    except Exception:  # unreadable asset → fall back to a tasteful ground
        img = None
    if img is None:
        return _gradient((sw, sh), (238, 232, 222), (214, 198, 178))
    # cover: scale to fill sw×sh, centre-crop
    scale = max(sw / img.width, sh / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                     Image.LANCZOS)
    x = (img.width - sw) // 2
    y = (img.height - sh) // 2
    return img.crop((x, y, x + sw, y + sh))


def _gradient(size: tuple, top: tuple, bot: tuple) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, top)
    d = ImageDraw.Draw(img)
    for yy in range(h):
        t = yy / max(1, h - 1)
        d.line([(0, yy), (w, yy)], fill=tuple(int(_lerp(top[i], bot[i], t)) for i in range(3)))
    return img


def _wrap(draw, text: str, fnt, maxw: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _ease(t: float) -> float:
    """Smoothstep ease-in-out — cinematic motion, not a linear slide."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _text_overlay(size: tuple, text: str, *, bold: bool, y: float,
                  caption: str | None) -> Image.Image:
    """A transparent RGBA layer with the hook text on a soft scrim (for
    readability over any photo) + an optional caption. Composited per-frame with
    a fade so it reads cleanly and never blends messily across a cut."""
    W, H = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    if text:
        fnt = _font(int(W * 0.082), bold=bold)
        d0 = ImageDraw.Draw(layer)
        lines = _wrap(d0, text, fnt, int(W * 0.82))
        lh = int(W * 0.082) + 16
        block = lh * len(lines)
        cy = int(H * y)
        pad = int(W * 0.07)
        scrim = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(scrim).rectangle(
            [-40, cy - block // 2 - pad, W + 40, cy + block // 2 + pad],
            fill=(15, 18, 22, 120))
        scrim = scrim.filter(ImageFilter.GaussianBlur(38))
        layer = Image.alpha_composite(layer, scrim)
        d = ImageDraw.Draw(layer)
        yy = cy - block // 2
        for ln in lines:
            tw = d.textlength(ln, font=fnt)
            d.text(((W - tw) / 2, yy), ln, font=fnt, fill=(255, 255, 255, 255))
            yy += lh
    if caption:
        cf = _font(int(W * 0.04))
        d = ImageDraw.Draw(layer)
        cw = d.textlength(caption, font=cf)
        d.text(((W - cw) / 2, int(H * 0.9)), caption, font=cf, fill=(255, 255, 255, 235))
    return layer


def _apply_overlay(frame: Image.Image, overlay: Image.Image, alpha: float) -> Image.Image:
    if alpha <= 0:
        return frame
    ov = overlay
    if alpha < 1:
        a = ov.split()[3].point(lambda p: int(p * alpha))
        ov = Image.merge("RGBA", (*ov.split()[:3], a))
    return Image.alpha_composite(frame.convert("RGBA"), ov).convert("RGB")


def _ken_burns(src: Image.Image, i: int, n: int, pan: tuple, out: tuple) -> Image.Image:
    W, H = out
    sw, sh = src.size
    t = _ease(i / max(1, n - 1))
    s = _lerp(0.96, 0.86, t)                          # gentle, slow zoom in
    cw = min(int(sw * s), sw)
    ch = min(int(cw * H / W), sh)
    x0, y0, x1, y1 = pan
    x = int(_lerp(x0, x1, t) * (sw - cw))
    y = int(_lerp(y0, y1, t) * (sh - ch))
    return src.crop((x, y, x + cw, y + ch)).resize((W, H), Image.LANCZOS)


def _blend_tail(a: list[Image.Image], b: list[Image.Image], n: int) -> list[Image.Image]:
    """Crossfade the last ``n`` frames of ``a`` into the first ``n`` of ``b``."""
    if n <= 0 or n >= len(a) or n >= len(b):
        return a + b
    mid = [Image.blend(a[len(a) - n + i], b[i], (i + 1) / (n + 1)) for i in range(n)]
    return a[:-n] + mid + b[n:]


def compose_frames(spec: ReelSpec) -> list[Image.Image]:
    """Render a ReelSpec into 9:16 frames (pure Pillow — no ffmpeg).

    Eased Ken-Burns motion, a readable text scrim that fades in/out per slide,
    and short crossfades between slides — so it feels produced, not a rushed
    slideshow."""
    slides: list[list[Image.Image]] = []
    for sl in spec.slides:
        src = _load(sl.image, spec.size)
        n = max(1, sl.frames)
        overlay = _text_overlay(spec.size, sl.text, bold=sl.bold, y=sl.text_y,
                                caption=sl.caption)
        fade = max(1, int(n * 0.2))                   # text fades in and out
        frames: list[Image.Image] = []
        for i in range(n):
            f = _ken_burns(src, i, n, sl.pan, spec.size)
            if i < fade:
                a = i / fade
            elif i >= n - fade:
                a = (n - 1 - i) / fade
            else:
                a = 1.0
            frames.append(_apply_overlay(f, overlay, min(1.0, max(0.0, a))))
        slides.append(frames)
    if len(slides) == 1:
        return slides[0]
    xf = max(0, min(spec.xfade_frames, min(len(s) for s in slides) // 2))
    out = list(slides[0])
    for nxt in slides[1:]:
        out = _blend_tail(out, nxt, xf)
    return out


def _ffmpeg_encode(frames: list[Image.Image], out_path: str, *, fps: int) -> str:
    """Default encoder: frames → H.264 mp4 (silent) via ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        for i, fr in enumerate(frames):
            fr.save(os.path.join(tmp, f"f{i:05d}.png"))
        cmd = [
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", os.path.join(tmp, "f%05d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError as exc:
            raise ReelError("ffmpeg is not installed — install it to render video "
                            "(apt-get install ffmpeg).") from exc
        except subprocess.CalledProcessError as exc:
            raise ReelError(f"ffmpeg failed: {exc.stderr[-300:].decode(errors='ignore')}") \
                from exc
    return out_path


class ReelError(RuntimeError):
    """Raised when a reel cannot be rendered."""


class ReelStudio:
    """Renders a ReelSpec to an mp4 via a replaceable encoder (default ffmpeg)."""

    def __init__(self, encoder: Callable[..., str] | None = None) -> None:
        self._encode = encoder or _ffmpeg_encode

    def render(self, spec: ReelSpec, out_path: str | Path) -> dict[str, Any]:
        out_path = str(out_path)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        frames = compose_frames(spec)
        self._encode(frames, out_path, fps=spec.fps)
        duration = round(len(frames) / spec.fps, 1)
        return {
            "path": out_path, "fmt": spec.fmt, "caption": spec.caption,
            "hashtags": list(spec.hashtags), "sound": spec.sound,
            "duration_s": duration, "frames": len(frames),
            "product_key": spec.product_key, "campaign_id": spec.campaign_id,
            "listing_url": spec.listing_url, "size": list(spec.size),
        }
