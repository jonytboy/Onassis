"""The Operations Manager — single point of operational control.

It makes **no commercial decisions**. It verifies that every module is healthy
*before* a Daily Cycle, aborts the cycle if a critical dependency is down
(recording the reason and notifying the CEO), and produces an Operations Report
*after* a cycle.

Pre-flight checks: Etsy, Pinterest, Revenue Engine, Analytics Engine, database
integrity, exports folder, AI provider, AI budget, cash reserve. Which checks
are *critical* (abort on failure) depends on the mode: a ``dry_run`` only needs
the read/decision modules, so AI/budget/cash/exports are advisory there.

The Operations Manager wraps the :class:`~onassis.daily_cycle.DailyCycle`; it
coordinates and reports, it never decides.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.daily_cycle import DailyCycle
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# Which checks abort the cycle if they fail, per mode.
_CRITICAL = {
    "production": {"revenue", "analytics", "database", "exports",
                   "ai_provider", "ai_budget", "cash_reserve"},
    "dry_run": {"revenue", "analytics", "database"},
}


class OperationsManager:
    """Health gate + reporting around the Daily Cycle. Decides nothing."""

    def __init__(self, config: Config, db: Database, cycle: DailyCycle | None = None) -> None:
        self.config = config
        self.db = db
        self.cycle = cycle or DailyCycle(config, db)
        # Reuse the cycle's already-built modules for the health checks.
        self.etsy = self.cycle.etsy
        self.pinterest = self.cycle.pinterest
        self.revenue = self.cycle.revenue
        self.analytics = self.cycle.analytics
        self.profit = self.cycle.profit
        self.policy = config.policy or {}

    # --- Guarded entry point ----------------------------------------

    def run(self, mode: str = "production") -> dict[str, Any]:
        """Pre-flight, then run the cycle (or abort), then report."""
        pre = self.check(mode)
        if not pre["healthy"]:
            report = self._aborted_report(mode, pre)
            self.db.insert_operations_report(report)
            self._notify_ceo(report)
            return {"mode": mode, "status": "aborted", "aborted": True,
                    "stages": [], "preflight": pre, "operations_report": report}

        cycle = self.cycle.run(mode)
        report = self._report(mode, pre, cycle)
        self.db.insert_operations_report(report)
        return {**cycle, "aborted": False, "preflight": pre, "operations_report": report}

    # --- Pre-flight checks ------------------------------------------

    def check(self, mode: str = "production") -> dict[str, Any]:
        """Run all dependency checks and return health (no cycle, no decisions)."""
        critical = _CRITICAL.get(mode, _CRITICAL["production"])
        checks = [
            self._check("etsy", "Etsy connection",
                        "ok" if self.etsy.is_configured else "warn",
                        "connected" if self.etsy.is_configured else "not configured", critical),
            self._check("pinterest", "Pinterest connection",
                        "ok" if self.pinterest.is_configured else "warn",
                        "connected" if self.pinterest.is_configured else "not configured", critical),
            self._guard("revenue", "Revenue Engine", self.revenue.company_profit, critical),
            self._guard("analytics", "Analytics Engine", self.analytics.overall, critical),
            self._check("database", "Database integrity",
                        "ok" if self.db.integrity_ok() else "fail",
                        "integrity ok" if self.db.integrity_ok() else "integrity check failed",
                        critical),
            self._check_exports(critical),
            self._check("ai_provider", "AI provider",
                        "ok" if self.config.anthropic_api_key else "fail",
                        "available" if self.config.anthropic_api_key else "no API key",
                        critical),
            self._check_ai_budget(critical),
            self._check_cash_reserve(critical),
        ]
        healthy = not any(c["status"] == "fail" and c["critical"] for c in checks)
        return {"mode": mode, "healthy": healthy, "checks": checks}

    def _check(self, key, label, status, detail, critical) -> dict[str, Any]:
        return {"name": key, "label": label, "status": status,
                "critical": key in critical, "detail": detail}

    def _guard(self, key, label, fn, critical) -> dict[str, Any]:
        try:
            fn()
            return self._check(key, label, "ok", "responding", critical)
        except Exception as exc:
            return self._check(key, label, "fail", str(exc), critical)

    def _check_exports(self, critical) -> dict[str, Any]:
        from pathlib import Path

        from onassis.config import ROOT_DIR

        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        base = base if base.is_absolute() else (ROOT_DIR / base)
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".ops_write_test"
            probe.write_text("ok")
            probe.unlink()
            return self._check("exports", "Exports folder", "ok", f"writable ({base})", critical)
        except Exception as exc:
            return self._check("exports", "Exports folder", "fail", str(exc), critical)

    def _check_ai_budget(self, critical) -> dict[str, Any]:
        remaining = self.profit.remaining_ai_budget()
        status = "ok" if remaining > 0 else "fail"
        return self._check("ai_budget", "AI budget", status,
                           f"{remaining:.2f} remaining today", critical)

    def _check_cash_reserve(self, critical) -> dict[str, Any]:
        cash = self.profit.cash_balance()
        reserve = float(self.policy.get("cash_reserve", 0) or 0)
        status = "ok" if cash >= reserve else "fail"
        return self._check("cash_reserve", "Cash reserve", status,
                           f"cash {cash:.2f} vs. reserve {reserve:.2f}", critical)

    # --- Reports ----------------------------------------------------

    def _report(self, mode, pre, cycle) -> dict[str, Any]:
        by_stage = {s["stage"]: s for s in cycle["stages"]}
        errors = sum(1 for s in cycle["stages"] if s["status"] == "failed")
        warnings = sum(1 for c in pre["checks"] if c["status"] == "warn")
        overall = "degraded" if (errors or warnings) else "healthy"

        rev = self.revenue.revenue_today()
        business = {
            "campaigns_generated": 1 if by_stage.get("Create Product Campaign", {}).get("status") == "ok" else 0,
            "listings_built": 1 if by_stage.get("Build Etsy Listing Package", {}).get("status") == "ok" else 0,
            "listings_published": 1 if by_stage.get("Publish Draft", {}).get("status") == "ok" else 0,
            "revenue_imported": rev["gross_revenue"],
            "orders_imported": rev["orders"],
            "profit_today": rev["net_profit"],
            "ai_spend_today": round(self.profit.ai_spend_today(), 2),
        }
        return {
            "mode": mode,
            "status": cycle["status"],
            "ceo_notified": False,
            "system": {"overall_health": overall, "runtime_seconds": cycle["duration_seconds"],
                       "errors": errors, "warnings": warnings},
            "business": business,
            "recommendations": self._recommendations(pre, cycle),
            "preflight": pre,
        }

    def _aborted_report(self, mode, pre) -> dict[str, Any]:
        failed = [c for c in pre["checks"] if c["status"] == "fail" and c["critical"]]
        warnings = sum(1 for c in pre["checks"] if c["status"] == "warn")
        recs = [f"{c['label']} failed: {c['detail']}." for c in failed]
        recs.append("Daily Cycle aborted before execution. CEO notified.")
        return {
            "mode": mode,
            "status": "aborted",
            "ceo_notified": True,
            "system": {"overall_health": "critical", "runtime_seconds": 0.0,
                       "errors": len(failed), "warnings": warnings},
            "business": {"campaigns_generated": 0, "listings_built": 0,
                         "listings_published": 0, "revenue_imported": 0,
                         "orders_imported": 0, "profit_today": 0, "ai_spend_today": 0},
            "recommendations": recs,
            "preflight": pre,
        }

    def _recommendations(self, pre, cycle) -> list[str]:
        recs: list[str] = []
        for c in pre["checks"]:
            if c["status"] == "fail":
                recs.append(f"{c['label']} failed: {c['detail']}.")
            elif c["status"] == "warn":
                recs.append(f"{c['label']}: {c['detail']}.")
        for s in cycle["stages"]:
            if s["status"] == "failed":
                recs.append(f"Stage '{s['stage']}' failed: {s.get('error')}.")
        # A clear positive signal when the read path worked.
        revenue_stage = next((s for s in cycle["stages"] if s["stage"] == "Import Revenue"), None)
        if revenue_stage and revenue_stage["status"] == "ok":
            recs.append("Revenue sync successful.")
        return recs or ["No action required."]

    def _notify_ceo(self, report: dict[str, Any]) -> None:
        reasons = "; ".join(report["recommendations"])
        log.warning("CEO NOTIFIED — operations aborted the cycle: %s", reasons)

    # --- Reads ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        rep = self.db.get_latest_operations_report()
        if rep:
            return {"status": rep["status"], "mode": rep["mode"],
                    "system": rep["system"], "recommendations": rep["recommendations"],
                    "created_at": rep["created_at"]}
        pre = self.check("production")
        return {"status": "never_run", "healthy": pre["healthy"], "checks": pre["checks"]}

    def report(self) -> dict[str, Any]:
        rep = self.db.get_latest_operations_report()
        return rep or {"message": "No operations report yet."}
