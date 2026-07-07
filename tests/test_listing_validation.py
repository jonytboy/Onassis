"""Tests for Etsy pre-publish validation + sanitisation (Sprint 41.2)."""

from __future__ import annotations

from onassis.failure_help import explain
from onassis.listing_validation import operator_summary, validate_listing


def _base(**over):
    listing = {"title": "Coastal Linen Throw", "description": "A lovely coastal throw." * 3,
               "tags": ["linen", "coastal"], "price": 30.0}
    listing.update(over)
    return listing


def test_valid_listing_passes():
    r = validate_listing(_base())
    assert r["ok"] is True and r["blocking"] is False


def test_multiple_ampersands_are_sanitised():
    r = validate_listing(_base(title="Coffee & Tea & Home"))
    assert r["ok"] is True
    assert r["listing"]["title"] == "Coffee & Tea and Home"
    assert any(i["code"] == "multiple_ampersands" and i["fixed"] for i in r["issues"])


def test_title_over_140_is_trimmed():
    r = validate_listing(_base(title="word " * 40))
    assert len(r["listing"]["title"]) <= 140
    assert any(i["code"] == "title_too_long" for i in r["issues"])


def test_tags_capped_and_deduped():
    r = validate_listing(_base(tags=["a"] * 20 + ["toolongtagnamethatexceeds20chars"]))
    assert len(r["listing"]["tags"]) <= 13
    assert any(i["code"] in ("too_many_tags", "tag_too_long") for i in r["issues"])


def test_missing_title_is_blocking():
    r = validate_listing(_base(title=""))
    assert r["ok"] is False and r["blocking"] is True
    assert "no title" in operator_summary(r).lower()


def test_missing_price_is_blocking():
    r = validate_listing(_base(price=0))
    assert r["ok"] is False
    assert any(i["code"] == "invalid_price" and i["blocking"] for i in r["issues"])


def test_short_description_is_warning_not_blocking():
    r = validate_listing(_base(description="short"), min_description=50)
    assert r["ok"] is True                      # non-blocking
    assert any(i["code"] == "description_short" and not i["blocking"] for i in r["issues"])


# --- Failure help ----------------------------------------------------

def test_explain_maps_ampersand_error():
    h = explain("HTTP 400 too_many_invalid_characters")
    assert "&" in h["cause"] and h["retryable"] is True and "and" in h["suggestion"]


def test_explain_maps_auth_error():
    h = explain("HTTP 401 invalid_token")
    assert "uthenticat" in h["cause"] and h["retryable"] is True


def test_explain_not_configured():
    h = explain(None, status="not_configured")
    assert "not connected" in h["cause"].lower() and h["retryable"] is False


def test_explain_unknown_is_still_retryable():
    h = explain("something weird happened")
    assert h["retryable"] is True and h["raw"] == "something weird happened"
