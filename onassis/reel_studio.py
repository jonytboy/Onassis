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

Audio: every clip carries an AAC audio stream (many publishers reject a video
with zero audio streams). If the operator drops **royalty-free** tracks into the
music folder (``REEL_MUSIC_DIR`` env, or ``assets/music/``) one is muxed in,
chosen deterministically per clip; otherwise a silent track is used. We never
ship copyrighted/trending audio — that is a takedown/strike risk — so the
operator supplies the music they are licensed to use.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

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


def _slide_frames(sl: "Slide", size: tuple[int, int]) -> Iterator[Image.Image]:
    """Yield one slide's frames lazily (Ken-Burns motion + fading text scrim)."""
    src = _load(sl.image, size)
    n = max(1, sl.frames)
    overlay = _text_overlay(size, sl.text, bold=sl.bold, y=sl.text_y,
                            caption=sl.caption)
    fade = max(1, int(n * 0.2))
    for i in range(n):
        f = _ken_burns(src, i, n, sl.pan, size)
        if i < fade:
            a = i / fade
        elif i >= n - fade:
            a = (n - 1 - i) / fade
        else:
            a = 1.0
        yield _apply_overlay(f, overlay, min(1.0, max(0.0, a)))


def _xfade_for(spec: ReelSpec) -> int:
    if len(spec.slides) < 2:
        return 0
    return max(0, min(spec.xfade_frames,
                      min(max(1, s.frames) for s in spec.slides) // 2))


def frame_count(spec: ReelSpec) -> int:
    """How many frames the reel will have — computed from the spec, so the
    renderer never has to hold them all just to count."""
    total = sum(max(1, s.frames) for s in spec.slides)
    return total - _xfade_for(spec) * max(0, len(spec.slides) - 1)


def iter_frames(spec: ReelSpec) -> Iterator[Image.Image]:
    """Stream a ReelSpec's 9:16 frames ONE AT A TIME (low memory — never holds
    the whole reel), with eased Ken-Burns motion, a fading text scrim, and short
    crossfades between slides. Buffers at most ~xfade frames at a boundary."""
    slides = spec.slides
    if not slides:
        return
    if len(slides) == 1:
        yield from _slide_frames(slides[0], spec.size)
        return
    xf = _xfade_for(spec)
    prev_tail: list[Image.Image] | None = None
    for idx, sl in enumerate(slides):
        n = max(1, sl.frames)
        last = idx == len(slides) - 1
        tail: list[Image.Image] = []
        for i, frame in enumerate(_slide_frames(sl, spec.size)):
            if prev_tail is not None and i < xf:      # crossfade in from prev slide
                yield Image.blend(prev_tail[i], frame, (i + 1) / (xf + 1))
            elif not last and i >= n - xf:            # hold the tail for next slide
                tail.append(frame)
            else:
                yield frame
        prev_tail = None if last else tail


def compose_frames(spec: ReelSpec) -> list[Image.Image]:
    """Materialise every frame (backwards-compatible helper). Prefer
    :func:`iter_frames` for rendering — it streams and stays low-memory."""
    return list(iter_frames(spec))


_MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}


def _music_dir() -> Path:
    """Where the operator drops royalty-free tracks (env override, else assets/music)."""
    env = os.environ.get("REEL_MUSIC_DIR")
    if env:
        return Path(env)
    try:
        from onassis.config import ROOT_DIR
        return ROOT_DIR / "assets" / "music"
    except Exception:
        return Path("assets/music")


def _pick_music(out_path: str) -> str | None:
    """Pick one royalty-free track for this clip, or None for a silent track.
    Deterministic in the output path so re-rendering a clip keeps the same music."""
    try:
        tracks = sorted(str(p) for p in _music_dir().glob("*")
                        if p.suffix.lower() in _MUSIC_EXTS)
    except Exception:
        tracks = []
    if not tracks:
        return None
    idx = int(hashlib.sha256(out_path.encode("utf-8")).hexdigest(), 16) % len(tracks)
    return tracks[idx]


def _ffmpeg_encode(frames: list[Image.Image], out_path: str, *, fps: int) -> str:
    """Default encoder: frames → H.264 mp4 via ffmpeg.

    The output is normalised to what every social platform (TikTok, Instagram
    Reels, Buffer, Facebook) expects: H.264 High profile, ``yuv420p``, a constant
    frame rate, ``+faststart`` for progressive download — and a **silent AAC audio
    track**. That last one matters: TikTok/Buffer routinely reject a video with
    *zero* audio streams, so we always mux a silent track rather than ship
    audio-less MP4s that fail validation downstream."""
    with tempfile.TemporaryDirectory() as tmp:
        for i, fr in enumerate(frames):
            fr.save(os.path.join(tmp, f"f{i:05d}.png"))
        music = _pick_music(out_path)
        if music:
            # Loop the track to cover the video, trimmed to length by -shortest.
            audio_in = ["-stream_loop", "-1", "-i", music]
        else:
            # A silent stereo track so the MP4 always carries an audio stream.
            audio_in = ["-f", "lavfi", "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=48000"]
        cmd = [
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", os.path.join(tmp, "f%05d.png"),
            *audio_in,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart", out_path,
        ]
        try:
            # Hard timeout so a wedged ffmpeg can never hang the whole build for
            # hours — one clip failing is caught upstream and simply skipped.
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        except FileNotFoundError as exc:
            raise ReelError("ffmpeg is not installed — install it to render video "
                            "(apt-get install ffmpeg).") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReelError("ffmpeg timed out (>120s) — skipping this clip.") from exc
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
        # Stream frames to the encoder (never hold the whole reel in memory).
        n_frames = frame_count(spec)
        self._encode(iter_frames(spec), out_path, fps=spec.fps)
        duration = round(n_frames / spec.fps, 1)
        return {
            "path": out_path, "fmt": spec.fmt, "caption": spec.caption,
            "hashtags": list(spec.hashtags), "sound": spec.sound,
            "duration_s": duration, "frames": n_frames,
            "product_key": spec.product_key, "campaign_id": spec.campaign_id,
            "listing_url": spec.listing_url, "size": list(spec.size),
        }
