"""Framed-on-a-wall mockups — the listing image that actually sells a print.

Takes a finished artwork and composes it as a framed print hanging on a softly
lit wall, with a mat, a dark frame and a real drop shadow. Pure PIL. Also builds
a before→after strip for the photo products (the buyer's photo beside the
finished portrait), so a shopper sees the *result*, not a description of it.
"""

from __future__ import annotations

from typing import Any

WALL = (226, 221, 211)
FRAME = (36, 32, 29)
MAT = (247, 245, 239)


def frame_on_wall(art: Any, *, size: int = 1600, art_frac: float = 0.62,
                  wall=WALL, frame=FRAME, mat=MAT):
    """Return an RGB ``size``×``size`` image of ``art`` framed on a wall."""
    from PIL import Image, ImageDraw, ImageFilter

    W = H = size
    bg = Image.new("RGB", (W, H), wall)
    d = ImageDraw.Draw(bg)
    # Soft vertical light gradient on the wall + a skirting line near the floor.
    for y in range(H):
        t = y / H
        c = tuple(int(wall[i] * (1.0 - 0.10 * t)) for i in range(3))
        d.line([0, y, W, y], fill=c)
    d.line([0, int(H * 0.93), W, int(H * 0.93)], fill=tuple(int(v * 0.86) for v in wall),
           width=max(2, W // 400))

    # Size the art (keep its aspect), then mat + frame around it.
    art = art.convert("RGB")
    ah = int(H * art_frac)
    aw = int(art.width * ah / art.height)
    if aw > W * 0.72:
        aw = int(W * 0.72)
        ah = int(art.height * aw / art.width)
    art = art.resize((aw, ah), Image.LANCZOS)
    mat_w = int(size * 0.035)
    frame_w = int(size * 0.014)
    fw, fh = aw + 2 * (mat_w + frame_w), ah + 2 * (mat_w + frame_w)
    fx, fy = (W - fw) // 2, int(H * 0.44 - fh / 2)

    # Drop shadow: blurred dark rectangle, offset down-right.
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = int(size * 0.012)
    sd.rectangle([fx + off, fy + off * 2, fx + fw + off, fy + fh + off * 2],
                 fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(size * 0.02)))
    bg = Image.alpha_composite(bg.convert("RGBA"), shadow).convert("RGB")
    d = ImageDraw.Draw(bg)

    d.rectangle([fx, fy, fx + fw, fy + fh], fill=frame)
    d.rectangle([fx + frame_w, fy + frame_w, fx + fw - frame_w, fy + fh - frame_w], fill=mat)
    bg.paste(art, (fx + frame_w + mat_w, fy + frame_w + mat_w))
    return bg


def stationery_flatlay(card: Any, *, size: int = 1600, linen=(236, 231, 221),
                       kraft=(199, 178, 148), flap=(184, 162, 132)):
    """A card lying on linen with an envelope behind it — the stationery mockup
    (an invitation is a card, not wall art). Pure PIL fallback for when the
    photographic scene isn't available."""
    from PIL import Image, ImageDraw, ImageFilter

    W = H = size
    bg = Image.new("RGB", (W, H), linen)
    d = ImageDraw.Draw(bg)
    for y in range(0, H, 3):                                   # faint weave
        d.line([0, y, W, y], fill=tuple(int(v * 0.985) for v in linen))
    # Envelope (behind, offset down-right), with a flap.
    ex, ey, ew, eh = int(W * 0.30), int(H * 0.36), int(W * 0.50), int(H * 0.36)
    d.rectangle([ex, ey, ex + ew, ey + eh], fill=kraft)
    d.polygon([(ex, ey), (ex + ew, ey), (ex + ew / 2, ey + eh * 0.55)], fill=flap)
    # Card, slightly rotated, with a soft shadow.
    card = card.convert("RGB")
    ch = int(H * 0.60)
    cw = int(card.width * ch / card.height)
    card = card.resize((cw, ch), Image.LANCZOS)
    rot = card.rotate(-5, expand=True, fillcolor=(0, 0, 0))
    mask = Image.new("L", card.size, 255).rotate(-5, expand=True)
    cx, cy = int(W * 0.24), int(H * 0.16)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh = Image.new("RGBA", rot.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, 95), (0, 0), mask)
    shadow.paste(sh, (cx + int(size * 0.012), cy + int(size * 0.02)), sh)
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(size * 0.018)))
    bg = Image.alpha_composite(bg.convert("RGBA"), shadow).convert("RGB")
    bg.paste(rot, (cx, cy), mask)
    # A gold sprig in the corner.
    d = ImageDraw.Draw(bg)
    gx, gy = int(W * 0.80), int(H * 0.84)
    d.line([gx - W * 0.10, gy, gx, gy - H * 0.06], fill=(184, 146, 84), width=max(2, W // 500))
    for t in (0.25, 0.5, 0.75):
        px, py = gx - W * 0.10 * (1 - t), gy - H * 0.06 * t
        d.ellipse([px - W * 0.012, py - H * 0.006, px + W * 0.012, py + H * 0.006],
                  fill=(184, 146, 84))
    return bg


def before_after(photo: Any, result: Any, *, size: int = 1600,
                 labels=("your photo", "your portrait")):
    """A side-by-side strip: the original photo next to the finished portrait."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = size, int(size * 0.62)
    out = Image.new("RGB", (W, H), MAT)
    d = ImageDraw.Draw(out)
    pane_w = int(W * 0.46)
    pane_h = int(H * 0.78)
    gap = (W - 2 * pane_w) // 3
    top = int(H * 0.06)
    try:
        f = ImageFont.truetype("DejaVuSerif.ttf", int(size / 34))
    except Exception:
        f = ImageFont.load_default()
    for i, (img, label) in enumerate(((photo, labels[0]), (result, labels[1]))):
        img = img.convert("RGB").copy()
        img.thumbnail((pane_w, pane_h), Image.LANCZOS)
        x = gap + i * (pane_w + gap) + (pane_w - img.width) // 2
        y = top + (pane_h - img.height) // 2
        out.paste(img, (x, y))
        tw = d.textlength(label, font=f)
        d.text((gap + i * (pane_w + gap) + (pane_w - tw) / 2, top + pane_h + int(H * 0.03)),
               label, fill=(60, 96, 130), font=f)
    # Arrow between panes.
    ax = gap + pane_w + gap // 2
    ay = top + pane_h // 2
    d.text((ax - int(size / 60), ay - int(size / 40)), "→", fill=(184, 146, 84),
           font=ImageFont.load_default() if f is None else f)
    return out
