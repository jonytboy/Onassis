"""The Chief Marketing Officer — a deterministic marketing manager (Sprint 42).

The CEO decides *what* to sell; the CMO decides *how* to sell it. Consistent with
the rest of ONASSIS (the CEO is a deterministic module, not an LLM agent), the
CMO composes marketing strategy, launch plans, a content calendar, budget
allocation and growth recommendations deterministically from real performance
data — using the existing replaceable copy providers for wording. No new AI
agent; fully testable offline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from onassis.commercial import CommercialIntelligence
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# The weekly cadence (Obj 4): a channel per day; Sunday is the CEO review.
WEEKDAY_CHANNEL = {0: "pinterest", 1: "facebook", 2: "instagram", 3: "tiktok",
                   4: "email", 5: "blog", 6: "review"}
# Channels the CMO schedules onto calendar days (Pinterest is handled by the
# Traffic Engine's own pin schedule, so it isn't re-scheduled here).
_SCHEDULED_CHANNELS = {"facebook", "instagram", "tiktok", "email", "blog"}
# A product's launch sequence, in order.
LAUNCH_SEQUENCE = ["blog", "pinterest", "instagram", "facebook", "tiktok", "email"]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class CMOManager:
    def __init__(self, config: Config, db: Database,
                 commercial: CommercialIntelligence | None = None) -> None:
        self.config = config
        self.db = db
        self.commercial = commercial or CommercialIntelligence(config, db)

    # --- Content calendar (Obj 4) -----------------------------------

    def content_calendar(self, start_date: str | None = None, days: int = 14) -> list[dict[str, Any]]:
        d0 = date.fromisoformat(start_date or _today())
        out = []
        for i in range(days):
            d = d0 + timedelta(days=i)
            wd = d.weekday()
            out.append({"date": d.isoformat(), "weekday": _WEEKDAYS[wd],
                        "channel": WEEKDAY_CHANNEL[wd]})
        return out

    def schedule_pending(self, start_date: str | None = None) -> dict[str, Any]:
        """Assign each undelivered asset a calendar date matching its channel's day
        — so marketing is *scheduled*, not just created. Spreads multiple assets
        of the same channel across successive matching days."""
        cal = self.content_calendar(start_date, days=28)
        dates_for: dict[str, list[str]] = {}
        for entry in cal:
            dates_for.setdefault(entry["channel"], []).append(entry["date"])
        counters: dict[str, int] = {}
        scheduled = 0
        for a in self.db.list_pending_marketing_assets(limit=1000):
            if a.get("scheduled_date"):
                continue
            ch = a["channel"]
            if ch not in _SCHEDULED_CHANNELS:
                continue
            dates = dates_for.get(ch) or []
            if not dates:
                continue
            idx = counters.get(ch, 0)
            self.db.schedule_marketing_asset(a["id"], dates[idx % len(dates)])
            counters[ch] = idx + 1
            scheduled += 1
        if scheduled:
            log.info("CMO scheduled %d marketing asset(s) across the calendar.", scheduled)
        return {"scheduled": scheduled}

    def calendar_view(self, start_date: str | None = None) -> list[dict[str, Any]]:
        """The calendar with the scheduled assets attached to each day."""
        cal = self.content_calendar(start_date, days=14)
        scheduled = self.db.list_scheduled_marketing(on_or_after=(start_date or _today()))
        by_date: dict[str, list] = {}
        for a in scheduled:
            by_date.setdefault(a["scheduled_date"], []).append(
                {"channel": a["channel"], "product_key": a.get("product_key")})
        for day in cal:
            day["assets"] = by_date.get(day["date"], [])
        return cal

    # --- Budget allocation (Obj 2) ----------------------------------

    def budget_allocation(self, total: float = 100.0) -> dict[str, float]:
        """Split a marketing budget across channels weighted by real performance
        (clicks + weighted sales), with a small floor for every channel."""
        perf = self.commercial.channel_performance()
        weights = {c["channel"]: (c["clicks"] + c["sales"] * 10 + 1.0) for c in perf}
        total_w = sum(weights.values()) or 1.0
        return {ch: round(total * w / total_w, 2) for ch, w in weights.items()}

    # --- Launch plan (Obj 2) ----------------------------------------

    def launch_plan(self, product_key: str) -> dict[str, Any]:
        return {"product_key": product_key,
                "sequence": [{"day": i + 1, "channel": ch}
                             for i, ch in enumerate(LAUNCH_SEQUENCE)]}

    # --- Strategy + recommendations (Obj 2) -------------------------

    def strategy(self) -> dict[str, Any]:
        ceo = self.commercial.ceo_commercial()
        return {
            "focus_channel": ceo.get("best_channel") or "pinterest",
            "recommendations": ceo.get("recommendations", []),
            "budget_split": self.budget_allocation(100.0),
            "calendar": self.content_calendar(days=7),
        }
