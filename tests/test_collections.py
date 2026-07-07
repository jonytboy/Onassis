"""Tests for categorisation, category-balanced selection, and collections."""

from __future__ import annotations

from onassis.collections import (
    balance_selection, categorise, collection_name, describe_collection)


def _s(key, score):
    return {"product_key": key, "composite_score": score}


# --- Categorisation --------------------------------------------------

def test_categorise():
    assert categorise("premium_tshirt") == "apparel"
    assert categorise("heavyweight_hoodie") == "apparel"
    assert categorise("premium_poster") == "print"
    assert categorise("ceramic_mug") == "drinkware"
    assert categorise("tote_bag") == "homeware"
    assert categorise("hardcover_notebook") == "stationery"
    assert categorise("mystery_thing") == "other"


# --- Balanced selection (the apparel fix) ----------------------------

def test_balance_rescues_apparel_when_it_falls_out():
    # Learning has pushed apparel down: top-4 are all home/print — apparel missing.
    chosen = [_s("ceramic_mug", 95), _s("premium_poster", 92),
              _s("tote_bag", 90), _s("framed_poster", 88)]
    pool = chosen + [_s("premium_tshirt", 84), _s("heavyweight_hoodie", 80)]
    result = balance_selection(chosen, pool)
    keys = [s["product_key"] for s in result]
    assert "premium_tshirt" in keys                 # apparel rescued
    assert keys[0] == "ceramic_mug"                 # top pick always kept
    assert len(result) == 4                          # same size


def test_balance_is_noop_when_already_diverse():
    chosen = [_s("ceramic_mug", 95), _s("premium_poster", 92),
              _s("premium_tshirt", 88), _s("tote_bag", 85)]
    pool = chosen + [_s("heavyweight_hoodie", 80)]
    result = balance_selection(chosen, pool)
    assert [s["product_key"] for s in result] == [s["product_key"] for s in chosen]


def test_balance_keeps_top_pick_and_size():
    chosen = [_s("ceramic_mug", 99), _s("tote_bag", 90)]
    pool = chosen + [_s("premium_tshirt", 70)]
    result = balance_selection(chosen, pool)
    assert result[0]["product_key"] == "ceramic_mug" and len(result) == 2


# --- Collections -----------------------------------------------------

def test_collection_name():
    assert collection_name({"theme": "mediterranean morning"}) == "Mediterranean Morning Collection"
    assert collection_name(None, {"name": "Salt Air"}) == "Salt Air Collection"


def test_describe_collection_reports_category_balance():
    launched = [_s("ceramic_mug", 95), _s("premium_tshirt", 88), _s("premium_poster", 85)]
    d = describe_collection(launched, {"theme": "coastal"})
    assert d["name"] == "Coastal Collection"
    assert d["has_apparel"] is True and d["category_count"] == 3
    assert d["size"] == 3
