"""The Marketing Engine — a whole promotion kit for every published product.

A listing nobody sees never sells. The moment a product goes live, this engine
produces its complete, channel-native marketing kit — and **every asset links
straight back to the Etsy listing**:

* **Pinterest** — 5 pins across multiple aspect ratios, each with a keyword-rich,
  search-optimised description and a board suggestion.
* **Instagram** — a reel script (scene-by-scene), a carousel, and captions.
* **Facebook** — an organic post plus a boosted-post suggestion (budget +
  audience).
* **Blog** — an SEO article (title, slug, meta description, headed sections).
* **Email** — a newsletter (subject, preview, body) with the listing CTA.

It is deterministic — it composes real copy from the listing the system already
produced (title, description, tags, SEO keywords, price) — so it needs no network
and is fully offline-testable. A replaceable copy provider can enrich the wording
later without changing the pipeline. Assets are persisted so the Traffic Engine
can distribute them and the dashboard can count them.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

CHANNELS = ["pinterest", "instagram", "facebook", "tiktok", "blog", "email"]


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:80] or "listing"


class MarketingEngine:
    """Builds (and stores) the full marketing kit for one published product."""

    def __init__(self, config: Config, db: Database | None = None) -> None:
        self.config = config
        self.db = db
        cfg = getattr(config, "marketing", None) or {}
        self.brand = (config.brand or {}).get("name", "Local Celebrity")
        pin = cfg.get("pinterest") or {}
        self.pins_per_product = int(pin.get("pins_per_product", 5))
        self.aspect_ratios = pin.get("aspect_ratios") or ["2:3", "1:1", "9:16"]
        self.hashtags = pin.get("hashtags") or [
            "#MediterraneanStyle", "#SlowLiving", "#CoastalHome", "#EtsyFinds"]
        ig = cfg.get("instagram") or {}
        self.carousel_slides = int(ig.get("carousel_slides", 5))
        self.reel_beats = int(ig.get("reel_beats", 5))

    # --- The kit -----------------------------------------------------

    def build(self, listing: dict[str, Any], *, listing_url: str | None = None,
              listing_id: str | None = None, campaign_id: int | None = None,
              product_key: str | None = None, store: bool = True) -> dict[str, Any]:
        """Produce every channel asset for one live product. All link to Etsy."""
        url = listing_url or (f"https://www.etsy.com/listing/{listing_id}"
                              if listing_id else "")
        ctx = self._context(listing, url)

        kit = {
            "product_key": product_key or listing.get("product_key"),
            "listing_id": listing_id, "listing_url": url,
            "pinterest": self._pinterest(ctx),
            "instagram": self._instagram(ctx),
            "facebook": self._facebook(ctx),
            "tiktok": self._tiktok(ctx),
            "blog": self._blog(ctx),
            "email": self._email(ctx),
        }
        # Sanity: every asset points back to the listing.
        assert self._all_link_back(kit, url), "a marketing asset is missing the Etsy link"

        if store and self.db:
            for channel in CHANNELS:
                self.db.insert_marketing_asset({
                    "campaign_id": campaign_id, "product_key": kit["product_key"],
                    "listing_id": listing_id, "listing_url": url,
                    "channel": channel, "payload": kit[channel]})
        log.info("Marketing kit built for %s: %d pins + IG/FB/blog/email, all -> %s.",
                 kit["product_key"], len(kit["pinterest"]["pins"]), url or "(no url)")
        return kit

    def blog_only(self, listing: dict[str, Any], *, listing_url: str | None = None,
                  listing_id: str | None = None, campaign_id: int | None = None,
                  product_key: str | None = None, store: bool = True) -> dict[str, Any]:
        """Generate ONLY the SEO blog articles for a product (deterministic, $0) —
        so blog content can be produced on demand without a full production run or
        the other channels."""
        url = listing_url or (f"https://www.etsy.com/listing/{listing_id}"
                              if listing_id else "")
        blog = self._blog(self._context(listing, url))
        if store and self.db:
            self.db.insert_marketing_asset({
                "campaign_id": campaign_id,
                "product_key": product_key or listing.get("product_key"),
                "listing_id": listing_id, "listing_url": url,
                "channel": "blog", "payload": blog})
        return blog

    # --- Channels ----------------------------------------------------

    def _pinterest(self, c: dict[str, Any]) -> dict[str, Any]:
        pins: list[dict[str, Any]] = []
        angles = ["Style it your way", "The story behind the design",
                  "How to use it", "Why it belongs in your home", "A gift they'll love"]
        for i in range(self.pins_per_product):
            keyword = c["keywords"][i % len(c["keywords"])]
            angle = angles[i % len(angles)]
            pins.append({
                "title": f"{c['title_short']} — {keyword.title()}"[:100],
                "description": self._pin_description(c, keyword, angle),
                "aspect_ratio": self.aspect_ratios[i % len(self.aspect_ratios)],
                "board": self._board(c, keyword),
                "keywords": self._pin_keywords(c, keyword),
                "hashtags": self.hashtags,
                "link": c["url"], "alt_text": f"{c['title_short']} — {keyword}"[:250],
            })
        return {"pins": pins, "count": len(pins)}

    def _pin_description(self, c: dict[str, Any], keyword: str, angle: str) -> str:
        kw = ", ".join(self._pin_keywords(c, keyword)[:4])
        return (f"{angle}: {c['title_short']}. {c['hook']} "
                f"{keyword.title()} for {c['audience']}. "
                f"Shop the {c['brand']} listing on Etsy — link below. "
                f"{kw}. {' '.join(self.hashtags)}")[:495]

    def _pin_keywords(self, c: dict[str, Any], keyword: str) -> list[str]:
        seen: list[str] = []
        for k in [keyword, *c["keywords"], *c["tags"]]:
            k = str(k).strip().lower()
            if k and k not in seen:
                seen.append(k)
        return seen[:8]

    def _board(self, c: dict[str, Any], keyword: str) -> str:
        theme = c["theme"].title()
        return f"{self.brand} · {theme}" if theme else f"{self.brand} · {keyword.title()}"

    def _instagram(self, c: dict[str, Any]) -> dict[str, Any]:
        reel = [
            {"beat": 1, "on_screen": c["title_short"], "voiceover": c["hook"]},
            {"beat": 2, "on_screen": "The details", "voiceover":
             f"Made for {c['audience']} who love {c['theme']}."},
            {"beat": 3, "on_screen": "In your space", "voiceover":
             f"{c['use']}"},
            {"beat": 4, "on_screen": "Why it's special", "voiceover": c["hook"]},
            {"beat": 5, "on_screen": "Shop on Etsy", "voiceover":
             "Tap the link in bio to shop the listing."},
        ][: self.reel_beats]
        carousel = [{"slide": 1, "text": c["title_short"]}]
        for i, kw in enumerate(c["keywords"][: self.carousel_slides - 2], start=2):
            carousel.append({"slide": i, "text": f"{kw.title()} — {c['brand']}"})
        carousel.append({"slide": len(carousel) + 1,
                         "text": f"Shop now on Etsy → {c['url']}"})
        captions = [
            f"{c['hook']} {c['title_short']} — now live. Link in bio → {c['url']} "
            f"{' '.join(self.hashtags)}",
            f"{c['title_short']} for {c['audience']}. Shop the {c['brand']} listing "
            f"on Etsy: {c['url']}",
        ]
        return {"reel_script": reel, "carousel": carousel, "captions": captions,
                "link": c["url"]}

    def _facebook(self, c: dict[str, Any]) -> dict[str, Any]:
        post = (f"{c['hook']} Meet {c['title_short']} — {c['blurb']} "
                f"Shop it on Etsy: {c['url']}")
        boost = {
            "suggested_daily_budget": 5.0, "duration_days": 5,
            "objective": "traffic",
            "audience": {"interests": c["keywords"][:5],
                         "description": f"{c['audience']} interested in {c['theme']}"},
            "cta": "Shop Now", "link": c["url"],
        }
        return {"post": {"body": post, "link": c["url"]}, "boost": boost}

    # Angles that turn one product into several SEO assets (Sprint 42, Obj 3).
    _BLOG_ANGLES = [
        ("launch", "{title}: {theme} for Your Home",
         ["Introducing {title}", "Why {theme} Works", "How to Style It", "Shop the Piece"]),
        ("gift_guide", "The Best {theme} Gifts — Featuring {title}",
         ["A Thoughtful Gift", "Who It's For", "Why It Delights", "Where to Buy"]),
        ("interior", "Styling {title} in a {theme} Home",
         ["Setting the Scene", "Pairings & Palettes", "The Finishing Touch", "Get the Look"]),
        ("lifestyle", "Living the {theme} Life with {title}",
         ["A Slower Morning", "Everyday Rituals", "Bringing It Home", "Make It Yours"]),
    ]

    def _tiktok(self, c: dict[str, Any]) -> dict[str, Any]:
        """A TikTok kit — future-ready for AI video generation (Sprint 42, Obj 3)."""
        hook = f"POV: you found the perfect {c['title_short'].lower()} ✨"
        script = [
            {"beat": 1, "shot": "Close-up reveal", "line": hook},
            {"beat": 2, "shot": "In-use / lifestyle",
             "line": f"Made for {c['audience']} who love {c['theme']}."},
            {"beat": 3, "shot": "Detail pan", "line": c["hook"]},
            {"beat": 4, "shot": "Call to action",
             "line": "Link in bio to shop it on Etsy."},
        ]
        return {
            "hook": hook,
            "script": script,
            "voiceover": " ".join(b["line"] for b in script),
            "caption": f"{c['title_short']} — {c['blurb']} 🛒 Etsy (link in bio)",
            "hashtags": [*self.hashtags, "#TikTokMadeMeBuyIt", "#SmallBusiness"],
            "ai_video_prompt": (f"A warm, sunlit {c['theme']} scene showcasing "
                                f"{c['title_short']}; slow cinematic push-in, natural "
                                f"light, Mediterranean palette, cosy and aspirational."),
            "image_sequence": [f"{c['title_short']} — {kw}" for kw in c["keywords"][:4]],
            "music_suggestion": "Warm acoustic / lo-fi Mediterranean instrumental",
            "link": c["url"],
        }

    def _blog(self, c: dict[str, Any]) -> dict[str, Any]:
        articles = []
        for slug_key, title_tpl, headings in self._BLOG_ANGLES:
            title = title_tpl.format(title=c["title_short"], theme=c["theme"].title())
            sections = [{
                "heading": h,
                "body": (f"{c['hook']} {c['blurb']} {c['use']} "
                         f"{c['title_short']} for {c['audience']} who love {c['theme']}. "
                         f"Shop it on Etsy: {c['url']}.").strip(),
            } for h in headings]
            body = "\n\n".join(f"## {s['heading']}\n{s['body']}" for s in sections)
            articles.append({
                "angle": slug_key, "title": title, "slug": _slug(title),
                "meta_description": f"{c['blurb']} Shop {c['title_short']} on Etsy."[:160],
                "keywords": c["keywords"], "sections": sections,
                "body": body, "word_count": len(body.split()), "cta_link": c["url"],
            })
        primary = articles[0]
        # Keep the primary article's fields at the top level (back-compat) plus
        # the full set of SEO articles this product generates.
        return {**primary, "articles": articles, "article_count": len(articles),
                "cta_link": c["url"]}

    def _email(self, c: dict[str, Any]) -> dict[str, Any]:
        subject = f"New: {c['title_short']} ✨"
        body = (f"Hello,\n\n{c['hook']} We've just released {c['title_short']} — "
                f"{c['blurb']}\n\nMade for {c['audience']} who love {c['theme']}. "
                f"{c['use']}\n\nShop it now on Etsy:\n{c['url']}\n\n"
                f"With warmth,\nThe {c['brand']} team")
        return {"subject": subject, "preview": c["blurb"][:100], "body": body,
                "cta_link": c["url"]}

    # --- Context (deterministic copy from the listing) ---------------

    def _context(self, listing: dict[str, Any], url: str) -> dict[str, Any]:
        title = (listing.get("title") or listing.get("campaign_name")
                 or listing.get("product_name") or "New arrival")
        title_short = title.split(" — ")[0].split(",")[0].strip()[:60]
        description = (listing.get("description") or "").strip()
        blurb = (description.split(". ")[0].strip() or title_short)
        if blurb and not blurb.endswith("."):
            blurb += "."
        keywords = self._keywords(listing)
        tags = [str(t).lower() for t in (listing.get("tags") or [])][:13]
        theme = (listing.get("theme") or listing.get("motif")
                 or (keywords[0] if keywords else "Mediterranean lifestyle"))
        return {
            "url": url, "brand": self.brand, "title_short": title_short,
            "blurb": blurb, "keywords": keywords or ["mediterranean lifestyle"],
            "tags": tags, "theme": str(theme),
            "audience": (self.config.brand or {}).get(
                "target_audience", "design lovers"),
            "hook": "Bring the slow, sun-soaked calm of the Mediterranean home.",
            "use": "Style it in a light-filled corner and let it set the mood.",
        }

    @staticmethod
    def _keywords(listing: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for k in [*(listing.get("seo_keywords") or []), *(listing.get("tags") or [])]:
            k = str(k).strip().lower()
            if k and k not in out:
                out.append(k)
        return out[:10]

    @staticmethod
    def _all_link_back(kit: dict[str, Any], url: str) -> bool:
        if not url:
            return True  # nothing to link to yet (e.g. draft with no listing id)
        checks = [
            all(p["link"] == url for p in kit["pinterest"]["pins"]),
            kit["instagram"]["link"] == url,
            kit["facebook"]["post"]["link"] == url,
            kit["tiktok"]["link"] == url,
            kit["blog"]["cta_link"] == url,
            kit["email"]["cta_link"] == url,
        ]
        return all(checks)

    # --- Reads -------------------------------------------------------

    def assets_for(self, product_key: str) -> list[dict[str, Any]]:
        return self.db.list_marketing_assets(product_key=product_key) if self.db else []
