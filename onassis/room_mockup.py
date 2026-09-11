"""Photographic room mockups — the art hung in a real-looking styled room.

A flat frame on a beige field reads as synthetic; what sells on Etsy is a
*photograph* of a styled room with the print on the wall. We get that without
any hand-made template: the image model generates a room scene ONCE with an
empty frame whose inside is a flat magenta rectangle (a colour that never occurs
naturally), we find that rectangle by colour, and paste each product's artwork
into it. The template is cached per scene, so it costs one generation ever and
every product reuses it. If the model doesn't give a clean magenta area, the
caller falls back to the drawn frame-on-wall.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SCENES: dict[str, str] = {
    "living": (
        "Interior photograph of a bright, stylish Mediterranean living room: white "
        "plaster wall, a low oak sideboard with a ceramic vase and an olive branch, "
        "soft natural daylight from the left. On the wall hangs ONE large portrait-"
        "orientation picture frame with a thin black edge and a white mat, seen "
        "perfectly straight-on. The ENTIRE inside of the mat is a flat, solid, "
        "bright magenta (#FF00FF) rectangle with no texture, no reflections and no "
        "content. Realistic photo, no text, no people."
    ),
    "stationery": (
        "Top-down flat-lay photograph of a wedding stationery suite on natural "
        "linen: a kraft paper envelope, a sprig of olive, a small gold wax seal, "
        "soft diffused daylight. ONE portrait-orientation invitation card lies "
        "perfectly flat and square to the camera in the centre. The ENTIRE face of "
        "the card is a flat, solid, bright magenta (#FF00FF) rectangle with no "
        "texture, no shadow and no content. Realistic photo, no text, no people."
    ),
    "nursery": (
        "Interior photograph of a calm, sunlit nursery: pale wall, a wooden crib "
        "edge, a soft rug, a small plant, gentle daylight. On the wall hangs ONE "
        "portrait-orientation picture frame with a thin natural-wood edge and a "
        "white mat, seen perfectly straight-on. The ENTIRE inside of the mat is a "
        "flat, solid, bright magenta (#FF00FF) rectangle with no texture or "
        "content. Realistic photo, no text, no people."
    ),
}


def _magenta_mask(img):
    from PIL import ImageChops
    r, g, b = img.convert("RGB").split()
    hi_r = r.point(lambda v: 255 if v > 180 else 0)
    hi_b = b.point(lambda v: 255 if v > 180 else 0)
    lo_g = g.point(lambda v: 255 if v < 110 else 0)
    return ImageChops.multiply(ImageChops.multiply(hi_r, hi_b), lo_g)


def find_frame_box(img, *, min_area_frac: float = 0.02,
                   min_fill: float = 0.55) -> tuple[int, int, int, int] | None:
    """Bounding box of the magenta placeholder, or None if there isn't a clean
    one (too small, or not rectangular enough)."""
    mask = _magenta_mask(img)
    box = mask.getbbox()
    if not box:
        return None
    x0, y0, x1, y1 = box
    area = (x1 - x0) * (y1 - y0)
    if area < min_area_frac * img.width * img.height:
        return None
    filled = mask.crop(box).histogram()[255]
    if filled / max(1, area) < min_fill:
        return None
    return box


def composite(template, art, box: tuple[int, int, int, int]):
    """Paste ``art`` into ``box`` (contain-fit, centred, on white)."""
    from PIL import Image
    out = template.convert("RGB").copy()
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    art = art.convert("RGB")
    scale = min(bw / art.width, bh / art.height)
    aw, ah = max(1, int(art.width * scale)), max(1, int(art.height * scale))
    fitted = art.resize((aw, ah), Image.LANCZOS)
    out.paste((248, 246, 240), (x0, y0, x1, y1))              # mat-white fill
    out.paste(fitted, (x0 + (bw - aw) // 2, y0 + (bh - ah) // 2))
    return out


def room_template(backend: Any, cache_dir: Path, scene: str = "living",
                  size: tuple[int, int] = (1536, 1024)) -> Path | None:
    """The cached scene image for ``scene``; generated once via the image model.
    Returns None if the backend can't produce photos (dev renderer) or fails."""
    from onassis.connectors.image_backend import ImageSpec
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / f"room-{scene}.png"
    if p.exists():
        return p
    if getattr(backend, "name", "local") == "local" or not hasattr(backend, "generate"):
        return None
    try:
        data = backend.generate(ImageSpec(kind="MOCKUP", width=size[0], height=size[1],
                                          prompt=SCENES.get(scene, SCENES["living"])))
        p.write_bytes(data)
        return p
    except Exception:
        return None


def room_mockup(backend: Any, art, cache_dir: Path, scene: str = "living"):
    """``art`` hung in a photographic room, or None if no usable template."""
    from PIL import Image
    p = room_template(backend, cache_dir, scene)
    if not p:
        return None
    template = Image.open(p)
    box = find_frame_box(template)
    if not box:
        return None
    return composite(template, art, box)
