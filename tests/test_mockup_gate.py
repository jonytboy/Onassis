"""Tests for the Mockup Quality Gate (P1)."""

from __future__ import annotations

from onassis.mockup_gate import (BLOCK_MESSAGE, evaluate_listing, image_publishable,
                                  listing_mockup_status)


def _img(**kw):
    base = {"order": 1, "filename": "hero.jpg", "quality_score": 90}
    base.update(kw)
    return base


def test_real_passing_mockup_is_publishable():
    assert image_publishable(_img(quality_pass=True, fallback_used=False,
                                  generation_ok=True)) is True


def test_fallback_image_is_never_publishable():
    assert image_publishable(_img(quality_pass=True, fallback_used=True)) is False


def test_failed_generation_is_not_publishable():
    assert image_publishable(_img(quality_pass=True, generation_ok=False)) is False


def test_explicit_quality_fail_blocks():
    assert image_publishable(_img(quality_pass=False)) is False


def test_listing_blocks_when_all_fallback():
    listing = {"images": [_img(quality_pass=True, fallback_used=True),
                          _img(order=2, quality_pass=True, fallback_used=True)]}
    r = evaluate_listing(listing)
    assert r["ok"] is False and r["fallback"] == 2
    assert r["message"] == BLOCK_MESSAGE
    assert "placeholder" in r["reason"].lower()


def test_listing_passes_with_one_real_mockup():
    listing = {"images": [_img(quality_pass=True, fallback_used=True),
                          _img(order=2, quality_pass=True, fallback_used=False,
                               generation_ok=True)]}
    r = evaluate_listing(listing)
    assert r["ok"] is True and r["passing"] == 1 and r["message"] == ""


def test_legacy_listing_without_metadata_is_not_blocked():
    # Older/foreign packages with no quality metadata get the benefit of the doubt.
    listing = {"images": [{"order": 1, "filename": "hero.jpg", "alt_text": "x"}]}
    assert evaluate_listing(listing)["ok"] is True


def test_local_dev_mockup_blocked_in_production_only():
    # A local-rendered mockup (dev placeholder) is fine in dev, blocked in prod.
    listing = {"images": [_img(provider="local", quality_pass=True, fallback_used=False)]}
    assert evaluate_listing(listing, allow_local=True)["ok"] is True     # dev/test
    prod = evaluate_listing(listing, allow_local=False)
    assert prod["ok"] is False and prod["dev_rendered"] == 1
    assert "DEV placeholder" in prod["reason"] or "real image model" in prod["reason"]


def test_real_openai_mockup_passes_in_production():
    listing = {"images": [_img(provider="openai", quality_pass=True, fallback_used=False)]}
    assert evaluate_listing(listing, allow_local=False)["ok"] is True


def test_status_word():
    assert listing_mockup_status(None)["status"] == "none"
    blocked = {"images": [_img(quality_pass=True, fallback_used=True)]}
    assert listing_mockup_status(blocked)["status"] == "failed"
    good = {"images": [_img(quality_pass=True)]}
    assert listing_mockup_status(good)["status"] == "ok"
