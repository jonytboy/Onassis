"""Tests for the Artwork Studio and its replaceable image backends.

These verify ONASSIS generates REAL image files (not placeholders): valid,
correctly sized images for every asset kind, an 8-10 image commercial gallery,
a deterministic quality gate that regenerates weak artwork, and a replaceable
backend that falls back safely.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from onassis.artwork import ArtworkReview, ArtworkStudio
from onassis.connectors.image_backend import (
    GALLERY, MASTER, MOCKUP, PRINT, PRODUCT, ImageBackend, ImageSpec,
    LocalRenderBackend, RemoteImageBackend, build_image_backend, resolve_colour,
)


def _brief() -> dict:
    return {"design_brief": {
        "product_name": "Amalfi Morning Tee", "brand": "Local Celebrity",
        "theme": "slow coastal mornings", "shirt_colour": "ecru",
        "print_colour": "terracotta", "listing_title_seed": "Amalfi Mornings",
        "artwork_description": "a line-drawn lemon branch and rising sun",
        "typography_direction": "serif lowercase",
        "transparent_background_required": True, "dpi_requirement": 300}}


def _small_studio(**over) -> ArtworkStudio:
    cfg = SimpleNamespace(image={"backend": "local", "master_px": 96, "print_px": 96,
                                 "gallery_px": 96, "gallery_count": 9, "max_attempts": 2,
                                 **over}, brand={"name": "Local Celebrity"})
    return ArtworkStudio(cfg)


# --- Colour resolution ----------------------------------------------

def test_resolve_colour_handles_names_hex_and_phrases():
    assert resolve_colour("terracotta") == (196, 105, 74)   # brand word
    assert resolve_colour("#ffffff") == (255, 255, 255)     # hex
    assert resolve_colour("natural/ecru") == (238, 231, 213)  # phrase → first known
    assert resolve_colour(None) == (74, 128, 138)           # default
    assert resolve_colour("not-a-colour-xyz") == (74, 128, 138)  # graceful default


# --- Local renderer: real, correctly-sized images -------------------

@pytest.mark.parametrize("kind,scene,transparent", [
    (MASTER, "hero", False), (PRINT, "hero", True), (PRODUCT, "hero", False),
    (MOCKUP, "lifestyle", False), (MOCKUP, "closeup", False),
    (MOCKUP, "scale", False), (GALLERY, "room", False),
])
def test_local_backend_produces_valid_sized_images(kind, scene, transparent):
    spec = ImageSpec(kind=kind, width=128, height=128, palette=["ecru", "terracotta",
                     "sea", "olive"], title="Amalfi", product_type="ceramic mug",
                     scene=scene, transparent=transparent)
    data = LocalRenderBackend().generate(spec)
    img = Image.open(io.BytesIO(data))
    assert img.size == (128, 128)
    assert len(data) > 800                      # a real file, not a 1×1 placeholder


# --- Quality gate ---------------------------------------------------

def test_review_rejects_blank_and_flat_images():
    review = ArtworkReview()
    spec = ImageSpec(kind=MASTER, width=64, height=64)
    flat = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 200, 200)).save(flat, format="PNG")
    verdict = review.evaluate(flat.getvalue(), spec)
    assert verdict["accepted"] is False
    assert "flat" in " ".join(verdict["reasons"]) or not verdict["checks"]["coverage_ok"]


def test_review_accepts_a_real_rendered_image():
    spec = ImageSpec(kind=MASTER, width=96, height=96, palette=["ecru", "terracotta"],
                     title="Amalfi Mornings")
    data = LocalRenderBackend().generate(spec)
    assert ArtworkReview().evaluate(data, spec)["accepted"] is True


# --- Master artwork + print file ------------------------------------

def test_generate_master_writes_real_files(tmp_path):
    studio = _small_studio()
    result = studio.generate_master(_brief(), tmp_path)
    assert result["files"] == ["master_artwork.png", "print_file.png"]
    for name in result["files"]:
        path = tmp_path / name
        assert path.exists() and path.stat().st_size > 0
        Image.open(path).verify()
    assert result["master_review"]["accepted"] and result["print_review"]["accepted"]


def test_print_file_has_transparency(tmp_path):
    studio = _small_studio()
    studio.generate_master(_brief(), tmp_path)
    img = Image.open(tmp_path / "print_file.png")
    assert img.mode == "RGBA"                    # transparent background preserved


# --- Product gallery (8-10 real commercial images) ------------------

def test_build_product_gallery_makes_8_to_10_real_images(tmp_path):
    studio = _small_studio()
    manifest = studio.build_product_gallery(
        _brief(), {"product_key": "ceramic_mug", "product_name": "Ceramic Mug"},
        tmp_path / "images")
    assert 8 <= len(manifest) <= 10
    assert manifest[0]["filename"] == "hero.jpg"             # hero leads the gallery
    orders = [m["order"] for m in manifest]
    assert orders == sorted(orders) and orders[0] == 1       # ordered for Etsy
    for m in manifest:
        path = tmp_path / "images" / m["filename"]
        assert path.exists() and path.stat().st_size > 0
        Image.open(path).verify()
        assert m["review"]["accepted"] is True
        assert m["alt_text"]


# --- Regeneration + backend fallback --------------------------------

class _FlatBackend(ImageBackend):
    """Always returns a flat image (fails QC) — to test regeneration."""
    name = "flat"

    def __init__(self):
        self.calls = 0

    def generate(self, spec: ImageSpec) -> bytes:
        self.calls += 1
        buf = io.BytesIO()
        Image.new("RGB", (spec.width, spec.height), (180, 180, 180)).save(buf, format="PNG")
        return buf.getvalue()


def test_weak_artwork_is_regenerated_then_best_effort(tmp_path):
    studio = _small_studio()
    backend = _FlatBackend()
    studio._backend = backend
    studio._fallback = backend  # force the flat result through (no local rescue)
    result = studio.generate_master(_brief(), tmp_path)
    # It tried max_attempts times before accepting the best effort.
    assert backend.calls >= studio.max_attempts
    assert result["master_review"]["accepted"] is False   # QC honestly reports weak
    assert (tmp_path / "master_artwork.png").exists()      # a real file still exists


class _BoomBackend(ImageBackend):
    """Raises on generate — to test the safe fallback to the local renderer."""
    name = "boom"

    def generate(self, spec: ImageSpec) -> bytes:
        raise RuntimeError("provider down")


def test_backend_failure_falls_back_to_local(tmp_path):
    studio = _small_studio()
    studio._backend = _BoomBackend()
    result = studio.generate_master(_brief(), tmp_path)
    # The local fallback produced a real, QC-passing file despite the failure.
    assert (tmp_path / "master_artwork.png").stat().st_size > 0
    assert result["master_review"]["accepted"] is True


# --- Backend selection ----------------------------------------------

def test_build_backend_defaults_to_local():
    cfg = SimpleNamespace(image={})
    assert isinstance(build_image_backend(cfg), LocalRenderBackend)


def test_build_backend_remote_requires_a_key():
    with_key = SimpleNamespace(image={"backend": "remote", "api_key": "sk-test"})
    assert isinstance(build_image_backend(with_key), RemoteImageBackend)
    no_key = SimpleNamespace(image={"backend": "remote", "api_key": None})
    assert isinstance(build_image_backend(no_key), LocalRenderBackend)  # safe fallback


def test_auto_backend_uses_remote_only_with_key():
    auto_key = SimpleNamespace(image={"backend": "auto", "api_key": "sk-test"})
    assert isinstance(build_image_backend(auto_key), RemoteImageBackend)
    auto_nokey = SimpleNamespace(image={"backend": "auto"})
    assert isinstance(build_image_backend(auto_nokey), LocalRenderBackend)


def test_remote_backend_size_mapping():
    assert RemoteImageBackend._nearest_size(1000, 1000) == "1024x1024"
    assert RemoteImageBackend._nearest_size(800, 1200) == "1024x1536"
    assert RemoteImageBackend._nearest_size(1200, 800) == "1536x1024"
