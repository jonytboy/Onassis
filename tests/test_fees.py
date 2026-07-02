"""Tests for the Etsy fee model — the real unit economics."""

from __future__ import annotations

import pytest

from onassis.fees import FeeModel


@pytest.fixture
def model() -> FeeModel:
    # Etsy defaults: 6.5% txn, 4% + £0.20 payment, £0.20 listing, 15% offsite @ 30%.
    return FeeModel()


def test_blended_fees_include_offsite_ads_expectation(model):
    # £48 order, unknown attribution -> blended Offsite Ads (15% × 30%).
    fees = model.order_fees(48.0)
    # marketplace = 0.065*48 + 0.20 listing + 0.15*0.30*48 offsite = 3.12+0.20+2.16
    assert fees["marketplace_fees"] == pytest.approx(5.48, abs=0.01)
    # payment = 0.04*48 + 0.20 = 2.12
    assert fees["payment_fees"] == pytest.approx(2.12, abs=0.01)
    assert fees["offsite_fees"] == pytest.approx(2.16, abs=0.01)
    assert fees["total_fees"] == pytest.approx(7.60, abs=0.01)


def test_attributed_order_pays_full_offsite_rate(model):
    attributed = model.order_fees(48.0, offsite=True)["offsite_fees"]
    not_attributed = model.order_fees(48.0, offsite=False)["offsite_fees"]
    assert attributed == pytest.approx(0.15 * 48, abs=0.01)   # full 15%
    assert not_attributed == 0.0                              # no ad fee


def test_shipping_is_part_of_the_fee_base(model):
    no_ship = model.total_fees(20.0, offsite=False)
    with_ship = model.total_fees(20.0, shipping=5.0, offsite=False)
    assert with_ship > no_ship   # Etsy charges fees on item + shipping


def test_unit_net_profit_is_after_production_and_fees(model):
    # A £22 mug costing £7.50 to make, blended fees.
    profit = model.unit_net_profit(22.0, 7.5)
    fees = model.total_fees(22.0)
    assert profit == pytest.approx(22.0 - 7.5 - fees, abs=0.01)
    assert profit < 22.0 - 7.5           # fees really are deducted


def test_net_margin_reflects_real_economics(model):
    # A "60% gross margin" product is far thinner after real fees.
    margin = model.net_margin(22.0, 7.5)
    gross_margin = (22.0 - 7.5) / 22.0   # 0.66 before fees
    assert 0.20 < margin < 0.55          # realistic POD net margin
    assert margin < gross_margin - 0.10  # real fees meaningfully thin the margin
    assert model.net_margin(0.0, 5.0) == 0.0   # guard against divide-by-zero


def test_from_config_reads_overrides():
    class _Cfg:
        fees = {"transaction_rate": 0.05, "offsite_ads_rate": 0.0,
                "payment_fixed": 0.0, "listing_fee": 0.0, "payment_rate": 0.0}

    m = FeeModel.from_config(_Cfg())
    # Only the 5% transaction fee remains.
    assert m.total_fees(100.0, offsite=False) == pytest.approx(5.0, abs=0.01)


def test_from_config_defaults_when_absent():
    class _Cfg:
        fees = {}

    m = FeeModel.from_config(_Cfg())
    assert m.transaction_rate == 0.065 and m.offsite_ads_rate == 0.15
