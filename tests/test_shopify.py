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
        self.products_by_id = {}

    def create_product(self, payload):
        if self.fail:
            raise RuntimeError("Shopify 422 unprocessable")
        self.created.append(payload)
        product = {"handle": "linen-throw", "status": payload["product"]["status"]}
        if not self.no_id:
            product["id"] = self.product_id
        return {"product": product}

    def update_product(self, product_id, payload):
        self.product_updates = getattr(self, "product_updates", [])
        self.product_updates.append((str(product_id), payload))
        return {"product": {"id": product_id, "status": "active"}}

    def get_product(self, product_id):
        return {"product": self.products_by_id.get(str(product_id),
                                                   {"id": product_id})}

    def graphql(self, query, variables=None):
        self.graphql_calls = getattr(self, "graphql_calls", [])
        self.graphql_calls.append((query, variables))
        if "productCreateMedia" in query:
            return {"data": {"productCreateMedia":
                             {"media": [{"status": "UPLOADED"}], "mediaUserErrors": []}}}
        n = getattr(self, "existing_videos", {}).get(str((variables or {}).get("id")), 0)
        return {"data": {"product": {"media":
                {"edges": [{"node": {"mediaContentType": "VIDEO"}} for _ in range(n)]}}}}

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


def test_build_product_formats_description_as_html():
    from onassis.connectors.shopify import _text_to_html

    listing = {**_listing(), "description":
               "WHAT IT IS\nA soft linen throw.\n\nMade for slow mornings.\n\n"
               "- breathable linen\n- stonewashed finish"}
    body = ShopifyConnector.build_product(listing)["product"]["body_html"]
    assert "<h3>What It Is</h3>" in body          # caps line → heading
    assert "<p>A soft linen throw." in body       # paragraph
    assert "<ul><li>breathable linen</li>" in body  # bullets → list
    # Already-HTML descriptions are passed through unchanged.
    assert _text_to_html("<p>hi</p>") == "<p>hi</p>"
    assert _text_to_html("") == ""


def test_text_to_html_splits_runon_blob_into_paragraphs():
    """A description with no line breaks still becomes multiple paragraphs, and an
    inline ALL-CAPS label is lifted into a heading."""
    from onassis.connectors.shopify import _text_to_html

    blob = ("Bring the slow calm of the coast home. It is soft and warm. "
            "WHAT IT IS A generous rounded ceramic mug. Made for slow mornings. "
            "Style it in a light-filled corner and let it set the mood.")
    html = _text_to_html(blob)
    assert html.count("<p>") >= 2                  # real paragraphs, not one blob
    assert "<h3>What It Is</h3>" in html           # inline caps label → heading
    assert "<br>" not in html


def test_reformat_reflows_a_single_p_blob_and_keeps_structured_html():
    """A description already wrapped in one <p> (from an earlier pass) is
    re-flowed into real paragraphs/headings; well-structured HTML is left alone;
    and '&' / single-word labels become headings."""
    from onassis.connectors.shopify import _reformat_description

    blob = ("<p>Carries that feeling into your morning. MATERIALS &amp; FEEL Made "
            "from durable stoneware. CARE Dishwasher safe.</p>")
    out = _reformat_description(blob)
    assert out.count("<p>") >= 2
    assert "<h3>Materials &amp; Feel</h3>" in out or "<h3>Materials & Feel</h3>" in out
    assert "<h3>Care</h3>" in out
    # Already-structured content (2+ blocks) is returned unchanged.
    structured = "<h3>A</h3><p>one</p><p>two</p>"
    assert _reformat_description(structured) == structured

    # Labels buried inside SEVERAL <p> blocks are still lifted into headings
    # (the real Shopify case), and the result is idempotent.
    multi = ("<p>Intro sentence here. WHAT IT IS A generously sized mug.</p>"
             "<p>The illustration is crisp. SIZE &amp; USE Holds 325ml. CARE "
             "Dishwasher safe.</p>")
    fixed = _reformat_description(multi)
    for h in ("<h3>What It Is</h3>", "<h3>Care</h3>"):
        assert h in fixed
    assert ("<h3>Size & Use</h3>" in fixed or "<h3>Size &amp; Use</h3>" in fixed)
    assert _reformat_description(fixed) == fixed        # idempotent


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


def test_attach_product_videos_is_idempotent(config):
    """Videos attach via GraphQL; products that already have a video (or no url)
    are skipped, so re-running never duplicates media."""
    _configured(config)
    client = FakeAdminClient()
    client.existing_videos = {"gid://shopify/Product/200": 1}   # already has one
    conn = ShopifyConnector(config, client=client)
    res = conn.attach_product_videos([
        {"product_id": "100", "video_url": "https://cdn/x.mp4", "alt": "A"},  # add
        {"product_id": "200", "video_url": "https://cdn/y.mp4", "alt": "B"},  # skip
        {"product_id": "300", "video_url": "", "alt": "C"},                   # skip (no url)
    ])
    assert res["checked"] == 3 and res["added"] == 1 and res["skipped"] == 2
    assert any("productCreateMedia" in q for q, _ in client.graphql_calls)


def test_reformat_product_descriptions_in_place(config):
    """Existing plain-text product descriptions are re-rendered as HTML in place;
    ones already HTML are left untouched."""
    _configured(config)
    client = FakeAdminClient()
    client.products_by_id = {
        "100": {"id": 100, "body_html": "WHAT IT IS\nA soft throw.\n\nCosy and warm."},
        "200": {"id": 200, "body_html": "<p>Already formatted.</p>"},
    }
    res = ShopifyConnector(config, client=client).reformat_product_descriptions(["100", "200"])
    assert res["checked"] == 2 and res["updated"] == 1 and res["skipped"] == 1
    # Only product 100 was rewritten, with real HTML.
    updated = dict(client.product_updates)
    assert "100" in updated and "200" not in updated
    assert "<h3>What It Is</h3>" in updated["100"]["product"]["body_html"]


def test_live_blog_articles_and_update(config):
    """Connector primitives for the in-place rewrite: list live articles and PUT
    new fields (body/image) onto one, using the configured blog."""
    _configured(config)
    config.shopify["blog_id"] = 7
    client = FakeAdminClient()
    client.live_articles = [{"id": 1, "title": "A", "body_html": "x"}]
    conn = ShopifyConnector(config, client=client)
    assert [a["id"] for a in conn.live_blog_articles()] == [1]
    conn.update_blog_article("1", {"body_html": "<h2>Hi</h2>",
                                   "image": {"src": "https://cdn/x.jpg"}})
    blog_id, aid, payload = client.updates[0]
    assert blog_id == "7" and aid == "1"
    assert payload["article"]["body_html"] == "<h2>Hi</h2>"
    assert payload["article"]["image"]["src"] == "https://cdn/x.jpg"


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
