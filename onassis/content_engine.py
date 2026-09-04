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

    def _shopify(self) -> Any:
        conn = getattr(self, "_shopify_conn", None)
        if conn is None:
            from onassis.connectors.shopify import ShopifyConnector
            conn = self._shopify_conn = ShopifyConnector(self.config, self.db)
        return conn

    def _gather_shopify(self, campaign_id: int, product_key: str) -> dict[str, Any] | None:
        """Fallback imagery when there's no local listing package (e.g. the
        container was recycled, or the product was imported): pull the product's
        images straight from Shopify and build a slideshow context from them."""
        import re

        if not campaign_id:
            return None
        conn = self._shopify()
        if not conn.can_publish:
            return None
        pub = self.db.get_latest_publication(
            campaign_id, "shopify", product_id=f"{campaign_id}-{product_key}")
        if not pub or not pub.get("listing_id"):
            return None
        media = conn.product_media(str(pub["listing_id"]))
        imgs = media.get("images") or []
        if not imgs:
            return None
        img_dir = self._exports_base() / str(campaign_id) / str(product_key) / "images"
        paths = conn.download_images(imgs[:6], img_dir)
        if not paths:
            return None
        # Map images onto the scene roles the slide templates look for.
        roles = ["hero", "lifestyle", "room", "closeup", "scale", "product"]
        by_scene = {roles[i % len(roles)]: p for i, p in enumerate(paths)}
        tags = media.get("tags") or []
        return {
            "campaign_id": campaign_id, "product_key": product_key,
            "title": media.get("title") or product_key,
            "description": re.sub(r"<[^>]+>", " ", media.get("description") or "").strip(),
            "theme": (tags[0] if tags else "") or "the Mediterranean",
            "product_name": media.get("title") or product_key,
            "tags": tags, "price": None,
            "listing_url": self._product_url(campaign_id, product_key) or "",
            "by_scene": by_scene, "master": paths[0],
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
        # No local package (or no imagery in it)? Fall back to the product's
        # Shopify images so imported / recycled products still get a slideshow.
        if ctx is None or (not ctx["by_scene"] and not ctx.get("master")):
            ctx = self._gather_shopify(campaign_id, product_key) or ctx
        if ctx is None:
            return {"ok": False, "reason": "No listing package and no Shopify images "
                    "for this product.", "clips": []}
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

    def build_batch(self, limit: int = 20, formats: list[str] | None = None,
                    skip_existing: bool = True) -> dict[str, Any]:
        """Build slideshow clips for active products that have a built listing
        package, up to ``limit`` clips. Idempotent by default: ``skip_existing``
        builds only the formats a product doesn't already have, so it's safe to
        run repeatedly (e.g. daily) without re-rendering or duplicating clips.
        Returns ``{built, skipped, no_package, clips}``."""
        formats = formats or FORMATS
        have: set = set()
        if skip_existing:
            for c in self.db.list_short_form(limit=5000):
                have.add((c.get("product_key"), c.get("fmt")))
        made: list[dict[str, Any]] = []
        counts = {"inactive": 0, "no_campaign_or_key": 0, "already_have": 0,
                  "no_images": 0, "built_products": 0}
        details: list[dict[str, Any]] = []
        # Approved/live products only — a product held pending approval (with its
        # placeholder artwork) must not get clips built ahead of the operator's OK.
        for p in self._content_products():
            if len(made) >= limit:
                break
            if not p.get("active", 1):
                counts["inactive"] += 1
                continue
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            cid = p.get("campaign_id")
            name = p.get("name") or p.get("sku") or key
            if not key or not cid:
                counts["no_campaign_or_key"] += 1
                details.append({"product": name, "reason": "no campaign_id/product_key"})
                continue
            need = [f for f in formats if (key, f) not in have]
            if not need:
                counts["already_have"] += 1
                continue
            r = self.build_for_product(cid, key, formats=need)
            if r.get("clips"):
                counts["built_products"] += 1
                made.extend(r["clips"])
                log.info("Clips: built %d for %s (%d total).",
                         len(r["clips"]), name, len(made))
            else:
                counts["no_images"] += 1
                details.append({"product": name,
                                "reason": r.get("reason", "no clips built")})
                log.info("Clips: skipped %s — %s.", name,
                         r.get("reason", "no clips built"))
        return {"built": len(made[:limit]), "skipped": counts["already_have"],
                "no_package": counts["no_images"], "clips": made[:limit],
                "counts": counts, "details": details[:50]}

    def _public_base(self) -> str:
        return (self.cfg.get("public_base")
                or (self.config.gelato or {}).get("file_base_url") or "").rstrip("/")

    def _hero_path(self, campaign_id: int, product_key: str) -> Path:
        """Where the hero image lives on disk (mirrors the /exports URL layout)."""
        return (self._exports_base() / str(campaign_id) / str(product_key)
                / "images" / "hero.jpg")

    def _hero_url(self, campaign_id: int, product_key: str) -> str | None:
        base = self._public_base()
        if not base:
            return None
        # Only advertise a hero image that actually exists on disk. A missing
        # file makes Shopify 422-reject the WHOLE blog post ("image failed to
        # download"), which silently stalls the entire blog pipeline — so a
        # product whose art is gone publishes text-only instead of blocking.
        if not self._hero_path(campaign_id, product_key).exists():
            return None
        return f"{base}/{campaign_id}/{product_key}/images/hero.jpg"

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

    def _blocked_from_content(self) -> set[tuple[str, str]]:
        """SKUs/product_keys the operator has NOT approved for publish — content
        must never run ahead of approval. A product whose approval decision is
        'awaiting' or 'rejected' is blocked; 'approved' or no approval row (legacy /
        auto-published) is allowed."""
        blocked: set[tuple[str, str]] = set()
        try:
            for a in self.db.list_product_approvals():
                if (a.get("decision") or "awaiting") in ("awaiting", "rejected"):
                    if a.get("sku"):
                        blocked.add(("sku", a["sku"]))
                    if a.get("product_key"):
                        blocked.add(("pk", a["product_key"]))
        except Exception:  # approvals are a gate, never a hard dependency
            log.debug("approval lookup failed — not blocking content", exc_info=True)
        return blocked

    def _content_products(self) -> list[dict[str, Any]]:
        """Active products that are cleared for marketing content — i.e. not held
        pending (or rejected) in the approval workspace."""
        blocked = self._blocked_from_content()
        out = []
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            if ("sku", p.get("sku")) in blocked or ("pk", p.get("product_key")) in blocked:
                continue
            out.append(p)
        return out

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
        # Only approved/live products — never generate for a product still held
        # pending approval (that's how placeholder-artwork drafts got blogs).
        products = self._content_products()
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
        # Approved/live products only — the evergreen blog schedule must not cycle
        # content for products still held pending approval.
        for p in self._content_products():
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

    def attach_videos_to_shopify(self) -> dict[str, Any]:
        """Attach each product's slideshow video to its Shopify product page as
        VIDEO media (using the public mp4 URL). Idempotent — products that
        already have a video are skipped — so it's safe to run repeatedly and to
        rewrite the whole catalogue at once."""
        conn = self._shopify()
        base = {"checked": 0, "added": 0, "skipped": 0, "details": []}
        if not conn.can_publish:
            return {**base, "ok": False, "reason": "Shopify is not connected."}
        if not self._public_base():
            return {**base, "ok": False,
                    "reason": "No public base URL to host the video (set public_base "
                              "so Shopify can fetch the mp4)."}
        items = []
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            cid = p.get("campaign_id")
            if not cid or not key:
                continue
            pub = self.db.get_latest_publication(
                cid, "shopify", product_id=f"{cid}-{key}")
            if not pub or not pub.get("listing_id"):
                continue
            urls = self._reel_urls(cid, key)
            if not urls:
                continue
            items.append({"product_id": str(pub["listing_id"]), "video_url": urls[0],
                          "alt": p.get("name") or key})
        if not items:
            return {**base, "ok": False,
                    "reason": "No products with both a rendered clip and a Shopify "
                              "listing yet — build clips first."}
        return {"ok": True, **conn.attach_product_videos(items)}

    def launch_product(self, campaign_id: int, product_key: str) -> dict[str, Any]:
        """Launch burst for a NEW product: generate + publish its blog article
        NOW (not dripped), build its slideshow clips, attach the video to Shopify
        + Etsy, and post the blog link + video to Facebook immediately. This is
        the 'it's news' path — evergreen recycling handles everything afterwards."""
        from onassis.distribution import ChannelDistributor

        out: dict[str, Any] = {"ok": True, "product_key": product_key,
                               "blog": 0, "clips": 0, "facebook": 0, "tiktok": 0}
        dist = ChannelDistributor(self.config, self.db)
        # 1) Ensure this product has a blog article, then publish immediately.
        have = {a.get("product_key")
                for a in self.db.list_marketing_assets(channel="blog")}
        if product_key not in have:
            self.generate_blog(limit=1000)
        blog = dist.distribute(channels=["blog"]).get("by_channel", {}).get("blog", {})
        out["blog"] = blog.get("posted", 0)
        # 2) Build the product's clips, then attach the video to Shopify + Etsy.
        out["clips"] = self.build_for_product(campaign_id, product_key).get("count", 0)
        self.attach_videos_to_shopify()
        self.attach_videos_to_etsy()
        # 3) Facebook + TikTok: queue this product's link/video and post now.
        self.queue_facebook_posts()
        fb = dist.distribute(channels=["facebook"], force=True).get(
            "by_channel", {}).get("facebook", {})
        out["facebook"] = fb.get("posted", 0)
        self.queue_tiktok_posts()
        tt = dist.distribute(channels=["tiktok"], force=True).get(
            "by_channel", {}).get("tiktok", {})
        out["tiktok"] = tt.get("posted", 0)
        return out

    def _channel_delivery_failing(self, channel: str, look: int = 6,
                                  need: int = 4) -> bool:
        """True when a channel's most-recent deliveries are ALL failing (e.g. the
        Make webhook is out of operations, or a token expired). Used to pause the
        evergreen drip so it doesn't manufacture a new asset every run that just
        piles up as 'failed' while the channel is down. It resumes automatically
        the moment one delivery succeeds again (a single 'posted' clears it)."""
        decided = [a for a in self.db.list_marketing_assets(channel=channel)
                   if a.get("status") in ("posted", "failed")][:look]
        if len(decided) < need:
            return False                        # not enough evidence to pause
        return all(a.get("status") == "failed" for a in decided)

    def queue_evergreen_facebook(self, per_day: int | None = None) -> dict[str, Any]:
        """Re-share product videos to Facebook on a rotation — least-recently-
        shared products first — so social cycles through the whole catalogue and
        loops back to the beginning (blog links already cycle via recycled
        articles). Gentle by default (1/day). Set ``evergreen_video_per_day: 0``
        to turn it off."""
        per_day = (per_day if per_day is not None
                   else int(self.cfg.get("evergreen_video_per_day", 1)))
        base = self._public_base()
        if per_day <= 0 or not base:
            return {"queued": 0}
        # Don't manufacture new re-shares while the channel is down — they'd only
        # pile up as 'failed'. Resumes automatically once a post succeeds.
        if self._channel_delivery_failing("facebook"):
            return {"queued": 0, "paused": "facebook delivery failing"}
        last_share: dict[str, str] = {}
        for a in self.db.list_marketing_assets(channel="facebook"):
            post = (a.get("payload") or {}).get("post") or {}
            k = a.get("product_key")
            if post.get("video_url") and k:
                ts = a.get("created_at") or ""
                if ts > last_share.get(k, ""):
                    last_share[k] = ts
        cands = []
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            cid = p.get("campaign_id")
            if not cid or not key:
                continue
            urls = self._reel_urls(cid, key)
            if not urls:
                continue
            cands.append((last_share.get(key, ""), cid, key,
                          p.get("name") or key, urls[0]))
        cands.sort(key=lambda x: x[0])          # oldest-shared (or never) first
        queued = 0
        for _, cid, key, name, url in cands[:per_day]:
            self.db.insert_marketing_asset({
                "campaign_id": cid, "product_key": key, "channel": "facebook",
                "payload": {"post": {"video_url": url,
                                     "body": f"{name} — a favourite from the "
                                             f"collection ✨"}}})
            queued += 1
        return {"queued": queued}

    def queue_evergreen_reels(self, per_run: int | None = None) -> dict[str, Any]:
        """Re-share product reels on a rotation — least-recently-shared products
        first — so the reel channel cycles through the whole catalogue and loops
        back to the beginning. These go out on the ``tiktok`` channel (which the
        Make router publishes to FB Reels / TikTok / etc). One per run by default;
        the *frequency* is the cron cadence, so 1/run + an every-few-hours cron
        gives an every-few-hours reel drip. ``evergreen_reels_per_run: 0`` = off."""
        per_run = (per_run if per_run is not None
                   else int(self.cfg.get("evergreen_reels_per_run", 1)))
        base = self._public_base()
        if per_run <= 0 or not base:
            return {"queued": 0}
        # Don't manufacture new re-shares while the channel is down (e.g. Make
        # out of operations) — they'd only pile up as 'failed'. Resumes
        # automatically once a reel posts successfully again.
        if self._channel_delivery_failing("tiktok"):
            return {"queued": 0, "paused": "tiktok delivery failing"}
        last_share: dict[str, str] = {}
        for a in self.db.list_marketing_assets(channel="tiktok"):
            k = a.get("product_key")
            if k:
                ts = a.get("created_at") or ""
                if ts > last_share.get(k, ""):
                    last_share[k] = ts
        cands = []
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            cid = p.get("campaign_id")
            if not cid or not key:
                continue
            urls = self._reel_urls(cid, key)
            if not urls:
                continue
            cands.append((last_share.get(key, ""), cid, key,
                          p.get("name") or key, urls[0]))
        cands.sort(key=lambda x: x[0])          # oldest-shared (or never) first
        queued = 0
        for _, cid, key, name, url in cands[:per_run]:
            self.db.insert_marketing_asset({
                "campaign_id": cid, "product_key": key, "channel": "tiktok",
                "payload": {"post": {"video_url": url,
                                     "body": f"{name} ✨ #reels #SmallBusiness"}}})
            queued += 1
        return {"queued": queued}

    def marketing_overview(self) -> dict[str, Any]:
        """Everything the Marketing tab shows in one place: per-product content —
        slideshow clips, blog articles and Facebook posts — each with its status
        and link, plus totals across the catalogue."""
        base = self._public_base()
        products: dict[str, dict[str, Any]] = {}
        # Product names, keyed the same way — so a row can be labelled without
        # seeding an (empty) row for every product in the catalogue.
        names: dict[str, str] = {}
        for p in self.db.list_products():
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            if key:
                names[f"{p.get('campaign_id')}-{key}"] = p.get("name") or key

        def _row(cid: Any, key: str, name: str | None = None) -> dict[str, Any]:
            # Rows are created ONLY when a product has real content attached — the
            # Content Library never shows empty products (that scales to nothing
            # useful at hundreds of products).
            k = f"{cid}-{key}"
            row = products.setdefault(k, {"campaign_id": cid, "product_key": key,
                                          "name": name or names.get(k) or key,
                                          "clips": [], "blogs": [], "facebook": [],
                                          "tiktok": []})
            if name:
                row["name"] = name
            return row

        for c in self.db.list_short_form(limit=2000):
            key = c.get("product_key")
            if not key:
                continue
            cid = c.get("campaign_id")
            url = f"{base}/reels/{cid}/{key}/{c.get('fmt')}.mp4" if base else ""
            _row(cid, key)["clips"].append({"id": c.get("id"), "fmt": c.get("fmt"),
                                            "url": url, "caption": c.get("caption")})
        for a in self.db.list_marketing_assets(channel="blog"):
            pl = a.get("payload") or {}
            ref = a.get("delivery_ref") or ""
            link = next((x.strip() for x in ref.split("|")
                         if x.strip().startswith("http")), "")
            _row(a.get("campaign_id"), a.get("product_key"))["blogs"].append({
                "id": a.get("id"), "title": pl.get("title"), "angle": pl.get("angle"),
                "status": a.get("status") or "pending",
                "scheduled_date": a.get("scheduled_date"), "url": link})
        for a in self.db.list_marketing_assets(channel="facebook"):
            post = (a.get("payload") or {}).get("post") or {}
            _row(a.get("campaign_id"), a.get("product_key"))["facebook"].append({
                "id": a.get("id"),
                "kind": "video" if post.get("video_url") else "link",
                "status": a.get("status") or "pending",
                "link": post.get("link") or post.get("video_url")})
        for a in self.db.list_marketing_assets(channel="tiktok"):
            _row(a.get("campaign_id"), a.get("product_key"))["tiktok"].append({
                "id": a.get("id"), "status": a.get("status") or "pending"})
        rows = sorted(products.values(), key=lambda r: (r["name"] or "").lower())
        posted = lambda items: sum(1 for i in items if i.get("status") == "posted")
        totals = {
            "products": len(rows),
            "clips": sum(len(r["clips"]) for r in rows),
            "blogs": sum(len(r["blogs"]) for r in rows),
            "blogs_posted": sum(posted(r["blogs"]) for r in rows),
            "facebook": sum(len(r["facebook"]) for r in rows),
            "facebook_posted": sum(posted(r["facebook"]) for r in rows),
            "tiktok": sum(len(r["tiktok"]) for r in rows),
            "tiktok_posted": sum(posted(r["tiktok"]) for r in rows)}
        return {"products": rows, "totals": totals}

    def delete_content(self, kind: str, item_id: int) -> dict[str, Any]:
        """Delete one piece of content from the Marketing tab. ``kind`` is
        ``clip`` (a short-form video — its rendered file is removed too) or a
        marketing-asset channel (``blog``/``facebook``/``tiktok``/``instagram``/
        ``email``). Returns ``{ok, kind, id, removed_file}``."""
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "A numeric id is required."}
        if kind == "clip":
            path = self.db.delete_short_form(item_id)
            removed_file = False
            if path:
                p = Path(path)
                if not p.is_absolute():
                    p = self._exports_base().parent / path if "exports" in str(path) else Path(path)
                for cand in {Path(path), p}:
                    try:
                        if cand.exists():
                            cand.unlink()
                            removed_file = True
                    except OSError as exc:
                        log.warning("Could not delete clip file %s: %s", cand, exc)
            log.info("Deleted short-form clip %s (file removed=%s).", item_id, removed_file)
            return {"ok": True, "kind": "clip", "id": item_id, "removed_file": removed_file}
        ok = self.db.delete_marketing_asset(item_id)
        log.info("Deleted marketing asset %s (kind=%s, found=%s).", item_id, kind, ok)
        return {"ok": ok, "kind": kind, "id": item_id,
                "error": None if ok else "No such asset."}

    def reprice_products(self, apply: bool = False,
                         limit: int | None = None) -> dict[str, Any]:
        """Recompute every live product's price with the current pricing strategy
        and (when ``apply``) push it to its Shopify listing. Default is a PREVIEW —
        it returns the old→new price for each product and changes nothing, so you
        can eyeball a blanket reprice before it touches the store."""
        from onassis.connectors.shopify import ShopifyConnector
        from onassis.pricing import PricingEngine
        pe = PricingEngine(self.config, self.db)
        shop = getattr(self, "_shopify_conn", None) or ShopifyConnector(self.config, self.db)
        rows: list[dict[str, Any]] = []
        changed = errors = 0
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            pk = p.get("product_key") or p.get("sku")
            cid = p.get("campaign_id")
            cost = float(p.get("production_cost") or 0)
            new_price = pe.optimise(cost)["price"] if cost > 0 else None
            pub = (self.db.get_latest_publication(cid, "shopify", product_id=f"{cid}-{pk}")
                   if cid else None)
            listing_id = (pub or {}).get("listing_id")
            row: dict[str, Any] = {"name": p.get("name") or pk, "product_key": pk,
                                   "production_cost": cost, "new_price": new_price,
                                   "old_price": None, "listing_id": listing_id,
                                   "status": "ok"}
            if not new_price:
                row["status"] = "no_cost"          # can't price without a cost
            elif not listing_id:
                row["status"] = "not_on_shopify"
            elif shop.can_publish:
                try:
                    row["old_price"] = shop.product_price(listing_id)
                    if apply:
                        shop.set_product_price(listing_id, new_price)
                        row["status"] = "repriced"
                        changed += 1
                    else:
                        row["status"] = "would_reprice"
                except Exception as exc:  # never let one product abort the run
                    row["status"] = "error"
                    row["error"] = str(exc)[:200]
                    errors += 1
            else:
                row["status"] = "shopify_not_connected"
            rows.append(row)
            if limit and len(rows) >= limit:
                break
        log.info("Reprice %s: %d product(s), %d %s, %d error(s).",
                 "APPLY" if apply else "preview", len(rows), changed,
                 "repriced" if apply else "to change", errors)
        return {"ok": True, "applied": apply, "changed": changed, "errors": errors,
                "count": len(rows), "products": rows}

    def _listing_alive(self, platform: str, listing_id: str, shop: Any,
                       etsy_holder: dict[str, Any]) -> bool:
        """Does this marketplace listing still exist? Only a definite 404 counts
        as dead — any other error is treated as alive (never prune on a transient
        failure)."""
        try:
            if platform == "shopify":
                if not shop.can_publish:
                    return True
                shop.product_price(listing_id)
                return True
            if platform == "etsy":
                if "engine" not in etsy_holder:
                    from onassis.etsy_automation import EtsyAutomationEngine
                    e = EtsyAutomationEngine(self.config, self.db)
                    etsy_holder["engine"] = e if e.is_configured else None
                e = etsy_holder["engine"]
                if e is None:
                    return True
                e.client.get_listing_inventory(listing_id)
                return True
        except Exception as exc:
            msg = str(exc)
            return not ("404" in msg or "Not Found" in msg or "not found" in msg)
        return True

    def prune_dead_publications(self, apply: bool = False) -> dict[str, Any]:
        """Remove publication rows whose marketplace listing has been deleted (404)
        — the stale rows that make the reprice/library show phantom products. Keeps
        the product and any live listings it still has. Preview by default."""
        from onassis.connectors.shopify import ShopifyConnector
        shop = getattr(self, "_shopify_conn", None) or ShopifyConnector(self.config, self.db)
        etsy_holder: dict[str, Any] = {}
        alive_cache: dict[tuple, bool] = {}
        dead: list[dict[str, Any]] = []
        checked = 0
        for pub in self.db.list_publications():
            plat = pub.get("platform")
            lid = pub.get("listing_id")
            pid = pub.get("id")
            if plat not in ("shopify", "etsy") or not lid or pid is None:
                continue
            key = (plat, str(lid))
            if key not in alive_cache:
                checked += 1
                alive_cache[key] = self._listing_alive(plat, str(lid), shop, etsy_holder)
            if not alive_cache[key]:
                dead.append({"id": pid, "platform": plat, "listing_id": lid,
                             "campaign_id": pub.get("campaign_id"),
                             "product_id": pub.get("product_id")})
        removed = 0
        if apply:
            for row in dead:
                if self.db.delete_publication(row["id"]):
                    removed += 1
        log.info("Prune dead listings %s: checked %d, %d dead, %d removed.",
                 "APPLY" if apply else "preview", checked, len(dead), removed)
        return {"ok": True, "applied": apply, "checked": checked, "dead": len(dead),
                "removed": removed, "publications": dead}

    def restore_product_names(self, apply: bool = False) -> dict[str, Any]:
        """Restore each product's ORIGINAL title from its listing.json package —
        undo a bad rename. Preview by default; ``apply`` sets the DB name and pushes
        the original title back to every live Shopify + Etsy listing."""
        from onassis.connectors.shopify import ShopifyConnector
        shop = getattr(self, "_shopify_conn", None) or ShopifyConnector(self.config, self.db)
        etsy = None
        rows: list[dict[str, Any]] = []
        changed = errors = 0
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            cid = p.get("campaign_id")
            key = p.get("product_key") or p.get("sku")
            listing = self._gather(cid, key) if (cid and key) else None
            original = (listing or {}).get("title")
            old_name = p.get("name")
            row: dict[str, Any] = {"id": p.get("id"), "old_name": old_name,
                                   "original": original, "platforms": [], "status": "ok"}
            if not original:
                row["status"] = "no_package"        # no listing.json title to restore
            elif original == old_name:
                row["status"] = "unchanged"
            elif apply:
                self.db.set_product_name(p["id"], original)
                for platform in ("shopify", "etsy"):
                    pub = (self.db.get_latest_publication(cid, platform,
                                                          product_id=f"{cid}-{key}")
                           if cid else None)
                    lid = (pub or {}).get("listing_id")
                    if not lid:
                        continue
                    try:
                        if platform == "shopify" and shop.can_publish:
                            shop.set_product_title(lid, original)
                            row["platforms"].append("shopify")
                        elif platform == "etsy":
                            if etsy is None:
                                from onassis.etsy_automation import EtsyAutomationEngine
                                etsy = EtsyAutomationEngine(self.config, self.db)
                            if etsy.is_configured:
                                etsy.update_title(lid, original, source="restore")
                                row["platforms"].append("etsy")
                    except Exception as exc:
                        row["status"] = "error"
                        row["error"] = str(exc)[:150]
                        errors += 1
                if row["status"] != "error":
                    row["status"] = "restored"
                    changed += 1
            rows.append(row)
        log.info("Restore names %s: %d product(s), %d restored, %d error(s).",
                 "APPLY" if apply else "preview", len(rows), changed, errors)
        return {"ok": True, "applied": apply, "changed": changed, "errors": errors,
                "count": len(rows), "products": rows}

    @staticmethod
    def _type_label(product_key: str | None) -> str:
        from onassis.product_naming import type_label
        return type_label(product_key)

    @staticmethod
    def _display_name(design: str | None, type_name: str | None) -> str:
        from onassis.product_naming import display_name
        return display_name(design, type_name=type_name)

    def rename_products(self, apply: bool = False,
                        limit: int | None = None) -> dict[str, Any]:
        """Rename each product to include its design (the campaign name), so the
        store shows distinct listings instead of 28 identical 'Ceramic Mug's.
        Preview by default; ``apply`` updates the DB name and pushes the new title
        to every live Shopify + Etsy listing."""
        from onassis.connectors.shopify import ShopifyConnector
        shop = getattr(self, "_shopify_conn", None) or ShopifyConnector(self.config, self.db)
        etsy = None
        rows: list[dict[str, Any]] = []
        changed = errors = 0
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            cid = p.get("campaign_id")
            camp = self.db.get_campaign(cid) if cid else None
            design = (camp or {}).get("name")
            old_name = p.get("name")
            key = p.get("product_key") or p.get("sku")
            # Type comes from the STABLE product_key, never the current name —
            # otherwise a re-run feeds the composed name back and drops the type.
            new_name = self._display_name(design, self._type_label(key))
            row: dict[str, Any] = {"id": p.get("id"), "old_name": old_name,
                                   "new_name": new_name, "design": design,
                                   "platforms": [], "status": "ok"}
            if new_name == old_name or not design:
                row["status"] = "unchanged"
                rows.append(row)
                if limit and len(rows) >= limit:
                    break
                continue
            if apply:
                self.db.set_product_name(p["id"], new_name)
                for platform in ("shopify", "etsy"):
                    pub = (self.db.get_latest_publication(cid, platform,
                                                          product_id=f"{cid}-{key}")
                           if cid else None)
                    lid = (pub or {}).get("listing_id")
                    if not lid:
                        continue
                    try:
                        if platform == "shopify" and shop.can_publish:
                            shop.set_product_title(lid, new_name)
                            row["platforms"].append("shopify")
                        elif platform == "etsy":
                            if etsy is None:
                                from onassis.etsy_automation import EtsyAutomationEngine
                                etsy = EtsyAutomationEngine(self.config, self.db)
                            if etsy.is_configured:
                                etsy.update_title(lid, new_name, source="rename")
                                row["platforms"].append("etsy")
                    except Exception as exc:  # one platform never aborts the run
                        row["status"] = "error"
                        row["error"] = str(exc)[:150]
                        errors += 1
                if row["status"] != "error":
                    row["status"] = "renamed"
                    changed += 1
            rows.append(row)
            if limit and len(rows) >= limit:
                break
        log.info("Rename %s: %d product(s), %d renamed, %d error(s).",
                 "APPLY" if apply else "preview", len(rows), changed, errors)
        return {"ok": True, "applied": apply, "changed": changed, "errors": errors,
                "count": len(rows), "products": rows}

    def clear_marketing(self, channel: str | None = None,
                        status: str | None = None) -> dict[str, Any]:
        """Bulk-delete marketing assets to de-clutter the Marketing tab — e.g. every
        failed article (status='failed'), or a whole channel's queue. Returns
        ``{ok, deleted}``."""
        n = self.db.delete_marketing_assets(channel=channel, status=status)
        log.info("Cleared %d marketing asset(s) (channel=%s, status=%s).",
                 n, channel or "*", status or "*")
        return {"ok": True, "deleted": n}

    def clear_clips(self, product_key: str | None = None) -> dict[str, Any]:
        """Delete short-form clips in bulk (all, or one product's) and remove their
        rendered files — the fast way to clear placeholder/duplicate clips before
        regenerating. Returns ``{ok, deleted, files_removed}``."""
        deleted = files = 0
        for c in self.db.list_short_form(limit=5000):
            if product_key and c.get("product_key") != product_key:
                continue
            r = self.delete_content("clip", c.get("id"))
            if r.get("ok"):
                deleted += 1
                files += 1 if r.get("removed_file") else 0
        log.info("Cleared %d clip(s) (%d file(s) removed)%s.", deleted, files,
                 f" for {product_key}" if product_key else "")
        return {"ok": True, "deleted": deleted, "files_removed": files}

    def queue_tiktok_posts(self) -> dict[str, Any]:
        """Queue a TikTok video post for each product's slideshow (idempotent).
        The distributor ships them once TikTok is connected and the toggle is on."""
        base = self._public_base()
        if not base:
            return {"ok": False, "videos": 0, "reason": "No public base URL for videos."}
        existing = set()
        for a in self.db.list_marketing_assets(channel="tiktok"):
            post = (a.get("payload") or {}).get("post") or {}
            if post.get("video_url"):
                existing.add(post["video_url"])
        videos = 0
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            cid = p.get("campaign_id")
            if not cid or not key:
                continue
            urls = self._reel_urls(cid, key)
            if not urls or urls[0] in existing:
                continue
            name = p.get("name") or key
            self.db.insert_marketing_asset({
                "campaign_id": cid, "product_key": key, "channel": "tiktok",
                "payload": {"post": {"video_url": urls[0],
                                     "body": f"{name} ✨ #TikTokMadeMeBuyIt "
                                             f"#SmallBusiness"}}})
            existing.add(urls[0])
            videos += 1
        return {"ok": True, "videos": videos}

    def queue_facebook_posts(self) -> dict[str, Any]:
        """Queue Facebook Page posts for the content we've produced: a link post
        for each published blog article, and a video post for each product's
        slideshow. Idempotent — dedupes against Facebook assets already queued —
        so it's safe to run every day. The existing distributor ships them (once
        Facebook is connected and the channel toggle is on)."""
        existing_links, existing_videos = set(), set()
        for a in self.db.list_marketing_assets(channel="facebook"):
            pl = a.get("payload") or {}
            post = pl.get("post") or {}
            if post.get("link"):
                existing_links.add(post["link"])
            if post.get("video_url") or pl.get("video_url"):
                existing_videos.add(post.get("video_url") or pl.get("video_url"))
        import re

        def _fix_blog_url(link: str) -> str:
            # Storefront blog URLs use the blog HANDLE, not its numeric id — a
            # numeric id 404s. Replace a numeric /blogs/<id>/ segment with the handle.
            m = re.match(r"(https?://[^/]+/blogs/)(\d+)(/.+)", link)
            if not m:
                return link
            handle = self._shopify().blog_handle(m.group(2)) or m.group(2)
            return f"{m.group(1)}{handle}{m.group(3)}"

        blogs = videos = 0
        # 1) Published blog articles → link posts (drives traffic to the store).
        for a in self.db.list_marketing_assets(channel="blog"):
            if (a.get("status") or "") != "posted":
                continue
            ref = a.get("delivery_ref") or ""
            link = next((p.strip() for p in ref.split("|")
                         if p.strip().startswith("http")), "")
            link = _fix_blog_url(link)
            if not link or link in existing_links:
                continue
            pl = a.get("payload") or {}
            title = pl.get("title") or "New on the blog"
            self.db.insert_marketing_asset({
                "campaign_id": a.get("campaign_id"), "product_key": a.get("product_key"),
                "channel": "facebook",
                "payload": {"post": {"body": f"{title}\n\nRead more on our blog:",
                                     "link": link}}})
            existing_links.add(link)
            blogs += 1
        # 2) Product slideshow clips → video posts.
        base = self._public_base()
        if base:
            for p in self.db.list_products():
                if not p.get("active", 1):
                    continue
                key = (p.get("product_key") or p.get("sku")
                       or (f"product-{p.get('id')}" if p.get("id") else None))
                cid = p.get("campaign_id")
                if not cid or not key:
                    continue
                urls = self._reel_urls(cid, key)
                if not urls or urls[0] in existing_videos:
                    continue
                name = p.get("name") or key
                self.db.insert_marketing_asset({
                    "campaign_id": cid, "product_key": key, "channel": "facebook",
                    "payload": {"post": {"video_url": urls[0],
                                         "body": f"{name} — see it in action ✨"}}})
                existing_videos.add(urls[0])
                videos += 1
        return {"ok": True, "blogs": blogs, "videos": videos,
                "queued": blogs + videos}

    def attach_videos_to_etsy(self) -> dict[str, Any]:
        """Upload each product's slideshow video to its Etsy listing. Idempotent
        (skips listings that already have a video). Needs the clip rendered
        locally — run the clip build first (the daily cycle does)."""
        from onassis.connectors.etsy import EtsyConnector

        conn = getattr(self, "_etsy_conn", None)
        if conn is None:
            conn = self._etsy_conn = EtsyConnector(self.config, self.db)
        base = {"checked": 0, "added": 0, "skipped": 0, "details": []}
        if not conn.is_configured:
            return {**base, "ok": False, "reason": "Etsy is not connected."}
        clip_by_key: dict[str, str] = {}
        for c in self.db.list_short_form(limit=2000):
            k = c.get("product_key")
            if k and k not in clip_by_key and c.get("path"):
                clip_by_key[k] = c["path"]
        items = []
        for p in self.db.list_products():
            if not p.get("active", 1):
                continue
            key = (p.get("product_key") or p.get("sku")
                   or (f"product-{p.get('id')}" if p.get("id") else None))
            cid = p.get("campaign_id")
            if not cid or not key:
                continue
            pub = self.db.get_latest_publication(cid, "etsy", product_id=f"{cid}-{key}")
            if not pub or not pub.get("listing_id"):
                continue
            vp = clip_by_key.get(key)
            if not vp:
                continue
            items.append({"listing_id": pub["listing_id"], "video_path": vp,
                          "name": p.get("name") or key})
        if not items:
            return {**base, "ok": False,
                    "reason": "No products with both a rendered clip and an Etsy "
                              "listing yet — build clips first."}
        return {"ok": True, **conn.attach_listing_videos(items)}

    def reformat_shopify_descriptions(self) -> dict[str, Any]:
        """Reformat the descriptions of our live Shopify products in place, so
        posts published with a plain-text description get proper HTML. Targets
        only products we published (from our publication records)."""
        conn = getattr(self, "_shopify_conn", None)
        if conn is None:
            from onassis.connectors.shopify import ShopifyConnector
            conn = self._shopify_conn = ShopifyConnector(self.config, self.db)
        if not conn.can_publish:
            return {"ok": False, "reason": "Shopify is not connected.",
                    "checked": 0, "updated": 0, "skipped": 0}
        ids, seen = [], set()
        for pub in self.db.list_publications():
            if pub.get("platform") != "shopify":
                continue
            lid = pub.get("listing_id")
            if lid and str(lid) not in seen:
                seen.add(str(lid))
                ids.append(str(lid))
        if not ids:
            return {"ok": False, "reason": "No published Shopify products found.",
                    "checked": 0, "updated": 0, "skipped": 0}
        return {"ok": True, **conn.reformat_product_descriptions(ids)}

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
        used: dict[str, int] = {}
        have_variants: set = set()
        to_place = []                       # already-generated pending, needs a date
        for a in self.db.list_marketing_assets(channel="blog"):
            status = a.get("status") or "pending"
            sd = a.get("scheduled_date")
            pl = a.get("payload") or {}
            if sd and sd >= today_s:
                # ANY asset already sitting on a future day fills that slot — a
                # day that has already posted (or already failed) must not get a
                # second post piled on, nor a failed one re-queued every run.
                used[sd] = used.get(sd, 0) + 1
                have_variants.add((a.get("product_key"), pl.get("angle")))
            elif status == "pending":
                # Undated / past-dated PENDING assets can be re-dated forward.
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
