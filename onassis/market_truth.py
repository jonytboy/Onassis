"""The Market Truth Engine — stop guessing demand.

The Market Intelligence scoring was always deterministic, but its raw signals
were *estimated by an LLM*. This module replaces that guess with **real data**:

* **Google Trends** — real interest-over-time momentum per keyword.
* **Pinterest Trends** — real trending-keyword momentum.
* **Etsy search data** — real impressions/clicks/orders per query, from the
  ``etsy_search_terms`` the Intelligence Engine imports.

``RealSignalsProvider`` assembles a market signal per keyword from these real
sources and fills only genuinely-unavailable fields with **conservative
defaults** (never an LLM hallucination), recording in each rationale which
signals were real. It is a drop-in replacement for ``LLMSignalsProvider`` — the
deterministic scoring/banding/ranking in :class:`MarketIntelligence` is
unchanged, so selecting this provider **recalculates opportunity scores from live
market data**.

Every external client is injectable and tolerant: a network failure degrades that
one signal to its default, never crashing research.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.market_intelligence import SignalsProvider

log = get_logger(__name__)

# Conservative mid-band defaults for signals with no real source yet.
_DEFAULTS = {
    "search_demand": 45, "bestseller_frequency": 40, "seasonal_trend": 50,
    "pinterest_trend": 50, "google_trend": 50, "competition": 55, "saturation": 55,
    "keyword_difficulty": 55, "review_velocity": 40,
}
_DEFAULT_PRICE = 26.0
_DEFAULT_MONTHLY_SALES = 20
_DEFAULT_COMPETITORS = 800
_DEFAULT_REVIEWS = 1500


class GoogleTrendsClient:
    """Best-effort Google Trends interest-over-time (real, tolerant, injectable).

    Uses the public explore -> widget token -> multiline flow. Any failure returns
    ``None`` so the provider falls back to a conservative default rather than a
    guess. No API key; Google Trends is public."""

    def __init__(self, *, base_url: str = "https://trends.google.com/trends/api",
                 geo: str = "", timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.geo = geo
        self.timeout = timeout

    def interest(self, keyword: str) -> int | None:
        import json

        import httpx

        try:
            explore = httpx.get(
                f"{self.base_url}/explore",
                params={"hl": "en-GB", "tz": "0",
                        "req": json.dumps({"comparisonItem": [
                            {"keyword": keyword, "geo": self.geo, "time": "today 12-m"}],
                            "category": 0, "property": ""})},
                timeout=self.timeout)
            token, req = self._widget(explore.text)
            if not token:
                return None
            data = httpx.get(f"{self.base_url}/widgetdata/multiline",
                             params={"hl": "en-GB", "tz": "0", "req": json.dumps(req),
                                     "token": token}, timeout=self.timeout)
            return self._average(data.text)
        except Exception as exc:  # any hiccup -> defer to the default
            log.info("Google Trends unavailable for '%s' (%s).", keyword, exc)
            return None

    @staticmethod
    def _widget(text: str) -> tuple[str | None, dict[str, Any]]:
        import json

        payload = json.loads(text.lstrip(")]}',\n") or "{}")
        for widget in payload.get("widgets", []):
            if widget.get("id") == "TIMESERIES":
                return widget.get("token"), widget.get("request", {})
        return None, {}

    @staticmethod
    def _average(text: str) -> int | None:
        import json

        payload = json.loads(text.lstrip(")]}',\n") or "{}")
        values = [pt.get("value", [0])[0]
                  for pt in payload.get("default", {}).get("timelineData", [])]
        if not values:
            return None
        return int(round(sum(values) / len(values)))


class PinterestTrendsClient:
    """Pinterest v5 trending-keywords momentum (real, tolerant, injectable)."""

    def __init__(self, access_token: str | None, *,
                 base_url: str = "https://api.pinterest.com/v5",
                 region: str = "GB", timeout: float = 15.0) -> None:
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.region = region
        self.timeout = timeout

    def top_terms(self) -> list[str]:
        import httpx

        if not self.access_token:
            return []
        try:
            resp = httpx.get(
                f"{self.base_url}/trends/keywords/{self.region}/top/growing",
                headers={"Authorization": f"Bearer {self.access_token}"},
                params={"limit": 50}, timeout=self.timeout)
            if resp.status_code >= 400:
                return []
            items = resp.json().get("trends", resp.json().get("items", [])) or []
            return [str(i.get("keyword", i)).lower() for i in items]
        except Exception as exc:
            log.info("Pinterest Trends unavailable (%s).", exc)
            return []


class EtsySearchSignals:
    """Real Etsy search demand for a keyword, from imported search-term data."""

    def __init__(self, db: Database, *, impression_ceiling: int = 5000) -> None:
        self.db = db
        self.impression_ceiling = impression_ceiling

    def demand(self, keyword: str) -> dict[str, Any] | None:
        kw = keyword.lower().strip()
        rows = [r for r in self.db.list_etsy_search_terms()
                if kw in (r.get("term") or "").lower()]
        if not rows:
            return None
        impressions = sum(int(r.get("impressions", 0) or 0) for r in rows)
        clicks = sum(int(r.get("clicks", 0) or 0) for r in rows)
        orders = sum(int(r.get("orders", 0) or 0) for r in rows)
        score = min(100, round(100 * impressions / self.impression_ceiling))
        return {"search_demand": score, "impressions": impressions,
                "clicks": clicks, "orders": orders}


class RealSignalsProvider(SignalsProvider):
    """Assembles market signals from REAL sources (Google/Pinterest Trends + Etsy
    search data), conservative defaults elsewhere — no LLM."""

    name = "real"

    def __init__(self, config: Config, db: Database, *,
                 google_client: Any | None = None,
                 pinterest_trends: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "market", None) or {}
        trends_cfg = self.cfg.get("trends") or {}
        self.geo = trends_cfg.get("google_geo", "GB")
        self._google = google_client
        self._pinterest = pinterest_trends
        self.etsy = EtsySearchSignals(
            db, impression_ceiling=int(trends_cfg.get("etsy_impression_ceiling", 5000)))

    # Lazy real clients (only built if actually used).
    @property
    def google(self) -> GoogleTrendsClient:
        if self._google is None:
            self._google = GoogleTrendsClient(geo=self.geo)
        return self._google

    @property
    def pinterest(self) -> PinterestTrendsClient:
        if self._pinterest is None:
            token = (self.config.pinterest or {}).get("access_token")
            self._pinterest = PinterestTrendsClient(
                token, region=(self.cfg.get("trends") or {}).get("pinterest_region", "GB"))
        return self._pinterest

    def estimate(self, keywords: list[str], brand: str) -> list[dict[str, Any]]:
        seeds = list(keywords) or self._seed_keywords()
        pinterest_terms = self._safe(lambda: set(self.pinterest.top_terms()), set())
        signals: list[dict[str, Any]] = []
        for kw in seeds:
            signals.append(self._signal(kw, pinterest_terms))
        return signals

    @staticmethod
    def _safe(fn, default):
        """Never let one broken data source crash research — degrade to default."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - resilience is the point
            log.info("Market signal source failed (%s); using default.", exc)
            return default

    def _signal(self, keyword: str, pinterest_terms: set[str]) -> dict[str, Any]:
        real: list[str] = []
        s = dict(_DEFAULTS)

        google = self._safe(lambda: self.google.interest(keyword), None)
        if google is not None:
            s["google_trend"] = int(max(0, min(100, google)))
            s["seasonal_trend"] = s["google_trend"]
            real.append("google_trend")

        if pinterest_terms:
            kw = keyword.lower()
            hit = any(kw in term or term in kw for term in pinterest_terms)
            s["pinterest_trend"] = 85 if hit else 35
            real.append("pinterest_trend")

        etsy = self._safe(lambda: self.etsy.demand(keyword), None)
        if etsy:
            s["search_demand"] = etsy["search_demand"]
            s["bestseller_frequency"] = min(100, 40 + etsy["orders"] * 5)
            s["review_velocity"] = min(100, 30 + etsy["clicks"])
            real.append("etsy_search_demand")

        # Where we have no real demand signal, ground it in the trend momentum
        # we DO have rather than a flat default.
        if "etsy_search_demand" not in real and ("google_trend" in real or "pinterest_trend" in real):
            s["search_demand"] = int(round((s["google_trend"] + s["pinterest_trend"]) / 2))

        return {
            "keyword": keyword,
            "product_type": self._product_type(keyword),
            "theme": self._theme(keyword),
            **{f: int(s[f]) for f in _DEFAULTS},
            "avg_selling_price": (etsy and _DEFAULT_PRICE) or _DEFAULT_PRICE,
            "est_monthly_sales": (etsy["orders"] * 4 if etsy and etsy["orders"]
                                  else _DEFAULT_MONTHLY_SALES),
            "competitor_count": _DEFAULT_COMPETITORS,
            "review_count": _DEFAULT_REVIEWS,
            "rationale": ("real signals: " + ", ".join(real)) if real
            else "no live signal available — conservative defaults",
            "real_signals": real,
        }

    # --- Seed derivation (deterministic, no LLM) --------------------

    def _seed_keywords(self) -> list[str]:
        seeds: list[str] = list(self.cfg.get("seed_keywords") or [])
        # From real Etsy search terms we've observed.
        for row in self.db.top_search_terms(20):
            term = row.get("term")
            if term and term not in seeds:
                seeds.append(term)
        # From the brand's own strategic pillars + catalogue product types.
        for pillar in (self.config.brand or {}).get("content_pillars", []):
            seeds.append(str(pillar))
        for item in (self.config.expansion or {}).get("catalogue", []):
            name = item.get("name")
            if name:
                seeds.append(name)
        # De-dupe, cap.
        out: list[str] = []
        for s in seeds:
            if s and s not in out:
                out.append(s)
        return out[: int(self.cfg.get("default_count", 8))]

    @staticmethod
    def _product_type(keyword: str) -> str:
        k = keyword.lower()
        for word in ("mug", "tote", "hoodie", "sweatshirt", "t-shirt", "tee", "poster",
                     "print", "canvas", "notebook", "card"):
            if word in k:
                return word
        return "print"

    @staticmethod
    def _theme(keyword: str) -> str:
        return keyword.strip().title()
