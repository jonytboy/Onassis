"""Tests for the Etsy Intelligence Engine — real conversion + search terms."""

from __future__ import annotations

import pytest

from onassis.etsy_intelligence import EtsyIntelligence, SearchTermsProvider
from onassis.revenue import RevenueEngine


def _listing_with_sales(config, db, *, listing_id, sku, key, views, units, price=22.0):
    db.insert_product({"sku": sku, "product_key": key, "campaign_id": 1,
                       "production_cost": 7.5})
    db.upsert_etsy_listing({"listing_id": listing_id, "product_id": sku,
                            "campaign_id": 1, "views": views, "num_favorers": 5})
    rev = RevenueEngine(config, db)
    for i in range(units):
        rev.record_order({"order_ref": f"{sku}-{i}", "product_id": sku, "platform": "etsy",
                          "sale_price": price, "quantity": 1, "production_cost": 7.5})


def test_real_conversion_from_views_and_orders(config, db):
    _listing_with_sales(config, db, listing_id=555, sku="1-mug", key="ceramic_mug",
                        views=200, units=6)
    report = EtsyIntelligence(config, db).conversion_report()
    mug = next(p for p in report["products"] if p["sku"] == "1-mug")
    assert mug["conversion"] == pytest.approx(0.03)      # 6 / 200
    assert report["shop"]["conversion"] == pytest.approx(0.03)
    assert report["shop"]["orders"] == 6


def test_conversion_lookup_maps_sku_to_rate(config, db):
    _listing_with_sales(config, db, listing_id=1, sku="1-mug", key="ceramic_mug",
                        views=100, units=5)
    lookup = EtsyIntelligence(config, db).conversion_lookup()
    assert lookup["1-mug"] == pytest.approx(0.05)


def test_search_terms_default_provider_imports_nothing(config, db):
    out = EtsyIntelligence(config, db).import_search_terms()
    assert out["imported"] == 0 and out["source"] == "none"   # honest: no public API


def test_search_terms_from_a_real_provider_are_stored(config, db):
    _listing_with_sales(config, db, listing_id=555, sku="1-mug", key="ceramic_mug",
                        views=100, units=2)

    class StubTerms(SearchTermsProvider):
        name = "stub"

        def fetch(self, shop_id, listings):
            return [{"term": "greek mug", "listing_id": 555, "impressions": 400,
                     "clicks": 40, "orders": 2},
                    {"term": "aegean pottery", "listing_id": 555, "impressions": 100,
                     "clicks": 5, "orders": 0}]

    intel = EtsyIntelligence(config, db, search_provider=StubTerms())
    out = intel.import_search_terms()
    assert out["imported"] == 2 and out["source"] == "stub"
    perf = intel.keyword_performance()
    top = next(t for t in perf if t["term"] == "greek mug")
    assert top["clicks"] == 40 and top["ctr"] == pytest.approx(0.10)
    # Linked to the product behind the listing.
    stored = db.list_etsy_search_terms(term="greek mug")[0]
    assert stored["product_key"] == "ceramic_mug"


def test_intelligence_bundles_conversion_and_keywords(config, db):
    _listing_with_sales(config, db, listing_id=1, sku="1-mug", key="ceramic_mug",
                        views=100, units=3)
    intel = EtsyIntelligence(config, db)
    bundle = intel.intelligence()
    assert bundle["shop"]["conversion"] == pytest.approx(0.03)
    assert "products" in bundle and bundle["search_terms_source"] == "none"


def test_safe_with_no_listings(config, db):
    report = EtsyIntelligence(config, db).conversion_report()
    assert report["shop"]["conversion"] == 0.0
    assert report["products"] == []
