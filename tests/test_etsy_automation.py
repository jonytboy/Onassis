"""Tests for the Etsy Automation Engine — write decisions back to Etsy, audited."""

from __future__ import annotations

import pytest

from onassis.etsy_automation import EtsyAutomationEngine


class FakeEtsyWrite:
    """Stub Etsy write client — records calls, returns canned inventory."""

    def __init__(self, *, price=22.0, fail_on=None):
        self.calls: list[tuple] = []
        self.price = price
        self.fail_on = fail_on or set()

    def update_listing(self, listing_id, fields):
        if "update_listing" in self.fail_on:
            raise RuntimeError("Etsy 400 boom")
        self.calls.append(("update_listing", listing_id, fields))
        return {"listing_id": listing_id, **fields}

    def deactivate_listing(self, listing_id):
        self.calls.append(("deactivate", listing_id))
        return self.update_listing(listing_id, {"state": "inactive"})

    def get_listing_inventory(self, listing_id):
        return {"products": [{"sku": "s", "property_values": [],
                              "offerings": [{"price": {"amount": int(self.price * 100),
                                                       "divisor": 100},
                                             "quantity": 10, "is_enabled": True}]}],
                "price_on_property": [], "quantity_on_property": [], "sku_on_property": []}

    def set_price_and_quantity(self, listing_id, *, price=None, quantity=None):
        if "price" in self.fail_on:
            raise RuntimeError("Etsy inventory 400")
        self.calls.append(("price", listing_id, price, quantity))
        return {"updated": True}

    def upload_listing_image(self, listing_id, path, *, rank=1, alt_text=None, overwrite=False):
        self.calls.append(("image", listing_id, path, rank))
        return {"listing_image_id": rank}


def _engine(config, db, client):
    return EtsyAutomationEngine(config, db, client=client)


def _live_product(db, sku="1-ceramic_mug", key="ceramic_mug", listing_id="555",
                  price=22.0, cost=7.5):
    db.insert_product({"sku": sku, "product_key": key, "campaign_id": 1,
                       "production_cost": cost})
    db.upsert_etsy_listing({"listing_id": int(listing_id), "product_id": sku,
                            "campaign_id": 1, "price": price, "state": "active"})
    db.insert_publication({"platform": "etsy", "product_id": sku, "campaign_id": 1,
                           "listing_id": listing_id, "mode": "live", "status": "live"})


def test_update_title_is_applied_and_audited(config, db):
    client = FakeEtsyWrite()
    out = _engine(config, db, client).update_title("555", "New Better Title", old="Old",
                                                    reason="A/B", source="learning")
    assert out["status"] == "applied"
    assert ("update_listing", "555", {"title": "New Better Title"}) in client.calls
    audit = db.list_etsy_changes("555")
    assert audit[0]["field"] == "title" and audit[0]["status"] == "applied"
    assert audit[0]["new_value"] == "New Better Title" and audit[0]["source"] == "learning"


def test_price_update_writes_inventory(config, db):
    client = FakeEtsyWrite()
    out = _engine(config, db, client).update_price("555", 26.5, old=22.0, reason="reprice")
    assert out["status"] == "applied"
    assert ("price", "555", 26.5, None) in client.calls


def test_unchanged_price_is_skipped_not_written(config, db):
    client = FakeEtsyWrite()
    out = _engine(config, db, client).update_price("555", 22.0, old=22.0)
    assert out["status"] == "skipped"
    assert client.calls == []                     # no write attempted
    assert db.list_etsy_changes("555")[0]["status"] == "skipped"


def test_tags_coerced_to_thirteen(config, db):
    client = FakeEtsyWrite()
    _engine(config, db, client).update_tags("555", [f"t{i}" for i in range(20)])
    _, _, fields = next(c for c in client.calls if c[0] == "update_listing")
    assert len(fields["tags"]) == 13


def test_failed_write_is_audited_not_raised(config, db):
    client = FakeEtsyWrite(fail_on={"update_listing"})
    out = _engine(config, db, client).update_title("555", "X", reason="r")
    assert out["status"] == "failed"
    row = db.list_etsy_changes("555")[0]
    assert row["status"] == "failed" and "boom" in row["error"]


def test_deactivate_retires_the_listing(config, db):
    client = FakeEtsyWrite()
    out = _engine(config, db, client).deactivate("555", reason="no sales")
    assert out["status"] == "applied"
    assert ("deactivate", "555") in client.calls
    assert db.list_etsy_changes("555")[0]["field"] == "state"


def test_apply_learning_actions_reprices_and_retires_on_etsy(config, db):
    _live_product(db, sku="1-ceramic_mug", key="ceramic_mug", listing_id="555")
    _live_product(db, sku="1-tote_bag", key="tote_bag", listing_id="777", cost=9.0)
    client = FakeEtsyWrite()
    digest = {"actions": {
        "retire": [{"sku": "1-tote_bag", "product_key": "tote_bag", "why": "loses money"}],
        "adjust": [{"sku": "1-ceramic_mug", "product_key": "ceramic_mug",
                    "improvements": ["reprice", "refresh_hero"], "why": "no conversion"}],
    }}
    out = EtsyAutomationEngine(config, db, client=client).apply_learning_actions(digest)
    assert out["applied"] == 2                    # one reprice + one deactivate
    assert ("deactivate", "777") in client.calls
    assert any(c[0] == "price" and c[1] == "555" for c in client.calls)


def test_reprice_is_blocked_by_financial_protection(config, db):
    _live_product(db, sku="1-ceramic_mug", key="ceramic_mug", listing_id="555")

    class DenyProtection:
        def guard_price_change(self, *a, **k):
            return {"approved": False, "reason": "protected profit below zero"}

    client = FakeEtsyWrite()
    eng = EtsyAutomationEngine(config, db, client=client, protection=DenyProtection())
    digest = {"actions": {"retire": [], "adjust": [
        {"sku": "1-ceramic_mug", "product_key": "ceramic_mug",
         "improvements": ["reprice"], "why": "no conversion"}]}}
    out = eng.apply_learning_actions(digest)
    assert out["applied"] == 0 and out["skipped"] == 1
    assert not any(c[0] == "price" for c in client.calls)      # nothing written to Etsy
    audit = db.list_etsy_changes("555")
    assert any("Financial Protection" in (r.get("reason") or "") for r in audit)


def test_not_configured_is_a_safe_noop(config, db):
    config.etsy = {}                              # no api key
    out = EtsyAutomationEngine(config, db).apply_learning_actions(
        {"actions": {"retire": [{"sku": "x"}], "adjust": []}})
    assert out["applied"] == 0 and "not configured" in out["reason"]


def test_retire_without_a_live_listing_is_skipped(config, db):
    db.insert_product({"sku": "1-mug", "product_key": "ceramic_mug", "campaign_id": 1})
    client = FakeEtsyWrite()
    digest = {"actions": {"retire": [{"sku": "1-mug", "product_key": "ceramic_mug"}],
                          "adjust": []}}
    out = EtsyAutomationEngine(config, db, client=client).apply_learning_actions(digest)
    assert out["skipped"] == 1 and client.calls == []
