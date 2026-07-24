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
        if self.no_id:
            return {"article": {}}
        return {"article": {"id": 555, "handle": "linen-throw-story"}}

    def get_article(self, blog_id, article_id):
        if getattr(self, "verify_missing", False):
            return {"article": {}}
        return {"article": {"id": int(article_id), "handle": "linen-throw-story",
                            "url": f"https://shop.myshopify.com/blogs/{blog_id}/linen-throw-story"}}

    def list_articles(self, blog_id):
        return {"articles": list(getattr(self, "live_articles", []))}

    def update_article(self, blog_id, article_id, payload):
        self.updates = getattr(self, "updates", [])
        self.updates.append((blog_id, article_id, payload))
        return {"article": {"id": int(article_id)}}


def _configured(config):
    config.shopify = {"store_domain": "shop.myshopify.com", "client_id": "cid",
                      "client_secret": "csecret", "api_version": "2024-10"}
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


def test_list_blogs(config):
    _configured(config)
    client = FakeAdminClient()
    client.list_blogs = lambda: {"blogs": [{"id": 1, "title": "News"},
                                           {"id": 2, "title": "Journal"}]}
    blogs = ShopifyConnector(config, client=client).list_blogs()
    assert blogs == [{"id": "1", "title": "News"}, {"id": "2", "title": "Journal"}]


def test_publish_article_verifies_and_returns_url(config):
    _configured(config)
    config.shopify["blog_id"] = 7
    conn = ShopifyConnector(config, client=FakeAdminClient())
    res = conn.publish_article({"title": "A Mediterranean Morning", "body": "…"})
    assert res["ok"] and res["verified"] is True
    assert res["id"] == "555"
    assert res["url"] == "https://shop.myshopify.com/blogs/7/linen-throw-story"


def test_publish_article_url_uses_the_blog_handle(config):
    """Storefront URLs use the blog HANDLE, not its id (an id URL 404s and looks
    like it never posted). Also returns an admin URL."""
    _configured(config)
    config.shopify["blog_id"] = 7
    client = FakeAdminClient()
    client.list_blogs = lambda: {"blogs": [{"id": 7, "handle": "news"}]}
    res = ShopifyConnector(config, client=client).publish_article(
        {"title": "A Mediterranean Morning", "body": "…"})
    assert res["url"] == "https://shop.myshopify.com/blogs/news/linen-throw-story"
    assert res["admin_url"] == "https://shop.myshopify.com/admin/blogs/7/articles/555"
    # Published immediately via `published: true`. We must NOT send our own
    # published_at — a server clock ahead of Shopify's would make it a future
    # (scheduled, hidden) post, emptying the blog.
    _, payload = client.articles[0]
    assert payload["article"]["published"] is True
    assert "published_at" not in payload["article"]


def test_blog_diagnostics_reveals_posts_on_the_wrong_blog(config):
    """The diagnostic reads every blog + its article count, so posts that landed
    on a different blog than the selected one are obvious."""
    _configured(config)
    config.shopify["blog_id"] = 7                     # operator selected blog 7…
    client = FakeAdminClient()
    client.list_blogs = lambda: {"blogs": [{"id": 7, "handle": "news", "title": "News"},
                                           {"id": 9, "handle": "journal", "title": "Journal"}]}
    # …but every article actually lives on blog 9.
    def _articles(blog_id):
        if str(blog_id) == "9":
            return {"articles": [{"id": 1, "title": "Morning", "handle": "morning",
                                  "published": True, "published_at": "2020-01-01T00:00:00+00:00"}]}
        return {"articles": []}
    client.list_articles = _articles
    dx = ShopifyConnector(config, client=client).blog_diagnostics()
    assert dx["selected_blog_id"] == "7"
    by_id = {b["id"]: b for b in dx["blogs"]}
    assert by_id["7"]["articles"] == 0 and by_id["7"]["selected"] is True
    assert by_id["9"]["articles"] == 1                # the posts are here!
    assert dx["selected_articles"] == []             # nothing on the selected blog


def test_republish_hidden_makes_scheduled_articles_live(config):
    """Recovery: articles stuck with a FUTURE published_at (clock skew) are
    re-published to now; already-live ones are left alone."""
    _configured(config)
    config.shopify["blog_id"] = 7
    client = FakeAdminClient()
    updated = []
    client.list_articles = lambda blog_id: {"articles": [
        {"id": 1, "published": True, "published_at": "2999-01-01T00:00:00+00:00"},  # future → hidden
        {"id": 2, "published": False, "published_at": None},                         # unpublished
        {"id": 3, "published": True, "published_at": "2020-01-01T00:00:00+00:00"},   # already live
    ]}
    client.update_article = lambda blog_id, aid, payload: updated.append((aid, payload)) or {"article": {"id": aid}}
    res = ShopifyConnector(config, client=client).republish_hidden()
    assert res["checked"] == 3 and res["fixed"] == 2 and res["already_live"] == 1
    # The two hidden ones were re-published with published_at cleared.
    assert {aid for aid, _ in updated} == {"1", "2"}
    assert all(p["article"]["published"] is True and p["article"]["published_at"] is None
               for _, p in updated)


def test_rewrite_blog_articles_updates_matches_in_place(config):
    """Existing live posts are rewritten in place, matched by title (case/space
    insensitive); unmatched posts are left untouched, never deleted."""
    _configured(config)
    config.shopify["blog_id"] = 7
    client = FakeAdminClient()
    client.live_articles = [
        {"id": 1, "title": "Riviera Mug: Mediterranean for Your Home"},   # match
        {"id": 2, "title": "  riviera mug: mediterranean for your home "},  # match (normalised)
        {"id": 3, "title": "Some Unrelated Post"},                        # no match → skip
    ]
    new = {"Riviera Mug: Mediterranean for Your Home":
           {"body": "<h2>Hi</h2>", "keywords": ["mug"],
            "image": "https://cdn/x.jpg"}}
    res = ShopifyConnector(config, client=client).rewrite_blog_articles(new)
    assert res["checked"] == 3 and res["rewritten"] == 2 and res["skipped"] == 1
    # Both matches got the new HTML body + featured image; the odd one wasn't touched.
    touched = {aid for _, aid, _ in client.updates}
    assert touched == {"1", "2"}
    body = client.updates[0][2]["article"]
    assert body["body_html"] == "<h2>Hi</h2>" and body["image"]["src"] == "https://cdn/x.jpg"


def test_publish_article_fails_when_unverifiable(config):
    _configured(config)
    config.shopify["blog_id"] = 7
    client = FakeAdminClient()
    client.verify_missing = True                # created, but not retrievable
    res = ShopifyConnector(config, client=client).publish_article({"title": "x", "body": "y"})
    assert res["ok"] is False and res["verified"] is False
    assert "not found" in res["error"].lower()


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
