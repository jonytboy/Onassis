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

    def _reel_urls(self, campaign_id: int, product_key: str) -> list[str]:
        """Public URLs of a product's rendered clips (for embedding in the blog)."""
        base = (self.cfg.get("public_base")
                or (self.config.gelato or {}).get("file_base_url") or "").rstrip("/")
        if not base:
            return []
        clips = [c for c in self.db.list_short_form(limit=500)
                 if c.get("product_key") == product_key and c.get("campaign_id") == campaign_id]
        return [f"{base}/reels/{campaign_id}/{product_key}/{c['fmt']}.mp4" for c in clips]

    # --- Blog articles (on demand, deterministic) -------------------

    def generate_blog(self, limit: int = 50) -> dict[str, Any]:
        """Generate SEO blog articles for active products that don't have any yet
        (deterministic, $0). 'Publish blog now' then ships them to Shopify. This
        decouples blog content from a full production run."""
        from onassis.marketing import MarketingEngine

        me = MarketingEngine(self.config, self.db)
        have = {a.get("product_key") for a in self.db.list_marketing_assets(channel="blog")}
        made = 0
        for p in self.db.list_products():
            if made >= limit:
                break
            key, cid = p.get("product_key"), p.get("campaign_id")
            if not p.get("active", 1) or not key or not cid or key in have:
                continue
            ctx = self._gather(cid, key) or {}
            listing = {"title": ctx.get("title") or p.get("name") or key,
                       "description": ctx.get("description") or "",
                       "theme": ctx.get("theme") or "",
                       "product_name": ctx.get("product_name") or p.get("name") or key,
                       "tags": ctx.get("tags") or [], "product_key": key}
            me.blog_only(listing, listing_url=ctx.get("listing_url"),
                         campaign_id=cid, product_key=key, videos=self._reel_urls(cid, key))
            have.add(key)
            made += 1
        return {"generated": made}

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
