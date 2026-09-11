"""Tests for onassis.starmap — the deterministic astronomy + rendering."""

from __future__ import annotations

from datetime import datetime, timezone

from onassis.starmap import (equatorial_to_altaz, julian_date, load_catalog,
                             local_sidereal_time, render_star_map)


def test_julian_date_epoch():
    # J2000.0 is exactly JD 2451545.0 (2000-01-01 12:00 UTC).
    jd = julian_date(datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    assert abs(jd - 2451545.0) < 1e-6


def test_polaris_altitude_equals_observer_latitude():
    # Polaris sits ~1° from the celestial pole, so its altitude ≈ the observer's
    # latitude at any time — a clean check that the alt/az maths is right.
    lst = local_sidereal_time(datetime(2024, 3, 20, 21, 0, tzinfo=timezone.utc), 0.0)
    alt, _ = equatorial_to_altaz(37.95, 89.264, lst, lat_deg=51.5)
    assert abs(alt - 51.5) < 1.5


def test_star_below_horizon_has_negative_altitude():
    # A far-southern star is never up from a mid-northern latitude.
    lst = local_sidereal_time(datetime(2024, 6, 21, 22, 0, tzinfo=timezone.utc), 0.0)
    alt, _ = equatorial_to_altaz(95.99, -52.7, lst, lat_deg=51.5)   # Canopus
    assert alt < 0


def test_load_catalog_falls_back_to_builtin():
    stars = load_catalog(None)
    assert len(stars) > 20
    assert all(s.mag <= 6.5 for s in stars)


def test_load_catalog_reads_hyg_csv(tmp_path):
    p = tmp_path / "cat.csv"
    p.write_text("ra,dec,mag\n6.75,-16.7,-1.46\n0.0,0.0,9.9\n")   # 2nd is too faint
    stars = load_catalog(str(p))
    assert len(stars) == 1
    assert abs(stars[0].ra_deg - 101.25) < 0.1        # 6.75h × 15 = 101.25°


def test_render_star_map_produces_a_poster(tmp_path):
    img = render_star_map(datetime(2024, 6, 21, 22, 30, tzinfo=timezone.utc),
                          lat=51.5, lon=-0.13, size=400,
                          caption=["London", "21 June 2024"])
    assert img.size == (400, int(400 * 1.4))
    # Centre of the disc is the dark sky colour (a star map, not a blank page).
    assert img.getpixel((200, int(400 * 0.08) + 10)) != (245, 242, 233)
