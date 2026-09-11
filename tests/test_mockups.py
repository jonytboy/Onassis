"""Room / stationery mockups: magenta-frame detection, compositing, fallbacks."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from onassis.room_mockup import (composite, find_frame_box, room_mockup,
                                 room_template)
from onassis.wall_mockup import frame_on_wall, stationery_flatlay


def _scene_with_magenta(size=(800, 600), box=(300, 120, 520, 440)):
    img = Image.new("RGB", size, (210, 200, 185))
    ImageDraw.Draw(img).rectangle(box, fill=(255, 0, 255))
    return img, box


def test_find_frame_box_locates_the_magenta_placeholder():
    img, box = _scene_with_magenta()
    found = find_frame_box(img)
    assert found is not None
    assert all(abs(a - b) <= 1 for a, b in zip(found, box))


def test_find_frame_box_rejects_scenes_without_a_clean_placeholder():
    assert find_frame_box(Image.new("RGB", (400, 300), (200, 190, 180))) is None
    img = Image.new("RGB", (400, 300), (200, 190, 180))
    ImageDraw.Draw(img).rectangle([10, 10, 20, 20], fill=(255, 0, 255))   # too small
    assert find_frame_box(img) is None


def test_composite_puts_the_art_inside_the_frame():
    scene, box = _scene_with_magenta()
    art = Image.new("RGB", (200, 300), (20, 40, 80))
    out = composite(scene, art, box)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    assert out.getpixel((cx, cy)) == (20, 40, 80)          # art in the frame
    assert out.getpixel((10, 10)) == (210, 200, 185)         # scene untouched


class _PhotoBackend:
    name = "openai"
    def generate(self, spec):
        import io
        img, _ = _scene_with_magenta((spec.width, spec.height),
                                     (spec.width // 3, spec.height // 5,
                                      spec.width * 2 // 3, spec.height * 4 // 5))
        b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()


class _LocalBackend:
    name = "local"
    def generate(self, spec):  # the dev renderer can't make photos
        raise AssertionError("must not be called")


def test_room_template_is_generated_once_and_cached(tmp_path):
    b = _PhotoBackend()
    p1 = room_template(b, tmp_path, "living")
    assert p1 and Path(p1).exists()
    b.generate = lambda spec: (_ for _ in ()).throw(AssertionError("regenerated"))
    assert room_template(b, tmp_path, "living") == p1              # cached


def test_room_mockup_falls_back_to_none_for_the_dev_renderer(tmp_path):
    art = Image.new("RGB", (100, 140), (30, 30, 30))
    assert room_mockup(_LocalBackend(), art, tmp_path) is None


def test_room_mockup_composites_with_a_real_scene(tmp_path):
    art = Image.new("RGB", (100, 140), (30, 60, 90))
    out = room_mockup(_PhotoBackend(), art, tmp_path, "nursery")
    assert out is not None and out.size == (1536, 1024)


def test_drawn_fallbacks_render():
    art = Image.new("RGB", (500, 700), (240, 236, 228))
    assert frame_on_wall(art, size=400).size == (400, 400)
    assert stationery_flatlay(art, size=400).size == (400, 400)
