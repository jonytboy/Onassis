"""Tests for the Channel Distributor (Sprint 41) — IG/FB/Blog/Email, fakes."""

from __future__ import annotations

from onassis.distribution import ChannelDistributor


class FakeFacebook:
    can_publish = True
    def __init__(self): self.calls = 0
    def post(self, body, link=None):
        self.calls += 1
        return {"ok": True, "ref": "fb1"}


class FakeInstagram:
    can_publish = True
    def __init__(self): self.calls = 0
    def post(self, caption, image_url=None):
        self.calls += 1
        if not image_url:
            return {"ok": False, "skipped": True, "reason": "no image url"}
        return {"ok": True, "ref": "ig1"}


class FakeEmail:
    can_publish = True
    def __init__(self): self.calls = 0
    def send(self, subject, body, to=None):
        self.calls += 1
        return {"ok": True, "ref": "em1"}


class FakeShopifyBlog:
    can_publish = True
    def __init__(self): self.published = []
    def publish_article(self, payload):
        self.published.append(payload.get("title"))
        return {"ok": True, "id": str(len(self.published)),
                "url": f"https://shop/blogs/news/{len(self.published)}"}


def _distributor(config, db, **kw):
    return ChannelDistributor(config, db, instagram=FakeInstagram(), facebook=FakeFacebook(),
                              email=FakeEmail(), shopify=FakeShopifyBlog(), **kw)


def _seed(db, channel, payload, url="https://etsy.com/listing/1"):
    return db.insert_marketing_asset({"campaign_id": 1, "product_key": "mug",
                                      "listing_id": "1", "listing_url": url,
                                      "channel": channel, "payload": payload})


def _enable_fb(db):
    db.set_setting("business.facebook_enabled", True)


def test_distributes_all_channels(config, db):
    config.shopify = {"blog_id": 7}
    _enable_fb(db)
    _seed(db, "facebook", {"post": {"body": "hi", "link": "u"}})
    _seed(db, "instagram", {"captions": ["cap"], "image_url": "http://img/x.jpg"})
    _seed(db, "email", {"subject": "s", "body": "b"})
    _seed(db, "blog", {"title": "t", "body": "b", "keywords": ["k"]})

    r = _distributor(config, db).distribute()
    assert r["processed"] == 4 and r["posted"] == 4 and r["failed"] == 0
    # Every asset is now marked delivered.
    assert db.list_pending_marketing_assets(channels=["facebook", "instagram", "blog", "email"]) == []
    posted = db.count_marketing_assets_by_status("posted")
    assert posted == 4


def test_distribution_is_idempotent(config, db):
    config.shopify = {"blog_id": 7}
    _seed(db, "email", {"subject": "s", "body": "b"})
    dist = _distributor(config, db)
    dist.distribute()
    again = dist.distribute()
    assert again["processed"] == 0        # nothing re-sent


def test_instagram_without_image_is_skipped_not_failed(config, db):
    _seed(db, "instagram", {"captions": ["cap"]})   # no image_url
    r = _distributor(config, db).distribute()
    assert r["skipped"] == 1 and r["failed"] == 0
    asset = db.list_marketing_assets(channel="instagram")[0]
    assert asset["status"] == "skipped"


def test_facebook_disabled_in_settings_is_skipped(config, db):
    # facebook_enabled defaults False in Business Settings.
    _seed(db, "facebook", {"post": {"body": "hi", "link": "u"}})
    r = _distributor(config, db).distribute()
    assert r["skipped"] == 1
    asset = db.list_marketing_assets(channel="facebook")[0]
    assert "disabled" in (asset["delivery_error"] or "")


def test_unconfigured_channels_skip_safely(config, db):
    config.shopify = {}
    # Real (unconfigured) connectors → safe no-op skips, nothing crashes.
    _seed(db, "blog", {"title": "t", "body": "b"})
    _seed(db, "email", {"subject": "s", "body": "b"})
    dist = ChannelDistributor(config, db)   # real gated connectors
    r = dist.distribute()
    assert r["processed"] == 2 and r["posted"] == 0
    assert r["skipped"] == 2 and r["failed"] == 0


# --- Sprint 41.1: Business Settings toggles are operational controls ---

def test_blog_publishes_multiple_articles_with_urls(config, db):
    config.shopify = {"blog_id": 7}
    shop = FakeShopifyBlog()
    _seed(db, "blog", {"cta_link": "u", "articles": [
        {"title": "Launch"}, {"title": "Gift Guide"}, {"title": "Lifestyle"}]})
    dist = ChannelDistributor(config, db, instagram=FakeInstagram(), facebook=FakeFacebook(),
                              email=FakeEmail(), shopify=shop)
    r = dist.distribute()
    assert r["posted"] == 1 and len(shop.published) == 3      # all 3 SEO articles
    asset = db.list_marketing_assets(channel="blog")[0]
    assert asset["status"] == "posted" and "https://shop/blogs" in asset["delivery_ref"]


def test_distribute_can_target_only_the_shopify_blog(config, db):
    """Sprint 44.2 — the Shopify blog publishes directly even when social goes
    to Make; distribute(channels=['blog']) touches only the blog."""
    config.shopify = {"blog_id": 7}
    shop = FakeShopifyBlog()
    _seed(db, "blog", {"articles": [{"title": "Launch"}]})
    _seed(db, "facebook", {"post": {"body": "hi"}})       # must NOT be touched
    dist = ChannelDistributor(config, db, instagram=FakeInstagram(), facebook=FakeFacebook(),
                              email=FakeEmail(), shopify=shop)
    r = dist.distribute(channels=["blog"])
    assert r["processed"] == 1 and r["posted"] == 1 and len(shop.published) == 1
    assert db.list_marketing_assets(channel="facebook")[0]["status"] != "posted"


def test_failed_delivery_can_be_retried(config, db):
    _seed(db, "email", {"subject": "s", "body": "b"})
    # First run with an email connector that fails -> recorded failed (not skipped).

    class BoomEmail:
        can_publish = True
        def send(self, subject, body, to=None):
            raise RuntimeError("smtp down")
    d1 = ChannelDistributor(config, db, instagram=FakeInstagram(), facebook=FakeFacebook(),
                            email=BoomEmail(), shopify=FakeShopifyBlog())
    assert d1.distribute()["failed"] == 1
    assert db.list_marketing_assets(channel="email")[0]["status"] == "failed"
    # Retry with a working connector -> re-queued and delivered.
    d2 = ChannelDistributor(config, db, instagram=FakeInstagram(), facebook=FakeFacebook(),
                            email=FakeEmail(), shopify=FakeShopifyBlog())
    r = d2.retry_failed()
    assert r["requeued"] == 1 and r["posted"] == 1
    assert db.list_marketing_assets(channel="email")[0]["status"] == "posted"


def test_marketing_off_skips_every_channel(config, db):
    config.shopify = {"blog_id": 7}
    db.set_setting("business.marketing_enabled", False)
    for ch in ("instagram", "email", "blog"):
        _seed(db, ch, {"captions": ["c"], "subject": "s", "body": "b", "title": "t"})
    r = _distributor(config, db).distribute()
    assert r["posted"] == 0 and r["skipped"] == 3


def test_facebook_toggle_off_then_on(config, db):
    _seed(db, "facebook", {"post": {"body": "hi", "link": "u"}})
    # OFF (default) → skipped, nothing posted.
    fb = FakeFacebook()
    d1 = ChannelDistributor(config, db, instagram=FakeInstagram(), facebook=fb,
                            email=FakeEmail(), shopify=FakeShopifyBlog())
    assert d1.distribute()["skipped"] == 1 and fb.calls == 0
    # ON → posted.
    db.set_setting("business.facebook_enabled", True)
    _seed(db, "facebook", {"post": {"body": "hi2", "link": "u"}})
    fb2 = FakeFacebook()
    d2 = ChannelDistributor(config, db, instagram=FakeInstagram(), facebook=fb2,
                            email=FakeEmail(), shopify=FakeShopifyBlog())
    r = d2.distribute()
    assert r["posted"] == 1 and fb2.calls == 1
