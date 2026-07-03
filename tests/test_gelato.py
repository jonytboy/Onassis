"""Tests for the Gelato Fulfilment Engine — paid order -> production, true cost."""

from __future__ import annotations

import pytest

from onassis.connectors.gelato import GelatoConnector
from onassis.revenue import RevenueEngine

_ADDRESS = {
    "name": "Jane Doe", "first_line": "1 Harbour View", "second_line": "Flat 2",
    "city": "Brighton", "state": "East Sussex", "zip": "BN1 1AA",
    "country_iso": "GB", "email": "jane@example.com",
}


class FakeGelato:
    """A stub Gelato client — records payloads, returns canned responses."""

    def __init__(self, *, create=None, get=None, fail_times=0):
        self.created: list[dict] = []
        self._create = create or {"id": "gel-1", "fulfillmentStatus": "created"}
        self._get = get or {}
        self.fail_times = fail_times

    def create_order(self, payload):
        self.created.append(payload)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("Gelato 503 transient")
        return self._create

    def get_order(self, gelato_order_id):
        return {**self._get, "id": gelato_order_id}


def _setup_paid_order(config, db, *, cost=7.5, address=_ADDRESS):
    """A published product + its Etsy listing + a paid order for it."""
    # The product the publication points at (sku = "<cid>-<key>").
    db.insert_product({"sku": "1-ceramic_mug", "name": "Ceramic Mug", "campaign_id": 1,
                       "product_key": "ceramic_mug", "production_cost": cost})
    # The Etsy publication links listing_id 555 -> that product.
    db.insert_publication({"platform": "etsy", "product_id": "1-ceramic_mug",
                           "campaign_id": 1, "listing_id": "555", "mode": "live",
                           "status": "live"})
    # A paid Etsy order references the listing id and carries the buyer address.
    RevenueEngine(config, db).record_order({
        "order_ref": "etsy-900-1", "product_id": "555", "campaign_id": 1,
        "platform": "etsy", "sale_price": 22.0, "quantity": 1,
        "production_cost": cost, "shipping_address": address})
    return db.list_orders()[0]


@pytest.fixture
def gelato_cfg(config):
    config.gelato = {**(config.gelato or {}), "max_retries": 3,
                     "retry_backoff_seconds": 0, "file_base_url": "https://cdn.test",
                     "currency": "GBP"}
    return config


def test_paid_order_becomes_a_gelato_order(gelato_cfg, db):
    _setup_paid_order(gelato_cfg, db)
    client = FakeGelato()
    conn = GelatoConnector(gelato_cfg, db, client=client)
    out = conn.fulfil_new_orders()
    assert out["submitted"] == 1 and out["failed"] == 0
    # The payload maps product UID, quantity, print file, and the address.
    payload = client.created[0]
    assert payload["orderReferenceId"] == "etsy-900-1"
    item = payload["items"][0]
    assert item["productUid"] == "home-and-living_product_mug_11oz"   # from catalogue
    assert item["files"][0]["url"].endswith("/1/ceramic_mug/print_file.png")
    assert payload["shippingAddress"]["postCode"] == "BN1 1AA"
    assert payload["shippingAddress"]["country"] == "GB"
    # A fulfilment row is recorded.
    f = db.get_fulfilment("etsy-900-1")
    assert f["status"] == "created" and f["gelato_order_id"] == "gel-1"


def test_order_is_never_fulfilled_twice(gelato_cfg, db):
    _setup_paid_order(gelato_cfg, db)
    conn = GelatoConnector(gelato_cfg, db, client=FakeGelato())
    conn.fulfil_new_orders()
    again = conn.fulfil_new_orders()
    assert again["submitted"] == 0                # order_ref already fulfilled
    assert len(db.list_fulfilments()) == 1


def test_retries_then_succeeds_on_transient_failure(gelato_cfg, db):
    _setup_paid_order(gelato_cfg, db)
    client = FakeGelato(fail_times=2)             # first two attempts 503, third ok
    conn = GelatoConnector(gelato_cfg, db, client=client)
    out = conn.submit_order(db.list_orders()[0])
    assert out["status"] == "created"
    assert db.get_fulfilment("etsy-900-1")["attempts"] == 3


def test_permanent_failure_is_recorded_not_raised(gelato_cfg, db):
    _setup_paid_order(gelato_cfg, db)
    client = FakeGelato(fail_times=99)            # always fails
    conn = GelatoConnector(gelato_cfg, db, client=client)
    out = conn.submit_order(db.list_orders()[0])
    assert out["status"] == "failed"
    f = db.get_fulfilment("etsy-900-1")
    assert f["status"] == "failed" and "transient" in f["last_error"]


def test_status_sync_records_tracking_and_true_cost(gelato_cfg, db):
    _setup_paid_order(gelato_cfg, db, cost=7.5)
    get_resp = {
        "fulfillmentStatus": "shipped",
        "shipments": [{"trackingCode": "TRK123", "trackingUrl": "https://track/TRK123",
                       "shipmentMethodName": "DHL"}],
        "receipts": [{"totalAmount": 9.25}],       # ACTUAL production+ship cost
    }
    conn = GelatoConnector(gelato_cfg, db, client=FakeGelato(get=get_resp))
    conn.fulfil_new_orders()
    conn.sync_status()

    f = db.get_fulfilment("etsy-900-1")
    assert f["status"] == "shipped"
    assert f["tracking_number"] == "TRK123" and f["carrier"] == "DHL"
    assert f["actual_cost"] == pytest.approx(9.25)
    assert f["cost_booked"] == 1
    # The true-vs-estimate difference (9.25 - 7.50) is booked to the ledger once.
    adj = [e for e in db.list_ledger() if "Gelato true production cost" in (e.get("note") or "")]
    assert len(adj) == 1
    assert adj[0]["amount"] == pytest.approx(1.75)


def test_cost_is_booked_only_once(gelato_cfg, db):
    _setup_paid_order(gelato_cfg, db)
    get_resp = {"fulfillmentStatus": "shipped", "receipts": [{"totalAmount": 9.25}]}
    conn = GelatoConnector(gelato_cfg, db, client=FakeGelato(get=get_resp))
    conn.fulfil_new_orders()
    conn.sync_status()
    conn.sync_status()                             # a second poll must not double-book
    adj = [e for e in db.list_ledger() if "Gelato true production cost" in (e.get("note") or "")]
    assert len(adj) == 1


def test_not_configured_is_a_safe_noop(config, db):
    config.gelato = {}                             # no api key, no file base url
    conn = GelatoConnector(config, db)
    assert conn.can_fulfil is False
    out = conn.fulfil_new_orders()
    assert out["submitted"] == 0 and "not configured" in out["reason"]


def test_missing_address_fails_cleanly(gelato_cfg, db):
    _setup_paid_order(gelato_cfg, db, address={"name": "No Address"})  # no line/country
    conn = GelatoConnector(gelato_cfg, db, client=FakeGelato())
    out = conn.submit_order(db.list_orders()[0])
    assert out["status"] == "failed" and "address" in out["reason"]
