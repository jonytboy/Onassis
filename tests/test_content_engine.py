"""Tests for the Content Engine (Sprint 48) — short-form video factory."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from onassis.content_engine import ContentEngine
from onassis.reel_studio import ReelStudio


def _stub_encoder(frames, out_path, *, fps):
    Path(out_path).write_bytes(b"\x00\x00\x00\x18ftypmp42")   # pretend mp4
    return out_path


def _build_package(config, tmp_path, cid=1, key="ceramic_mug"):
    """Write a minimal built listing package (listing.json + gallery images)."""
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    folder = tmp_path / "exports" / str(cid) / key
    images = folder / "images"
    images.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (600, 600), (240, 232, 222)).save(folder / "master_artwork.png")
    manifest = []
    for i, scene in enumerate(["hero", "lifestyle", "room", "closeup"]):
        fn = f"{scene}.jpg"
        Image.new("RGB", (600, 600), (200, 150, 120)).save(images / fn)
        manifest.append({"order": i, "mockup_type": scene, "filename": fn, "alt_text": scene})
    (folder / "listing.json").write_text(json.dumps({
        "title": "Casa Med Ceramic Mug", "product_name": "Casa Med Mug",
        "theme": "the Amalfi coast", "tags": ["mediterranean", "mug", "coastal"],
        "price": 22.0, "listing_url": "https://etsy.com/listing/1",
        "mockup_manifest": manifest,
    }), encoding="utf-8")
    return cid, key


def _engine(config, db) -> ContentEngine:
    # Tiny frames so rendering is instant in tests (no ffmpeg either way).
    config.content = {"reel_size": [96, 170], "reel_slide_frames": 2}
    return ContentEngine(config, db, studio=ReelStudio(encoder=_stub_encoder))


def test_build_for_product_makes_one_clip_per_format(config, db, tmp_path):
    cid, key = _build_package(config, tmp_path)
    r = _engine(config, db).build_for_product(cid, key)
    assert r["ok"] and r["count"] == 3
    fmts = {c["fmt"] for c in r["clips"]}
    assert fmts == {"style_slide", "product_in_use", "gifting"}
    # Each clip has a real mp4 file, a caption, and hashtags.
    for c in r["clips"]:
        assert Path(c["path"]).exists()
        assert c["caption"] and c["hashtags"]
        assert (tmp_path / "exports" / "reels" / str(cid) / key / f"{c['fmt']}.mp4").exists()
        assert (tmp_path / "exports" / "reels" / str(cid) / key / f"{c['fmt']}.json").exists()
    # Persisted to the queue.
    assert db.count_short_form() == 3
    assert db.count_short_form(status="queued") == 3


def test_build_skips_products_without_a_package(config, db, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    r = _engine(config, db).build_for_product(99, "ghost")
    assert r["ok"] is False and "no Shopify images" in r["reason"]
    assert db.count_short_form() == 0


def test_caption_and_hashtags_reflect_the_product(config, db, tmp_path):
    cid, key = _build_package(config, tmp_path)
    clips = _engine(config, db).build_for_product(cid, key)["clips"]
    gifting = next(c for c in clips if c["fmt"] == "gifting")
    assert "gift" in gifting["caption"].lower()
    assert any(h.startswith("#") for h in gifting["hashtags"])
    assert "#mediterraneanstyle" in gifting["hashtags"]


def test_generate_blog_creates_articles_on_demand(config, db, tmp_path):
    """Blog articles can be generated for existing products without a production
    run (deterministic, no LLM) — closing the 'no articles to publish' gap."""
    cid, key = _build_package(config, tmp_path)
    db.insert_product({"sku": f"{cid}-{key}", "name": "Mug", "campaign_id": cid,
                       "product_key": key})
    eng = _engine(config, db)
    assert db.count_marketing_assets(channel="blog") == 0
    r = eng.generate_blog()
    assert r["generated"] == 1
    assets = db.list_marketing_assets(channel="blog")
    assert len(assets) == 1
    assert assets[0]["payload"].get("articles")          # real SEO articles
    # Idempotent: a product that already has blog articles isn't duplicated.
    assert eng.generate_blog()["generated"] == 0


def test_generate_blog_covers_products_without_a_campaign_key(config, db, tmp_path):
    """A product that predates the campaign_id/product_key columns still gets an
    article (key derived from the sku) — a missing key must not be a silent skip."""
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    db.insert_product({"sku": "LINEN-THROW", "name": "Linen Throw"})   # no cid/key
    eng = _engine(config, db)
    r = eng.generate_blog()
    assert r["generated"] == 1 and r["products"] == 1
    assets = db.list_marketing_assets(channel="blog")
    assert len(assets) == 1 and assets[0]["product_key"] == "LINEN-THROW"
    assert assets[0]["payload"].get("articles")
    # And it de-dupes on the derived key (no silent duplicate on a second run).
    assert eng.generate_blog()["generated"] == 0


class _FakeShopifyConn:
    """Stand-in for the Shopify connector: serves live articles and records the
    in-place updates the rewrite makes."""

    can_publish = True

    def __init__(self, live):
        self._live = live
        self.updates = []

    def live_blog_articles(self):
        return self._live

    def update_blog_article(self, article_id, fields, *, blog_id=None):
        self.updates.append((str(article_id), fields))
        return {"article": {"id": article_id}}


def test_rewrite_live_articles_matches_by_embedded_etsy_link(config, db):
    """Live posts are matched to a product by the Etsy listing URL in their body
    (robust to title drift), not by a regenerated title. Matched posts get the
    fresh HTML body + Shopify link; unmappable ones are reported, not touched."""
    cid = 5
    config.shopify = {**(config.shopify or {}), "blog_id": "7"}
    db.insert_product({"sku": "MUG", "name": "Riviera Mug", "campaign_id": cid,
                       "product_key": "mug", "active": True})
    db.insert_publication({"platform": "etsy", "campaign_id": cid,
                           "product_id": f"{cid}-mug", "listing_id": "4538228548",
                           "status": "live", "mode": "live"})
    eng = _engine(config, db)
    live = [
        # Title has drifted from what we'd generate now, but the body carries the
        # Etsy listing link — so it still maps to the mug product.
        {"id": 11, "title": "Totally Different Old Title",
         "body_html": "See https://www.etsy.com/listing/4538228548 for details."},
        {"id": 22, "title": "Unrelated", "body_html": "no product link here"},
    ]
    eng._shopify_conn = _FakeShopifyConn(live)
    res = eng.rewrite_live_blog_articles()
    assert res["ok"] and res["checked"] == 2
    assert res["rewritten"] == 1 and res["skipped"] == 1        # matched by Etsy id
    updated_id, fields = eng._shopify_conn.updates[0]
    assert updated_id == "11"
    assert "<h2>" in fields["body_html"]                        # fresh HTML body
    assert "/products/" in fields["body_html"] or "etsy.com" in fields["body_html"]


def test_refill_blog_schedule_fills_forward_and_recycles(config, db, tmp_path):
    """The evergreen queue keeps a forward schedule filled — one post per day over
    the horizon — cycling the catalogue's content when unique variants run out, and
    it's idempotent (never over-fills past the horizon)."""
    db.insert_product({"sku": "MUG", "name": "Mug"})     # one product → 4 angles = 4 variants
    eng = _engine(config, db)
    r = eng.refill_blog_schedule(per_day=1, horizon_days=10)
    assert r["created"] == 10 and r["scheduled"] == 10   # 10-day horizon filled
    assert r["pool"] == 4 and r["next"] and r["last"]    # recycles the 4 variants
    dates = [a["scheduled_date"] for a in db.list_marketing_assets(channel="blog")]
    assert len(set(dates)) == 10                         # exactly one per day
    # Idempotent — a second pass adds nothing (queue already full to the horizon).
    assert eng.refill_blog_schedule(per_day=1, horizon_days=10)["created"] == 0


def test_ensure_blog_schedule_drips_and_is_idempotent(config, db, tmp_path):
    """ensure_blog_schedule dates unscheduled pending articles forward at
    per_day, and re-running is a no-op (it never re-drips what it already
    placed) — safe to call every daily run."""
    config.content = {**(config.content or {}), "reel_slide_frames": 2, "blog_per_day": 1}
    for i in range(3):
        db.insert_product({"sku": f"P{i}", "name": f"Product {i}"})
    eng = _engine(config, db)
    r = eng.ensure_blog_schedule()          # generates + schedules 3 across 3 days
    assert r["newly_scheduled"] == 3 and r["scheduled"] == 3
    assert r["next"] and r["last"] and r["next"] <= r["last"]
    # Each day holds exactly per_day (=1) — three distinct dates.
    dates = [a["scheduled_date"] for a in db.list_marketing_assets(channel="blog")]
    assert len(set(dates)) == 3 and all(d for d in dates)
    # Idempotent: nothing new to schedule on a second pass.
    assert eng.ensure_blog_schedule(generate=False)["newly_scheduled"] == 0


def test_schedule_blog_backlog_drips_across_days(config, db, tmp_path):
    """The whole blog backlog can be generated and spread over future days so the
    daily run posts a steady trickle (not a same-day dump)."""
    for i in range(1, 6):
        cid, key = _build_package(config, tmp_path, cid=i, key=f"mug{i}")
        db.insert_product({"sku": f"{cid}-{key}", "name": f"Mug {i}",
                           "campaign_id": cid, "product_key": key})
    eng = _engine(config, db)
    r = eng.schedule_blog_backlog(per_day=2, start="2026-07-24")
    assert r["scheduled"] == 5 and r["per_day"] == 2
    assert r["first_date"] == "2026-07-24" and r["last_date"] == "2026-07-26"
    # Two go out today, two tomorrow, one the day after — dripped, not dumped.
    dates = sorted(a["scheduled_date"]
                   for a in db.list_marketing_assets(channel="blog"))
    assert dates == ["2026-07-24", "2026-07-24", "2026-07-25",
                     "2026-07-25", "2026-07-26"]
    # Only the two due today are picked up by a distribute run on that day.
    due = db.list_pending_marketing_assets(channel="blog", due_on="2026-07-24")
    assert len(due) == 2


def test_blog_embeds_image_video_and_product_link(config, db, tmp_path):
    """Blog articles carry the hero picture, the clips, and a product link for SEO."""
    cid, key = _build_package(config, tmp_path)   # listing.json links to etsy/listing/1
    db.insert_product({"sku": f"{cid}-{key}", "name": "Mug", "campaign_id": cid,
                       "product_key": key})
    config.gelato = {**(config.gelato or {}), "file_base_url": "https://cdn.onassis/exports"}
    eng = _engine(config, db)
    eng.build_for_product(cid, key)                       # produces clips
    eng.generate_blog()
    art = db.list_marketing_assets(channel="blog")[0]["payload"]["articles"][0]
    assert "<img" in art["body"] and "images/hero.jpg" in art["body"]   # picture
    assert "<video" in art["body"]                                       # clip
    assert 'href="https://etsy.com/listing/1"' in art["body"]           # SEO product link
    assert art["image"] and art["cta_link"]


def test_distribute_marks_queued_clips_handed_off(config, db, tmp_path):
    cid, key = _build_package(config, tmp_path)
    eng = _engine(config, db)
    eng.build_for_product(cid, key)
    # No Make webhook configured → still handed to the queue (not failed).
    r = eng.distribute()
    assert r["processed"] == 3 and r["handed_off"] == 3
    assert db.count_short_form(status="queued") == 0
    assert db.count_short_form(status="distributed") == 3


def test_batch_build_respects_the_limit(config, db, tmp_path):
    cid, key = _build_package(config, tmp_path)
    db.insert_product({"sku": f"{cid}-{key}", "name": "Mug", "campaign_id": cid,
                       "product_key": key})
    r = _engine(config, db).build_batch(limit=2)
    assert r["built"] == 2                     # capped, though 3 formats exist


def test_queue_facebook_posts_from_blogs_and_videos(config, db):
    """Published blog articles become FB link posts and product clips become FB
    video posts; re-running dedupes (idempotent)."""
    cid, key = 3, "mug"
    db.insert_product({"sku": "MUG", "name": "Mug", "campaign_id": cid,
                       "product_key": key, "active": True})
    # A published blog asset with a storefront URL in its delivery_ref.
    bid = db.insert_marketing_asset({"campaign_id": cid, "product_key": key,
                                     "channel": "blog", "payload": {"title": "Slow Mornings"}})
    db.schedule_marketing_asset(bid, "2026-01-01")
    db.set_marketing_asset_delivery(bid, "posted",
                                    ref="https://shop.example/blogs/news/slow-mornings")
    # A rendered clip for the product.
    db.insert_short_form({"campaign_id": cid, "product_id": f"{cid}-{key}",
                          "product_key": key, "fmt": "style_slide", "path": "/x.mp4",
                          "caption": "c", "hashtags": [], "sound": "s", "duration_s": 8,
                          "listing_url": "u"})
    eng = _engine(config, db)
    eng.cfg["public_base"] = "https://cdn.example"          # for reel URLs
    r = eng.queue_facebook_posts()
    assert r["blogs"] == 1 and r["videos"] == 1
    fb = db.list_marketing_assets(channel="facebook")
    assert len(fb) == 2
    kinds = {("video" if (a["payload"]["post"].get("video_url")) else "link") for a in fb}
    assert kinds == {"link", "video"}
    assert eng.queue_facebook_posts()["queued"] == 0        # idempotent


def test_launch_product_posts_blog_and_queues_facebook_immediately(config, db, tmp_path):
    """The launch burst publishes the product's blog now (not dripped), builds
    its clips, and posts to Facebook — all in one call."""
    cid, key = _build_package(config, tmp_path)
    db.insert_product({"sku": f"{cid}-{key}", "name": "Mug", "campaign_id": cid,
                       "product_key": key, "active": True})
    config.shopify = {"blog_id": "7"}

    class _Blog:
        can_publish = True
        def __init__(self): self.posted = []
        def publish_article(self, payload):
            self.posted.append(payload.get("title"))
            return {"ok": True, "id": str(len(self.posted)),
                    "url": f"https://shop/blogs/news/{len(self.posted)}"}

    from onassis.distribution import ChannelDistributor
    orig = ChannelDistributor.__init__
    def _patched(self, cfg, database, **kw):
        kw.setdefault("shopify", _Blog())
        orig(self, cfg, database, **kw)
    ChannelDistributor.__init__ = _patched
    try:
        db.set_setting("business.facebook_enabled", True)
        r = _engine(config, db).launch_product(cid, key)
    finally:
        ChannelDistributor.__init__ = orig
    assert r["ok"] and r["blog"] >= 1          # blog published immediately
    assert r["clips"] == 3                       # clips built
    posted_blog = [a for a in db.list_marketing_assets(channel="blog")
                   if a["status"] == "posted"]
    assert posted_blog                            # not left dripping


def test_evergreen_facebook_reshares_least_recently_shared_first(config, db):
    """Evergreen video re-share picks the product not yet shared before one that
    already has a FB video, so social cycles through the catalogue."""
    for i, name in ((1, "Alpha"), (2, "Beta")):
        db.insert_product({"sku": f"P{i}", "name": name, "campaign_id": i,
                           "product_key": f"p{i}", "active": True})
        db.insert_short_form({"campaign_id": i, "product_id": f"{i}-p{i}",
                              "product_key": f"p{i}", "fmt": "style_slide",
                              "path": "/x.mp4", "caption": "c", "hashtags": [],
                              "sound": "s", "duration_s": 8, "listing_url": "u"})
    # Alpha already has a FB video; Beta has none → Beta should go first.
    db.insert_marketing_asset({"campaign_id": 1, "product_key": "p1", "channel": "facebook",
                               "payload": {"post": {"video_url": "https://cdn/p1.mp4"}}})
    eng = _engine(config, db)
    eng.cfg["public_base"] = "https://cdn.example"
    r = eng.queue_evergreen_facebook(per_day=1)
    assert r["queued"] == 1
    fresh = [a for a in db.list_marketing_assets(channel="facebook")
             if a["product_key"] == "p2"]
    assert fresh and fresh[0]["payload"]["post"]["video_url"].endswith("/p2/style_slide.mp4")
    # Off switch honoured.
    assert eng.queue_evergreen_facebook(per_day=0)["queued"] == 0


def test_marketing_overview_aggregates_per_product(config, db):
    """The Marketing tab data groups clips, blogs and FB posts under each product
    with totals."""
    cid, key = 6, "mug"
    db.insert_product({"sku": "MUG", "name": "Riviera Mug", "campaign_id": cid,
                       "product_key": key, "active": True})
    db.insert_short_form({"campaign_id": cid, "product_id": f"{cid}-{key}",
                          "product_key": key, "fmt": "style_slide", "path": "/x.mp4",
                          "caption": "c", "hashtags": [], "sound": "s", "duration_s": 8,
                          "listing_url": "u"})
    bid = db.insert_marketing_asset({"campaign_id": cid, "product_key": key,
                                     "channel": "blog", "payload": {"title": "Slow"}})
    db.set_marketing_asset_delivery(bid, "posted", ref="https://shop/blogs/news/slow")
    db.insert_marketing_asset({"campaign_id": cid, "product_key": key, "channel": "facebook",
                               "payload": {"post": {"link": "https://shop/x"}}})
    eng = _engine(config, db)
    eng.cfg["public_base"] = "https://cdn.example"
    ov = eng.marketing_overview()
    assert ov["totals"] == {"products": 1, "clips": 1, "blogs": 1, "blogs_posted": 1,
                            "facebook": 1, "facebook_posted": 0}
    row = ov["products"][0]
    assert row["name"] == "Riviera Mug"
    assert row["clips"][0]["url"] == "https://cdn.example/reels/6/mug/style_slide.mp4"
    assert row["blogs"][0]["status"] == "posted" and row["facebook"][0]["kind"] == "link"


def test_facebook_blog_link_uses_handle_not_numeric_id(config, db):
    """FB link posts must use the blog HANDLE, not its numeric id (which 404s)."""
    cid, key = 4, "tote"
    db.insert_product({"sku": "TOTE", "name": "Tote", "campaign_id": cid,
                       "product_key": key, "active": True})
    bid = db.insert_marketing_asset({"campaign_id": cid, "product_key": key,
                                     "channel": "blog", "payload": {"title": "Coastal"}})
    db.schedule_marketing_asset(bid, "2026-01-01")
    db.set_marketing_asset_delivery(
        bid, "posted",
        ref="https://onassismed.com/blogs/125286220123/porto-raffia-tote")

    class _Conn:
        can_publish = True
        def blog_handle(self, blog_id):
            return "onassis-med-latest-news-products"

    eng = _engine(config, db)
    eng._shopify_conn = _Conn()
    eng.queue_facebook_posts()
    link = db.list_marketing_assets(channel="facebook")[0]["payload"]["post"]["link"]
    assert link == ("https://onassismed.com/blogs/onassis-med-latest-news-products/"
                    "porto-raffia-tote")


class _FakeShopifyMedia:
    """Serves product images from 'Shopify' and writes them locally on download."""

    can_publish = True

    def __init__(self, n=4):
        self._urls = [f"https://cdn.shopify.com/img{i}.jpg" for i in range(n)]

    def product_media(self, product_id):
        return {"title": "Riviera Mug", "description": "<p>A lovely mug.</p>",
                "tags": ["mediterranean", "mug"], "images": self._urls}

    def download_images(self, urls, dest_dir):
        from pathlib import Path as _P
        d = _P(dest_dir); d.mkdir(parents=True, exist_ok=True)
        out = []
        for i, _ in enumerate(urls):
            fp = d / f"shopify_{i}.jpg"
            Image.new("RGB", (600, 600), (180, 140, 110)).save(fp)
            out.append(str(fp))
        return out


def test_build_from_shopify_images_when_no_local_package(config, db, tmp_path):
    """A product with no local listing package still gets a slideshow built from
    its Shopify product images (the ephemeral-container / imported-product case)."""
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    cid, key = 8, "mug"
    db.insert_product({"sku": "MUG", "name": "Riviera Mug", "campaign_id": cid,
                       "product_key": key, "active": True})
    db.insert_publication({"platform": "shopify", "campaign_id": cid,
                           "product_id": f"{cid}-{key}", "listing_id": "9001",
                           "status": "live", "mode": "live"})
    eng = _engine(config, db)
    eng._shopify_conn = _FakeShopifyMedia()
    r = eng.build_for_product(cid, key)
    assert r["ok"] and r["count"] == 3            # built from Shopify images
    assert len(db.list_short_form(limit=10)) == 3


def test_batch_build_is_idempotent(config, db, tmp_path):
    """Re-running the batch builds only missing formats, so it never duplicates
    clips — safe to run daily."""
    cid, key = _build_package(config, tmp_path)
    db.insert_product({"sku": f"{cid}-{key}", "name": "Mug", "campaign_id": cid,
                       "product_key": key})
    eng = _engine(config, db)
    first = eng.build_batch()                  # builds all 3 formats
    assert first["built"] == 3 and first["skipped"] == 0
    again = eng.build_batch()                  # nothing new to build
    assert again["built"] == 0 and again["skipped"] == 1
    assert len(db.list_short_form(limit=100)) == 3   # no duplicates
