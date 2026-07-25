"""Tests for the Reel Studio (Sprint 48) — frame composition, no ffmpeg."""

from __future__ import annotations

from PIL import Image

from onassis.reel_studio import ReelSpec, ReelStudio, Slide, compose_frames


def _img(tmp_path, name, colour):
    p = tmp_path / name
    Image.new("RGB", (800, 800), colour).save(p)
    return str(p)


def test_compose_frames_are_vertical_and_counted():
    spec = ReelSpec(slides=[Slide(text="hook", frames=5), Slide(text="", frames=4)],
                    size=(120, 213), fps=12, xfade_frames=0)   # no crossfade → exact count
    frames = compose_frames(spec)
    assert len(frames) == 9
    assert all(f.size == (120, 213) for f in frames)   # 9:16 output


def test_crossfade_shortens_the_reel_and_stays_vertical():
    """A crossfade blends slide boundaries — fewer total frames, smoother feel."""
    spec = ReelSpec(slides=[Slide(text="a", frames=10), Slide(text="b", frames=10)],
                    size=(120, 213), xfade_frames=4)
    frames = compose_frames(spec)
    assert len(frames) == 16                        # 10 + 10 − 4 crossfade
    assert all(f.size == (120, 213) for f in frames)


def test_missing_image_falls_back_not_crashes(tmp_path):
    # A slide with a non-existent image still renders (tasteful fallback ground).
    spec = ReelSpec(slides=[Slide(image=str(tmp_path / "nope.png"), text="hi", frames=3)],
                    size=(120, 213))
    frames = compose_frames(spec)
    assert len(frames) == 3 and frames[0].size == (120, 213)


def test_real_image_is_cover_cropped(tmp_path):
    spec = ReelSpec(slides=[Slide(image=_img(tmp_path, "a.png", (200, 120, 90)), frames=2)],
                    size=(120, 213))
    frames = compose_frames(spec)
    assert frames[0].size == (120, 213)


def test_studio_render_uses_injected_encoder(tmp_path):
    """ReelStudio composes frames and hands them to a (stub) encoder — no ffmpeg."""
    captured = {}

    def stub_encoder(frames, out_path, *, fps):
        captured["frames"] = sum(1 for _ in frames)   # frames stream in now
        captured["fps"] = fps
        with open(out_path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42")   # pretend mp4
        return out_path

    studio = ReelStudio(encoder=stub_encoder)
    spec = ReelSpec(slides=[Slide(text="a", frames=4), Slide(text="b", frames=4)],
                    caption="cap", hashtags=["#x"], fmt="style_slide",
                    size=(120, 213), fps=24, xfade_frames=0)
    out = studio.render(spec, tmp_path / "clips" / "reel.mp4")
    assert captured["frames"] == 8 and captured["fps"] == 24
    assert out["fmt"] == "style_slide" and out["caption"] == "cap"
    assert out["frames"] == 8 and out["duration_s"] > 0
    assert (tmp_path / "clips" / "reel.mp4").exists()
