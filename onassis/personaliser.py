"""The Personaliser — buyer self-serve personalised products, Etsy-unlocked.

The model: a buyer purchases a digital listing on Etsy (Etsy brings the traffic
and takes the payment). Their download is a link into *this* app. They enter
their details (or upload a photo), see a live preview, and download the finished
high-resolution file — rendered by our engine, delivered instantly, no human step.

Two tiers of product, one funnel:

* **Tier 1 — computed / typographic (text in → poster out).** Deterministic,
  instant, £0 per order, perfect every time: place/coordinates posters, star
  maps, birth-stats prints, invitations.
* **Tier 2 — AI-from-a-photo (photo in → transformed out).** Pet portraits,
  Renaissance portraits, aged photographs. Stochastic, so the buyer is shown
  several variations and picks; costs image credit per order; hard-fails on a
  billing/quota problem rather than shipping junk.

Pure PIL for Tier 1; the configured image backend's ``edit`` for Tier 2.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from onassis.logger import get_logger

log = get_logger(__name__)

# Onassis palette — warm paper, deep ink, muted gold.
PAPER = (244, 241, 233)
INK = (18, 32, 58)
GOLD = (184, 146, 84)
SEA = (60, 96, 130)


@dataclass
class Product:
    key: str
    name: str
    tier: int                                  # 1 = computed, 2 = AI-from-photo
    blurb: str
    fields: list[dict[str, Any]] = field(default_factory=list)
    styles: list[dict[str, str]] = field(default_factory=list)   # tier 2
    price: float = 9.0


PRODUCTS: dict[str, Product] = {
    "place-poster": Product(
        "place-poster", "Personalised Place Poster", 1,
        "The place that matters — its name, coordinates and your date, in a clean "
        "Mediterranean typographic print.",
        fields=[
            {"key": "place", "label": "Place", "type": "text", "placeholder": "Positano, Italy", "required": True},
            {"key": "date", "label": "Date", "type": "date", "required": True},
            {"key": "names", "label": "Names", "type": "text", "placeholder": "Sam & Ellie"},
            {"key": "message", "label": "A line (optional)", "type": "text", "placeholder": "where it all began"},
        ], price=9.0),
    "star-map": Product(
        "star-map", "Custom Star Map", 1,
        "The real night sky over any place at any moment — a wedding, a birth, the "
        "night you met.",
        fields=[
            {"key": "place", "label": "Place", "type": "text", "placeholder": "London, United Kingdom", "required": True},
            {"key": "date", "label": "Date", "type": "date", "required": True},
            {"key": "time", "label": "Time", "type": "time", "default": "22:00"},
            {"key": "message", "label": "A line (optional)", "type": "text", "placeholder": "the night we said yes"},
        ], price=12.0),
    "birth-stats": Product(
        "birth-stats", "Birth Stats Print", 1,
        "A keepsake of the day they arrived — name, date, time, weight and length, "
        "beautifully typeset for the nursery.",
        fields=[
            {"key": "name", "label": "Baby's name", "type": "text", "placeholder": "Isla Rose", "required": True},
            {"key": "date", "label": "Born on", "type": "date", "required": True},
            {"key": "time", "label": "At", "type": "time"},
            {"key": "weight", "label": "Weight", "type": "text", "placeholder": "3.4 kg"},
            {"key": "length", "label": "Length", "type": "text", "placeholder": "51 cm"},
            {"key": "place", "label": "Place", "type": "text", "placeholder": "Bristol"},
        ], price=9.0),
    "invite": Product(
        "invite", "Wedding & Party Invitation", 1,
        "An elegant printable invitation with your names, date, venue and a personal "
        "line — print as many as you need.",
        fields=[
            {"key": "names", "label": "Names", "type": "text", "placeholder": "Sam & Ellie", "required": True},
            {"key": "event", "label": "Occasion", "type": "text", "placeholder": "invite you to their wedding", "default": "invite you to their wedding"},
            {"key": "date", "label": "Date", "type": "date", "required": True},
            {"key": "time", "label": "Time", "type": "time"},
            {"key": "venue", "label": "Venue", "type": "text", "placeholder": "Villa Cimbrone, Ravello"},
            {"key": "message", "label": "A line (optional)", "type": "text", "placeholder": "dinner, dancing & the sea"},
        ], price=9.0),
    "pet-portrait": Product(
        "pet-portrait", "Custom Pet Portrait", 2,
        "Upload a photo of your pet and get a painterly portrait of *them* — likeness, "
        "markings and all. Pick your favourite of three.",
        styles=[
            {"key": "oil", "label": "Oil painting",
             "prompt": "Transform this exact animal into a classical oil-on-canvas pet portrait. Preserve its precise likeness, markings, colouring and expression. Warm Mediterranean palette, soft studio light, gallery quality, no text."},
            {"key": "watercolour", "label": "Watercolour",
             "prompt": "Transform this exact animal into a loose, luminous watercolour portrait. Preserve its precise likeness, markings, colouring and expression. Soft washes, white paper edge, no text."},
            {"key": "royal", "label": "Royal portrait",
             "prompt": "Transform this exact animal into a regal 18th-century aristocratic portrait, wearing period finery. Preserve its precise facial likeness, markings and colouring. Rich oil painting, ornate but tasteful, no text."},
        ], price=18.0),
    "renaissance-portrait": Product(
        "renaissance-portrait", "Renaissance Portrait", 2,
        "You, painted like an old master. Upload a photo and choose your favourite "
        "of three.",
        styles=[
            {"key": "renaissance", "label": "Renaissance",
             "prompt": "Transform this exact person into a Renaissance-era oil painting portrait in the manner of an old master. Preserve their precise facial likeness and features. Period clothing, chiaroscuro light, aged canvas, no text."},
            {"key": "baroque", "label": "Baroque",
             "prompt": "Transform this exact person into a dramatic Baroque oil portrait. Preserve their precise facial likeness and features. Deep shadows, rich fabrics, gilded atmosphere, no text."},
            {"key": "impressionist", "label": "Impressionist",
             "prompt": "Transform this exact person into an Impressionist oil portrait. Preserve their precise facial likeness. Visible brushwork, soft natural light, no text."},
        ], price=18.0),
    "vintage-photo": Product(
        "vintage-photo", "Vintage Photograph", 2,
        "Your photo as an authentic aged photograph from another era — three "
        "variations to choose from.",
        styles=[
            {"key": "1900s", "label": "Early 1900s",
             "prompt": "Transform this exact photo into an authentic early-1900s sepia studio photograph. Preserve the subject's precise likeness. Period-appropriate styling, soft focus, film grain, aged paper texture, no text."},
            {"key": "1950s", "label": "1950s",
             "prompt": "Transform this exact photo into an authentic 1950s black-and-white photograph. Preserve the subject's precise likeness. Mid-century styling, silver-gelatin look, gentle grain, no text."},
            {"key": "1970s", "label": "1970s",
             "prompt": "Transform this exact photo into an authentic 1970s colour photograph. Preserve the subject's precise likeness. Faded Kodachrome tones, warm cast, slight grain, no text."},
        ], price=15.0),
}


# --- Shared drawing helpers -------------------------------------------

def _font(px: int, bold: bool = False):
    from PIL import ImageFont
    names = (("DejaVuSerif-Bold.ttf",) if bold else ()) + ("DejaVuSerif.ttf", "DejaVuSans.ttf")
    for n in names:
        try:
            return ImageFont.truetype(n, max(8, int(px)))
        except Exception:
            continue
    return ImageFont.load_default()


def _fit_font(draw, text: str, max_w: float, px: int, bold: bool = False):
    """Largest font at or below ``px`` that fits ``max_w``."""
    while px > 10:
        f = _font(px, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        px = int(px * 0.92)
    return _font(px, bold)


def _centre(draw, text: str, y: float, f, fill, W: float) -> float:
    w = draw.textlength(text, font=f)
    draw.text(((W - w) / 2, y), text, fill=fill, font=f)
    return y + f.size * 1.25


def _canvas(size: int, bg=PAPER):
    from PIL import Image, ImageDraw
    W, H = size, int(size * 1.4)
    img = Image.new("RGB", (W, H), bg)
    return img, ImageDraw.Draw(img), W, H


def _rule(draw, W, y, colour=GOLD, frac=0.12, width=2):
    draw.line([W / 2 - W * frac, y, W / 2 + W * frac, y], fill=colour, width=width)


def _stack(draw, items, W: float, H: float, centre_frac: float = 0.5) -> float:
    """Draw a vertical stack centred on the page, so a poster is composed rather
    than top-heavy. Items: ``("text", text, font, fill, gap)``, ``("rule", gap)``,
    or ``("custom", height, gap, fn)`` where ``fn(y)`` draws at ``y``."""
    rule_h = max(2, int(W // 400))
    total = 0.0
    for it in items:
        if it[0] == "text":
            total += it[2].size * 1.25 + it[4]
        elif it[0] == "rule":
            total += rule_h + it[1]
        else:
            total += it[1] + it[2]
    y = H * centre_frac - total / 2
    for it in items:
        if it[0] == "text":
            y = _centre(draw, it[1], y, it[2], it[3], W) + it[4]
        elif it[0] == "rule":
            _rule(draw, W, y + rule_h / 2)
            y += rule_h + it[1]
        else:
            it[3](y)
            y += it[1] + it[2]
    return y


def _fmt_date(s: str) -> str:
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d %B %Y")
    except Exception:
        return s or ""


def _coords(lat: float, lon: float) -> str:
    return (f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}   "
            f"{abs(lon):.4f}° {'E' if lon >= 0 else 'W'}")


def watermark(img):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    f = _font(int(img.width / 9), bold=True)
    t = "PREVIEW"
    w = d.textlength(t, font=f)
    d.text(((img.width - w) / 2, img.height * 0.44), t, fill=(206, 96, 96), font=f)
    return img


# --- Tier 1 renderers ------------------------------------------------

def render_place_poster(fields: dict[str, Any], size: int = 2000):
    img, d, W, H = _canvas(size)
    place = (fields.get("place") or "Somewhere").strip()
    lat, lon = float(fields.get("lat", 0)), float(fields.get("lon", 0))
    # Sun-and-sea motif: a gold disc sinking behind a horizon line.
    cx, cy, r = W / 2, H * 0.30, W * 0.13
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD)
    d.rectangle([0, cy + r * 0.25, W, H], fill=PAPER)          # horizon clips the sun
    d.line([W * 0.18, cy + r * 0.25, W * 0.82, cy + r * 0.25], fill=INK, width=max(2, W // 500))
    for i in range(3):                                          # sea lines
        y = cy + r * 0.25 + (i + 1) * W * 0.03
        d.line([W * (0.28 + i * 0.04), y, W * (0.72 - i * 0.04), y], fill=SEA,
               width=max(1, W // 700))
    y = H * 0.50
    f_place = _fit_font(d, place.upper(), W * 0.8, int(W / 9), bold=True)
    y = _centre(d, place.upper(), y, f_place, INK, W)
    _rule(d, W, y + W * 0.01)
    y += W * 0.045
    y = _centre(d, _coords(lat, lon), y, _font(int(W / 30)), SEA, W)
    if fields.get("date"):
        y = _centre(d, _fmt_date(fields["date"]), y, _font(int(W / 28)), INK, W)
    if fields.get("names"):
        y += W * 0.02
        y = _centre(d, fields["names"], y, _font(int(W / 22), bold=True), INK, W)
    if fields.get("message"):
        _centre(d, fields["message"], y, _font(int(W / 34)), SEA, W)
    return img


def render_star_map(fields: dict[str, Any], size: int = 2000):
    from onassis.starmap import render_star_map as _render
    dt = datetime.strptime(f"{fields.get('date')} {fields.get('time') or '22:00'}",
                           "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    lat, lon = float(fields.get("lat", 0)), float(fields.get("lon", 0))
    cap = [fields.get("place") or "Under this sky",
           dt.strftime("%d %B %Y · %H:%M"), _coords(lat, lon)]
    if fields.get("message"):
        cap.append(fields["message"])
    return _render(dt, lat, lon, size=size, caption=cap)


def render_birth_stats(fields: dict[str, Any], size: int = 2000):
    img, d, W, H = _canvas(size)
    name = (fields.get("name") or "Baby").strip()
    f_name = _fit_font(d, name, W * 0.82, int(W / 7), bold=True)
    born = _fmt_date(fields.get("date", ""))
    if fields.get("time"):
        born += f"  ·  {fields['time']}"
    items: list = [
        ("text", "welcome to the world", _font(int(W / 30)), SEA, W * 0.02),
        ("text", name, f_name, INK, W * 0.02),
        ("rule", W * 0.05),
        ("text", born, _font(int(W / 24)), INK, 0),
    ]
    if fields.get("place"):
        items.append(("text", fields["place"], _font(int(W / 30)), SEA, 0))
    stats = [(k, v) for k, v in (("weight", fields.get("weight")),
                                 ("length", fields.get("length"))) if v]
    if stats:
        fv, fk = _font(int(W / 14), bold=True), _font(int(W / 34))

        def _stats(y, stats=stats, fv=fv, fk=fk):
            col = W / (len(stats) + 1)
            for i, (k, v) in enumerate(stats, start=1):
                d.text((col * i - d.textlength(v, font=fv) / 2, y), v, fill=INK, font=fv)
                d.text((col * i - d.textlength(k, font=fk) / 2, y + fv.size * 1.25), k,
                       fill=SEA, font=fk)
        items.append(("custom", fv.size * 1.25 + fk.size * 1.25, W * 0.07, _stats))
    r = W * 0.03
    items.append(("custom", 2 * r, 0,
                  lambda y: d.ellipse([W / 2 - r, y, W / 2 + r, y + 2 * r], fill=GOLD)))
    _stack(d, items, W, H, centre_frac=0.48)
    return img


def render_invite(fields: dict[str, Any], size: int = 2000):
    img, d, W, H = _canvas(size)
    # Thin double border.
    m = W * 0.06
    d.rectangle([m, m, W - m, H - m], outline=GOLD, width=max(2, W // 400))
    m2 = m + W * 0.012
    d.rectangle([m2, m2, W - m2, H - m2], outline=GOLD, width=max(1, W // 900))
    names = (fields.get("names") or "").strip()
    f_names = _fit_font(d, names, W * 0.78, int(W / 9), bold=True)
    when = _fmt_date(fields.get("date", ""))
    if fields.get("time"):
        when += f"  ·  {fields['time']}"
    items: list = [
        ("text", "together with their families", _font(int(W / 32)), SEA, W * 0.02),
        ("text", names, f_names, INK, W * 0.01),
        ("text", fields.get("event") or "invite you to celebrate",
         _font(int(W / 26)), INK, W * 0.03),
        ("rule", W * 0.05),
        ("text", when, _font(int(W / 22), bold=True), INK, 0),
    ]
    if fields.get("venue"):
        items.append(("text", fields["venue"], _font(int(W / 28)), SEA, W * 0.04))
    if fields.get("message"):
        items.append(("text", fields["message"], _font(int(W / 32)), INK, 0))
    _stack(d, items, W, H, centre_frac=0.5)
    return img


_TIER1 = {
    "place-poster": render_place_poster, "star-map": render_star_map,
    "birth-stats": render_birth_stats, "invite": render_invite,
}


def render_tier1(key: str, fields: dict[str, Any], *, size: int = 2000,
                 preview: bool = False):
    """Render a Tier-1 product. ``preview`` → smaller + watermarked."""
    fn = _TIER1[key]
    img = fn(fields, size=size)
    return watermark(img) if preview else img


# --- Tier 2: AI-from-photo ---------------------------------------------

def transform_photo(config: Any, image_bytes: bytes, key: str, style: str,
                    *, n: int = 3) -> list[bytes]:
    """Turn a buyer's photo into ``n`` variations of the chosen style. Hard-fails
    on billing/quota/auth so junk is never delivered; records image spend."""
    import time as _time

    from onassis.ai_accounting import (_BLOCKING_KINDS, classify_ai_error,
                                       record_image)
    from onassis.connectors.image_backend import build_image_backend

    product = PRODUCTS[key]
    prompt = next((s["prompt"] for s in product.styles if s["key"] == style),
                  product.styles[0]["prompt"] if product.styles else "")
    backend = build_image_backend(config)
    if not hasattr(backend, "edit"):
        raise RuntimeError("AI image provider is not configured — set the OpenAI "
                           "image key to enable photo products.")
    started = _time.monotonic()
    try:
        out = backend.edit(image_bytes, prompt, n=n)
    except Exception as exc:
        record_image(provider=backend.name, model=getattr(backend, "model", ""),
                     images=0, duration_ms=int((_time.monotonic() - started) * 1000),
                     ok=False, detail=str(exc)[:200])
        alert = classify_ai_error(str(exc))
        if alert["kind"] in _BLOCKING_KINDS:
            raise RuntimeError(f"Image provider blocked ({alert['kind']}): "
                               f"{alert['message']}") from exc
        raise
    record_image(provider=backend.name, model=getattr(backend, "model", ""),
                 quality=getattr(backend, "quality", ""), images=len(out),
                 duration_ms=int((_time.monotonic() - started) * 1000), ok=True)
    return out


def upscale_for_print(image_bytes: bytes, target_long_edge: int = 2400) -> bytes:
    """Bring a model's native output up to a print-ready size."""
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    scale = target_long_edge / max(img.size)
    if scale > 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", dpi=(300, 300))
    return buf.getvalue()


# --- Access + geocoding ----------------------------------------------

def geocode(q: str) -> dict[str, Any] | None:
    """Place name → coordinates via OpenStreetMap Nominatim (free, no key)."""
    import httpx
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search",
                      params={"q": q, "format": "json", "limit": 1},
                      headers={"User-Agent": "OnassisPersonaliser/1.0"}, timeout=10)
        hits = r.json()
        if hits:
            h = hits[0]
            return {"lat": float(h["lat"]), "lon": float(h["lon"]),
                    "name": h.get("display_name", q)}
    except Exception:
        log.debug("geocode failed", exc_info=True)
    return None


def verify_order(config: Any, db: Any, order_ref: str) -> tuple[bool, str]:
    """Is this a real purchase? Etsy receipt id → look it up on the shop. Demo
    codes (``personaliser.demo_codes``) unlock without Etsy, for testing."""
    ref = (order_ref or "").strip()
    if not ref:
        return False, "Enter your Etsy order number."
    pcfg = dict(getattr(config, "personaliser", None) or {})
    demo = {str(c).strip().upper() for c in (pcfg.get("demo_codes") or ["DEMO"])}
    if ref.upper() in demo:
        return True, "demo"
    digits = "".join(ch for ch in ref if ch.isdigit())
    if not digits:
        return False, "That doesn't look like an Etsy order number."
    try:
        from onassis.etsy_automation import EtsyAutomationEngine
        etsy = EtsyAutomationEngine(config, db)
        if not etsy.is_configured:
            return False, "Etsy isn't connected on this shop yet."
        receipt = etsy.client.get_receipt(digits)
        if receipt and receipt.get("receipt_id"):
            return True, "etsy"
    except Exception as exc:
        log.info("order verify failed for %s: %s", digits, exc)
    return False, "We couldn't find that order. Check the number on your Etsy receipt."
