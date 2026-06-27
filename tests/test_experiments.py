"""Tests for the Experiment Engine."""

from __future__ import annotations

import pytest

from onassis.experiments import ExperimentEngine, ExperimentError, _proportion_confidence


@pytest.fixture
def engine(config, db):
    return ExperimentEngine(config, db)


def _start(engine, **kw):
    base = dict(product_id="SKU1", variable="title", hypothesis="A better title sells more",
                success_metric="conversion_rate", baseline_value=0.02)
    base.update(kw)
    return engine.start(**base)


# --- Lifecycle ------------------------------------------------------

def test_start_creates_active_experiment(engine):
    exp = _start(engine)
    assert exp["status"] == "active"
    assert exp["variable"] == "title"
    assert "id" in exp


def test_unknown_variable_rejected(engine):
    with pytest.raises(ExperimentError, match="Unknown variable"):
        _start(engine, variable="colour_of_the_sky")


def test_no_duplicate_active_experiment_per_variable(engine):
    _start(engine)
    with pytest.raises(ExperimentError, match="already exists"):
        _start(engine)  # same product + variable while one is active


def test_one_active_per_variable_but_other_variables_ok(engine):
    _start(engine, variable="title")
    other = _start(engine, variable="price")  # different variable is fine
    assert other["variable"] == "price"


def test_can_restart_after_completion(engine):
    first = _start(engine)
    engine.complete(first["id"], result_value=0.03)
    again = _start(engine)  # no longer active, so a new one is allowed
    assert again["id"] != first["id"]


# --- Completion: result, confidence, learning, promotion ------------

def test_complete_records_win_and_promotes(engine, db):
    exp = _start(engine, baseline_value=0.02)
    done = engine.complete(exp["id"], result_value=0.05)  # higher is better -> win
    assert done["status"] == "completed"
    assert done["result"] == "win"
    assert done["learning"]
    assert done["promoted"] == 1
    assert db.list_promoted_learnings()[0]["id"] == exp["id"]


def test_complete_records_loss_not_promoted(engine):
    exp = _start(engine, baseline_value=0.05)
    done = engine.complete(exp["id"], result_value=0.02)  # went down -> loss
    assert done["result"] == "loss"
    assert done["promoted"] == 0


def test_complete_computes_statistical_confidence(engine):
    exp = _start(engine, success_metric="conversion_rate")
    done = engine.complete(
        exp["id"], baseline_value=0.02, result_value=0.04,
        samples={"baseline_n": 1000, "baseline_success": 20,
                 "variant_n": 1000, "variant_success": 40},
    )
    assert done["confidence"] is not None
    assert done["confidence"] > 90  # a clear, significant lift


def test_complete_without_samples_has_no_confidence(engine):
    exp = _start(engine)
    done = engine.complete(exp["id"], result_value=0.05)
    assert done["confidence"] is None


def test_cannot_complete_twice(engine):
    exp = _start(engine)
    engine.complete(exp["id"], result_value=0.03)
    with pytest.raises(ExperimentError, match="not active"):
        engine.complete(exp["id"], result_value=0.04)


def test_proportion_confidence_significant_vs_noise():
    strong = _proportion_confidence(20, 1000, 60, 1000)   # big lift, big n
    weak = _proportion_confidence(2, 10, 3, 10)           # tiny n
    assert strong > 95
    assert weak < strong


# --- Reads ----------------------------------------------------------

def test_active_and_list(engine):
    a = _start(engine, variable="title")
    _start(engine, variable="price")
    engine.complete(a["id"], result_value=0.03)
    assert len(engine.list()) == 2
    assert len(engine.active()) == 1  # only 'price' still active


# --- Decision support ----------------------------------------------

def test_has_active_and_last_result(engine):
    exp = _start(engine, variable="keywords")
    assert engine.has_active("SKU1", "keywords") is True
    engine.complete(exp["id"], baseline_value=0.05, result_value=0.02)
    assert engine.has_active("SKU1", "keywords") is False
    assert engine.last_result("SKU1", "keywords") == "loss"


# --- Optimiser uses experiments -------------------------------------

def test_optimiser_skips_variable_under_active_experiment(config, db):
    from onassis.optimiser import ProductOptimiser

    sku = "9001"
    db.insert_product({"sku": sku, "name": "Throw", "production_cost": 14})
    from onassis.revenue import RevenueEngine
    RevenueEngine(config, db).record_order({
        "occurred_at": "2026-06-26T10:00:00+00:00", "sale_date": "2026-06-26",
        "order_ref": "o1", "product_id": sku, "platform": "etsy",
        "sale_price": 48, "quantity": 1, "production_cost": 14,
    })
    # Profitable but starved of traffic -> would recommend a Pinterest campaign...
    db.upsert_etsy_listing({"listing_id": int(sku), "product_id": sku, "views": 5})

    opt = ProductOptimiser(config, db)
    assert opt.analyse_product(db.get_product_by_sku(sku))["action_key"] == "pinterest_campaign"

    # ...but with that experiment already running, it must not duplicate it.
    ExperimentEngine(config, db).start(
        product_id=sku, variable="pinterest_campaign",
        hypothesis="more traffic", success_metric="visits")
    rec = opt.analyse_product(db.get_product_by_sku(sku))
    assert rec["action_key"] == "leave_unchanged"
    assert "already running" in rec["reasoning"]
