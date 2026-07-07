"""Tests for Commercial Intelligence (Sprint 42 Phase 1)."""

from __future__ import annotations

import pytest

from onassis.commercial import CommercialIntelligence
from onassis.revenue import RevenueEngine


@pytest.fixture
def seeded(config, db):
    # A product with an Etsy listing, listing stats, traffic funnel and sales.
    db.insert_product({"sku": "1-mug", "name": "Ceramic Mug", "campaign_id": 1,
                       "product_key": "mug"})
    db.insert_publication({"platform": "etsy", "product_id": "1-mug", "campaign_id": 1,
                           "listing_id": "555", "mode": "draft", "status": "draft"})
    db.upsert_listing_stat({"listing_id": 555, "stat_date": "2026-07-01", "views": 100,
                            "favourites": 10, "orders": 4, "revenue": 88.0})
    db.insert_traffic_funnel({"funnel_date": "2026-07-01", "product_key": "mug",
                              "source": "pinterest", "impressions": 1000, "clicks": 50,
                              "visits": 40, "sales": 4})
    db.upsert_product_performance({"product_key": "mug", "units_sold": 4, "orders": 4,
                                   "gross_revenue": 88.0, "net_profit": 30.0})
    rev = RevenueEngine(config, db)
    for _ in range(4):
        rev.record_order({"product_id": "1-mug", "campaign_id": 1, "platform": "etsy",
                          "sale_price": 22.0, "quantity": 1, "production_cost": 7.5,
                          "occurred_at": "2026-07-01T10:00:00", "sale_date": "2026-07-01"})
    # AI + marketing spend attributed to the product.
    db.insert_ledger_entry({"kind": "cost", "category": "ai", "amount": 2.0,
                            "product_id": "1-mug", "campaign_id": 1})
    db.insert_ledger_entry({"kind": "cost", "category": "advertising", "amount": 5.0,
                            "product_id": "1-mug", "campaign_id": 1})
    return CommercialIntelligence(config, db)


# --- Product analytics (Obj 7) ---------------------------------------

def test_product_analytics_accumulates_intelligence(seeded):
    rows = seeded.product_analytics()
    mug = next(r for r in rows if r["sku"] == "1-mug")
    assert mug["views"] == 100 and mug["clicks"] == 50 and mug["favourites"] == 10
    assert mug["sales"] == 4 and mug["revenue"] == 88.0 and mug["profit"] == 30.0
    assert mug["ai_cost"] == 2.0 and mug["marketing_cost"] == 5.0
    assert mug["roi"] == round(30.0 / 7.0, 4)     # profit / (ai + marketing)
    assert mug["conversion"] == round(4 / 40, 4)
    assert mug["refunds"] == 0


# --- Channel performance (Obj 5) -------------------------------------

def test_channel_performance_per_platform(seeded):
    chans = {c["channel"]: c for c in seeded.channel_performance()}
    assert set(chans) >= {"pinterest", "instagram", "facebook", "tiktok", "email", "blog"}
    pin = chans["pinterest"]
    assert pin["impressions"] == 1000 and pin["clicks"] == 50
    assert pin["ctr"] == round(50 / 1000, 4) and pin["sales"] == 4


# --- Attribution (Obj 6) ---------------------------------------------

def test_attribution_by_source(seeded):
    a = seeded.attribution()
    assert a["attributed_sales"] == 4
    pin = next(s for s in a["sources"] if s["source"] == "pinterest")
    assert pin["sales"] == 4 and pin["share"] == 1.0
    assert pin["revenue"] > 0


# --- CEO commercial dashboard (Obj 12) -------------------------------

def test_ceo_commercial_is_business_focused(seeded):
    d = seeded.ceo_commercial()
    for k in ("revenue_today", "profit_today", "orders", "best_product",
              "customer_acquisition_cost", "average_order_value", "conversion_rate",
              "ai_spend", "marketing_spend", "net_margin", "recommendations"):
        assert k in d
    assert d["orders"] == 4
    assert d["revenue_today"] == pytest.approx(88.0)
    assert d["average_order_value"] == pytest.approx(22.0)
    assert d["best_product"] == "mug"
    assert d["marketing_spend"] == 5.0 and d["ai_spend"] == 2.0
    assert d["recommendations"]


def test_empty_system_is_safe(config, db):
    d = CommercialIntelligence(config, db).ceo_commercial()
    assert d["orders"] == 0 and d["revenue_today"] == 0
    assert d["recommendations"]                       # never empty
    assert CommercialIntelligence(config, db).product_analytics() == []
