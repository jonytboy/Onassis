"""Tests for Adaptive Pricing — price discovery from real sales."""

from __future__ import annotations

from onassis.adaptive_pricing import AdaptivePricer


class FakeShop:
    can_publish = True
    def __init__(self, price=30.0):
        self.price = price
        self.set = []
    def product_price(self, pid):
        return self.price
    def set_product_price(self, pid, price):
        self.set.append((pid, price))
        self.price = price
        return {"ok": True}


def _cfg(config):
    config.pricing = {"strategy": "adaptive", "adaptive_start_profit": 3.0,
                      "adaptive_min_profit": 0.5, "adaptive_max_profit": 8.0,
                      "adaptive_step": 0.5, "adaptive_window_days": 7,
                      "shipping_cost": 5.0}
    config.fees = {}
    return config


def _product(db, cid=1, key="tee", cost=12.0, listing="500"):
    db.insert_product({"sku": key.upper(), "name": "Tee", "campaign_id": cid,
                       "product_key": key, "active": True, "production_cost": cost})
    db.insert_publication({"campaign_id": cid, "product_id": f"{cid}-{key}",
                           "platform": "shopify", "listing_id": listing,
                           "mode": "live", "status": "active"})


def _pricer(config, db, shop):
    return AdaptivePricer(_cfg(config), db, shopify=shop)


def test_seeds_new_product_at_start_margin(config, db):
    _product(db)
    shop = FakeShop()
    r = _pricer(config, db, shop).reprice(apply=True, today="2026-08-10")
    row = r["products"][0]
    assert row["direction"] == "seed" and row["new_target"] == 3.0
    assert shop.set and db.get_price_state(1, "tee")["target_profit"] == 3.0


def test_no_sales_walks_price_down(config, db):
    _product(db)
    shop = FakeShop()
    p = _pricer(config, db, shop)
    p.reprice(apply=True, today="2026-08-01")          # seed at 3.0
    r = p.reprice(apply=True, today="2026-08-10")       # a window later, no sales
    row = r["products"][0]
    assert row["direction"] == "down" and row["new_target"] == 2.5


def test_a_sale_nudges_price_up(config, db):
    _product(db)
    shop = FakeShop()
    p = _pricer(config, db, shop)
    p.reprice(apply=True, today="2026-08-01")          # seed at 3.0
    db.insert_order({"occurred_at": "2026-08-08T10:00:00Z", "sale_date": "2026-08-08",
                     "product_id": "1-tee", "campaign_id": 1, "quantity": 1,
                     "platform": "shopify", "sale_price": 22.0,
                     "gross_revenue": 22.0, "total_cost": 20.0, "gross_profit": 2.0,
                     "net_profit": 1.0, "profit_margin": 0.05, "roi": 0.05})
    r = p.reprice(apply=True, today="2026-08-10")       # window has a sale
    row = r["products"][0]
    assert row["direction"] == "up" and row["new_target"] == 3.5


def test_holds_within_the_window_and_skips_shopify(config, db):
    _product(db)
    shop = FakeShop()
    p = _pricer(config, db, shop)
    p.reprice(apply=True, today="2026-08-01")          # seed
    n_before = len(shop.set)
    r = p.reprice(apply=True, today="2026-08-03")       # only 2 days later → hold
    assert r["products"][0]["direction"] == "hold"
    assert len(shop.set) == n_before                    # no Shopify write


def test_floor_is_never_breached(config, db):
    _product(db)
    shop = FakeShop()
    config.pricing["adaptive_start_profit"] = 0.5       # already at the floor
    p = AdaptivePricer(config, db, shopify=shop)
    p.reprice(apply=True, today="2026-08-01")
    r = p.reprice(apply=True, today="2026-08-10")       # no sales, would go below floor
    assert r["products"][0]["new_target"] == 0.5        # clamped at the floor
