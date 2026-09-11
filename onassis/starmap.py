"""Personalised star-map generator — the night sky for a date, time and place.

Give it *when* (UTC) and *where* (lat/lon) and it computes the real positions of
the stars overhead at that moment and renders a print-ready poster. This is the
computed, personalised product the machine does better than a human: every order
is a unique, accurate sky — a wedding night, a birth, a first date.

Pure Python maths (no numpy) + PIL rendering (no matplotlib), so it runs anywhere
the app already runs. Star positions come from a catalogue file when present
(HYG format: columns ``ra`` in hours, ``dec`` in degrees, ``mag``) and otherwise
from a small built-in bright-star set so it always produces *something*.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# --- A small built-in bright-star fallback (J2000: ra°, dec°, magnitude) ------
# Enough to render a recognisable sky (Orion, the Plough, bright stars) when no
# full catalogue file is installed. Production uses the full HYG catalogue.
_BRIGHT_STARS: list[tuple[float, float, float]] = [
    (101.287, -16.716, -1.46), (95.988, -52.696, -0.74), (213.915, 19.182, -0.05),
    (279.234, 38.784, 0.03), (79.172, 45.998, 0.08), (78.634, -8.202, 0.13),
    (114.825, 5.225, 0.34), (88.793, 7.407, 0.42), (24.428, -57.237, 0.46),
    (297.696, 8.868, 0.77), (68.980, 16.509, 0.85), (247.352, -26.432, 0.96),
    (201.298, -11.161, 0.97), (116.329, 28.026, 1.14), (344.413, -29.622, 1.16),
    (310.358, 45.280, 1.25), (152.093, 11.967, 1.35), (186.650, -63.099, 0.58),
    (191.930, -59.688, 1.63), (210.956, -60.373, 0.61), (85.190, -1.943, 1.77),
    (84.053, -1.202, 1.69), (83.002, -0.299, 2.23), (165.932, 61.751, 1.79),
    (165.460, 56.382, 2.37), (183.856, 57.033, 2.44), (193.507, 55.960, 3.31),
    (200.981, 54.925, 1.77), (206.885, 49.313, 2.27), (37.954, 89.264, 1.98),
    (51.081, 49.861, 1.79), (177.265, 14.572, 2.14), (146.463, 23.774, 3.88),
    (222.676, -16.042, 2.75), (263.402, -37.104, 1.62), (276.043, -34.385, 1.85),
    (306.412, 40.257, 1.25), (332.058, -46.961, 1.74), (326.046, 9.875, 2.39),
    (10.897, 35.620, 2.07), (2.097, 29.090, 2.06), (17.433, 35.621, 2.27),
]


@dataclass
class SkyStar:
    ra_deg: float
    dec_deg: float
    mag: float


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
    """Local apparent sidereal time in degrees (0–360). ``lon_deg`` east-positive."""
    jd = julian_date(dt)
    d = jd - 2451545.0
    t = d / 36525.0
    gmst = (280.46061837 + 360.98564736629 * d
            + 0.000387933 * t * t - (t * t * t) / 38710000.0)
    return (gmst + lon_deg) % 360.0


def equatorial_to_altaz(ra_deg: float, dec_deg: float, lst_deg: float,
                        lat_deg: float) -> tuple[float, float]:
    """Convert a star's (RA, Dec) to (altitude, azimuth) in degrees for an
    observer at ``lat_deg`` with local sidereal time ``lst_deg``. Azimuth is
    measured from North, increasing eastward."""
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
    if math.sin(ha) > 0:            # east/west disambiguation
        az = 360.0 - az
    return math.degrees(alt), az


def load_catalog(path: str | None) -> list[SkyStar]:
    """Load stars from an HYG-format CSV (``ra`` hours, ``dec`` deg, ``mag``),
    keeping the naked-eye set (mag ≤ 6.5). Falls back to the built-in bright
    stars when no usable file is given."""
    if path and Path(path).exists():
        stars: list[SkyStar] = []
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    mag = float(row.get("mag", ""))
                    if mag > 6.5:
                        continue
                    stars.append(SkyStar(float(row["ra"]) * 15.0,   # hours→deg
                                         float(row["dec"]), mag))
                except (TypeError, ValueError, KeyError):
                    continue
        if stars:
            return stars
    return [SkyStar(ra, dec, mag) for ra, dec, mag in _BRIGHT_STARS]


def render_star_map(dt: datetime, lat: float, lon: float, *, size: int = 2400,
                    catalog_path: str | None = None,
                    caption: Iterable[str] | None = None,
                    sky=(11, 18, 38), ink=(245, 242, 233)):
    """Render the sky over (lat, lon) at UTC ``dt`` as a print-ready poster.

    Returns a PIL ``Image``. ``caption`` lines (place, date, coordinates) are
    drawn beneath the disc — the personalisation. ``sky`` is the disc colour,
    ``ink`` the paper/star colour."""
    from PIL import Image, ImageDraw

    W = size
    H = int(size * 1.4)                                  # portrait poster
    img = Image.new("RGB", (W, H), ink)
    draw = ImageDraw.Draw(img)

    margin = int(W * 0.08)
    disc = W - 2 * margin
    R = disc / 2
    cx = W / 2
    cy = margin + R
    draw.ellipse([cx - R, cy - R, cx + R, cy + R], fill=sky)

    lst = local_sidereal_time(dt, lon)
    for s in load_catalog(catalog_path):
        alt, az = equatorial_to_altaz(s.ra_deg, s.dec_deg, lst, lat)
        if alt < 0:                                      # below the horizon
            continue
        r = (90.0 - alt) / 90.0 * R                      # zenith centre, horizon edge
        a = math.radians(az)
        x = cx + r * math.sin(a)
        y = cy - r * math.cos(a)
        rad = max(0.6, (2.6 - s.mag) * (W / 1400))       # brighter = bigger
        draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=ink)

    # thin border ring
    ring = max(2, int(W / 600))
    draw.ellipse([cx - R, cy - R, cx + R, cy + R], outline=ink, width=ring)

    if caption:
        _draw_caption(draw, list(caption), W, cy + R, H, sky)
    return img


def _draw_caption(draw, lines, W, top, H, colour):
    from PIL import ImageFont

    try:
        big = ImageFont.truetype("DejaVuSerif.ttf", int(W / 22))
        small = ImageFont.truetype("DejaVuSerif.ttf", int(W / 40))
    except Exception:
        big = small = ImageFont.load_default()
    y = top + (H - top) * 0.18
    for i, line in enumerate(lines):
        font = big if i == 0 else small
        w = draw.textlength(line, font=font)
        draw.text(((W - w) / 2, y), line, fill=colour, font=font)
        y += (int(W / 18) if i == 0 else int(W / 28))
