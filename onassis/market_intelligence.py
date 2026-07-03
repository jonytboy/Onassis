"""The Market Intelligence Engine — research before invention.

ONASSIS must stop inventing products in a vacuum. Before any opportunity is
created, this engine builds a **Market Intelligence Report**: it scores candidate
keywords/niches on real commercial signals and computes an **opportunity** band,
so the CEO chooses what to build from data — never at random.

For each keyword it captures the signals a seller actually cares about — Etsy
search demand, bestseller frequency, review counts + velocity, seasonal /
Pinterest / Google-Trends momentum, competition, saturation, competitor count,
keyword difficulty, average selling price and estimated monthly sales — then
derives, **deterministically**:

* ``demand``      (0-100) — how much money is moving through the keyword.
* ``competition`` (0-100) — how hard it is to win.
* ``opportunity`` — ``demand − competition`` banded VERY HIGH / HIGH / MEDIUM / LOW.

The raw signals are *estimated* by a replaceable ``SignalsProvider`` (the default
uses the LLM, exactly as the rest of ONASSIS estimates market reads; a real Etsy /
Google-Trends provider can be registered later with no engine change). The
scoring, banding and ranking are pure deterministic functions, so the report is
explainable, testable and auditable. Every report row is stored.

This is a manager module (like the Optimiser or the Opportunity Engine), not a
new AI agent — the agent roster is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.llm import LLMClient
from onassis.logger import get_logger

log = get_logger(__name__)

VERY_HIGH, HIGH, MEDIUM, LOW = "VERY HIGH", "HIGH", "MEDIUM", "LOW"

# How the raw signals roll up into demand vs competition (need not sum to 1).
_DEMAND_WEIGHTS = {
    "search_demand": 0.28, "bestseller_frequency": 0.16, "est_monthly_sales": 0.16,
    "seasonal_trend": 0.10, "pinterest_trend": 0.12, "google_trend": 0.10,
    "review_velocity": 0.08,
}
_COMPETITION_WEIGHTS = {
    "competition": 0.34, "saturation": 0.24, "keyword_difficulty": 0.22,
    "competitor_density": 0.12, "review_density": 0.08,
}

# Every 0-100 signal the report scores (raw counts/prices handled separately).
_SIGNAL_FIELDS = (
    "search_demand", "bestseller_frequency", "seasonal_trend", "pinterest_trend",
    "google_trend", "competition", "saturation", "keyword_difficulty",
    "review_velocity",
)

_SIGNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keyword": {"type": "string"},
        "product_type": {"type": "string"},
        "theme": {"type": "string"},
        **{f: {"type": "integer"} for f in _SIGNAL_FIELDS},
        "avg_selling_price": {"type": "number"},
        "est_monthly_sales": {"type": "integer"},
        "competitor_count": {"type": "integer"},
        "review_count": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": ["keyword", "product_type", "theme", *_SIGNAL_FIELDS,
                 "avg_selling_price", "est_monthly_sales", "competitor_count",
                 "review_count", "rationale"],
    "additionalProperties": False,
}

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"signals": {"type": "array", "items": _SIGNAL_SCHEMA}},
    "required": ["signals"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a ruthless Etsy market analyst for a premium Mediterranean lifestyle "
    "print-on-demand brand. For each candidate keyword/niche you estimate real "
    "market signals as HONEST integers 0-100 (higher = more of that thing), plus "
    "the average selling price (GBP), estimated monthly sales, competitor count "
    "and typical review count. Base estimates on how Etsy actually behaves: strong "
    "demand with low competition and saturation is the prize; high demand with high "
    "saturation is a trap. Be specific and realistic, not optimistic."
)


def _clamp(value: Any, default: int = 50) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _band(gap: int) -> str:
    """Opportunity band from the demand−competition gap (matches the report spec)."""
    if gap >= 40:
        return VERY_HIGH
    if gap >= 25:
        return HIGH
    if gap >= 10:
        return MEDIUM
    return LOW


def _density(count: int, ceiling: int) -> int:
    """Map a raw count to a 0-100 'crowdedness' score (log-ish, saturating)."""
    if count <= 0:
        return 0
    return _clamp(100.0 * min(1.0, count / float(ceiling or 1)))


class SignalsProvider:
    """Replaceable source of raw market signals for a batch of keywords."""

    name = "base"

    def estimate(self, keywords: list[str], brand: str) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


class LLMSignalsProvider(SignalsProvider):
    """Estimates market signals with the LLM (default). A real Etsy / Google
    Trends provider can replace this without touching the engine."""

    name = "llm"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    def estimate(self, keywords: list[str], brand: str) -> list[dict[str, Any]]:
        prompt = self._prompt(keywords, brand)
        gen = self.llm.generate_json(system=_SYSTEM, prompt=prompt, schema=_SCHEMA)
        return gen.get("signals", []) or []

    def _prompt(self, keywords: list[str], brand: str) -> str:
        kw = "\n".join(f"- {k}" for k in keywords) if keywords else (
            "- (propose strong, specific niches for this brand yourself)")
        n = len(keywords) or 8
        seed = ("\nCANDIDATE KEYWORDS to score:\n" + kw) if keywords else (
            f"\nPropose and score {n} specific, high-intent Etsy keywords/niches for "
            f"this brand (2-4 words each, e.g. 'greek island tote', 'slow living "
            f"kitchen print').")
        return f"""Build a market-intelligence read for the brand '{brand}'.
{seed}

For EACH keyword return honest estimates:
- search_demand, bestseller_frequency, seasonal_trend, pinterest_trend,
  google_trend, competition, saturation, keyword_difficulty, review_velocity
  — each an integer 0-100.
- avg_selling_price (GBP), est_monthly_sales, competitor_count, review_count
  — realistic numbers.
- product_type, theme, and a one-line `rationale`.
Score honestly: crowded, saturated keywords must show high competition even if
demand is high."""


class MarketIntelligence:
    """Builds, scores, stores and serves the Market Intelligence Report."""

    def __init__(self, config: Config, db: Database,
                 signals_provider: SignalsProvider | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "market", None) or {}
        self.default_count = int(self.cfg.get("default_count", 8))
        self.competitor_ceiling = int(self.cfg.get("competitor_ceiling", 1500))
        self.review_ceiling = int(self.cfg.get("review_ceiling", 5000))
        self.provider = signals_provider or self._default_provider()

    def _default_provider(self) -> "SignalsProvider":
        """Select the signals provider from config: 'real' (live market data) or
        'llm' (estimates). Built lazily — no network until research() runs."""
        choice = str(self.cfg.get("signals_provider", "llm")).lower()
        if choice == "real":
            from onassis.market_truth import RealSignalsProvider
            return RealSignalsProvider(self.config, self.db)
        return LLMSignalsProvider(self.config)

    # --- Research ---------------------------------------------------

    def research(self, keywords: list[str] | None = None,
                 count: int | None = None) -> dict[str, Any]:
        """Score candidate keywords and store the report, best opportunity first."""
        brand = (self.config.brand or {}).get("name", "ONASSIS")
        seeds = list(keywords or self.cfg.get("seed_keywords") or [])
        if not seeds:
            seeds = []  # let the provider propose niches
        raw = self.provider.estimate(seeds[: (count or self.default_count)] if seeds else [],
                                     brand)
        scored = [self._score(item) for item in raw if item.get("keyword")]
        scored.sort(key=lambda s: s["opportunity_score"], reverse=True)
        run_at = datetime.now(timezone.utc).isoformat()
        for row in scored:
            self.db.insert_market_signal({**row, "brand": brand, "run_at": run_at})
        provider = getattr(self.provider, "name", "llm")
        log.info("Market intelligence (%s): scored %d keyword(s); top: %s.",
                 provider, len(scored), scored[0]["keyword"] if scored else "—")
        return {"brand": brand, "count": len(scored), "keywords": scored,
                "provider": provider}

    def recalculate_opportunity_scores(self, keywords: list[str] | None = None,
                                       count: int | None = None) -> dict[str, Any]:
        """Re-run research through the configured provider and re-score/rank the
        opportunities — the entry point for refreshing scores from live data."""
        return self.research(keywords, count)

    def _score(self, item: dict[str, Any]) -> dict[str, Any]:
        signals = {f: _clamp(item.get(f)) for f in _SIGNAL_FIELDS}
        competitor_count = max(0, int(item.get("competitor_count", 0) or 0))
        review_count = max(0, int(item.get("review_count", 0) or 0))
        competitor_density = _density(competitor_count, self.competitor_ceiling)
        review_density = _density(review_count, self.review_ceiling)

        demand = self._blend(signals, _DEMAND_WEIGHTS, extra={
            "est_monthly_sales": _clamp(min(100, (item.get("est_monthly_sales", 0) or 0)))})
        competition = self._blend(signals, _COMPETITION_WEIGHTS, extra={
            "competitor_density": competitor_density, "review_density": review_density})
        gap = demand - competition
        return {
            "keyword": str(item.get("keyword", "")).strip(),
            "product_type": item.get("product_type", ""),
            "theme": item.get("theme", ""),
            **signals,
            "avg_selling_price": round(float(item.get("avg_selling_price", 0) or 0), 2),
            "est_monthly_sales": int(item.get("est_monthly_sales", 0) or 0),
            "competitor_count": competitor_count,
            "review_count": review_count,
            "demand": demand,
            "competition": competition,
            "opportunity_score": max(0, gap),
            "opportunity": _band(gap),
            "rationale": item.get("rationale", ""),
            "payload": item,
        }

    @staticmethod
    def _blend(signals: dict[str, int], weights: dict[str, float],
               extra: dict[str, int] | None = None) -> int:
        values = {**signals, **(extra or {})}
        total = sum(weights.values()) or 1.0
        return _clamp(sum(values.get(k, 0) * w for k, w in weights.items()) / total)

    # --- Reads ------------------------------------------------------

    def top(self, limit: int = 5, min_band: str | None = None) -> list[dict[str, Any]]:
        """The highest-opportunity keywords from the latest research."""
        rows = self.db.top_market_signals(limit=limit * 3)
        if min_band:
            order = [LOW, MEDIUM, HIGH, VERY_HIGH]
            floor = order.index(min_band)
            rows = [r for r in rows if r.get("opportunity") in order[floor:]]
        return rows[:limit]

    def latest(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.top_market_signals(limit=limit)

    def format_report(self, rows: list[dict[str, Any]] | None = None) -> str:
        rows = rows if rows is not None else self.latest()
        if not rows:
            return "No market intelligence yet. Run research first."
        out = ["MARKET INTELLIGENCE REPORT (best opportunity first)", ""]
        for r in rows:
            out.append(f'"{r["keyword"]}"')
            out.append(f"  Demand: {r['demand']}   Competition: {r['competition']}   "
                       f"Opportunity: {r['opportunity']}")
            out.append(f"  ~£{r['avg_selling_price']:.0f} · ~{r['est_monthly_sales']}/mo · "
                       f"{r['competitor_count']} competitors")
            out.append("")
        return "\n".join(out)
