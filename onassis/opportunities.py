"""The Product Opportunity Engine — discover ideas before any design work.

ONASSIS should never start designing until it knows *what* is worth designing.
This engine generates commercially viable **product opportunities** and stores
them as the permanent product development **backlog**, ranked by expected
commercial value. It deliberately does **not** generate images, mock-ups, or
listings — it only discovers high-quality ideas.

For every opportunity it captures the commercial framing (brand, theme, target
customer, emotional angle, product type, search intent, seasonal relevance) and
a scorecard (commercial / originality / brand-fit scores, estimated demand,
estimated competition, confidence — all 0-100), plus concrete creative
direction (product name, one-sentence concept, and suggested colour palette,
typography, illustration, photography, and mock-up styles).

The creative + scoring fields are LLM-generated; the **expected commercial
value** used for ranking is computed **deterministically** from the component
scores, so the ordering of the backlog is explainable and testable. Duplicate
concepts are avoided two ways: the prompt is told which concepts already exist,
and a normalised fingerprint is enforced at insert time.

The Product Optimiser and the CEO consume this ranked queue (see
:meth:`OpportunityEngine.select_next`) — capital is committed to the highest
expected return, exactly as the CEO evaluates every other investment.

This is a manager module (like the Brain or the Optimiser), not a BaseAgent —
the agent roster is unchanged.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from onassis.ceo import CEOAgent
from onassis.config import Config
from onassis.database import Database
from onassis.llm import LLMClient
from onassis.logger import get_logger
from onassis.market_intelligence import MarketIntelligence
from onassis.proposals import Proposal

log = get_logger(__name__)

_SCORE_FIELDS = (
    "commercial_score", "originality_score", "brand_fit_score",
    "estimated_demand", "estimated_competition", "confidence",
)

_OPPORTUNITY_PROPS: dict[str, Any] = {
    "theme": {"type": "string"},
    "target_customer": {"type": "string"},
    "emotional_angle": {"type": "string"},
    "product_type": {"type": "string"},
    "search_intent": {"type": "string"},
    "seasonal_relevance": {"type": "string"},
    "commercial_score": {"type": "integer"},
    "originality_score": {"type": "integer"},
    "brand_fit_score": {"type": "integer"},
    "estimated_demand": {"type": "integer"},
    "estimated_competition": {"type": "integer"},
    "confidence": {"type": "integer"},
    "product_name": {"type": "string"},
    "concept": {"type": "string"},
    "colour_palette": {"type": "array", "items": {"type": "string"}},
    "typography_style": {"type": "string"},
    "illustration_style": {"type": "string"},
    "photography_style": {"type": "string"},
    "mockup_style": {"type": "string"},
}

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _OPPORTUNITY_PROPS,
                "required": list(_OPPORTUNITY_PROPS.keys()),
                "additionalProperties": False,
            },
        }
    },
    "required": ["opportunities"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the Product Opportunity Engine for a premium Mediterranean "
    "lifestyle brand. You discover commercially viable product opportunities "
    "BEFORE any design work begins. You think like a sharp merchandiser: real "
    "search demand, a clear target customer, a genuine emotional hook, and an "
    "honest read on competition. Every idea must be original (no trademarks, no "
    "copyrighted characters, no derivative knock-offs) and on-brand. Score "
    "honestly on a 0-100 scale — not everything is a 90. You only propose "
    "ideas and creative direction; you never produce images, mock-ups, or "
    "listings."
)

_DEFAULT_WEIGHTS = {
    "commercial": 0.35, "originality": 0.15, "brand_fit": 0.20,
    "demand": 0.20, "competition": 0.10,
}


def _clamp(value: Any, lo: int = 0, hi: int = 100, default: int = 50) -> int:
    try:
        return max(lo, min(hi, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for fingerprints."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(text).lower())).strip()


def dedupe_key(product_type: str, theme: str, emotional_angle: str) -> str:
    """A stable fingerprint of a concept, used to reject duplicates."""
    return "|".join(_normalise(p) for p in (product_type, theme, emotional_angle))


class OpportunityEngine:
    """Generates, ranks, stores, and serves product opportunities."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.cfg = config.opportunity or {}
        self.weights = {**_DEFAULT_WEIGHTS, **(self.cfg.get("weights") or {})}
        self.default_count = int(self.cfg.get("default_count", 8))
        self.dev_cost = float(self.cfg.get("development_cost", 12.0))
        self.revenue_potential = float(self.cfg.get("revenue_potential", 250.0))
        self.ceo = CEOAgent(config, db)
        # Products are drawn from the Market Intelligence report — never invented
        # in a vacuum. The CEO then chooses from the highest-opportunity keywords.
        self.market = MarketIntelligence(config, db)
        self.min_opportunity_band = str(
            (config.market or {}).get("min_opportunity_band", "HIGH"))
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    # --- Scoring (deterministic) ------------------------------------

    def expected_value(self, opp: dict[str, Any]) -> float:
        """Expected commercial value (0-100), scaled by confidence.

        Rewards commercial potential, originality, brand fit, and demand;
        penalises competition; then scales by how confident the read is.
        """
        w = self.weights
        base = (
            w["commercial"] * opp["commercial_score"]
            + w["originality"] * opp["originality_score"]
            + w["brand_fit"] * opp["brand_fit_score"]
            + w["demand"] * opp["estimated_demand"]
            + w["competition"] * (100 - opp["estimated_competition"])
        )
        weight_sum = sum(w.values()) or 1.0
        normalised = base / weight_sum
        return round(normalised * (opp["confidence"] / 100.0), 2)

    # --- Generation -------------------------------------------------

    def generate(
        self,
        count: int | None = None,
        *,
        brand: str | None = None,
        season: str | None = None,
        focus: str | None = None,
    ) -> dict[str, Any]:
        """Generate, dedupe, score, rank, and persist new opportunities.

        Returns ``{requested, generated, duplicates_skipped, opportunities}``
        where ``opportunities`` are the newly stored ideas, best first.
        """
        count = int(count or self.default_count)
        brand = brand or (self.config.brand or {}).get("name", "ONASSIS")

        # Opportunities are drawn FROM the Market Intelligence report — the daily
        # cycle's Market Research stage (or --generate-opportunities) builds it
        # first, so the CEO never chooses from randomly invented niches.
        market = (self.market.top(count, min_band=self.min_opportunity_band)
                  or self.market.top(count))

        gen = self.llm.generate_json(
            system=_SYSTEM,
            prompt=self._prompt(count, brand, season, focus, market),
            schema=_SCHEMA,
        )
        raw = gen.get("opportunities", []) or []

        existing_keys = self.db.opportunity_dedupe_keys()
        seen: set[str] = set()
        stored: list[dict[str, Any]] = []
        duplicates = 0

        for item in raw:
            key = dedupe_key(
                item.get("product_type", ""), item.get("theme", ""),
                item.get("emotional_angle", ""),
            )
            if not key.strip("|") or key in existing_keys or key in seen:
                duplicates += 1
                continue
            seen.add(key)
            opp = self._build(item, brand, key)
            opp["id"] = self.db.insert_opportunity(opp)
            stored.append(opp)

        stored.sort(key=lambda o: o["expected_value"], reverse=True)
        log.info(
            "Generated %d opportunity(ies) (%d duplicate(s) skipped) for brand %s.",
            len(stored), duplicates, brand,
        )
        return {
            "requested": count,
            "generated": len(stored),
            "duplicates_skipped": duplicates,
            "opportunities": stored,
        }

    def _build(self, item: dict[str, Any], brand: str, key: str) -> dict[str, Any]:
        scores = {f: _clamp(item.get(f)) for f in _SCORE_FIELDS}
        opp = {
            "opportunity_id": f"OPP-{uuid.uuid4().hex[:8]}",
            "brand": brand,
            "theme": item.get("theme", ""),
            "target_customer": item.get("target_customer", ""),
            "emotional_angle": item.get("emotional_angle", ""),
            "product_type": item.get("product_type", ""),
            "search_intent": item.get("search_intent", ""),
            "seasonal_relevance": item.get("seasonal_relevance", ""),
            **scores,
            "product_name": item.get("product_name", ""),
            "concept": item.get("concept", ""),
            "colour_palette": [str(c).strip() for c in item.get("colour_palette", []) if str(c).strip()],
            "typography_style": item.get("typography_style", ""),
            "illustration_style": item.get("illustration_style", ""),
            "photography_style": item.get("photography_style", ""),
            "mockup_style": item.get("mockup_style", ""),
            "dedupe_key": key,
            "status": "backlog",
            "payload": item,
        }
        opp["expected_value"] = self.expected_value(opp)
        return opp

    # --- Reads (the ranked backlog) ---------------------------------

    def list_opportunities(self, status: str | None = None) -> list[dict[str, Any]]:
        """The whole backlog (or one status), ranked by expected value."""
        return self.db.list_opportunities(status=status)

    def top(self, limit: int = 5) -> list[dict[str, Any]]:
        """The highest expected-value opportunities still in the backlog."""
        return self.db.list_opportunities(status="backlog", limit=limit)

    def overview(self) -> dict[str, Any]:
        return {
            "total": self.db.count_opportunities(),
            "backlog": self.db.count_opportunities(status="backlog"),
            "selected": self.db.count_opportunities(status="selected"),
        }

    # --- Selection: the Optimiser + CEO choose from the queue -------

    def proposal_for(self, opp: dict[str, Any], agent_name: str = "OpportunityEngine") -> Proposal:
        """Frame an opportunity as an investment proposal for the CEO."""
        expected_revenue = round((opp["expected_value"] / 100.0) * self.revenue_potential, 2)
        competition, confidence = opp["estimated_competition"], opp["confidence"]
        if competition >= 70 or confidence < 40:
            risk = "high"
        elif competition <= 30 and confidence >= 70:
            risk = "low"
        else:
            risk = "medium"
        reasoning = (
            f"Opportunity {opp['opportunity_id']} '{opp['product_name']}' "
            f"({opp['product_type']}, {opp['theme']}): commercial {opp['commercial_score']}, "
            f"originality {opp['originality_score']}, brand-fit {opp['brand_fit_score']}, "
            f"demand {opp['estimated_demand']}, competition {opp['estimated_competition']}, "
            f"confidence {confidence}. Expected commercial value {opp['expected_value']:.1f}/100."
        )
        return Proposal(
            agent_name=agent_name,
            requested_action=f"Develop product opportunity {opp['opportunity_id']} "
                             f"('{opp['product_name']}')",
            estimated_cost=self.dev_cost,
            expected_revenue=expected_revenue,
            confidence=confidence,
            risk_level=risk,
            reasoning=reasoning,
        )

    def select(
        self, opportunity_id: str, *, agent_name: str = "CEO"
    ) -> dict[str, Any] | None:
        """Have the CEO decide on a specific opportunity from the queue.

        On APPROVE the opportunity is marked ``selected``; otherwise it stays
        in the backlog. Returns ``{opportunity, proposal, ceo}`` or ``None`` if
        the opportunity does not exist.
        """
        opp = self.db.get_opportunity(opportunity_id)
        if opp is None:
            return None
        proposal = self.proposal_for(opp, agent_name=agent_name)
        decision = self.ceo.evaluate(proposal, store=False)

        from onassis.proposals import APPROVE

        if decision["verdict"] == APPROVE and opp["status"] != "selected":
            self.db.update_opportunity(
                opportunity_id,
                {"status": "selected", "selected_by": agent_name,
                 "selected_at": datetime.now(timezone.utc).isoformat()},
            )
            opp = self.db.get_opportunity(opportunity_id) or opp
        return {"opportunity": opp, "proposal": proposal.to_dict(), "ceo": decision}

    def select_next(
        self, *, agent_name: str = "ProductOptimiser", store: bool = False
    ) -> dict[str, Any] | None:
        """Pick the top backlog opportunity and have the CEO decide on it.

        This is how the Product Optimiser and CEO choose what to develop from
        the ranked queue. On APPROVE the opportunity is marked ``selected``;
        otherwise it stays in the backlog for reconsideration. Returns
        ``{opportunity, proposal, ceo}`` or ``None`` if the backlog is empty.
        """
        backlog = self.top(limit=1)
        if not backlog:
            return None
        return self.select(backlog[0]["opportunity_id"], agent_name=agent_name)

    # --- Prompt -----------------------------------------------------

    def _prompt(self, count: int, brand: str, season: str | None, focus: str | None,
                market: list[dict[str, Any]] | None = None) -> str:
        brand_cfg = self.config.brand or {}
        existing = self.db.list_opportunities()
        # Tell the model what already exists so it doesn't repeat concepts.
        avoid = "; ".join(
            f"{o['product_type']} / {o['theme']} / {o['emotional_angle']}"
            for o in existing[:40]
        ) or "none yet"
        market_block = ""
        if market:
            lines = "\n".join(
                f'- "{m["keyword"]}" (demand {m["demand"]}, competition {m["competition"]}, '
                f'{m["opportunity"]}, ~£{m.get("avg_selling_price", 0):.0f})'
                for m in market
            )
            market_block = (
                "\nMARKET INTELLIGENCE — build opportunities FROM these researched "
                "high-opportunity keywords (do NOT invent unrelated niches). Base each "
                "opportunity on one of these keywords and set its `search_intent` to that "
                f"keyword:\n{lines}\n"
            )
        return f"""Generate {count} distinct, commercially viable product opportunities.

BRAND
- Name: {brand}
- Identity: {brand_cfg.get('identity', 'premium Mediterranean lifestyle')}
- Pillars: {", ".join(brand_cfg.get('content_pillars', [])) or 'coastal living, slow luxury, design'}
{f"- Season to weight toward: {season}" if season else ""}
{f"- Focus area: {focus}" if focus else ""}
{market_block}
ALREADY IN THE BACKLOG — do NOT repeat these concepts:
{avoid}

For EACH opportunity provide:
- theme, target_customer, emotional_angle, product_type, search_intent,
  seasonal_relevance.
- commercial_score, originality_score, brand_fit_score, estimated_demand,
  estimated_competition, confidence — each an HONEST integer 0-100.
- product_name: a distinctive, on-brand name.
- concept: ONE sentence describing the product idea.
- colour_palette: 3-5 colours.
- typography_style, illustration_style, photography_style, mockup_style:
  short creative direction for each (NOT the assets themselves).

Make each idea genuinely different from the others and from the backlog.
Original and on-brand only — no trademarks, no copyrighted characters."""
