"""Tests for the read-only Etsy connector (stub client — no network)."""

from __future__ import annotations

from datetime import date

import pytest

from onassis.connectors.etsy import EtsyConnector
from onassis.connectors.etsy_client import EtsyClient, EtsyConfigError

# 2026-06-26 ~ unix 1782... ; exact value doesn't matter, only that it's stable.
_TS = 1782950400  # a fixed created_timestamp


def _receipt(receipt_id, transaction_id, listing_id, amount, qty=1):
    return {
        "receipt_id": receipt_id,
        "created_timestamp": _TS,
        "transactions": [
            {
                "transaction_id": transaction_id,
                "listing_id": listing_id,
                "quantity": qty,
                "price": {"amount": amount, "divisor": 100, "currency_code": "GBP"},
            }
        ],
    }


def _listing(listing_id, title, amount, views, favourers, state="active"):
    return {
        "listing_id": listing_id,
        "title": title,
        "state": state,
        "url": f"https://etsy.com/listing/{listing_id}",
        "price": {"amount": amount, "divisor": 100, "currency_code": "GBP"},
        "views": views,
        "num_favorers": favourers,
        "created_timestamp": _TS,
    }


class StubEtsyClient:
    """In-memory stand-in for EtsyClient — same method surface, no network."""

    def __init__(self, receipts=None, listings=None):
        self._receipts = receipts or []
        self._listings = listings or []
        self.calls = 0

    def get_receipts(self, min_created=None):
        self.calls += 1
        # Honour incremental filtering like the real API would.
        if min_created:
            return [r for r in self._receipts if int(r["created_timestamp"]) > min_created]
        return list(self._receipts)

    def get_listings(self, state="active"):
        return list(self._listings)


@pytest.fixture
def connector(config, db):
    config.etsy = {"transaction_fee_rate": 0.065, "payment_fee_rate": 0.04}
    client = StubEtsyClient(
        receipts=[_receipt(1001, 5001, 9001, 4800, qty=1)],
        listings=[_listing(9001, "Linen Throw", 4800, views=200, favourers=12)],
    )
    return EtsyConnector(config, db, client=client)


# --- Configuration --------------------------------------------------

def test_injected_client_is_configured(connector):
    assert connector.is_configured is True


def test_unconfigured_without_creds(config, db):
    config.etsy = {}
    assert EtsyConnector(config, db).is_configured is False


def test_configured_without_shop_id(config, db):
    # A valid token is enough; the shop id is resolved from getMe when reading.
    config.etsy = {"api_key": "k", "access_token": "t"}  # no shop_id
    assert EtsyConnector(config, db).is_configured is True


def test_unconfigured_sync_is_safe(config, db):
    config.etsy = {}
    result = EtsyConnector(config, db).sync()
    assert result["configured"] is False


def test_real_client_requires_credentials():
    with pytest.raises(EtsyConfigError):
        EtsyClient(api_key=None, access_token=None, shop_id=None)


# --- Order import + mapping -----------------------------------------

def test_sync_imports_and_maps_order(connector, db):
    # production cost comes from a registered product linked to a campaign.
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    campaign_id = db.insert_campaign({"name": "C", "brief_id": brief_id})
    db.insert_product({"sku": "9001", "name": "Linen Throw", "production_cost": 14,
                       "campaign_id": campaign_id})

    summary = connector.sync()
    assert summary["imported_orders"] == 1

    orders = db.get_orders_by_platform("etsy")
    assert len(orders) == 1
    o = orders[0]
    assert o["order_ref"] == "etsy-1001-5001"
    assert o["sale_price"] == 48.0
    assert o["product_id"] == "9001"
    assert o["campaign_id"] == campaign_id          # linked to campaign
    assert o["production_cost"] == 14.0             # from the product
    assert o["marketplace_fees"] == pytest.approx(48 * 0.065)
    assert o["payment_fees"] == pytest.approx(48 * 0.04)
    # net profit = 48 - (14 + 3.12 + 1.92) = 28.96
    assert o["net_profit"] == pytest.approx(28.96, abs=0.01)


def test_sync_is_incremental_and_never_duplicates(connector, db):
    first = connector.sync()
    assert first["imported_orders"] == 1
    # Re-syncing the same data imports nothing new (dedupe + cursor).
    second = connector.sync()
    assert second["imported_orders"] == 0
    assert len(db.get_orders_by_platform("etsy")) == 1


def test_new_order_after_cursor_is_imported(config, db):
    client = StubEtsyClient(receipts=[_receipt(1001, 5001, 9001, 4800)])
    conn = EtsyConnector(config, db, client=client)
    conn.sync()
    # A newer receipt arrives.
    client._receipts.append(
        {**_receipt(1002, 5002, 9001, 2900), "created_timestamp": _TS + 10}
    )
    result = conn.sync()
    assert result["imported_orders"] == 1
    assert len(db.get_orders_by_platform("etsy")) == 2


# --- Listings + stats -----------------------------------------------

def test_sync_imports_listings_and_stats(connector, db):
    connector.sync()
    listings = db.list_etsy_listings()
    assert len(listings) == 1
    assert listings[0]["listing_id"] == 9001
    assert listings[0]["num_favorers"] == 12

    stats = db.list_listing_stats()
    assert len(stats) == 1
    s = stats[0]
    assert s["favourites"] == 12
    assert s["visits"] == 200
    assert s["orders"] == 1            # one order recorded for this listing
    assert s["conversion_rate"] == pytest.approx(1 / 200, abs=1e-4)
    assert s["imported_at"]            # every event timestamped


def test_listing_linked_to_product_campaign_revenue_profit(connector, db):
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    campaign_id = db.insert_campaign({"name": "C", "brief_id": brief_id})
    db.insert_product({"sku": "9001", "production_cost": 14, "campaign_id": campaign_id})

    connector.sync()
    enriched = connector.listings()[0]
    assert enriched["product_id"] == "9001"
    assert enriched["campaign_id"] == campaign_id
    assert enriched["revenue"] == 48.0
    assert enriched["net_profit"] == pytest.approx(28.96, abs=0.01)
    assert enriched["orders"] == 1


def test_listing_upsert_does_not_duplicate(connector, db):
    connector.sync()
    connector.sync()
    assert len(db.list_etsy_listings()) == 1     # upserted, not duplicated
    assert len(db.list_listing_stats()) == 1     # one snapshot per (listing, day)


# --- Revenue engine / CEO metrics update ----------------------------

def test_sync_updates_revenue_engine_and_cash(connector):
    before = connector.profit.cash_balance()
    summary = connector.sync()
    after = connector.profit.cash_balance()
    assert after > before                         # net profit raised cash
    assert summary["metrics"]["net_profit"] == connector.revenue.company_profit()["net_profit"]
