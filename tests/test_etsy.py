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


def test_create_draft_exposes_etsy_validation_body(monkeypatch):
    """On a 4xx, the publisher's draft client surfaces Etsy's full response
    body (field-level validation errors), not just the bare status line."""
    from onassis.connectors import etsy_client as ec

    class _Resp:
        status_code = 400
        text = "raw text"

        def json(self):
            return {
                "error": "Required field 'taxonomy_id' is missing",
                "error_description": "taxonomy_id must be a valid Etsy taxonomy id",
            }

    monkeypatch.setattr(ec.httpx, "post", lambda *a, **k: _Resp())
    client = ec.EtsyDraftClient(api_key="k", shop_id="9", access_token="t")

    with pytest.raises(ec.EtsyApiError) as excinfo:
        client.create_draft({"title": "T", "description": "D",
                             "shipping_profile_id": "123456", "readiness_state_id": "77"})

    msg = str(excinfo.value)
    assert "400" in msg
    assert "taxonomy_id" in msg  # the real field-level error is exposed
    assert "must be a valid Etsy taxonomy id" in msg


def test_create_draft_sends_shipping_profile_id_as_int(monkeypatch):
    """Etsy requires shipping_profile_id as an int. Config may supply a string,
    so the draft client must coerce it AND send JSON so the int type survives on
    the wire (form-urlencoded would stringify it)."""
    from onassis.connectors import etsy_client as ec

    captured = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"listing_id": 4242}

    def _fake_post(url, headers=None, json=None, timeout=None, **_):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(ec.httpx, "post", _fake_post)
    client = ec.EtsyDraftClient(api_key="k", shop_id="9", access_token="t")
    client.create_draft({
        "title": "T", "description": "D",
        "shipping_profile_id": "123456", "readiness_state_id": "77",
    })

    # Sent as JSON with shipping_profile_id as an actual int (not "123456").
    assert captured["json"]["shipping_profile_id"] == 123456
    assert isinstance(captured["json"]["shipping_profile_id"], int)


def test_resolve_shipping_profile_id_uses_first_active():
    """Mirrors shop-id resolution: pick the first active profile, then cache it."""
    from onassis.connectors import etsy_client as ec

    client = ec.EtsyClient(api_key="k", shop_id="9", access_token="t")
    client.get_shipping_profiles = lambda: [
        {"shipping_profile_id": 111, "title": "Old", "is_deleted": True},
        {"shipping_profile_id": 222, "title": "Standard", "is_deleted": False},
        {"shipping_profile_id": 333, "title": "Express", "is_deleted": False},
    ]
    assert client.resolve_shipping_profile_id() == 222
    # Cached — not re-fetched.
    client.get_shipping_profiles = lambda: []
    assert client.resolve_shipping_profile_id() == 222


def test_resolve_shipping_profile_id_raises_when_none_exist():
    from onassis.connectors import etsy_client as ec

    client = ec.EtsyClient(api_key="k", shop_id="9", access_token="t")
    client.get_shipping_profiles = lambda: []
    with pytest.raises(ec.EtsyApiError):
        client.resolve_shipping_profile_id()


def test_create_draft_auto_resolves_shipping_profile_when_unset(monkeypatch):
    """When no shipping_profile_id is configured, create_draft fetches the shop's
    profiles and sends the resolved id (as an int) — like shop-id resolution."""
    from onassis.connectors import etsy_client as ec

    captured = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"listing_id": 4242}

    def _fake_post(url, headers=None, json=None, timeout=None, **_):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(ec.httpx, "post", _fake_post)
    client = ec.EtsyDraftClient(api_key="k", shop_id="9", access_token="t")
    client.get_shipping_profiles = lambda: [
        {"shipping_profile_id": 555, "title": "Standard", "is_deleted": False},
    ]
    client.get_readiness_state_definitions = lambda: [
        {"readiness_state_id": 88, "readiness_state": "made_to_order"},
    ]
    # No shipping_profile_id in the listing package.
    client.create_draft({"title": "T", "description": "D"})

    assert captured["json"]["shipping_profile_id"] == 555
    assert isinstance(captured["json"]["shipping_profile_id"], int)


# --- readiness_state_id (required for physical listings) -------------

def test_create_draft_sanitises_materials_invalid_characters(monkeypatch):
    """Etsy rejects materials with anything but letters/numbers/whitespace
    (invalid_characters). The client sanitises the field (without removing it)."""
    from onassis.connectors import etsy_client as ec

    captured = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"listing_id": 4242}

    def _fake_post(url, headers=None, json=None, timeout=None, **_):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(ec.httpx, "post", _fake_post)
    client = ec.EtsyDraftClient(api_key="k", shop_id="9", access_token="t")
    client.create_draft({
        "title": "T", "description": "D",
        "shipping_profile_id": "1", "readiness_state_id": "2",
        # The exact kind of LLM-generated values Etsy rejects.
        "materials": ["100% organic cotton", "hand-dyed linen", "brass & wood", "!!!"],
    })

    # Disallowed chars -> space, whitespace collapsed, empties dropped; field kept.
    assert captured["json"]["materials"] == [
        "100 organic cotton", "hand dyed linen", "brass wood",
    ]


def test_resolve_readiness_state_id_uses_first_active():
    """readiness_state_id is a shop-specific id resolved from the shop, like the
    shipping profile: first active definition, then cached."""
    from onassis.connectors import etsy_client as ec

    client = ec.EtsyClient(api_key="k", shop_id="9", access_token="t")
    client.get_readiness_state_definitions = lambda: [
        {"readiness_state_id": 11, "readiness_state": "ready_to_ship", "is_deleted": True},
        {"readiness_state_id": 22, "readiness_state": "made_to_order", "is_deleted": False},
    ]
    assert client.resolve_readiness_state_id() == 22
    # Cached — not re-fetched.
    client.get_readiness_state_definitions = lambda: []
    assert client.resolve_readiness_state_id() == 22


def test_resolve_readiness_state_id_raises_when_none_exist():
    from onassis.connectors import etsy_client as ec

    client = ec.EtsyClient(api_key="k", shop_id="9", access_token="t")
    client.get_readiness_state_definitions = lambda: []
    with pytest.raises(ec.EtsyApiError):
        client.resolve_readiness_state_id()


def test_create_draft_auto_resolves_readiness_state_when_unset(monkeypatch):
    """When no readiness_state_id is configured, create_draft resolves it from
    the shop and sends it as an int (Etsy requires it for physical listings)."""
    from onassis.connectors import etsy_client as ec

    captured = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"listing_id": 4242}

    def _fake_post(url, headers=None, json=None, timeout=None, **_):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(ec.httpx, "post", _fake_post)
    client = ec.EtsyDraftClient(api_key="k", shop_id="9", access_token="t")
    client.get_shipping_profiles = lambda: [
        {"shipping_profile_id": 555, "is_deleted": False},
    ]
    client.get_readiness_state_definitions = lambda: [
        {"readiness_state_id": 99, "readiness_state": "made_to_order"},
    ]
    client.create_draft({"title": "T", "description": "D"})  # neither id configured

    assert captured["json"]["readiness_state_id"] == 99
    assert isinstance(captured["json"]["readiness_state_id"], int)


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
    # Real Etsy fees: transaction 6.5% + £0.20 listing + blended Offsite Ads
    # (15% × 30% attributed) = 3.12 + 0.20 + 2.16 = 5.48 marketplace;
    # payment 4% + £0.20 = 2.12.
    assert o["marketplace_fees"] == pytest.approx(5.48, abs=0.01)
    assert o["payment_fees"] == pytest.approx(2.12, abs=0.01)
    # net profit = 48 - (14 + 5.48 + 2.12) = 26.40
    assert o["net_profit"] == pytest.approx(26.40, abs=0.01)


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
    # net profit after real Etsy fees = 48 - (14 + 5.48 + 2.12) = 26.40
    assert enriched["net_profit"] == pytest.approx(26.40, abs=0.01)
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


class _VideoStubClient:
    """Stub with the listing-video surface — records uploads, no network."""

    def __init__(self, existing=None):
        self._existing = existing or {}          # listing_id -> [videos]
        self.uploads = []

    def get_listing_videos(self, listing_id):
        return self._existing.get(listing_id, [])

    def upload_listing_video(self, listing_id, video_path, *, name=None):
        self.uploads.append((listing_id, video_path, name))
        return {"video_id": 1}


def test_attach_listing_videos_is_idempotent(config, db, tmp_path):
    """Uploads a video per listing; skips listings that already have one or whose
    local clip is missing — safe to re-run without duplicating."""
    clip = tmp_path / "style_slide.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    client = _VideoStubClient(existing={20: [{"video_id": 9}]})   # 20 already has one
    conn = EtsyConnector(config, db, client=client)
    res = conn.attach_listing_videos([
        {"listing_id": 10, "video_path": str(clip), "name": "Mug"},     # upload
        {"listing_id": 20, "video_path": str(clip), "name": "Bowl"},    # skip (has one)
        {"listing_id": 30, "video_path": str(tmp_path / "gone.mp4"), "name": "X"},  # skip (missing)
    ])
    assert res["checked"] == 3 and res["added"] == 1 and res["skipped"] == 2
    assert client.uploads == [(10, str(clip), "Mug")]
