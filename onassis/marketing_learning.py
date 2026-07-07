"""The Marketing Learning Loop (Sprint 42 Phase 5, Obj 11).

Replaces "generate → forget" with "generate → launch → measure → compare → learn
→ improve → launch again". Each run measures every channel's effectiveness,
records a snapshot, compares it to the previous snapshot (improving / declining),
and turns that into learnings + a recommended budget shift the CMO acts on.
Deterministic; reuses the Commercial Intelligence measurements.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from onassis.commercial import CommercialIntelligence
from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class MarketingLearning:
    def __init__(self, config: Config, db: Database,
                 commercial: CommercialIntelligence | None = None) -> None:
        self.config = config
        self.db = db
        self.commercial = commercial or CommercialIntelligence(config, db)

    def effectiveness(self) -> list[dict[str, Any]]:
        """Score each channel: sales weigh most, then clicks, then CTR."""
        rows = []
        for c in self.commercial.channel_performance():
            eff = c["sales"] * 10 + c["clicks"] + (c["ctr"] or 0) * 100
            rows.append({"channel": c["channel"], "clicks": c["clicks"],
                         "sales": c["sales"], "effectiveness": round(eff, 2)})
        rows.sort(key=lambda r: r["effectiveness"], reverse=True)
        return rows

    def record(self, snapshot_date: str | None = None) -> dict[str, Any]:
        """Persist an effectiveness snapshot — the memory the loop learns from."""
        snapshot_date = snapshot_date or _today()
        eff = self.effectiveness()
        self.db.insert_marketing_learnings(snapshot_date, eff)
        return {"snapshot_date": snapshot_date, "channels": len(eff)}

    def digest(self, snapshot_date: str | None = None) -> dict[str, Any]:
        """Rank channels, compare to the last snapshot, and produce learnings +
        a recommended budget split weighted toward what works."""
        snapshot_date = snapshot_date or _today()
        eff = self.effectiveness()
        prev = self.db.previous_marketing_effectiveness(snapshot_date)
        for r in eff:
            p = prev.get(r["channel"])
            r["previous"] = p
            if p is None:
                r["trend"] = "new"
            elif r["effectiveness"] > p:
                r["trend"] = "improving"
            elif r["effectiveness"] < p:
                r["trend"] = "declining"
            else:
                r["trend"] = "flat"

        active = [r for r in eff if r["effectiveness"] > 0]
        best = active[0] if active else None
        learnings: list[str] = []
        if best:
            learnings.append(f"{best['channel']} is your most effective channel — "
                             "shift budget toward it.")
        improving = [r["channel"] for r in eff if r["trend"] == "improving"]
        declining = [r["channel"] for r in eff if r["trend"] == "declining"]
        if improving:
            learnings.append(f"Improving: {', '.join(improving)} — keep investing.")
        if declining:
            learnings.append(f"Declining: {', '.join(declining)} — review the creative.")
        if not active:
            learnings.append("No measurable channel results yet — keep publishing "
                             "and driving traffic.")

        # Budget weighted by effectiveness (with a floor so nothing is starved).
        weights = {r["channel"]: r["effectiveness"] + 1.0 for r in eff}
        total_w = sum(weights.values()) or 1.0
        recommended = {ch: round(100.0 * w / total_w, 1) for ch, w in weights.items()}
        return {"snapshot_date": snapshot_date, "channels": eff,
                "best_channel": best["channel"] if best else None,
                "learnings": learnings, "recommended_budget": recommended}
