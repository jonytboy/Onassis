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

from onassis.artwork import (
    ArtworkReview, ArtworkStudio, CommercialPromptBuilder, product_family,
)
from onassis.connectors.image_backend import (
    GALLERY, MASTER, MOCKUP, PRINT, PRODUCT, ImageBackend, ImageSpec,
    LocalRenderBackend, OpenAIImageBackend, PillowUpscaler, RemoteImageBackend,
    Upscaler, build_image_backend, build_upscaler, register_image_provider,
    register_upscaler, resolve_colour,
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


def test_qc_does_not_reject_on_dimension_mismatch():
    # A good 1024² image reviewed against a 3600² target must be ACCEPTED — the
    # model's native size is not a quality failure (it gets upscaled later).
    data = LocalRenderBackend().generate(ImageSpec(
        kind=MASTER, width=1024, height=1024, palette=["ecru", "terracotta"],
        title="Amalfi Mornings"))
    spec = ImageSpec(kind=MASTER, width=3600, height=3600,
                     palette=["ecru", "terracotta"], title="Amalfi Mornings")
    verdict = ArtworkReview().evaluate(data, spec)
    assert verdict["accepted"] is True
    assert verdict["checks"]["source_size"] == [1024, 1024]   # recorded, not judged


# --- Upscaling (native model size -> print resolution) --------------

class _Native1024Backend(ImageBackend):
    """Mimics GPT Image: always returns a rich 1024² image, ignoring target size."""

    name = "native1024"

    def __init__(self):
        self.calls = 0

    def generate(self, spec: ImageSpec) -> bytes:
        self.calls += 1
        native = ImageSpec(kind=spec.kind, width=1024, height=1024,
                           palette=spec.palette, title=spec.title,
                           transparent=spec.transparent,
                           product_type=spec.product_type, scene=spec.scene)
        return LocalRenderBackend().generate(native)


def test_studio_upscales_native_image_without_regenerating(tmp_path):
    studio = _small_studio(master_px=1600, print_px=2400, gallery_px=1000)
    backend = _Native1024Backend()
    studio._backend = backend
    result = studio.generate_master(_brief(), tmp_path)

    # The 1024² source was upscaled to the configured print targets.
    assert Image.open(tmp_path / "master_artwork.png").size == (1600, 1600)
    assert Image.open(tmp_path / "print_file.png").size == (2400, 2400)
    assert Image.open(tmp_path / "print_file.png").mode == "RGBA"   # alpha kept
    assert result["master_review"]["accepted"] and result["print_review"]["accepted"]
    # NO wasted regenerations: exactly one backend call per image (master + print).
    assert backend.calls == 2


def test_gallery_images_are_upscaled_jpegs(tmp_path):
    studio = _small_studio(gallery_px=1000)
    backend = _Native1024Backend()
    studio._backend = backend
    studio.build_product_gallery(
        _brief(), {"product_key": "ceramic_mug", "product_name": "Ceramic Mug"},
        tmp_path / "images")
    hero = Image.open(tmp_path / "images" / "hero.jpg")
    assert hero.size == (1000, 1000) and hero.format == "JPEG"
    assert backend.calls == studio.gallery_count   # one call each, no size regens


def test_pillow_upscaler_resizes_and_respects_format():
    buf = io.BytesIO()
    Image.new("RGBA", (1024, 1024), (200, 150, 120, 255)).save(buf, "PNG")
    src = buf.getvalue()
    png = PillowUpscaler().upscale(src, 3600, 3600, fmt="PNG")
    out = Image.open(io.BytesIO(png))
    assert out.size == (3600, 3600) and out.mode == "RGBA"     # alpha preserved
    jpg = PillowUpscaler().upscale(src, 1200, 1200, fmt="JPEG")
    out2 = Image.open(io.BytesIO(jpg))
    assert out2.size == (1200, 1200) and out2.format == "JPEG"  # flattened photo


def test_build_upscaler_default_and_registry():
    assert isinstance(build_upscaler(SimpleNamespace(image={})), PillowUpscaler)

    class _Noop(Upscaler):
        name = "noop"

        def upscale(self, data, w, h, *, fmt="JPEG"):
            return data

    register_upscaler("noop_test", lambda cfg: _Noop())
    assert build_upscaler(SimpleNamespace(image={"upscaler": "noop_test"})).name == "noop"


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


# --- Commercial prompt builder (the whole brief, not the title) -----

def _commercial_brief() -> dict:
    return {
        "theme": "slow coastal mornings",
        "target_customer": "design-loving travellers who cherish calm",
        "emotional_angle": "unhurried Mediterranean luxury",
        "artwork_description": "a hand-drawn lemon branch over a rising sun and calm sea",
        "design_rationale": "The citrus and sun motif signals warmth and escape.",
        "shirt_colour": "ecru", "print_colour": "terracotta",
        "seasonal_relevance": "summer", "product_name": "Amalfi Morning Tee",
    }


def test_product_family_classification():
    assert product_family("Premium Poster") == "poster"
    assert product_family("Ceramic Mug") == "mug"
    assert product_family("Premium T-Shirt") == "tshirt"
    assert product_family("Heavyweight Hoodie") == "hoodie"
    assert product_family("Sweatshirt") == "sweatshirt"     # not misread as t-shirt
    assert product_family("Tote Bag") == "tote"
    assert product_family("Framed Poster") == "framed_poster"
    assert product_family("Something Unknown") == "poster"   # safe default


def test_prompt_uses_the_whole_commercial_brief_not_just_title():
    prompt = CommercialPromptBuilder().scene(_commercial_brief(), "Ceramic Mug", "hero")
    # Customer, emotion, artwork intent, palette and conversion goal are all present.
    for expected in ("design-loving travellers", "unhurried Mediterranean luxury",
                     "lemon branch", "terracotta", "click-through and conversion"):
        assert expected in prompt


def test_prompts_are_product_specific():
    b = CommercialPromptBuilder()
    brief = _commercial_brief()
    mug = b.scene(brief, "Ceramic Mug", "hero")
    poster = b.scene(brief, "Premium Poster", "hero")
    tee = b.scene(brief, "Premium T-Shirt", "hero")
    # A poster, mug and tee get DIFFERENT compositions — not the same image blindly.
    assert mug != poster != tee
    assert "mug" in mug.lower()
    assert "poster" in poster.lower() or "print" in poster.lower()
    assert "t-shirt" in tee.lower() or "mannequin" in tee.lower()


def test_scene_prompts_differ_by_scene():
    b = CommercialPromptBuilder()
    brief = _commercial_brief()
    hero = b.scene(brief, "Ceramic Mug", "hero")
    lifestyle = b.scene(brief, "Ceramic Mug", "lifestyle")
    closeup = b.scene(brief, "Ceramic Mug", "closeup")
    assert hero != lifestyle != closeup
    assert "hero thumbnail" in hero.lower()
    assert "lifestyle" in lifestyle.lower() and "kitchen" in lifestyle.lower()
    assert "close-up" in closeup.lower()


def test_master_and_print_prompts_are_brief_driven():
    b = CommercialPromptBuilder()
    brief = _commercial_brief()
    master = b.master(brief)
    assert "lemon branch" in master and "design-loving travellers" in master
    assert "not a photo of a product" in master.lower()
    print_prompt = b.print_file(brief)
    assert "transparent background" in print_prompt.lower()


# --- OpenAI production backend --------------------------------------

class _Capture:
    def __init__(self, b64):
        self.b64 = b64
        self.payload = None
        self.headers = None

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.payload = json
        self.headers = headers

        class _Resp:
            status_code = 200

            def json(_self):
                return {"data": [{"b64_json": self.b64}]}
        return _Resp()


def _png_b64():
    import base64

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (120, 80, 60)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode(), buf.getvalue()


def test_openai_backend_transparent_print_payload(monkeypatch):
    import httpx

    b64, raw = _png_b64()
    cap = _Capture(b64)
    monkeypatch.setattr(httpx, "post", cap)
    be = OpenAIImageBackend("sk-x", quality="high")
    out = be.generate(ImageSpec(kind=PRINT, width=3600, height=3600, transparent=True,
                                prompt="PRINT PROMPT"))
    assert out == raw                                       # decoded b64 image bytes
    assert cap.payload["model"] == "gpt-image-1"
    assert cap.payload["quality"] == "high"
    assert cap.payload["background"] == "transparent"       # real transparency
    assert cap.payload["output_format"] == "png"
    assert cap.payload["prompt"] == "PRINT PROMPT"
    assert cap.headers["Authorization"] == "Bearer sk-x"


def test_openai_backend_opaque_photo_payload(monkeypatch):
    import httpx

    b64, _ = _png_b64()
    cap = _Capture(b64)
    monkeypatch.setattr(httpx, "post", cap)
    OpenAIImageBackend("sk-x").generate(
        ImageSpec(kind=MOCKUP, width=1536, height=1024, transparent=False, prompt="HERO"))
    assert cap.payload["output_format"] == "jpeg"           # photos are JPEG
    assert "background" not in cap.payload
    assert cap.payload["size"] == "1536x1024"               # landscape mapping


def test_openai_backend_raises_on_http_error(monkeypatch):
    import httpx

    class _Resp:
        status_code = 400

        def json(self):
            return {"error": {"message": "bad prompt"}}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError, match="400"):
        OpenAIImageBackend("sk-x").generate(ImageSpec(kind=MASTER, width=64, height=64))


# --- Provider registry (add providers without pipeline changes) -----

def test_registry_selects_openai_provider():
    cfg = SimpleNamespace(image={"backend": "auto", "provider": "openai",
                                 "api_key": "sk-x", "quality": "high"})
    assert isinstance(build_image_backend(cfg), OpenAIImageBackend)


def test_registry_supports_a_new_provider(monkeypatch):
    seen = {}

    def _factory(cfg, key):
        seen["key"] = key
        return OpenAIImageBackend(key, model="acme-1")

    register_image_provider("acme_test", _factory)
    cfg = SimpleNamespace(image={"backend": "remote", "provider": "acme_test",
                                 "api_key": "sk-y"})
    backend = build_image_backend(cfg)
    assert backend.model == "acme-1" and seen["key"] == "sk-y"


# --- Studio feeds the brief-driven prompt to the backend ------------

class _PromptCaptureBackend(ImageBackend):
    name = "capture"

    def __init__(self):
        self.prompts = []

    def generate(self, spec: ImageSpec) -> bytes:
        self.prompts.append(spec.prompt)
        return LocalRenderBackend().generate(spec)  # a real file, but record the prompt


def test_studio_sends_commercial_brief_to_the_backend(tmp_path):
    studio = _small_studio()
    cap = _PromptCaptureBackend()
    studio._backend = cap
    studio.generate_master(_brief(), tmp_path)
    studio.build_product_gallery(
        _brief(), {"product_key": "ceramic_mug", "product_name": "Ceramic Mug"},
        tmp_path / "images")
    joined = "\n".join(cap.prompts)
    # The model receives customer + emotion + artwork intent — not just the title.
    assert "line-drawn lemon branch" in joined
    assert "mug" in joined.lower()                            # product-specific
    assert "click-through and conversion" in joined           # conversion-optimised
