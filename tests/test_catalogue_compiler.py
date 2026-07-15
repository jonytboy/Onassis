"""Tests for the Catalogue Compiler (Sprint 46) — one-run bulk build-out."""

from __future__ import annotations

from typing import Any

from onassis.catalogue_compiler import CatalogueCompiler
from tests.test_daily_cycle import production_cycle  # noqa: F401  (shared fixture)


class FakeDaily:
    """A stand-in daily cycle: each build_unit records AI spend and returns
    products, so the compiler's loop policy (budget/count/targets) is tested
    without the real pipeline."""

    def __init__(self, db, *, cost_per_unit=1.0, products_per_unit=2,
                 category="Mugs", key="ceramic_mug", ideas=999):
        self.db = db
        self.cost = cost_per_unit
        self.products_per_unit = products_per_unit
        self.category = category
        self.key = key
        self.ideas = ideas
        self.calls = 0

    def build_unit(self, *, go_live=None, ignore_cap=False):
        self.calls += 1
        if self.calls > self.ideas:
            return {"status": "skipped", "reason": "no opportunity", "products": []}
        self.db.insert_ai_request({"provider": "openai", "model": "gpt-image-1",
                                   "kind": "image", "cost_usd": self.cost})
        prods = [{"product_key": self.key, "product_name": self.key,
                  "category": self.category} for _ in range(self.products_per_unit)]
        return {"status": "ok", "campaign_id": self.calls, "products": prods}


def test_compiler_stops_at_budget(config, db):
    daily = FakeDaily(db, cost_per_unit=2.0, products_per_unit=1)
    r = CatalogueCompiler(config, db, daily).compile(budget_usd=5.0)
    # $2/unit, cap $5 -> builds 3 units ($6 >= $5 stops before a 4th).
    assert r["stopped"] == "budget_reached"
    assert r["units"] == 3 and r["built"] == 3
    assert r["spend_usd"] == 6.0


def test_compiler_stops_at_max_products(config, db):
    daily = FakeDaily(db, cost_per_unit=0.1, products_per_unit=1)
    r = CatalogueCompiler(config, db, daily).compile(max_products=4)
    assert r["stopped"] == "max_products" and r["built"] == 4


def test_compiler_reports_products_by_category(config, db):
    daily = FakeDaily(db, cost_per_unit=1.0, products_per_unit=2, category="Aprons",
                      key="cotton_apron")
    r = CatalogueCompiler(config, db, daily).compile(max_products=4)
    assert r["by_category"].get("Aprons") == 4


def test_compiler_stops_when_targets_met(config, db):
    """When every (demand-weighted) target is already met, the compiler builds
    nothing — Build is done, it does not over-produce."""
    config.catalogue = {"targets": {"Mugs": 1}, "demand_weighting": False}
    db.insert_product({"sku": "1-ceramic_mug", "name": "Mug", "campaign_id": 1,
                       "product_key": "ceramic_mug"})   # Mugs already at target
    daily = FakeDaily(db)
    r = CatalogueCompiler(config, db, daily).compile(budget_usd=100)
    assert r["stopped"] == "targets_met" and r["built"] == 0 and daily.calls == 0


def test_compiler_stops_when_no_more_ideas(config, db):
    daily = FakeDaily(db, cost_per_unit=0.1, products_per_unit=1, ideas=2)
    r = CatalogueCompiler(config, db, daily).compile(budget_usd=100)
    assert r["stopped"] == "no_opportunities" and r["built"] == 2


def test_compiler_isolates_a_crashing_unit(config, db):
    class Boom(FakeDaily):
        def build_unit(self, *, go_live=None, ignore_cap=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient image error")
            return super().build_unit(go_live=go_live, ignore_cap=ignore_cap)
    daily = Boom(db, cost_per_unit=0.1, products_per_unit=1)
    r = CatalogueCompiler(config, db, daily).compile(max_products=2)
    # The crash is isolated; the run continues and still reaches its target.
    assert r["built"] == 2 and r["blocked"] >= 1


class StubOpps:
    """A backlog that refills a fixed number of times, then runs dry — models the
    opportunity engine so idea replenishment can be tested."""

    def __init__(self, per_gen=3, max_gens=2):
        self.backlog: list[str] = []
        self.per_gen = per_gen
        self.max_gens = max_gens
        self.gens = 0

    def top(self, limit=1):
        return self.backlog[:limit]

    def generate(self, count=None, focus=None):
        if self.gens >= self.max_gens:
            return {"generated": 0}
        self.gens += 1
        self.backlog += [f"opp{self.gens}_{i}" for i in range(self.per_gen)]
        return {"generated": self.per_gen}


class ReplenishDaily:
    """A daily cycle stub with an idea supply: build_unit consumes one idea."""

    def __init__(self, db, *, per_gen=3, max_gens=2):
        self.db = db
        self.opportunities = StubOpps(per_gen=per_gen, max_gens=max_gens)
        self.market = _MarketSpy()
        self.built = 0

    def build_unit(self, *, go_live=None, ignore_cap=False):
        if not self.opportunities.backlog:
            return {"status": "skipped", "reason": "no opportunity", "products": []}
        self.opportunities.backlog.pop(0)
        self.built += 1
        self.db.insert_ai_request({"provider": "openai", "model": "gpt-image-1",
                                   "kind": "image", "cost_usd": 0.1})
        return {"status": "ok", "campaign_id": self.built,
                "products": [{"product_key": "ceramic_mug", "product_name": "Mug",
                              "category": "Mugs"}]}


class _MarketSpy:
    def __init__(self):
        self.research_calls = 0

    def research(self):
        self.research_calls += 1
        return {}


def test_compiler_replenishes_ideas_until_supply_is_exhausted(config, db):
    """The compiler keeps building across multiple idea batches (replenishment),
    not just one — then stops cleanly when ideas genuinely run out."""
    daily = ReplenishDaily(db, per_gen=3, max_gens=2)
    r = CatalogueCompiler(config, db, daily).compile(budget_usd=1000)
    # 2 generations × 3 ideas = 6 products across replenishment rounds.
    assert r["built"] == 6 and r["stopped"] == "no_opportunities"
    # Fresh market research was triggered when a batch ran dry.
    assert daily.market.research_calls >= 1


# --- Integration with the real pipeline ------------------------------

def test_build_unit_builds_and_drafts_products(production_cycle, db):
    """DailyCycle.build_unit runs the real product tail once and drafts (not
    live) when go_live=False."""
    unit = production_cycle.build_unit(go_live=False, ignore_cap=True)
    assert unit["status"] == "ok"
    assert unit["products"] and all(p["product_key"] for p in unit["products"])
    # Drafts were created, nothing was taken live.
    assert not production_cycle.publisher._draft_client.activated


def test_compile_drives_the_real_pipeline(production_cycle, db):
    """A real (faked-LLM) compile builds at least one product and records spend,
    stopping cleanly when the single seeded opportunity is exhausted."""
    from onassis.catalogue_compiler import CatalogueCompiler
    r = CatalogueCompiler(production_cycle.config, db, production_cycle).compile(
        budget_usd=1000, mode="auto_draft")
    assert r["built"] >= 1
    assert r["stopped"] in ("no_opportunities", "targets_met", "budget_reached")
    assert r["products"][0]["product_key"]
