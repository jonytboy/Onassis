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


def test_evergreen_reels_cycles_and_respects_per_run(config, db):
    """The evergreen reel drip queues N reels/run, oldest-shared first, onto the
    tiktok channel (which Make routes to FB Reels/TikTok)."""
    for i, name in ((1, "Alpha"), (2, "Beta")):
        db.insert_product({"sku": f"P{i}", "name": name, "campaign_id": i,
                           "product_key": f"p{i}", "active": True})
        db.insert_short_form({"campaign_id": i, "product_id": f"{i}-p{i}",
                              "product_key": f"p{i}", "fmt": "style_slide",
                              "path": "/x.mp4", "caption": "c", "hashtags": [],
                              "sound": "s", "duration_s": 8, "listing_url": "u"})
    # Alpha already shared a reel; Beta never → Beta goes first.
    db.insert_marketing_asset({"campaign_id": 1, "product_key": "p1", "channel": "tiktok",
                               "payload": {"post": {"video_url": "https://cdn/p1.mp4"}}})
    eng = _engine(config, db)
    eng.cfg["public_base"] = "https://cdn.example"
    r = eng.queue_evergreen_reels(per_run=1)
    assert r["queued"] == 1
    fresh = [a for a in db.list_marketing_assets(channel="tiktok")
             if a["product_key"] == "p2"]
    assert fresh and fresh[0]["payload"]["post"]["video_url"].endswith("/p2/style_slide.mp4")
    assert eng.queue_evergreen_reels(per_run=0)["queued"] == 0


def test_evergreen_reels_pause_when_channel_failing(config, db):
    """When the tiktok channel's recent deliveries are ALL failing (e.g. Make is
    out of operations), the evergreen drip stops manufacturing new reels so
    failed rows don't pile up — and resumes the moment a post succeeds again."""
    db.insert_product({"sku": "P1", "name": "Alpha", "campaign_id": 1,
                       "product_key": "p1", "active": True})
    db.insert_short_form({"campaign_id": 1, "product_id": "1-p1",
                          "product_key": "p1", "fmt": "style_slide",
                          "path": "/x.mp4", "caption": "c", "hashtags": [],
                          "sound": "s", "duration_s": 8, "listing_url": "u"})
    eng = _engine(config, db)
    eng.cfg["public_base"] = "https://cdn.example"
    # 4 recent tiktok deliveries, all failed → channel considered down.
    for _ in range(4):
        aid = db.insert_marketing_asset(
            {"campaign_id": 1, "product_key": "p1", "channel": "tiktok",
             "payload": {"post": {"video_url": "https://cdn/x.mp4"}}})
        db.set_marketing_asset_delivery(aid, "failed", error="Make 400")
    assert eng.queue_evergreen_reels(per_run=1)["queued"] == 0        # paused
    # A single success clears the breaker → the drip resumes.
    aid = db.insert_marketing_asset(
        {"campaign_id": 1, "product_key": "p1", "channel": "tiktok",
         "payload": {"post": {"video_url": "https://cdn/y.mp4"}}})
    db.set_marketing_asset_delivery(aid, "posted", ref="ok")
    assert eng.queue_evergreen_reels(per_run=1)["queued"] == 1        # resumed


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
                            "facebook": 1, "facebook_posted": 0,
                            "tiktok": 0, "tiktok_posted": 0}
    row = ov["products"][0]
    assert row["name"] == "Riviera Mug"
    assert row["clips"][0]["url"] == "https://cdn.example/reels/6/mug/style_slide.mp4"
    assert row["blogs"][0]["status"] == "posted" and row["facebook"][0]["kind"] == "link"
    # Every item now carries its id so the Marketing tab can delete it.
    assert row["clips"][0]["id"] and row["blogs"][0]["id"] and row["facebook"][0]["id"]


def test_delete_content_removes_clip_file_and_assets(config, db, tmp_path):
    """delete_content removes a clip (and its rendered file) and a channel asset."""
    cid, key = 7, "tee"
    clip_file = tmp_path / "clip.mp4"
    clip_file.write_bytes(b"video-bytes")
    clip_id = db.insert_short_form({"campaign_id": cid, "product_id": f"{cid}-{key}",
                                    "product_key": key, "fmt": "style_slide",
                                    "path": str(clip_file), "caption": "c", "hashtags": [],
                                    "sound": "", "duration_s": 8, "listing_url": "u"})
    asset_id = db.insert_marketing_asset({"campaign_id": cid, "product_key": key,
                                          "channel": "tiktok",
                                          "payload": {"post": {"video_url": "u"}}})
    eng = _engine(config, db)

    r = eng.delete_content("clip", clip_id)
    assert r["ok"] and r["removed_file"] is True and not clip_file.exists()
    assert db.list_short_form(limit=10) == []

    r = eng.delete_content("tiktok", asset_id)
    assert r["ok"] and not db.list_marketing_assets(channel="tiktok")
    # Deleting an unknown id is a clean miss, not a crash.
    assert eng.delete_content("tiktok", 999999)["ok"] is False


def test_content_skips_products_pending_approval(config, db, tmp_path):
    """No blog is generated for a product still held pending approval (the bug:
    placeholder-artwork drafts got blogs before the operator approved them)."""
    _build_package(config, tmp_path, cid=1, key="approved_tee")
    _build_package(config, tmp_path, cid=2, key="pending_tee")
    db.insert_product({"sku": "APP", "name": "Approved Tee", "campaign_id": 1,
                       "product_key": "approved_tee", "active": True})
    db.insert_product({"sku": "PEND", "name": "Pending Tee", "campaign_id": 2,
                       "product_key": "pending_tee", "active": True})
    db.set_product_approval({"sku": "APP", "product_key": "approved_tee",
                             "campaign_id": 1, "decision": "approved"})
    db.set_product_approval({"sku": "PEND", "product_key": "pending_tee",
                             "campaign_id": 2, "decision": "awaiting"})
    eng = _engine(config, db)
    eng.generate_blog()
    keys = {a.get("product_key") for a in db.list_marketing_assets(channel="blog")}
    assert "approved_tee" in keys and "pending_tee" not in keys
    # And the clip builder skips the pending product too.
    assert "pending_tee" not in {p["product_key"] for p in eng._content_products()}


def test_restore_product_names_puts_back_the_listing_title(config, db, tmp_path):
    """restore_product_names reads the original title from listing.json and puts it
    back on the DB row + the live Shopify listing (undo a bad rename)."""
    cid, key = _build_package(config, tmp_path)     # listing.json title = 'Casa Med Ceramic Mug'
    db.insert_product({"sku": "MUG", "name": "WRONG — renamed badly", "campaign_id": cid,
                       "product_key": key, "active": True})
    db.insert_publication({"campaign_id": cid, "product_id": f"{cid}-{key}",
                           "platform": "shopify", "listing_id": "800",
                           "mode": "live", "status": "active"})

    class _Shop:
        can_publish = True
        def __init__(self): self.titles = []
        def set_product_title(self, pid, title): self.titles.append((pid, title)); return {"ok": True}

    eng = _engine(config, db)
    eng._shopify_conn = _Shop()
    preview = eng.restore_product_names(apply=False)
    assert preview["products"][0]["original"] == "Casa Med Ceramic Mug"
    assert not eng._shopify_conn.titles                 # preview pushes nothing

    applied = eng.restore_product_names(apply=True)
    assert applied["changed"] == 1
    assert eng._shopify_conn.titles == [("800", "Casa Med Ceramic Mug")]
    assert db.list_products()[0]["name"] == "Casa Med Ceramic Mug"


def test_prune_dead_publications_removes_only_404s(config, db):
    """Prune drops publications whose Shopify listing 404s, keeps the live one,
    and never prunes on a non-404 error."""
    cid = 9
    for lid, kind in (("live1", "ok"), ("dead1", "404"), ("boom1", "500")):
        db.insert_publication({"campaign_id": cid, "product_id": f"{cid}-{lid}",
                               "platform": "shopify", "listing_id": lid,
                               "mode": "live", "status": "active"})

    class _Shop:
        can_publish = True
        def product_price(self, lid):
            if lid == "dead1":
                raise RuntimeError("Shopify GET HTTP 404: Not Found")
            if lid == "boom1":
                raise RuntimeError("Shopify GET HTTP 500: server error")
            return 20.0

    eng = _engine(config, db)
    eng._shopify_conn = _Shop()
    preview = eng.prune_dead_publications(apply=False)
    assert preview["checked"] == 3 and preview["dead"] == 1 and preview["removed"] == 0
    assert preview["publications"][0]["listing_id"] == "dead1"

    applied = eng.prune_dead_publications(apply=True)
    assert applied["removed"] == 1
    remaining = {p["listing_id"] for p in db.list_publications()}
    assert remaining == {"live1", "boom1"}      # 404 gone; live + transient kept


def test_display_name_uses_collection_and_real_type():
    from onassis.content_engine import ContentEngine as CE
    # Campaign names ARE product concepts ('… Cushion', '… Tea Towel'); we keep the
    # COLLECTION and append the ACTUAL type so a tote is never named 'Cushion'.
    assert CE._display_name("Persiana Sun-Stripe Cushion", "Tote Bag") == \
        "Persiana Sun-Stripe — Tote Bag"
    assert CE._display_name("Tavola Lunga Linen Tea Towel", "Ceramic Mug") == \
        "Tavola Lunga — Ceramic Mug"
    # '&' collections keep three words.
    assert CE._display_name("Salt & Olive Bathing Bar", "Ceramic Mug") == \
        "Salt & Olive — Ceramic Mug"
    # A hoodie campaign on a Sweatshirt is a SWEATSHIRT, never a hoodie.
    assert CE._display_name("Riviera Sunset Heavyweight Hoodie", "Sweatshirt") == \
        "Riviera Sunset — Sweatshirt"
    assert CE._display_name(None, "Ceramic Mug") == "Ceramic Mug"
    # Idempotent: feeding a composed name back yields the same name.
    composed = CE._display_name("Salt & Olive Bathing Bar", "Ceramic Mug")
    assert CE._display_name(composed, "Ceramic Mug") == composed


def test_type_label_comes_from_product_key():
    from onassis.content_engine import ContentEngine as CE
    assert CE._type_label("ceramic_mug") == "Ceramic Mug"
    assert CE._type_label("heavyweight_hoodie") == "Heavyweight Hoodie"
    assert CE._type_label("sweatshirt") == "Sweatshirt"
    assert CE._type_label("some_new_thing") == "Some New Thing"


def test_rename_products_names_by_design_and_pushes(config, db):
    brief_id = db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": []})
    cid = db.insert_campaign({"name": "Salt & Olive Bathing Bar", "brief_id": brief_id})
    db.insert_product({"sku": "MUG", "name": "Ceramic Mug", "campaign_id": cid,
                       "product_key": "ceramic_mug", "active": True})
    db.insert_publication({"campaign_id": cid, "product_id": f"{cid}-ceramic_mug",
                           "platform": "shopify", "listing_id": "700",
                           "mode": "live", "status": "active"})

    class _Shop:
        can_publish = True
        def __init__(self): self.titles = []
        def set_product_title(self, pid, title): self.titles.append((pid, title)); return {"ok": True}

    eng = _engine(config, db)
    eng._shopify_conn = _Shop()
    preview = eng.rename_products(apply=False)
    row = preview["products"][0]
    assert row["new_name"] == "Salt & Olive — Ceramic Mug"
    assert not eng._shopify_conn.titles                 # preview pushes nothing

    applied = eng.rename_products(apply=True)
    assert applied["changed"] == 1
    assert eng._shopify_conn.titles == [("700", "Salt & Olive — Ceramic Mug")]
    assert db.list_products()[0]["name"] == "Salt & Olive — Ceramic Mug"
    # Re-run is a NO-OP — it must not collapse the name or re-push (the bug where a
    # second run dropped the product type).
    again = eng.rename_products(apply=True)
    assert again["changed"] == 0
    assert db.list_products()[0]["name"] == "Salt & Olive — Ceramic Mug"


def test_reprice_products_previews_then_applies(config, db):
    """reprice_products previews old->new per product, and --apply pushes it."""
    config.pricing = {"strategy": "flat", "flat_profit": 1.0, "shipping_cost": 5.0}
    config.fees = {}
    cid, key = 3, "tee"
    db.insert_product({"sku": "TEE", "name": "Riviera Tee", "campaign_id": cid,
                       "product_key": key, "active": True, "production_cost": 12.0})
    db.insert_publication({"campaign_id": cid, "product_id": f"{cid}-{key}",
                           "platform": "shopify", "listing_id": "999",
                           "mode": "live", "status": "active"})

    class _Shop:
        can_publish = True
        def __init__(self): self.set = []
        def product_price(self, pid): return 33.0
        def set_product_price(self, pid, price): self.set.append((pid, price)); return {"ok": True}

    eng = _engine(config, db)
    eng._shopify_conn = _Shop()
    preview = eng.reprice_products(apply=False)
    row = preview["products"][0]
    assert row["old_price"] == 33.0 and row["new_price"] < 33.0   # cheaper
    assert row["status"] == "would_reprice" and preview["changed"] == 0
    assert not eng._shopify_conn.set                              # nothing pushed

    applied = eng.reprice_products(apply=True)
    assert applied["changed"] == 1 and eng._shopify_conn.set[0][0] == "999"


def test_clear_marketing_bulk_deletes_by_channel_and_status(config, db):
    """clear_marketing wipes failed items and clears a channel's pending queue."""
    a = db.insert_marketing_asset({"campaign_id": 1, "product_key": "p", "channel": "blog",
                                   "payload": {"title": "ok"}})
    db.set_marketing_asset_delivery(a, "posted", ref="u")
    f = db.insert_marketing_asset({"campaign_id": 1, "product_key": "p", "channel": "blog",
                                   "payload": {"title": "bad"}})
    db.set_marketing_asset_delivery(f, "failed", error="boom")
    db.insert_marketing_asset({"campaign_id": 1, "product_key": "p", "channel": "blog",
                               "payload": {"title": "queued"}})   # pending
    eng = _engine(config, db)
    # Delete every failed item (across channels).
    assert eng.clear_marketing(status="failed")["deleted"] == 1
    # Clear the pending blog queue (leaves the posted one).
    assert eng.clear_marketing(channel="blog", status="pending")["deleted"] == 1
    remaining = db.list_marketing_assets(channel="blog")
    assert len(remaining) == 1 and remaining[0]["status"] == "posted"


def test_clear_clips_bulk_deletes_rows_and_files(config, db, tmp_path):
    """clear_clips wipes every short-form clip (and its file) — the fast reset
    before regenerating placeholder/duplicate clips."""
    for i in range(3):
        f = tmp_path / f"c{i}.mp4"
        f.write_bytes(b"v")
        db.insert_short_form({"campaign_id": 8, "product_id": f"8-hoodie",
                              "product_key": "hoodie", "fmt": f"style_slide_{i}",
                              "path": str(f), "caption": "c", "hashtags": [],
                              "sound": "", "duration_s": 7, "listing_url": "u"})
    eng = _engine(config, db)
    r = eng.clear_clips()
    assert r["ok"] and r["deleted"] == 3 and r["files_removed"] == 3
    assert db.list_short_form(limit=10) == []
    assert not list(tmp_path.glob("*.mp4"))


def test_credit_failure_never_substitutes_a_placeholder(config):
    """A billing/credit error must raise, never fall back to the dev renderer —
    otherwise the shop and videos silently fill with placeholder artwork."""
    import pytest

    from onassis.artwork import ArtworkStudio
    from onassis.connectors.image_backend import ImageSpec, MASTER

    class _BrokeBackend:
        name = "openai"
        model = "gpt-image-1"
        quality = "high"
        def generate(self, spec):
            raise RuntimeError("Your credit balance is too low to run this request.")

    studio = ArtworkStudio(config, backend=_BrokeBackend())
    assert studio.fallback_to_local is True   # even with fallback ON…
    with pytest.raises(RuntimeError, match="blocked"):
        studio._generate(ImageSpec(kind=MASTER, width=64, height=64, title="x"))


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


def test_hero_url_only_when_image_on_disk(config, db, tmp_path):
    """A hero URL is advertised only when the file exists on disk. A missing
    image makes Shopify 422-reject the entire blog post ('failed to download'),
    which silently stalls the whole blog — so a product whose art is gone
    publishes text-only instead of blocking the pipeline."""
    config.content = {"public_base": "https://api.example.com/exports"}
    config.listing = {**(config.listing or {}),
                      "exports_dir": str(tmp_path / "exports")}
    eng = ContentEngine(config, db)
    # No file yet -> no URL (so the article omits the image and still posts).
    assert eng._hero_url(44, "OPP-x") is None
    # Create the hero file -> the URL appears.
    imgs = tmp_path / "exports" / "44" / "OPP-x" / "images"
    imgs.mkdir(parents=True, exist_ok=True)
    (imgs / "hero.jpg").write_bytes(b"\xff\xd8\xff")   # pretend jpg
    assert eng._hero_url(44, "OPP-x") == (
        "https://api.example.com/exports/44/OPP-x/images/hero.jpg")


class _SeoLLM:
    """Stub LLM for SEO: returns a buyer-phrase title + tags, echoes context."""

    def __init__(self):
        self.calls = []

    def generate_json(self, *, system, prompt, schema):
        self.calls.append(prompt)
        return {"title": "Ceramic Mug, Mediterranean Coastal Mug, Greek Gift Mug",
                "tags": ["mediterranean mug", "coastal gift mug", "greek island mug"]}


class _FakeEtsyEngine:
    is_configured = True

    def __init__(self):
        self.titles = []
        self.tags = []

    def update_title(self, listing_id, title, *, reason="", source=""):
        self.titles.append((listing_id, title))

    def update_tags(self, listing_id, tags, *, reason="", source=""):
        self.tags.append((listing_id, list(tags)))


class _NoShop:
    can_publish = False


def test_rewrite_seo_preview_builds_titles_and_tags(config, db):
    """Preview rewrites a published product's title+tags around buyer phrases and
    changes nothing — returning the proposed values and target platforms."""
    db.insert_product({"sku": "MUG", "name": "Meridiano Coastal Ceramic Mug",
                       "campaign_id": 5, "product_key": "ceramic_mug", "active": True})
    db.insert_publication({"platform": "etsy", "product_id": "5-ceramic_mug",
                           "campaign_id": 5, "listing_id": "L555", "status": "live"})
    eng = _engine(config, db)
    eng._seo_llm = _SeoLLM()
    eng._shopify_conn = _NoShop()
    r = eng.rewrite_seo(apply=False)
    assert r["ok"] and r["applied"] is False and r["count"] == 1
    row = r["products"][0]
    assert row["status"] == "would_rewrite"
    assert row["platforms"] == ["etsy"]
    assert row["new_title"].startswith("Ceramic Mug")          # buyer phrase, not brand
    assert row["new_tags"] == ["mediterranean mug", "coastal gift mug",
                               "greek island mug"]
    # The product's real type reached the SEO prompt.
    assert "Ceramic Mug" in eng._seo_llm.calls[0]


def test_rewrite_seo_apply_pushes_to_etsy(config, db):
    """Apply pushes the new title + tags to the Etsy listing."""
    db.insert_product({"sku": "MUG", "name": "Meridiano Coastal Ceramic Mug",
                       "campaign_id": 5, "product_key": "ceramic_mug", "active": True})
    db.insert_publication({"platform": "etsy", "product_id": "5-ceramic_mug",
                           "campaign_id": 5, "listing_id": "L555", "status": "live"})
    eng = _engine(config, db)
    eng._seo_llm = _SeoLLM()
    eng._shopify_conn = _NoShop()
    fake_etsy = _FakeEtsyEngine()
    eng._seo_etsy = fake_etsy
    r = eng.rewrite_seo(apply=True)
    assert r["applied"] is True and r["changed"] == 1
    assert r["products"][0]["status"] == "rewritten"
    assert fake_etsy.titles == [("L555", "Ceramic Mug, Mediterranean Coastal Mug, "
                                          "Greek Gift Mug")]
    assert fake_etsy.tags == [("L555", ["mediterranean mug", "coastal gift mug",
                                        "greek island mug"])]


def test_rewrite_seo_skips_unpublished_products(config, db):
    """A product with no marketplace listing is reported, not pushed."""
    db.insert_product({"sku": "X", "name": "Ghost", "campaign_id": 9,
                       "product_key": "poster", "active": True})
    eng = _engine(config, db)
    eng._seo_llm = _SeoLLM()
    eng._shopify_conn = _NoShop()
    r = eng.rewrite_seo(apply=True)
    assert r["products"][0]["status"] == "not_published"
    assert r["changed"] == 0


class _BadTypeLLM:
    """LLM that relabels a hoodie as a sweatshirt/crewneck — the type-swap bug."""

    def generate_json(self, *, system, prompt, schema):
        return {"title": "Embroidered Sunset Sweatshirt, Cozy Crewneck Pullover",
                "tags": ["sweatshirt", "crewneck sweater"]}


def test_rewrite_seo_flags_garment_type_conflict_and_never_applies(config, db):
    """The type guard blocks the rename-disaster failure mode: a hoodie the model
    relabelled a sweatshirt/crewneck is flagged and NEVER pushed."""
    db.insert_product({"sku": "H", "name": "Riviera Sunset Hoodie", "campaign_id": 3,
                       "product_key": "heavyweight_hoodie", "active": True})
    db.insert_publication({"platform": "etsy", "product_id": "3-heavyweight_hoodie",
                           "campaign_id": 3, "listing_id": "L9", "status": "live"})
    eng = _engine(config, db)
    eng._seo_llm = _BadTypeLLM()
    eng._shopify_conn = _NoShop()
    fake_etsy = _FakeEtsyEngine()
    eng._seo_etsy = fake_etsy
    r = eng.rewrite_seo(apply=True)
    row = r["products"][0]
    assert row["status"] == "type_conflict"
    assert row.get("conflict")
    assert r["changed"] == 0
    assert fake_etsy.titles == [] and fake_etsy.tags == []   # nothing pushed
