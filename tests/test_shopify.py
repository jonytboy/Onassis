"""Tests for Shopify publishing (Sprint 41) — second sales channel, fake client."""

from __future__ import annotations

import json

import pytest

from onassis.connectors.shopify import ShopifyConnector
from onassis.shopify_publisher import ShopifyPublisher


class FakeAdminClient:
    """Fake Shopify Admin client — records calls, returns a product id."""

    def __init__(self, product_id=99001, fail=False, no_id=False):
        self.product_id = product_id
        self.fail = fail
        self.no_id = no_id
        self.created = []
        self.images = []
        self.articles = []

    def create_product(self, payload):
        if self.fail:
            raise RuntimeError("Shopify 422 unprocessable")
        self.created.append(payload)
        product = {"handle": "linen-throw", "status": payload["product"]["status"]}
        if not self.no_id:
            product["id"] = self.product_id
        return {"product": product}

    def update_product(self, product_id, payload):
        return {"product": {"id": product_id, "status": "active"}}

    def add_product_image(self, product_id, image_path, *, position=1, alt_text=None):
        self.images.append((image_path, position))
        return {"image": {"id": len(self.images)}}

    def create_article(self, blog_id, payload):
        self.articles.append((blog_id, payload))
        return {"article": {"id": 555}}


def _configured(config):
    config.shopify = {"store_domain": "shop.myshopify.com", "admin_token": "tok",
                      "api_version": "2024-10"}
    return config


def _listing(cid=1, key="mug"):
    return {"title": "Linen Throw", "description": "Lovely.", "tags": ["a", "b"],
            "price": 30.0, "product_id": f"{cid}-{key}", "product_key": key,
            "images": [{"filename": "g0.jpg", "order": 1, "alt_text": "x"}]}


# --- Connector -------------------------------------------------------

def test_build_product_maps_listing():
    p = ShopifyConnector.build_product(_listing(), active=False)["product"]
    assert p["title"] == "Linen Throw" and p["status"] == "draft"
    assert p["variants"][0]["price"] == "30.00" and p["variants"][0]["sku"] == "1-mug"


def test_build_product_active_when_go_live():
    assert ShopifyConnector.build_product(_listing(), active=True)["product"]["status"] == "active"


def test_publish_product_creates_and_uploads(config, tmp_path):
    _configured(config)
    imgs = tmp_path / "images"
    imgs.mkdir()
    (imgs / "g0.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    client = FakeAdminClient()
    conn = ShopifyConnector(config, client=client)
    res = conn.publish_product(_listing(), images_dir=imgs)
    assert res["ok"] and res["product_id"] == "99001"
    assert res["images_uploaded"] == 1
    assert res["url"] == "https://shop.myshopify.com/products/linen-throw"


def test_publish_product_rejects_missing_id(config, tmp_path):
    _configured(config)
    conn = ShopifyConnector(config, client=FakeAdminClient(no_id=True))
    with pytest.raises(RuntimeError, match="product id"):
        conn.publish_product(_listing())


def test_connector_gate_off_without_creds(config):
    config.shopify = {}
    assert ShopifyConnector(config).can_publish is False


# --- Publisher (records a shopify publication) -----------------------

def test_publisher_records_shopify_publication(config, db, tmp_path):
    _configured(config)
    pub = ShopifyPublisher(config, db, connector=ShopifyConnector(config, client=FakeAdminClient()))
    res = pub.publish(1, "mug", _listing())
    assert res["status"] == "draft"
    pubs = [p for p in db.list_publications() if p["platform"] == "shopify"]
    assert len(pubs) == 1 and pubs[0]["listing_id"] == "99001"
    assert pubs[0]["product_id"] == "1-mug"


def test_publisher_dedupes(config, db):
    _configured(config)
    pub = ShopifyPublisher(config, db, connector=ShopifyConnector(config, client=FakeAdminClient()))
    pub.publish(1, "mug", _listing())
    again = pub.publish(1, "mug", _listing())
    assert again["status"] == "skipped"
    assert len([p for p in db.list_publications() if p["platform"] == "shopify"]) == 1


def test_publisher_not_configured_is_safe_noop(config, db):
    config.shopify = {}
    res = ShopifyPublisher(config, db).publish(1, "mug", _listing())
    assert res["status"] == "not_configured"
    assert db.list_publications() == []


def test_publisher_records_failure(config, db):
    _configured(config)
    pub = ShopifyPublisher(config, db,
                           connector=ShopifyConnector(config, client=FakeAdminClient(fail=True)))
    res = pub.publish(1, "mug", _listing())
    assert res["status"] == "failed"
    stored = [p for p in db.list_publications() if p["platform"] == "shopify"][0]
    assert stored["status"] == "failed" and stored["listing_id"] is None
    # A failure leaves no active publication, so a retry is possible.
    assert db.get_active_publication(1, "shopify", product_id="1-mug") is None
