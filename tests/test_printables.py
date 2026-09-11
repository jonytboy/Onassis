"""Tests for onassis.printables — the digital printable packager."""

from __future__ import annotations

import zipfile
from pathlib import Path

from PIL import Image

from onassis.printables import (RATIOS, build_print_set, printable_description)


def _src(tmp_path, size=(2048, 2048)):
    p = tmp_path / "print_file.png"
    Image.new("RGB", size, (200, 150, 120)).save(p)
    return str(p)


def test_build_print_set_makes_one_jpeg_per_ratio_plus_zip(tmp_path):
    out = tmp_path / "out"
    res = build_print_set(_src(tmp_path), str(out))
    assert len(res["files"]) == len(RATIOS)
    for f in res["files"]:
        assert Path(f).exists() and f.endswith(".jpg")
    # The ZIP bundles exactly the ratio files.
    assert Path(res["zip"]).exists()
    with zipfile.ZipFile(res["zip"]) as z:
        assert len(z.namelist()) == len(RATIOS)


def test_build_print_set_ratios_are_correct_and_portrait(tmp_path):
    res = build_print_set(_src(tmp_path), str(tmp_path / "out"))
    for f in res["files"]:
        label = Path(f).stem.replace("print_", "")
        rw, rh = RATIOS[label]
        w, h = Image.open(f).size
        assert h > w                                    # portrait canvas
        assert abs((w / h) - (rw / rh)) < 0.02          # matches the ratio


def test_build_print_set_never_crops_the_art(tmp_path):
    """The art is fitted (letterboxed), so the full design is preserved — a
    square source keeps all its content, just centred on a white canvas."""
    res = build_print_set(_src(tmp_path, size=(2000, 2000)),
                          str(tmp_path / "out"), margin=0.05)
    img = Image.open(res["files"][0]).convert("RGB")
    # White margins exist (top-left corner is background, centre is artwork).
    assert img.getpixel((2, 2)) == (255, 255, 255)
    assert img.getpixel((img.width // 2, img.height // 2)) != (255, 255, 255)


def test_printable_description_states_digital_terms():
    d = printable_description("Mediterranean coastal print")
    low = d.lower()
    assert "instant download" in low
    assert "no physical item" in low or "nothing is posted" in low
    assert "300 dpi" in low
    assert "personal use" in low
