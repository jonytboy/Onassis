"""The Catalogue Compiler (Sprint 46) — build a catalogue in one event.

The daily cycle builds one product family per run. This turns that into a
**bulk build-out**: a single "Compile catalogue" event that keeps building
products across categories until the catalogue targets are met or a spend
budget is reached — so an operator can stand up a 100-product catalogue in one
go instead of waiting many daily cycles.

It adds no new pipeline: each product is built by the daily cycle's own
:meth:`DailyCycle.build_unit` (opportunity → design → artwork → campaign →
expand → publish drafts). The compiler only owns the **loop policy** — how many
units to build, when to stop, and the guardrails:

* **Budget cap** — the run stops the moment AI spend (LLM + image generation)
  reaches ``budget_usd``. This is a hard ceiling against surprise bills.
* **Target cap** — it stops once the (demand-weighted) catalogue targets are all
  met (Build → Optimise), so it never over-produces.
* **Auto-draft** — finished products land as private Etsy/Shopify drafts for
  review, never straight to public listings (unless ``mode='live'``).

Every unit is isolated: one failing/blocked build never stops the run.
"""

from __future__ import annotations

from typing import Any

from onassis.catalogue import CatalogueManager
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# A hard backstop on units per compile run — the budget/target caps are the real
# limits; this only guards against a runaway loop if neither is set.
_MAX_UNITS = 200


class CatalogueCompiler:
    def __init__(self, config: Config, db: Database, daily: Any) -> None:
        self.config = config
        self.db = db
        self.daily = daily

    def compile(self, *, budget_usd: float | None = None,
                max_products: int | None = None, mode: str = "auto_draft") -> dict[str, Any]:
        """Build products until a stop condition. ``mode``: 'auto_draft' (private
        Etsy/Shopify drafts, the default), 'live' (public listings), or 'build'
        (build packages only, no publish)."""
        from onassis.ai_accounting import set_recorder
        set_recorder(self.db, self.config)  # ensure AI spend is recorded for the cap

        publish = mode in ("auto_draft", "live")
        go_live = True if mode == "live" else (False if mode == "auto_draft" else None)

        start_spend = self.db.ai_spend_total()
        built: list[dict[str, Any]] = []
        by_category: dict[str, int] = {}
        units = 0
        blocked = 0
        stopped = "targets_met"

        while True:
            spend = self.db.ai_spend_total() - start_spend
            if budget_usd is not None and spend >= budget_usd:
                stopped = "budget_reached"
                break
            if max_products is not None and len(built) >= max_products:
                stopped = "max_products"
                break
            if units >= _MAX_UNITS:
                stopped = "unit_backstop"
                break
            if CatalogueManager(self.config, self.db).mode() == "optimise":
                stopped = "targets_met"
                break

            # Keep a fresh, diverse supply of ideas flowing so a big run doesn't
            # stall after one batch — research + generate more, biased to the gaps.
            if not self._ensure_ideas():
                stopped = "no_opportunities"
                break

            units += 1
            try:
                unit = self.daily.build_unit(
                    go_live=go_live, ignore_cap=True) if publish else \
                    self.daily.build_unit(go_live=None, ignore_cap=True)
            except Exception as exc:  # one unit crashing never stops the compile
                log.exception("[compile] unit %d crashed — continuing.", units)
                blocked += 1
                if _no_ideas(exc):
                    stopped = "no_opportunities"
                    break
                continue

            if unit.get("status") not in ("ok",) and not unit.get("products"):
                blocked += 1
                if unit.get("reason") in ("no opportunity", "no product opportunity available"):
                    stopped = "no_opportunities"
                    break
                continue

            for p in unit.get("products", []):
                built.append({**p, "campaign_id": unit.get("campaign_id")})
                cat = p.get("category") or "Other"
                by_category[cat] = by_category.get(cat, 0) + 1
            log.info("[compile] unit %d: %d product(s) built (%d total, $%.2f spent).",
                     units, len(unit.get("products", [])), len(built), spend)

        total_spend = round(self.db.ai_spend_total() - start_spend, 4)
        log.info("[compile] done: %d product(s) in %d unit(s), $%.2f, stopped=%s.",
                 len(built), units, total_spend, stopped)
        return {
            "built": len(built),
            "units": units,
            "blocked": blocked,
            "by_category": by_category,
            "spend_usd": total_spend,
            "budget_usd": budget_usd,
            "mode": mode,
            "stopped": stopped,
            "products": built,
            "catalogue": CatalogueManager(self.config, self.db).catalogue_dashboard(),
        }


    def _ensure_ideas(self, min_backlog: int = 1) -> bool:
        """Guarantee a supply of fresh product ideas so the compiler can keep
        building a *diverse* catalogue instead of stalling after one batch.

        When the backlog runs low it generates more (biased toward the under-
        represented categories); if the current market report is exhausted it
        researches fresh keywords first, then generates again. Returns False only
        when even fresh research yields no new ideas (genuinely nothing to build).

        A daily object without an opportunity engine (a stub) short-circuits to
        True — build_unit is then responsible for its own idea supply."""
        opps = getattr(self.daily, "opportunities", None)
        if opps is None:
            return True
        if len(opps.top(limit=min_backlog)) >= min_backlog:
            return True
        focus = ", ".join(
            CatalogueManager(self.config, self.db).priority_categories()[:4]) or None
        # First draw more from the current market report.
        try:
            opps.generate(focus=focus)
        except Exception:  # generation is best-effort — never crash the compile
            log.debug("idea generation failed", exc_info=True)
        if opps.top(limit=1):
            return True
        # Report exhausted — research fresh keywords, then generate again.
        market = getattr(self.daily, "market", None)
        if market is not None:
            try:
                market.research()
            except Exception:
                log.debug("market research failed", exc_info=True)
        try:
            opps.generate(focus=focus)
        except Exception:
            log.debug("idea generation (post-research) failed", exc_info=True)
        return bool(opps.top(limit=1))


def _no_ideas(exc: Exception) -> bool:
    return "opportunit" in str(exc).lower()
