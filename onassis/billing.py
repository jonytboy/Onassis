"""Billing monitor — AI provider spend + credit remaining, on the dashboard.

The operator shouldn't have to log into OpenAI and Anthropic to see how much is
left. ONASSIS already records every AI request's cost per provider; this rolls
that up per provider (today / month / total), and — since neither OpenAI nor
Anthropic exposes a live balance via API — lets the operator enter their current
top-up, then shows **remaining = top-up − spend since**, a burn rate, and an
estimated days-left. Also surfaces each provider's health (a 'credit too low' /
'billing limit' / 'key rejected' alert straight from the last API error).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from onassis.ai_accounting import provider_alert
from onassis.logger import get_logger

log = get_logger(__name__)

# Providers we always show, even before any spend is recorded.
KNOWN_PROVIDERS = ["openai", "anthropic"]


class BillingMonitor:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def set_credit(self, provider: str, amount: float,
                   as_of: str | None = None) -> dict[str, Any]:
        """Record a top-up: 'I have $amount of credit as of today'. Remaining is
        then tracked as this minus spend from ``as_of`` onward."""
        provider = (provider or "").strip().lower()
        if not provider:
            raise ValueError("A provider is required.")
        as_of = as_of or self._today()
        self.db.set_setting(f"billing.{provider}.credit", float(amount))
        self.db.set_setting(f"billing.{provider}.since", as_of)
        return self.summary()

    def summary(self) -> dict[str, Any]:
        today = self._today()
        month_start = today[:8] + "01"
        week_start = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()

        total_by = self.db.ai_spend_by_provider()
        month_by = self.db.ai_spend_by_provider(since_date=month_start)
        week_by = self.db.ai_spend_by_provider(since_date=week_start)
        today_by = self.db.ai_spend_by_provider(since_date=today)

        providers = sorted(set(KNOWN_PROVIDERS) | set(total_by))
        rows: list[dict[str, Any]] = []
        for p in providers:
            credit = self.db.get_setting(f"billing.{p}.credit", None)
            since = self.db.get_setting(f"billing.{p}.since", None)
            remaining = per_day = days_left = None
            if credit is not None:
                spend_since = (self.db.ai_spend_by_provider(since_date=since).get(p, 0.0)
                               if since else total_by.get(p, 0.0))
                remaining = round(float(credit) - spend_since, 2)
                per_day = round(week_by.get(p, 0.0) / 7.0, 4)
                if per_day > 0 and remaining is not None:
                    days_left = max(0, int(remaining / per_day))
            rows.append({
                "provider": p,
                "spend_today": round(today_by.get(p, 0.0), 4),
                "spend_month": round(month_by.get(p, 0.0), 2),
                "spend_total": round(total_by.get(p, 0.0), 2),
                "credit": (round(float(credit), 2) if credit is not None else None),
                "credit_since": since,
                "remaining": remaining,
                "per_day": per_day,
                "days_left": days_left,
                "health": provider_alert(self.db, p),
            })
        return {
            "providers": rows,
            "spend_today": round(sum(today_by.values()), 4),
            "spend_month": round(sum(month_by.values()), 2),
            "spend_total": round(sum(total_by.values()), 2),
            "note": ("Neither OpenAI nor Anthropic exposes a live balance via API, "
                     "so 'remaining' is your entered top-up minus tracked spend. "
                     "Update it whenever you add credit."),
        }
