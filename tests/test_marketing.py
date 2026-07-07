"""Tests for the Marketing Engine — a full promotion kit per published product."""

from __future__ import annotations

from onassis.marketing import CHANNELS, MarketingEngine

_LISTING = {
    "title": "Linen Throw — Slow Mediterranean Mornings, Stonewashed Coastal Blanket",
    "description": "A stonewashed linen throw made for unhurried coastal mornings. "
                   "Woven for warmth and calm.",
    "tags": ["linen throw", "coastal blanket", "slow living", "mediterranean decor"],
    "seo_keywords": ["linen throw", "coastal blanket", "stonewashed linen", "slow living"],
    "theme": "coastal mornings", "product_key": "linen_throw",
}


def _engine(config, db=None):
    return MarketingEngine(config, db)


def test_builds_every_channel(config, db):
    kit = _engine(config, db).build(_LISTING, listing_id="555", campaign_id=1,
                                    product_key="linen_throw")
    for channel in CHANNELS:
        assert channel in kit
    assert kit["listing_url"] == "https://www.etsy.com/listing/555"


def test_five_pins_across_multiple_aspect_ratios(config):
    kit = _engine(config).build(_LISTING, listing_id="555")
    pins = kit["pinterest"]["pins"]
    assert len(pins) == 5
    assert len({p["aspect_ratio"] for p in pins}) >= 2      # multiple aspect ratios
    for p in pins:
        assert p["description"] and len(p["keywords"]) > 0  # keyword-rich
        assert p["board"]


def test_blog_generates_multiple_seo_articles(config):
    blog = _engine(config).build(_LISTING, listing_id="555")["blog"]
    assert blog["article_count"] >= 3
    angles = {a["angle"] for a in blog["articles"]}
    assert {"launch", "gift_guide"} <= angles
    for a in blog["articles"]:
        assert a["title"] and a["body"] and a["cta_link"].endswith("/555")


def test_every_asset_links_back_to_etsy(config):
    url = "https://www.etsy.com/listing/999"
    kit = _engine(config).build(_LISTING, listing_url=url)
    assert all(p["link"] == url for p in kit["pinterest"]["pins"])
    assert kit["instagram"]["link"] == url
    assert kit["facebook"]["post"]["link"] == url
    assert kit["facebook"]["boost"]["link"] == url
    assert kit["blog"]["cta_link"] == url
    assert kit["email"]["cta_link"] == url
    assert url in kit["email"]["body"]


def test_instagram_has_reel_carousel_and_captions(config):
    ig = _engine(config).build(_LISTING, listing_id="1")["instagram"]
    assert len(ig["reel_script"]) >= 1
    assert len(ig["carousel"]) >= 2
    assert len(ig["captions"]) >= 1


def test_facebook_post_and_boost_suggestion(config):
    fb = _engine(config).build(_LISTING, listing_id="1")["facebook"]
    assert fb["post"]["body"]
    assert fb["boost"]["suggested_daily_budget"] > 0
    assert fb["boost"]["audience"]["interests"]


def test_blog_is_an_seo_article(config):
    blog = _engine(config).build(_LISTING, listing_id="1")["blog"]
    assert blog["title"] and blog["slug"] and blog["meta_description"]
    assert len(blog["sections"]) >= 3
    assert blog["word_count"] >= 40
    assert len(blog["meta_description"]) <= 160


def test_email_newsletter(config):
    email = _engine(config).build(_LISTING, listing_id="1")["email"]
    assert email["subject"] and email["body"] and email["cta_link"]


def test_assets_are_persisted_per_channel(config, db):
    _engine(config, db).build(_LISTING, listing_id="7", campaign_id=3,
                              product_key="linen_throw")
    assert db.count_marketing_assets() == len(CHANNELS)
    pins = db.list_marketing_assets(product_key="linen_throw", channel="pinterest")
    assert len(pins) == 1
    assert pins[0]["payload"]["count"] == 5


def test_safe_with_a_minimal_listing(config):
    kit = _engine(config).build({"title": "Ceramic Mug"}, listing_id="2")
    assert len(kit["pinterest"]["pins"]) == 5
    assert kit["blog"]["title"]
