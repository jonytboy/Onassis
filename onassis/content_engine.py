"""The Content Engine (Sprint 48) — a short-form video factory.

Turns a built product into a queue of vertical short-form clips (TikTok / Reels)
in several distinct, TAGGED formats, so we can measure which actually converts
rather than guessing:

* ``style_slide``    — aesthetic scenes with the product woven in (reach / desire)
* ``product_in_use`` — the product in an aspirational moment (purchase intent)
* ``gifting``        — "the perfect Mediterranean gift" framing (gift conversion)

Each clip is a self-contained content package — the mp4 plus a caption, hashtags
and a suggested sound — written to a **platform-agnostic queue** and, when a Make
webhook is configured, handed off for posting. The posting rail is deliberately
swappable: the queue is a plain folder a scheduler (Buffer/Metricool/…) can also
consume, so TikTok's gated posting API never blocks content production.

Hooks/captions are deterministic ($0, no LLM) and varied per format; rendering is
delegated to :class:`~onassis.reel_studio.ReelStudio` (ffmpeg, injectable).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from onassis.config import ROOT_DIR, Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.reel_studio import ReelSpec, ReelStudio, Slide

log = get_logger(__name__)

FORMATS = ["style_slide", "product_in_use", "gifting"]

# Scene roles we prefer per slot (falls back to whatever exists).
_AESTHETIC_SCENES = ["room", "lifestyle", "scale"]
_PRODUCT_SCENES = ["hero", "closeup", "product"]


class ContentEngine:
    def __init__(self, config: Config, db: Database, *,
                 studio: ReelStudio | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = dict(getattr(config, "content", None) or {})
        self.studio = studio or ReelStudio()
        self.size = tuple(self.cfg.get("reel_size", (1080, 1920)))
        self.fps = int(self.cfg.get("reel_fps", 30))
        # ~2s/slide + a crossfade — readable and produced, not a rushed slideshow.
        self.per_slide_frames = int(self.cfg.get("reel_slide_frames", 60))
        self.xfade = int(self.cfg.get("reel_xfade_frames", 10))

    # --- Asset discovery --------------------------------------------

    def _exports_base(self) -> Path:
        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        return base if base.is_absolute() else (ROOT_DIR / base)

    def _gather(self, campaign_id: int, product_key: str) -> dict[str, Any] | None:
        folder = self._exports_base() / str(campaign_id) / str(product_key)
        listing_path = folder / "listing.json"
        if not listing_path.exists():
            return None
        try:
            listing = json.loads(listing_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        images_dir = folder / "images"
        by_scene: dict[str, str] = {}
        for m in listing.get("mockup_manifest") or listing.get("images") or []:
            p = images_dir / m.get("filename", "")
            if p.exists() and p.stat().st_size > 0:
                by_scene.setdefault(m.get("mockup_type") or m.get("scene") or "", str(p))
        # master artwork is a good extra "style" frame.
        master = folder / "master_artwork.png"
        return {
            "campaign_id": campaign_id, "product_key": product_key,
            "title": listing.get("title") or product_key,
            "description": listing.get("description") or "",
            "theme": listing.get("theme") or "",
            "product_name": listing.get("product_name") or product_key,
            "tags": listing.get("tags") or listing.get("seo_keywords") or [],
            "price": listing.get("price"),
            "listing_url": listing.get("listing_url") or "",
            "by_scene": by_scene,
            "master": str(master) if master.exists() else None,
        }

    def _pick(self, ctx: dict[str, Any], roles: list[str], fallback: bool = True) -> str | None:
        for r in roles:
            if ctx["by_scene"].get(r):
                return ctx["by_scene"][r]
        if fallback:
            return next(iter(ctx["by_scene"].values()), ctx.get("master"))
        return None

    # --- Format templates -------------------------------------------

    def _hashtags(self, ctx: dict[str, Any]) -> list[str]:
        base = ["mediterraneanstyle", "slowliving", "etsyfinds", "homeaesthetic"]
        extra = [str(t).replace(" ", "").lower() for t in (ctx.get("tags") or [])[:4]]
        seen, out = set(), []
        for h in base + extra:
            h = "#" + h.lstrip("#")
            if h.lower() not in seen and len(h) > 2:
                seen.add(h.lower())
                out.append(h)
        return out[:8]

    def _spec(self, ctx: dict[str, Any], fmt: str) -> ReelSpec:
        aesth = [ctx["by_scene"].get(s) for s in _AESTHETIC_SCENES if ctx["by_scene"].get(s)]
        aesth = aesth or [ctx.get("master")]
        product = self._pick(ctx, _PRODUCT_SCENES)
        name = ctx["product_name"]
        theme = ctx["theme"] or "the Mediterranean"
        F = self.per_slide_frames

        if fmt == "gifting":
            slides = [
                Slide(image=aesth[0], text="POV: you found the perfect gift", frames=F,
                      text_y=0.28, pan=(0.0, 0.2, 0.3, 0.0)),
                Slide(image=product, text="", caption=name, frames=F, pan=(0.3, 0.0, 0.0, 0.2)),
                Slide(image=aesth[-1], text="handmade · giftable · unforgettable", bold=False,
                      frames=F, text_y=0.5, pan=(0.0, 0.0, 0.2, 0.2)),
                Slide(image=product, text="shop the collection", caption="link in bio  ↗",
                      frames=F, text_y=0.42, pan=(0.1, 0.1, 0.0, 0.0)),
            ]
            caption = f"The gift that says you *get* them ✨ {name} — {theme}."
        elif fmt == "product_in_use":
            slides = [
                Slide(image=product, text=f"the {name.lower()}", frames=F, text_y=0.22,
                      pan=(0.0, 0.0, 0.2, 0.2)),
                Slide(image=ctx["by_scene"].get("closeup") or product, text="", caption="the details",
                      frames=F, pan=(0.2, 0.2, 0.0, 0.0)),
                Slide(image=aesth[0], text="made for slow mornings", bold=False, frames=F,
                      text_y=0.5, pan=(0.0, 0.2, 0.3, 0.0)),
                Slide(image=product, text="shop now", caption="link in bio  ↗", frames=F,
                      text_y=0.42, pan=(0.1, 0.0, 0.0, 0.1)),
            ]
            caption = f"Bring a little {theme} home — {name}."
        else:  # style_slide (default)
            slides = [
                Slide(image=aesth[0], text=f"slow mornings on {theme}", frames=F, text_y=0.3,
                      pan=(0.0, 0.15, 0.25, 0.0)),
                Slide(image=aesth[-1] if len(aesth) > 1 else aesth[0],
                      text="sun-warmed, unhurried", bold=False, frames=F, pan=(0.25, 0.0, 0.0, 0.2)),
                Slide(image=product, text="", caption=name, frames=F, pan=(0.0, 0.0, 0.2, 0.2)),
                Slide(image=aesth[0], text="a slower ritual", bold=False, frames=F, text_y=0.5,
                      pan=(0.2, 0.2, 0.0, 0.0)),
                Slide(image=product, text="shop the collection", caption="link in bio  ↗",
                      frames=F, text_y=0.42, pan=(0.1, 0.1, 0.0, 0.0)),
            ]
            caption = f"A slow-living moment in {theme} — {name}."

        return ReelSpec(
            slides=slides, caption=caption, hashtags=self._hashtags(ctx), fmt=fmt,
            sound="trending soft/acoustic (add natively)", product_key=ctx["product_key"],
            campaign_id=ctx["campaign_id"], listing_url=ctx.get("listing_url"),
            size=self.size, fps=self.fps, xfade_frames=self.xfade)

    # --- Build ------------------------------------------------------

    def _queue_dir(self) -> Path:
        return self._exports_base() / "reels"

    def build_for_product(self, campaign_id: int, product_key: str,
                          formats: list[str] | None = None) -> dict[str, Any]:
        """Render short-form clips for one product (one per format) into the queue."""
        ctx = self._gather(campaign_id, product_key)
        if ctx is None:
            return {"ok": False, "reason": "No built listing package for this product "
                    "(build/publish it first).", "clips": []}
        if not ctx["by_scene"] and not ctx.get("master"):
            return {"ok": False, "reason": "No product imagery found to build video from.",
                    "clips": []}
        out_dir = self._queue_dir() / str(campaign_id) / str(product_key)
        clips: list[dict[str, Any]] = []
        for fmt in (formats or FORMATS):
            spec = self._spec(ctx, fmt)
            mp4 = out_dir / f"{fmt}.mp4"
            try:
                pkg = self.studio.render(spec, mp4)
            except Exception as exc:  # one format failing never stops the others
                log.warning("Reel render failed (%s/%s): %s", product_key, fmt, exc)
                continue
            (out_dir / f"{fmt}.json").write_text(json.dumps({
                "caption": pkg["caption"], "hashtags": pkg["hashtags"],
                "sound": pkg["sound"], "format": fmt, "listing_url": pkg["listing_url"],
            }, indent=2), encoding="utf-8")
            rec_id = self.db.insert_short_form({
                "campaign_id": campaign_id, "product_id": f"{campaign_id}-{product_key}",
                "product_key": product_key, "fmt": fmt, "path": pkg["path"],
                "caption": pkg["caption"], "hashtags": pkg["hashtags"],
                "sound": pkg["sound"], "duration_s": pkg["duration_s"],
                "listing_url": pkg["listing_url"]})
            clips.append({**pkg, "id": rec_id})
        return {"ok": True, "clips": clips, "count": len(clips),
                "queue_dir": str(out_dir)}

    def build_batch(self, limit: int = 20, formats: list[str] | None = None) -> dict[str, Any]:
        """Fill the daily queue: build clips for active products that have a
        built listing package, newest first, up to ``limit`` clips."""
        made: list[dict[str, Any]] = []
        for p in self.db.list_products():
            if len(made) >= limit:
                break
            if not p.get("active", 1) or not p.get("product_key") or not p.get("campaign_id"):
                continue
            r = self.build_for_product(p["campaign_id"], p["product_key"], formats=formats)
            made.extend(r.get("clips", []))
        return {"built": len(made[:limit]), "clips": made[:limit]}

    def _public_base(self) -> str:
        return (self.cfg.get("public_base")
                or (self.config.gelato or {}).get("file_base_url") or "").rstrip("/")

    def _hero_url(self, campaign_id: int, product_key: str) -> str | None:
        base = self._public_base()
        return f"{base}/{campaign_id}/{product_key}/images/hero.jpg" if base else None

    def _product_url(self, campaign_id: int, product_key: str) -> str | None:
        """Best public product link (Etsy/Shopify) for the article's SEO link."""
        for platform in ("etsy", "shopify"):
            pub = self.db.get_latest_publication(campaign_id, platform,
                                                 product_id=f"{campaign_id}-{product_key}")
            if pub:
                url = pub.get("listing_url") or pub.get("url")
                if url:
                    return url
                lid = pub.get("listing_id")
                if platform == "etsy" and lid and str(lid).isdigit():
                    return f"https://www.etsy.com/listing/{lid}"
        return None

    def _blog_media(self, campaign_id: int | None,
                    product_key: str) -> tuple[str, str, str]:
        """Best links + image for a product's blog article: the Shopify product
        link and image (so the theme shows the product, not a placeholder, and
        the CTA points at our own store) with the Etsy listing kept as a
        secondary link. Returns ``(shop_url, etsy_url, image_url)`` — any may be
        empty. Falls back to the hero image when the store has none."""
        image_url = (self._hero_url(campaign_id, product_key) or "") if campaign_id else ""
        shop_url, etsy_url = "", ""
        if not campaign_id:
            return shop_url, etsy_url, image_url
        pid = f"{campaign_id}-{product_key}"
        ep = self.db.get_latest_publication(campaign_id, "etsy", product_id=pid)
        if ep:
            etsy_url = ep.get("listing_url") or ep.get("url") or ""
            lid = ep.get("listing_id")
            if not etsy_url and lid and str(lid).isdigit():
                etsy_url = f"https://www.etsy.com/listing/{lid}"
        sp = self.db.get_latest_publication(campaign_id, "shopify", product_id=pid)
        if sp and sp.get("listing_id"):
            conn = getattr(self, "_shopify_conn", None)
            if conn is None:
                from onassis.connectors.shopify import ShopifyConnector
                conn = self._shopify_conn = ShopifyConnector(self.config, self.db)
            if conn.can_publish:
                d = conn.product_details(str(sp["listing_id"]))
                shop_url = d.get("url") or ""
                if not image_url:
                    image_url = d.get("image_url") or ""
        return shop_url, etsy_url, image_url

    def _reel_urls(self, campaign_id: int, product_key: str) -> list[str]:
        """Public URLs of a product's rendered clips (for embedding in the blog)."""
        base = self._public_base()
        if not base:
            return []
        clips = [c for c in self.db.list_short_form(limit=500)
                 if c.get("product_key") == product_key and c.get("campaign_id") == campaign_id]
        return [f"{base}/reels/{campaign_id}/{product_key}/{c['fmt']}.mp4" for c in clips]

    # --- Blog articles (on demand, deterministic) -------------------

    def generate_blog(self, limit: int = 50) -> dict[str, Any]:
        """Generate SEO blog articles for active products that don't have any yet
        (deterministic, $0). 'Publish blog now' then ships them to Shopify. This
        decouples blog content from a full production run.

        Every active product gets an article — a missing ``product_key`` or
        ``campaign_id`` is no longer a silent skip (we derive a stable key from the
        sku/id and treat the campaign as optional), so a bare ``0`` can only mean
        'all products already have articles' or 'there are no active products'. The
        result carries a breakdown so the operator sees exactly what happened."""
        from onassis.marketing import MarketingEngine

        me = MarketingEngine(self.config, self.db)
        have = {a.get("product_key") for a in self.db.list_marketing_assets(channel="blog")}
        made, skipped_existing, skipped_inactive = 0, 0, 0
        products = self.db.list_products()
        for p in products:
            if made >= limit:
                break
            if not p.get("active", 1):
                skipped_inactive += 1
                continue
            # A product may predate the campaign/product_key columns — derive a
            # stable key so it still gets (and de-dupes) an article.
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            if not key:
                continue
            cid = p.get("campaign_id")               # optional — asset allows NULL
            if key in have:
                skipped_existing += 1
                continue
            ctx = (self._gather(cid, key) if cid else None) or {}
            listing = {"title": ctx.get("title") or p.get("name") or key,
                       "description": ctx.get("description") or p.get("description") or "",
                       "theme": ctx.get("theme") or "",
                       "product_name": ctx.get("product_name") or p.get("name") or key,
                       "tags": ctx.get("tags") or [], "product_key": key}
            shop_url, etsy_url, image_url = self._blog_media(cid, key)
            me.blog_only(listing,
                         listing_url=(shop_url or ctx.get("listing_url")
                                      or self._product_url(cid, key)),
                         also_url=etsy_url, campaign_id=cid, product_key=key,
                         image_url=image_url or None,
                         videos=self._reel_urls(cid, key) if cid else None)
            have.add(key)
            made += 1
        return {"generated": made, "products": len(products),
                "skipped_existing": skipped_existing,
                "skipped_inactive": skipped_inactive}

    def schedule_blog_backlog(self, *, per_day: int = 2, start: str | None = None,
                              generate: bool = True) -> dict[str, Any]:
        """Schedule the whole blog backlog to drip out over future days.

        Generates articles for every product that lacks them (``generate``), then
        spreads all pending blog assets across future dates at ``per_day`` a day —
        so the daily marketing run publishes a steady trickle instead of dumping
        the lot at once (which reads as spam and is bad for SEO). Re-running
        re-spaces the backlog from ``start`` (today by default). Returns the count
        scheduled and the date range."""
        from datetime import datetime, timedelta, timezone

        if generate:
            self.generate_blog(limit=1000)
        per_day = max(1, int(per_day))
        start_dt = (datetime.strptime(start, "%Y-%m-%d")
                    if start else datetime.now(timezone.utc)).date()
        # Oldest first, so earlier products go out first.
        pending = [a for a in self.db.list_marketing_assets(channel="blog")
                   if (a.get("status") or "pending") == "pending"]
        pending.sort(key=lambda a: a.get("id") or 0)
        for i, asset in enumerate(pending):
            day = start_dt + timedelta(days=i // per_day)
            self.db.schedule_marketing_asset(asset["id"], day.strftime("%Y-%m-%d"))
        last = (start_dt + timedelta(days=(max(0, len(pending) - 1)) // per_day)
                ) if pending else start_dt
        return {"scheduled": len(pending), "per_day": per_day,
                "first_date": start_dt.strftime("%Y-%m-%d"),
                "last_date": last.strftime("%Y-%m-%d")}

    def ensure_blog_schedule(self, *, per_day: int | None = None,
                             horizon_days: int | None = None,
                             generate: bool = True) -> dict[str, Any]:
        """Keep a rolling forward blog schedule filled — the evergreen drip.

        Unlike ``schedule_blog_backlog`` (a one-shot re-space that the operator
        triggers), this is idempotent and safe to run every day: it generates any
        missing articles, then gives every *unscheduled* pending asset a future
        date so the daily run posts a steady trickle instead of dumping the lot.
        Assets already scheduled for the future are left where they are, so the
        calendar you can see stays stable. Returns the schedule summary."""
        from datetime import date, datetime, timedelta, timezone

        if generate:
            self.generate_blog(limit=1000)
        per_day = max(1, int(per_day if per_day is not None
                             else self.cfg.get("blog_per_day", 1)))
        today = datetime.now(timezone.utc).date()
        today_s = today.strftime("%Y-%m-%d")
        pending = [a for a in self.db.list_marketing_assets(channel="blog")
                   if (a.get("status") or "pending") == "pending"]
        # Slots already claimed by future-dated assets (don't double-book a day).
        used: dict[str, int] = {}
        unscheduled = []
        for a in pending:
            sd = a.get("scheduled_date")
            if sd and sd >= today_s:
                used[sd] = used.get(sd, 0) + 1
            else:                         # NULL, or a stale past date → re-drip
                unscheduled.append(a)
        unscheduled.sort(key=lambda a: a.get("id") or 0)   # oldest first
        cursor = today
        newly = 0
        for a in unscheduled:
            while used.get(cursor.strftime("%Y-%m-%d"), 0) >= per_day:
                cursor = cursor + timedelta(days=1)
            day = cursor.strftime("%Y-%m-%d")
            self.db.schedule_marketing_asset(a["id"], day)
            used[day] = used.get(day, 0) + 1
            newly += 1
        dates = sorted(used)
        return {"per_day": per_day, "newly_scheduled": newly,
                "scheduled": sum(used.values()),
                "next": dates[0] if dates else None,
                "last": dates[-1] if dates else None}

    def _blog_pool(self) -> list[dict[str, Any]]:
        """Every single-article blog variant (one per product × angle) the catalogue
        can produce right now — the content the evergreen schedule cycles through."""
        from onassis.marketing import MarketingEngine

        me = MarketingEngine(self.config, self.db)
        pool: list[dict[str, Any]] = []
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            if not key:
                continue
            cid = p.get("campaign_id")
            ctx = (self._gather(cid, key) if cid else None) or {}
            listing = {"title": ctx.get("title") or p.get("name") or key,
                       "description": ctx.get("description") or p.get("description") or "",
                       "theme": ctx.get("theme") or "",
                       "product_name": ctx.get("product_name") or p.get("name") or key,
                       "tags": ctx.get("tags") or [], "product_key": key}
            shop_url, etsy_url, image_url = self._blog_media(cid, key)
            blog = me.blog_only(
                listing, listing_url=(shop_url or ctx.get("listing_url")
                                      or self._product_url(cid, key)),
                also_url=etsy_url, campaign_id=cid, product_key=key,
                image_url=image_url or None,
                videos=self._reel_urls(cid, key) if cid else None, store=False)
            for art in blog.get("articles", []):
                pool.append({"product_key": key, "campaign_id": cid,
                             "listing_url": blog.get("cta_link") or "",
                             "angle": art.get("angle"), "article": art})
        return pool

    def rewrite_live_blog_articles(self) -> dict[str, Any]:
        """Rewrite the store's EXISTING blog articles in place with the current
        content — Shopify product link, featured image and HTML body — so posts
        published before those improvements are brought up to date without being
        deleted or re-created.

        Live articles are matched to a product by the Etsy listing URL embedded
        in the article body (robust against title drift), then to the specific
        angle by title, falling back to the product's first article. Articles we
        can't map are reported and left untouched."""
        import re

        conn = getattr(self, "_shopify_conn", None)
        if conn is None:
            from onassis.connectors.shopify import ShopifyConnector
            conn = self._shopify_conn = ShopifyConnector(self.config, self.db)
        blog_id = str((self.config.shopify or {}).get("blog_id") or "")
        base = {"blog_id": blog_id or None, "live_count": 0, "products": 0,
                "checked": 0, "rewritten": 0, "skipped": 0, "details": []}
        if not conn.can_publish:
            return {**base, "ok": False, "reason": "Shopify is not connected."}
        if not blog_id:
            return {**base, "ok": False,
                    "reason": "No Shopify blog selected (set the Blog ID on "
                              "Integrations → Shopify)."}
        pool = self._blog_pool()
        base["products"] = len({v["product_key"] for v in pool})
        if not pool:
            return {**base, "ok": False,
                    "reason": "No active products to rebuild content from — the "
                              "catalogue has no active product to regenerate an "
                              "article from."}

        def _norm(t: str | None) -> str:
            return " ".join((t or "").split()).strip().lower().rstrip(".!—-")

        def _etsy_id(text: str | None) -> str:
            m = re.search(r"etsy\.com/listing/(\d+)", text or "")
            return m.group(1) if m else ""

        # Index the freshly-built content: which Etsy id belongs to which
        # product, that product's articles, and a global title fallback.
        etsy_to_pk: dict[str, str] = {}
        by_pk: dict[str, list[dict]] = {}
        by_title: dict[str, dict] = {}
        for v in pool:
            art, pk = v["article"], v["product_key"]
            by_pk.setdefault(pk, []).append(art)
            by_title[_norm(art.get("title"))] = art
            eid = _etsy_id(art.get("etsy_url"))
            if eid:
                etsy_to_pk.setdefault(eid, pk)

        try:
            live = conn.live_blog_articles()
        except Exception as exc:  # noqa: BLE001
            return {**base, "ok": False, "reason": str(exc)}
        base["live_count"] = len(live)

        checked = rewritten = skipped = 0
        details = []
        for a in live:
            checked += 1
            title, aid = a.get("title"), a.get("id")
            candidates = by_pk.get(etsy_to_pk.get(_etsy_id(a.get("body_html")), ""), [])
            match = (next((c for c in candidates if _norm(c["title"]) == _norm(title)), None)
                     or (candidates[0] if candidates else by_title.get(_norm(title))))
            if not match:
                skipped += 1
                details.append({"id": aid, "title": title, "ok": False,
                                "reason": "no product match (no Etsy link / unknown listing)"})
                continue
            fields = {"body_html": match.get("body") or "",
                      "tags": ", ".join(match.get("keywords") or [])}
            if match.get("image"):
                fields["image"] = {"src": match["image"]}
            try:
                conn.update_blog_article(str(aid), fields)
                rewritten += 1
                details.append({"id": aid, "title": title, "ok": True})
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                details.append({"id": aid, "title": title, "ok": False,
                                "reason": str(exc)})
        reason = (f"Matched {rewritten} of {checked} live article(s) on blog "
                  f"{blog_id}." if checked else
                  f"The selected blog ({blog_id}) has no articles — the posts may "
                  f"be on a different blog. Use Diagnose to see which blog holds "
                  f"them, then set that Blog ID on Integrations → Shopify.")
        return {**base, "ok": True, "live_count": len(live), "checked": checked,
                "rewritten": rewritten, "skipped": skipped, "reason": reason,
                "details": details[:50]}

    def refill_blog_schedule(self, *, per_day: int | None = None,
                             horizon_days: int | None = None) -> dict[str, Any]:
        """Keep a rolling FORWARD blog schedule filled so there are always upcoming
        posts to see — the evergreen engine. Fills every empty slot in the next
        ``horizon_days`` at ``per_day`` a day: existing unscheduled articles first,
        then fresh product×angle variants, then (once those are used) cycling back
        through the catalogue — exactly 'cycle back to the beginning'. Idempotent:
        it only tops the queue up to the horizon, never past it."""
        from datetime import datetime, timedelta, timezone

        per_day = max(1, int(per_day if per_day is not None
                             else self.cfg.get("blog_per_day", 1)))
        horizon = max(1, int(horizon_days if horizon_days is not None
                             else self.cfg.get("blog_horizon_days", 21)))
        today = datetime.now(timezone.utc).date()
        today_s = today.strftime("%Y-%m-%d")
        pending = [a for a in self.db.list_marketing_assets(channel="blog")
                   if (a.get("status") or "pending") == "pending"]
        used: dict[str, int] = {}
        have_variants: set = set()
        to_place = []                       # already-generated pending, needs a date
        for a in pending:
            sd = a.get("scheduled_date")
            pl = a.get("payload") or {}
            if sd and sd >= today_s:
                used[sd] = used.get(sd, 0) + 1
                have_variants.add((a.get("product_key"), pl.get("angle")))
            else:
                to_place.append(a)
        to_place.sort(key=lambda a: a.get("id") or 0)
        pool = self._blog_pool()
        # Fresh (not already queued) variants first, then the whole pool to recycle.
        fresh = [v for v in pool if (v["product_key"], v["angle"]) not in have_variants]
        rotation = fresh + pool
        created, ri = 0, 0
        for d_offset in range(horizon):
            day = (today + timedelta(days=d_offset)).strftime("%Y-%m-%d")
            while used.get(day, 0) < per_day:
                if to_place:
                    self.db.schedule_marketing_asset(to_place.pop(0)["id"], day)
                elif rotation:
                    v = rotation[ri % len(rotation)]
                    ri += 1
                    art = v["article"]
                    aid = self.db.insert_marketing_asset({
                        "campaign_id": v["campaign_id"], "product_key": v["product_key"],
                        "listing_url": v["listing_url"], "channel": "blog",
                        "payload": {**art, "articles": [art], "angle": v["angle"],
                                    "evergreen": True}})
                    self.db.schedule_marketing_asset(aid, day)
                    created += 1
                else:
                    break                    # nothing to schedule at all
                used[day] = used.get(day, 0) + 1
            if not to_place and not rotation:
                break
        dates = sorted(used)
        return {"per_day": per_day, "horizon_days": horizon, "created": created,
                "scheduled": sum(used.values()), "pool": len(pool),
                "next": dates[0] if dates else None, "last": dates[-1] if dates else None}

    # --- Distribution (platform-agnostic) ---------------------------

    def distribute(self, limit: int = 50) -> dict[str, Any]:
        """Hand queued clips to the posting rail. A Make webhook (if configured)
        receives each package; either way the clip stays in the queue folder for a
        scheduler to consume. Never fails the clip — records the outcome."""
        from onassis.connectors.make import MakeConnector

        make = MakeConnector(self.config)
        pending = self.db.list_short_form(status="queued", limit=limit)
        sent = 0
        public_base = (self.cfg.get("public_base")
                       or (self.config.gelato or {}).get("file_base_url") or "").rstrip("/")
        for clip in pending:
            video_url = (f"{public_base}/reels/{clip['campaign_id']}/{clip['product_key']}/"
                         f"{clip['fmt']}.mp4") if public_base else None
            payload = {"type": "short_form_video", "platforms": ["tiktok", "reels"],
                       "video_url": video_url, "caption": clip["caption"],
                       "hashtags": clip["hashtags"], "sound": clip["sound"],
                       "listing_url": clip.get("listing_url"), "format": clip["fmt"]}
            ref = None
            if make.is_configured:
                try:
                    res = make.send(payload)
                    ref = res.get("ref") or "sent"
                except Exception as exc:  # noqa: BLE001
                    log.warning("Reel distribute (make) failed: %s", exc)
            self.db.set_short_form_status(clip["id"],
                                          "distributed" if (ref or not make.is_configured) else "failed",
                                          ref=ref)
            if ref or not make.is_configured:
                sent += 1
        return {"processed": len(pending), "handed_off": sent,
                "make_configured": make.is_configured}

    def dashboard(self) -> dict[str, Any]:
        clips = self.db.list_short_form(limit=200)
        by_fmt: dict[str, int] = {}
        for c in clips:
            by_fmt[c["fmt"]] = by_fmt.get(c["fmt"], 0) + 1
        return {"total": self.db.count_short_form(), "by_format": by_fmt,
                "queued": self.db.count_short_form(status="queued"),
                "recent": clips[:24]}
