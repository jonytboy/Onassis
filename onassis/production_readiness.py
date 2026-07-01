"""The Production Readiness audit — read-only.

This module ships **no new business functionality**. It inspects the existing
configuration and modules and reports how close ONASSIS is to operating in the
real world. It never generates content, never publishes, never decides — it
only reads what is already there (credentials, enabled modes, known dev-only
components) and produces a single, structured Production Readiness Report.

For every audited module it reports a status:

* ``Production Ready``  — works against the real world with what's configured.
* ``Partially Ready``   — works, but a dev-only component or missing secret
  limits it (e.g. a real publisher whose live mode is intentionally disabled,
  or LLM copy that still ships placeholder mock-up images).
* ``Development Only``   — cannot operate for real yet (missing credentials or
  an unimplemented live path).

For every blocker it reports: Description, Priority, Estimated effort, and a
Recommended fix. It finishes with a single ✅/❌ checklist and verifies the nine
named subsystems (Etsy publishing, Etsy reading, Pinterest publishing,
Analytics collection, Revenue collection, Compliance, CEO, Daily Cycle,
Operations Manager).
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# Status labels.
READY = "Production Ready"
PARTIAL = "Partially Ready"
DEV_ONLY = "Development Only"

# Blocker priorities.
CRITICAL = "Critical"
HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"


def _blocker(description: str, priority: str, effort: str, fix: str) -> dict[str, str]:
    return {
        "description": description,
        "priority": priority,
        "estimated_effort": effort,
        "recommended_fix": fix,
    }


class ProductionReadiness:
    """Audits configuration + modules and emits a Production Readiness Report."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # --- Credential / capability detection (read-only) --------------

    @property
    def has_ai(self) -> bool:
        return bool(self.config.anthropic_api_key)

    @property
    def etsy_app_configured(self) -> bool:
        """The Etsy app keystring + shop id are present (the flow can start)."""
        e = self.config.etsy or {}
        return bool(e.get("api_key") and e.get("shop_id"))

    @property
    def has_etsy(self) -> bool:
        """Etsy can actually call the API: a static token or an OAuth grant."""
        e = self.config.etsy or {}
        if not self.etsy_app_configured:
            return False
        if e.get("access_token"):
            return True
        from onassis.connectors.etsy_oauth import build_etsy_oauth

        return build_etsy_oauth(self.config).is_authorised

    @property
    def has_pinterest(self) -> bool:
        return bool((self.config.pinterest or {}).get("access_token"))

    @property
    def live_publishing_enabled(self) -> bool:
        modes = set((self.config.publishing or {}).get("enabled_modes", ["dry_run", "draft"]))
        return "live" in modes

    # --- Shared blockers --------------------------------------------

    def _ai_blocker(self) -> dict[str, str]:
        return _blocker(
            "ANTHROPIC_API_KEY is not set, so no real LLM content can be generated.",
            CRITICAL,
            "5 minutes (provision an Anthropic key)",
            "Set ANTHROPIC_API_KEY in the environment or .env file.",
        )

    def _etsy_blocker(self) -> dict[str, str]:
        if self.etsy_app_configured:
            # App keys are present; only the user-consent step remains.
            return _blocker(
                "Etsy app is configured (ETSY_CLIENT_ID + ETSY_SHOP_ID) but no account "
                "has authorised it yet, so there is no access token.",
                CRITICAL,
                "5 minutes (one-time browser consent)",
                "Run `python main.py --etsy-login`, approve access, then "
                "`python main.py --etsy-callback \"<redirect URL>\"`.",
            )
        return _blocker(
            "Etsy credentials are missing (ETSY_CLIENT_ID / ETSY_SHOP_ID).",
            CRITICAL,
            "1-2 hours (register an Etsy app + complete OAuth)",
            "Register an Etsy Open API v3 app, set ETSY_CLIENT_ID, "
            "ETSY_CLIENT_SECRET, ETSY_REDIRECT_URI and ETSY_SHOP_ID, then authorise "
            "via `python main.py --etsy-login`.",
        )

    def _pinterest_creds_blocker(self) -> dict[str, str]:
        return _blocker(
            "Pinterest credentials are missing (PINTEREST_ACCESS_TOKEN / "
            "PINTEREST_AD_ACCOUNT_ID).",
            MEDIUM,
            "1-2 hours (complete Pinterest OAuth)",
            "Create a Pinterest app, complete OAuth, and set PINTEREST_ACCESS_TOKEN "
            "and PINTEREST_AD_ACCOUNT_ID.",
        )

    # --- The module audit -------------------------------------------

    def _module(
        self, module: str, name: str, status: str, summary: str,
        blockers: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "module": module,
            "name": name,
            "status": status,
            "summary": summary,
            "blockers": blockers or [],
        }

    def audit_modules(self) -> list[dict[str, Any]]:
        """Per-module Production Ready / Partially Ready / Development Only."""
        ai, etsy, pin = self.has_ai, self.has_etsy, self.has_pinterest
        ai_b = [] if ai else [self._ai_blocker()]
        etsy_b = [] if etsy else [self._etsy_blocker()]

        modules: list[dict[str, Any]] = [
            # --- Infrastructure (deterministic, no external dependency) ---
            self._module("onassis/config.py", "Configuration", READY,
                         "Merges YAML + environment; secrets read from env/.env."),
            self._module("onassis/database.py", "SQLite database", READY,
                         "Schema is created idempotently; integrity check available."),
            self._module("onassis/logger.py", "Logging", READY,
                         "Rotating file + console logging."),
            self._module("onassis/scheduler.py", "Scheduler", READY,
                         "Daily trigger; deterministic."),

            # --- AI provider + LLM-backed content ---
            self._module(
                "onassis/llm.py", "AI provider (Anthropic)",
                READY if ai else DEV_ONLY,
                "Structured JSON generation via the Anthropic API."
                if ai else "No API key configured; cannot reach the LLM.",
                ai_b),
            self._module(
                "onassis/agents/content_director.py", "Content Director",
                READY if ai else DEV_ONLY,
                "Generates daily briefs via the LLM." if ai
                else "Requires the AI provider.", list(ai_b)),
            self._module(
                "onassis/agents/content_creator.py", "Content Creator",
                READY if ai else DEV_ONLY,
                "Generates campaign content via the LLM." if ai
                else "Requires the AI provider.", list(ai_b)),
            self._module(
                "onassis/campaign_manager.py", "Campaign Manager",
                READY if ai else PARTIAL,
                "Orchestrates director + creator into campaigns." if ai
                else "Campaign structure works; content needs the AI provider.",
                list(ai_b)),
            self._module(
                "onassis/brain.py", "ONASSIS Brain",
                READY if ai else PARTIAL,
                "Predicts and remembers; stores knowledge." if ai
                else "Storage works; predictions need the AI provider.", list(ai_b)),

            # --- Governance + finance (deterministic) ---
            self._module("onassis/compliance.py", "Compliance Director",
                         READY if ai else PARTIAL,
                         "Reviews proposals/campaigns; veto authority." if ai
                         else "Rule checks work; LLM-assisted review needs the AI provider.",
                         list(ai_b)),
            self._module("onassis/ceo.py", "CEO Agent", READY,
                         "Risk-adjusted ROI policy; deterministic decisions."),
            self._module("onassis/governance.py", "Governance chain", READY,
                         "Compliance veto + CEO decision pipeline; deterministic."),
            self._module("onassis/proposals.py", "Proposals", READY,
                         "Proposal value objects; deterministic."),
            self._module("onassis/profit.py", "Profit Engine", READY,
                         "Capital allocation + AI-budget accounting; deterministic."),
            self._module("onassis/revenue.py", "Revenue Intelligence Engine", READY,
                         "Per-order economics over the unified ledger; deterministic."),
            self._module("onassis/optimiser.py", "Product Optimiser", READY,
                         "Proposes one action per product; deterministic ranking."),
            self._module("onassis/experiments.py", "Experiment Engine", READY,
                         "A/B experiments + statistical confidence; deterministic."),
            self._module("onassis/expansion.py", "Revenue Expansion Engine", READY,
                         "Scores the product catalogue per design; CEO launches the "
                         "profitable set; learns from sales. Deterministic."),
            self._module(
                "onassis/opportunities.py", "Product Opportunity Engine",
                READY if ai else PARTIAL,
                "Discovers ranked product opportunities (the dev backlog); "
                "ranking/dedup/CEO selection are deterministic." if ai
                else "Backlog, ranking, dedup and CEO selection work; idea "
                     "generation needs the AI provider.",
                list(ai_b)),

            # --- Marketplace reads ---
            self._module(
                "onassis/connectors/etsy_client.py", "Etsy API client (read)",
                READY if etsy else DEV_ONLY,
                "Read-only Etsy Open API v3 client." if etsy
                else "Real client implemented; credentials missing.", list(etsy_b)),
            self._module(
                "onassis/connectors/etsy.py", "Etsy Operations Connector (read)",
                READY if etsy else DEV_ONLY,
                "Incremental, idempotent order/listing/stat import." if etsy
                else "Real, tested connector; credentials missing.", list(etsy_b)),

            # --- Analytics + Pinterest ---
            self._module(
                "onassis/analytics.py", "Analytics Collector",
                READY if etsy else PARTIAL,
                "Append-only metric snapshots + trends from Etsy data." if etsy
                else "Engine works; Etsy source needs credentials, Pinterest source "
                     "is not implemented.",
                list(etsy_b)),
            self._module(
                "onassis/connectors/pinterest.py", "Pinterest connector",
                DEV_ONLY,
                "Implements the metrics contract but the live fetch is a no-op; "
                "returns no data even when credentials are present.",
                ([self._pinterest_creds_blocker()] if not pin else []) + [
                    _blocker(
                        "PinterestConnector.fetch_metrics() is not implemented — it "
                        "returns an empty list even when configured, so no Pinterest "
                        "metrics are ever collected and Pinterest publishing does not exist.",
                        HIGH,
                        "1-2 days (map the Pinterest v5 analytics API onto metric rows)",
                        "Implement fetch_metrics() against the Pinterest v5 analytics "
                        "endpoint, mapping impressions/saves/outbound clicks/CTR onto "
                        "the canonical metric rows.",
                    ),
                ]),

            # --- Listing + publishing (write path) ---
            self._module(
                "onassis/design_package.py", "Design Package Builder",
                READY if ai else DEV_ONLY,
                "Turns a CEO-approved opportunity into a print-ready design "
                "package (brief, print spec, prompts, listing seed, compliance); "
                "CEO + compliance gated." if ai
                else "Gates + package assembly work; design generation needs the "
                     "AI provider.",
                list(ai_b)),
            self._module(
                "onassis/listing_factory.py", "Listing Factory",
                PARTIAL if ai else DEV_ONLY,
                "Builds upload-ready Etsy listing packages; copy is LLM-generated, "
                "pricing deterministic. Mock-up images are placeholder PNGs.",
                list(ai_b) + [
                    _blocker(
                        "Mock-up images are 1x1 placeholder PNGs (source='placeholder'); "
                        "no real product imagery is generated.",
                        MEDIUM,
                        "2-3 days (integrate an image/mock-up generator)",
                        "Replace _PLACEHOLDER_PNG with a real image-generation or "
                        "mock-up service that fills each required mock-up slot.",
                    ),
                ]),
            self._module(
                "onassis/publishing.py", "Autonomous Publisher",
                (READY if etsy else PARTIAL),
                "Publishes compliance-approved packages to Etsy as drafts; "
                "idempotent, retried, logged. Live mode is intentionally disabled.",
                list(etsy_b) + [
                    _blocker(
                        "Live publishing is not enabled — only Dry Run and Draft modes "
                        "are active; listings are never made live automatically.",
                        LOW,
                        "0.5 day (enable + verify the live path)",
                        "When ready for true live listings, add 'live' to "
                        "publishing.enabled_modes and extend the draft client to set a "
                        "live state. Drafts are the safe default until then.",
                    ),
                ]),

            # --- Orchestration ---
            self._module("onassis/daily_cycle.py", "Daily Cycle", READY,
                         "Single execution entry point; runs existing modules in "
                         "order, logs and continues past stage failures."),
            self._module("onassis/operations.py", "Operations Manager", READY,
                         "Pre-flight health gate + Operations Report; aborts and "
                         "notifies the CEO on a critical dependency failure."),
            self._module("onassis/orchestrator.py", "Legacy content pipeline", PARTIAL,
                         "Original v0.1 pipeline; still wires the placeholder "
                         "Publisher/Analytics agents. Superseded by the Daily Cycle "
                         "for production.",
                         [_blocker(
                             "Uses the placeholder Publisher and Analytics agents, not "
                             "the production PublisherService/AnalyticsEngine.",
                             LOW,
                             "0 days (use the Daily Cycle path in production)",
                             "Drive production via the Operations Manager / Daily Cycle; "
                             "keep the legacy pipeline only for the --once content demo.",
                         )]),

            # --- Explicit placeholders (superseded) ---
            self._module("onassis/agents/publisher.py", "Publisher agent (placeholder)",
                         DEV_ONLY,
                         "Intentional no-op from v0.1; superseded by "
                         "onassis/publishing.py (PublisherService).",
                         [_blocker(
                             "No-op placeholder agent; does not publish anything.",
                             LOW,
                             "0 days (already superseded)",
                             "Use onassis/publishing.py for real publishing; this agent "
                             "remains only as the legacy pipeline slot.",
                         )]),
            self._module("onassis/agents/analytics.py", "Analytics agent (placeholder)",
                         DEV_ONLY,
                         "Reports local counts only; superseded by "
                         "onassis/analytics.py (AnalyticsEngine).",
                         [_blocker(
                             "Placeholder agent reports local DB counts, not real "
                             "marketplace metrics.",
                             LOW,
                             "0 days (already superseded)",
                             "Use onassis/analytics.py for real metric collection; this "
                             "agent remains only as the legacy pipeline slot.",
                         )]),
        ]
        return modules

    # --- Subsystem verification -------------------------------------

    def verify_subsystems(self) -> list[dict[str, Any]]:
        """Verify the nine named subsystems the brief calls out."""
        ai, etsy, pin = self.has_ai, self.has_etsy, self.has_pinterest

        def v(name: str, status: str, detail: str) -> dict[str, str]:
            return {"subsystem": name, "status": status, "detail": detail}

        return [
            v("Etsy reading", READY if etsy else DEV_ONLY,
              "Read-only connector ready." if etsy
              else "Connector implemented; Etsy credentials missing."),
            v("Etsy publishing", READY if etsy else DEV_ONLY,
              "Draft publishing ready (live mode disabled by design)." if etsy
              else "Draft publisher implemented; Etsy write credentials missing."),
            v("Pinterest publishing", DEV_ONLY,
              "Not implemented — the Pinterest connector has no publishing path and "
              "its analytics fetch is a no-op."),
            v("Analytics collection", READY if etsy else PARTIAL,
              "Etsy-derived metrics collected; Pinterest source pending." if etsy
              else "Engine ready; Etsy source needs credentials, Pinterest pending."),
            v("Revenue collection", READY,
              "Per-order economics over the ledger; live once Etsy sync is configured."
              if etsy else
              "Engine ready; populates from the Etsy sync once credentials are set."),
            v("Compliance", READY if ai else PARTIAL,
              "Veto authority active." if ai
              else "Rule checks active; LLM-assisted review needs the AI provider."),
            v("CEO", READY, "Risk-adjusted ROI decisions; deterministic."),
            v("Daily Cycle", READY, "Single execution entry point; resilient to "
              "stage failures."),
            v("Operations Manager", READY, "Health gate + reporting; aborts on "
              "critical dependency failure."),
        ]

    # --- The single checklist ---------------------------------------

    def checklist(self) -> list[dict[str, Any]]:
        """A single ✅/❌ checklist, mirroring the brief's example."""
        ai, etsy, pin = self.has_ai, self.has_etsy, self.has_pinterest
        items = [
            ("AI provider (Anthropic API key)", ai),
            ("Etsy read credentials", etsy),
            ("Etsy write credentials (publishing)", etsy),
            ("Pinterest OAuth credentials", pin),
            ("Pinterest analytics/publishing implemented", False),
            ("Listing mock-up images (real, not placeholder)", False),
            ("Live publishing enabled", self.live_publishing_enabled),
            ("Database integrity", self.db.integrity_ok()),
        ]
        return [
            {"item": label, "ready": bool(ok), "mark": "✅" if ok else "❌"}
            for label, ok in items
        ]

    # --- The full report --------------------------------------------

    def report(self) -> dict[str, Any]:
        """Assemble the full Production Readiness Report."""
        modules = self.audit_modules()
        subsystems = self.verify_subsystems()
        checklist = self.checklist()

        counts = {READY: 0, PARTIAL: 0, DEV_ONLY: 0}
        for m in modules:
            counts[m["status"]] += 1

        blockers: list[dict[str, Any]] = []
        for m in modules:
            for b in m["blockers"]:
                blockers.append({"module": m["name"], **b})

        priority_order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}
        blockers.sort(key=lambda b: priority_order.get(b["priority"], 99))

        production_ready = all(c["ready"] for c in checklist if c["item"] in {
            "AI provider (Anthropic API key)",
            "Etsy read credentials",
            "Etsy write credentials (publishing)",
            "Database integrity",
        })

        report = {
            "environment": self.config.environment,
            "production_ready": production_ready,
            "summary": {
                "modules_audited": len(modules),
                "production_ready": counts[READY],
                "partially_ready": counts[PARTIAL],
                "development_only": counts[DEV_ONLY],
                "open_blockers": len(blockers),
            },
            "credentials": {
                "ai_provider": self.has_ai,
                "etsy": self.has_etsy,
                "pinterest": self.has_pinterest,
            },
            "modules": modules,
            "subsystems": subsystems,
            "blockers": blockers,
            "checklist": checklist,
        }
        log.info(
            "Production readiness: %d ready / %d partial / %d dev-only, %d blocker(s).",
            counts[READY], counts[PARTIAL], counts[DEV_ONLY], len(blockers),
        )
        return report
