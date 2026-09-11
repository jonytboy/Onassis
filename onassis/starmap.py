"""Personalised star-map generator — the night sky for a date, time and place.

Give it *when* (UTC) and *where* (lat/lon) and it computes the real positions of
the stars overhead at that moment and renders a print-ready poster: ~5,000 stars
to magnitude 6 plus the constellation figures, projected onto the visible dome.
This is the computed, personalised product the machine does better than a human —
every order is a unique, accurate sky (a wedding night, a birth, a first date).

Pure-Python astronomy (no numpy) + PIL rendering (no matplotlib), supersampled
for smooth stars and lines. Star/constellation data ships in ``onassis/data``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"

# Small built-in bright-star fallback (J2000 ra°, dec°, mag) if data is missing.
_BRIGHT_STARS: list[tuple[float, float, float]] = [
    (101.287, -16.716, -1.46), (279.234, 38.784, 0.03), (78.634, -8.202, 0.13),
    (88.793, 7.407, 0.42), (213.915, 19.182, -0.05), (297.696, 8.868, 0.77),
    (37.954, 89.264, 1.98), (165.932, 61.751, 1.79), (85.190, -1.943, 1.77),
    (84.053, -1.202, 1.69), (83.002, -0.299, 2.23),
]


@dataclass
class SkyStar:
    ra_deg: float
    dec_deg: float
    mag: float


# --- Astronomy ---------------------------------------------------------------

def julian_date(dt: datetime) -> float:
    """Julian Date for a UTC datetime (Gregorian calendar)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    day_frac = (dt.hour + dt.minute / 60 + dt.second / 3600) / 24
    return (int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + dt.day
            + day_frac + b - 1524.5)


def local_sidereal_time(dt: datetime, lon_deg: float) -> float:
    """Local sidereal time in degrees (0–360). ``lon_deg`` east-positive."""
    d = julian_date(dt) - 2451545.0
    t = d / 36525.0
    gmst = (280.46061837 + 360.98564736629 * d
            + 0.000387933 * t * t - (t * t * t) / 38710000.0)
    return (gmst + lon_deg) % 360.0


def equatorial_to_altaz(ra_deg: float, dec_deg: float, lst_deg: float,
                        lat_deg: float) -> tuple[float, float]:
    """(RA, Dec) → (altitude, azimuth) in degrees for an observer at ``lat_deg``
    with local sidereal time ``lst_deg``. Azimuth from North, increasing east."""
    ha = math.radians((lst_deg - ra_deg) % 360.0)
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)
    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)
    cos_alt = math.cos(alt)
    if cos_alt == 0:
        return math.degrees(alt), 0.0
    cos_az = (math.sin(dec) - math.sin(alt) * math.sin(lat)) / (cos_alt * math.cos(lat))
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.degrees(math.acos(cos_az))
    if math.sin(ha) > 0:
        az = 360.0 - az
    return math.degrees(alt), az


# --- Data --------------------------------------------------------------------

def load_stars(path: str | None = None) -> list[SkyStar]:
    """Load the star catalogue (GeoJSON: coordinates ``[ra°, dec°]``, ``mag``).
    Defaults to the bundled ``stars.6.json``; falls back to the built-in set."""
    p = Path(path) if path else _DATA_DIR / "stars.6.json"
    try:
        data = json.loads(p.read_text())
        out: list[SkyStar] = []
        for f in data.get("features", []):
            ra, dec = f["geometry"]["coordinates"]
            mag = float(f["properties"].get("mag", 99))
            out.append(SkyStar(float(ra) % 360.0, float(dec), mag))
        if out:
            return out
    except Exception:
        pass
    return [SkyStar(ra, dec, mag) for ra, dec, mag in _BRIGHT_STARS]


def load_constellation_lines(path: str | None = None) -> list[list[tuple[float, float]]]:
    """Load constellation stick-figures as a list of polylines of ``(ra°, dec°)``."""
    p = Path(path) if path else _DATA_DIR / "constellations.lines.json"
    lines: list[list[tuple[float, float]]] = []
    try:
        data = json.loads(p.read_text())
        for f in data.get("features", []):
            geom = f.get("geometry", {})
            for seg in geom.get("coordinates", []):
                lines.append([(float(pt[0]) % 360.0, float(pt[1])) for pt in seg])
    except Exception:
        return []
    return lines


# --- Rendering ---------------------------------------------------------------

def render_star_map(dt: datetime, lat: float, lon: float, *, size: int = 2000,
                    caption: list[str] | None = None, constellations: bool = True,
                    supersample: int = 2,
                    paper=(244, 241, 233), sky=(11, 19, 36),
                    star=(247, 246, 240), line=(90, 106, 140)):
    """Render the sky over (lat, lon) at UTC ``dt`` as a print-ready poster
    (PIL Image). ``caption`` lines (place, date, coordinates, words) go beneath
    the disc. Supersampled then downscaled for smooth stars and lines."""
    from PIL import Image, ImageDraw

    ss = max(1, int(supersample))
    W, H = size * ss, int(size * 1.4) * ss
    img = Image.new("RGB", (W, H), paper)
    draw = ImageDraw.Draw(img)

    margin = int(W * 0.075)
    R = (W - 2 * margin) / 2
    cx, cy = W / 2, margin + R
    draw.ellipse([cx - R, cy - R, cx + R, cy + R], fill=sky)

    lst = local_sidereal_time(dt, lon)
    la = lat

    def project(ra, dec):
        alt, az = equatorial_to_altaz(ra, dec, lst, la)
        if alt < -2:
            return None
        r = (90.0 - alt) / 90.0 * R
        a = math.radians(az)
        # Looking UP: North at top, East to the left (sky is mirrored vs a map).
        return (cx - r * math.sin(a), cy - r * math.cos(a), alt)

    # Constellation lines first (under the stars), only segments fully up.
    if constellations:
        lw = max(1, int(W / 1400))
        for poly in load_constellation_lines():
            prev = None
            for ra, dec in poly:
                cur = project(ra, dec)
                if prev and cur and prev[2] > 0 and cur[2] > 0:
                    draw.line([prev[0], prev[1], cur[0], cur[1]], fill=line, width=lw)
                prev = cur

    # Stars, brightest largest; the brightest get a soft halo.
    for s in load_stars():
        pr = project(s.ra_deg, s.dec_deg)
        if not pr or pr[2] < 0:
            continue
        x, y, _ = pr
        rad = max(0.5 * ss, (6.3 - s.mag) * 0.42 * ss)
        if s.mag < 1.6:                                  # bright-star glow
            halo = rad * 2.6
            draw.ellipse([x - halo, y - halo, x + halo, y + halo],
                         fill=_blend(sky, star, 0.18))
        draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=star)

    # Double border ring.
    rw = max(1, int(W / 900))
    draw.ellipse([cx - R, cy - R, cx + R, cy + R], outline=star, width=rw)
    inset = R * 0.965
    draw.ellipse([cx - inset, cy - inset, cx + inset, cy + inset],
                 outline=_blend(paper, star, 0.5), width=max(1, rw // 2))

    if caption:
        _draw_caption(draw, caption, W, cy + R, H, sky, star)

    if ss > 1:
        img = img.resize((size, int(size * 1.4)), Image.LANCZOS)
    return img


def _blend(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _draw_caption(draw, lines, W, top, H, sky, accent):
    from PIL import ImageFont

    def font(px, bold=False):
        # Prefer the bundled Cormorant Garamond (OFL) for a proper display serif.
        bundled = Path(__file__).parent / "data" / "fonts" / "CormorantGaramond-Variable.ttf"
        try:
            f = ImageFont.truetype(str(bundled), int(px * 1.18))
            try:
                f.set_variation_by_name("SemiBold" if bold else "Medium")
            except Exception:
                pass
            return f
        except Exception:
            pass
        for name in (("DejaVuSerif-Bold.ttf",) if bold else ()) + (
                "DejaVuSerif.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, px)
            except Exception:
                continue
        return ImageFont.load_default()

    area_top = top + (H - top) * 0.16
    title = font(int(W / 20), bold=True)
    sub = font(int(W / 40))
    # A thin divider above the title.
    dw = W * 0.12
    draw.line([W / 2 - dw, area_top - int(W / 30), W / 2 + dw, area_top - int(W / 30)],
              fill=_blend(sky, accent, 0.35), width=max(1, int(W / 1400)))
    y = area_top
    for i, ln in enumerate(lines):
        f = title if i == 0 else sub
        w = draw.textlength(ln, font=f)
        draw.text(((W - w) / 2, y), ln, fill=sky, font=f)
        y += int(W / 16) if i == 0 else int(W / 26)
