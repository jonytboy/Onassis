"""The ONASSIS Operations Centre — the browser control room for the business.

A lightweight, premium operations UI served by the existing FastAPI app (Jinja +
HTMX + Alpine, no build step, no SPA framework). It exposes **business
operations**, not software functions: a one-glance traffic-light status board,
one big **RUN BUSINESS** button that executes the whole commercial cycle with
live progress, and tabs for Business, Products, Marketing, Pipeline, System, an
Approval queue, and an Advanced (developer) drawer.

This module adds **no business logic** — every data point and action reuses an
existing engine (the Daily Cycle, CEO Dashboard, Portfolio, Marketing/Traffic,
Gelato, the ops scripts). It only presents and orchestrates them over HTTP.

Auth: the whole ``/operations`` subtree is exempt from the global API-key
middleware (so the browser shell can load and the UI can poll without tripping
the rate limiter) and instead uses its **own** operator check — in production
every data/action endpoint requires the operator key (``X-API-Key`` header or a
``?key=`` param); in development it's open. The shell page itself is open and
prompts for the key, which the UI stores locally and sends on every request.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from onassis.business_settings import BusinessSettings
from onassis.collections import collection_name
from onassis.config import ROOT_DIR
from onassis.logger import get_logger
from onassis.product_status import (
    FILTERS, derive_product_status, is_valid_listing_id, matches_filter)
from onassis.security import security_enabled

log = get_logger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Business modes.
RUNNING, PAUSED, STOPPED = "running", "paused", "emergency_stop"

# The canonical daily-cycle stages, in order, with operator-friendly labels.
STAGES: list[tuple[str, str]] = [
    ("Sync Etsy", "Sync Etsy"),
    ("Sync Pinterest", "Sync Pinterest"),
    ("Import Revenue", "Import Revenue"),
    ("Fulfil Orders", "Fulfil Orders (Gelato)"),
    ("Import Analytics", "Import Analytics"),
    ("Learn & Review Portfolio", "Learning & Portfolio"),
    ("Run Product Optimiser", "Optimise"),
    ("CEO Decision", "CEO Decision"),
    ("Market Research", "Research"),
    ("Create Product Opportunity", "Product Discovery"),
    ("Build Design Package", "Design + Compliance"),
    ("Generate Master Artwork", "Artwork"),
    ("Create Product Campaign", "Campaign"),
    ("Expand Products", "Product Set"),
    ("Publish Products", "Publish (Etsy + Shopify) + Gallery"),
    ("Generate Marketing Content", "Marketing (Pin/IG/FB/Blog/Email)"),
    ("Promote on Pinterest", "Traffic (Pinterest)"),
    ("Daily Report", "Daily Report"),
    ("CEO Dashboard", "CEO Report"),
    ("Record Results", "Record"),
]
_STAGE_NAMES = [s[0] for s in STAGES]

_START_RE = re.compile(r"^\[daily\] (.+): start$")
_END_RE = re.compile(r"^\[daily\] (.+): (ok|failed|skipped) \(")


# =====================================================================
# In-memory operations state (per app instance)
# =====================================================================

class OperationsState:
    """Business mode + current-run progress + a live log ring buffer."""

    def __init__(self) -> None:
        self.mode = RUNNING
        self._log: deque[dict[str, Any]] = deque(maxlen=800)
        self._seq = 0
        self._lock = threading.Lock()
        self.run: dict[str, Any] = {"status": "idle", "stages": self._fresh_stages(),
                                    "started_at": None, "finished_at": None, "summary": None}

    @staticmethod
    def _fresh_stages() -> list[dict[str, Any]]:
        return [{"name": n, "label": lbl, "status": "pending"} for n, lbl in STAGES]

    # --- logging ---
    def add_log(self, line: str, level: str = "info") -> None:
        with self._lock:
            self._seq += 1
            self._log.append({"seq": self._seq, "level": level,
                              "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                              "line": line})

    def logs_since(self, since: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._log if e["seq"] > since]

    # --- run lifecycle ---
    def begin_run(self) -> None:
        with self._lock:
            self.run = {"status": "running", "stages": self._fresh_stages(),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": None, "summary": None}

    def mark_stage(self, name: str, status: str) -> None:
        with self._lock:
            for s in self.run["stages"]:
                if s["name"] == name:
                    s["status"] = status
                    break

    def end_run(self, status: str, summary: Any = None) -> None:
        with self._lock:
            self.run["status"] = status
            self.run["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.run["summary"] = summary
            # Any stage left running/pending after a completed run is unknown.
            if status != "running":
                for s in self.run["stages"]:
                    if s["status"] == "running":
                        s["status"] = "ok"

    @property
    def is_running(self) -> bool:
        return self.run.get("status") == "running"


class _RunLogHandler(logging.Handler):
    """Streams onassis logs into the live console AND drives the stage lights by
    parsing the Daily Cycle's own start/finish log lines — no cycle changes."""

    def __init__(self, state: OperationsState) -> None:
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        level = "error" if record.levelno >= logging.ERROR else (
            "warn" if record.levelno >= logging.WARNING else "info")
        self.state.add_log(msg, level)
        m = _START_RE.match(msg)
        if m:
            self.state.mark_stage(m.group(1), "running")
            return
        m = _END_RE.match(msg)
        if m:
            self.state.mark_stage(m.group(1), {"ok": "ok", "failed": "failed",
                                               "skipped": "skipped"}[m.group(2)])


# =====================================================================
# Traffic-light status board
# =====================================================================

def _light(status: str, label: str, detail: str = "") -> dict[str, str]:
    return {"status": status, "label": label, "detail": detail}


def _disk_light() -> dict[str, str]:
    total, used, free = shutil.disk_usage(ROOT_DIR)
    pct = round(used / total * 100)
    status = "green" if pct < 85 else ("amber" if pct < 95 else "red")
    return _light(status, "Disk", f"{pct}% used · {free // (1024**3)} GB free")


def _memory_light() -> dict[str, str]:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        pct_avail = round(avail / total * 100) if total else 0
        status = "green" if pct_avail > 15 else ("amber" if pct_avail > 7 else "red")
        return _light(status, "Memory", f"{pct_avail}% available")
    except Exception:
        return _light("grey", "Memory", "unavailable")


def _service_light(service: str) -> dict[str, str]:
    if not shutil.which("systemctl"):
        return _light("grey", "Service", "systemctl n/a (dev)")
    try:
        out = subprocess.run(["systemctl", "is-active", service],
                             capture_output=True, text=True, timeout=5)
        state = (out.stdout or out.stderr).strip()
        return _light("green" if state == "active" else "red", "Service",
                      f"{service}: {state or 'unknown'}")
    except Exception as exc:
        return _light("amber", "Service", f"unchecked ({exc})")


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT_DIR), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _ai_provider_light(db: Any, provider: str, label: str, configured: bool) -> dict[str, str]:
    """A traffic light for an AI provider that reflects its live BILLING health
    (Sprint 44.1): a billing/credit/auth problem from the most recent call turns
    it red with the exact reason — no more silent 'completed_with_failures'."""
    if not configured:
        return _light("red", label, "not configured")
    try:
        from onassis.ai_accounting import provider_alert
        alert = provider_alert(db, provider)
    except Exception:
        alert = {"ok": True, "kind": "ok", "message": ""}
    if not alert["ok"]:
        return _light("red", label, f"{label}: {alert['message']}")
    return _light("green", label, "configured")


def _cred_light(configured: bool, ready: bool, label: str,
                not_built: bool = False) -> dict[str, str]:
    if not_built:
        return _light("grey", label, "not integrated")
    if ready:
        return _light("green", label, "configured")
    if configured:
        return _light("amber", label, "partially configured")
    return _light("red", label, "not configured")


def build_status(app_state: Any, state: OperationsState, request: Request) -> dict[str, Any]:
    """The full traffic-light board — one glance tells the operator if the
    business is healthy."""
    config = app_state.config
    db = app_state.db
    etsy = app_state.daily.etsy
    gelato = app_state.daily.gelato
    pinterest = app_state.daily.pinterest
    env = getattr(config, "environment", "development")

    # Database integrity.
    try:
        db_ok = db.integrity_ok()
    except Exception:
        db_ok = False

    # HTTPS / Cloudflare (behind a proxy the scheme is in X-Forwarded-Proto).
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    via_cf = "cf-ray" in request.headers or "cloudflare" in request.headers.get("server", "").lower()
    https_status = "green" if proto == "https" else ("amber" if env != "production" else "red")

    etsy_cfg = bool((config.etsy or {}).get("api_key"))
    gelato_cfg = bool((config.gelato or {}).get("api_key"))
    pin_cfg = bool((config.pinterest or {}).get("access_token"))
    shopify_conn = getattr(getattr(app_state.daily, "shopify", None), "connector", None)
    shopify_cfg = bool((config.shopify or {}).get("client_id")
                       or (config.shopify or {}).get("admin_token"))

    lights = {
        "environment": _light("green" if env == "production" else "amber",
                              "Environment", env),
        "server": _light("green", "Server", "online"),
        "service": _service_light((config.security or {}).get("service", "onassis")
                                  if isinstance(config.security, dict) else "onassis"),
        "api": _light("green", "API", "responding"),
        "disk": _disk_light(),
        "memory": _memory_light(),
        "database": _light("green" if db_ok else "red", "Database",
                          "integrity ok" if db_ok else "integrity FAILED"),
        "anthropic": _ai_provider_light(db, "anthropic", "Anthropic",
                                        bool(config.anthropic_api_key)),
        "openai": _ai_provider_light(db, "openai", "OpenAI",
                                     bool((config.image or {}).get("api_key"))),
        "etsy": _cred_light(etsy_cfg, bool(getattr(etsy, "is_configured", False)), "Etsy"),
        "shopify": _cred_light(shopify_cfg, bool(getattr(shopify_conn, "is_configured", False)),
                               "Shopify"),
        "gelato": _cred_light(gelato_cfg, bool(getattr(gelato, "can_fulfil", False)), "Gelato"),
        "pinterest": _cred_light(pin_cfg, bool(getattr(pinterest, "can_publish", False)),
                                 "Pinterest"),
        "facebook": _cred_light(False, False, "Facebook", not_built=True),
        "instagram": _cred_light(False, False, "Instagram", not_built=True),
        "https": _light(https_status, "HTTPS / Cloudflare",
                        f"{proto}{' · via Cloudflare' if via_cf else ''}"),
    }

    # Overall: red if any CRITICAL light is red; amber if any amber; else green.
    critical = ["service", "api", "database", "anthropic", "disk"]
    crit_status = [lights[k]["status"] for k in critical]
    if "red" in crit_status:
        overall = "red"
    elif "amber" in [l["status"] for l in lights.values()]:
        overall = "amber"
    else:
        overall = "green"

    return {
        "git_commit": _git_commit(),
        "business_mode": state.mode,
        "run_status": state.run.get("status"),
        "lights": lights,
        "overall": _light(overall, "Overall Business Status",
                          {"green": "Healthy", "amber": "Attention",
                           "red": "Action required"}[overall]),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# =====================================================================
# Router
# =====================================================================

def build_operations_router(get_state) -> APIRouter:
    """Build the Operations Centre router. ``get_state`` returns the per-app
    :class:`OperationsState`."""
    router = APIRouter(prefix="/operations", tags=["operations-centre"])

    def _require_operator(request: Request) -> None:
        """Ops-centre auth: open in dev; in production require the operator key
        (X-API-Key header or ?key= query). Works with plain browser fetches."""
        config = request.app.state.config
        if not security_enabled(config):
            return
        key = (config.security or {}).get("api_key")
        provided = (request.headers.get((config.security or {}).get("api_key_header", "X-API-Key"))
                    or request.query_params.get("key"))
        if not key or provided != key:
            raise HTTPException(status_code=401, detail="Operator key required.")

    # --- The shell (open; the UI prompts for the key) ---
    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def operations_home(request: Request) -> Any:
        config = request.app.state.config
        return _TEMPLATES.TemplateResponse(request, "operations.html", {
            "environment": getattr(config, "environment", "development"),
            "secured": security_enabled(config),
            "app_version": getattr(config, "version", ""),
        })

    # --- Status board ---
    @router.get("/api/status")
    def api_status(request: Request) -> Any:
        _require_operator(request)
        return build_status(request.app.state, get_state(request.app), request)

    # --- RUN BUSINESS + progress + logs ---
    @router.post("/api/run-business")
    def api_run_business(request: Request) -> Any:
        _require_operator(request)
        state = get_state(request.app)
        if state.mode != RUNNING:
            raise HTTPException(status_code=409,
                                detail=f"Business is {state.mode} — resume it first.")
        if state.is_running:
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        daily = request.app.state.daily
        state.begin_run()
        state.add_log("RUN BUSINESS requested — starting the full commercial cycle.")

        def worker() -> None:
            handler = _RunLogHandler(state)
            root = logging.getLogger("onassis")
            root.addHandler(handler)
            try:
                result = daily.run("production")
                status = "completed_with_failures" if any(
                    s["status"] == "failed" for s in state.run["stages"]) else "completed"
                summary = _run_summary(result)
                try:
                    summary["publish"] = _publish_summary(request.app.state)
                except Exception:  # summary is best-effort; never fail the run on it
                    log.debug("publish summary unavailable", exc_info=True)
                state.end_run(result.get("status", status), summary)
                state.add_log(f"RUN BUSINESS finished: {result.get('status')}.")
            except Exception as exc:  # never crash the server on a run failure
                state.end_run("failed", {"error": str(exc)})
                state.add_log(f"RUN BUSINESS crashed: {exc}", "error")
                log.exception("Operations Centre run failed")
            finally:
                root.removeHandler(handler)

        threading.Thread(target=worker, name="run-business", daemon=True).start()
        return {"status": "started"}

    @router.get("/api/run")
    def api_run(request: Request) -> Any:
        _require_operator(request)
        return get_state(request.app).run

    @router.get("/api/logs")
    def api_logs(request: Request, since: int = 0) -> Any:
        _require_operator(request)
        return {"logs": get_state(request.app).logs_since(since)}

    # --- Business tab ---
    @router.get("/api/business")
    def api_business(request: Request) -> Any:
        _require_operator(request)
        s = request.app.state
        board = s.daily.dashboard.build()
        report = s.report.build()
        try:
            experiments = len(s.experiments.active())
        except Exception:
            experiments = 0
        recs = report.get("recommendations", {})
        rec = (f"Expand {recs['expand'][0]}" if recs.get("expand")
               else (f"Kill {recs['kill'][0]}" if recs.get("kill")
                     else "Keep driving traffic to the live listings."))
        return {
            "revenue_yesterday": board["revenue_yesterday"],
            "profit_yesterday": board["profit_yesterday"],
            "orders": report["orders"]["company"],
            "conversion": board["conversion"],
            "visitors": board["visitors"],
            "roi": board["roi"],
            "ai_spend": board["ai_cost"],
            "advertising_spend": s.db.cost_by_category("advertising"),
            "best_product": board["best_seller"],
            "worst_product": board["worst_seller"],
            "experiments_running": experiments,
            "cash_balance": board["cash_balance"],
            "recommendation": rec,
            "headline": board["headline"],
        }

    # --- Products tab (dashboard + filters + publish summary) ---
    @router.get("/api/products")
    def api_products(request: Request, filter: str | None = None) -> Any:
        _require_operator(request)
        s = request.app.state
        return {"products": _products(s, filter),
                "summary": _publish_summary(s),
                "filters": ["all", *FILTERS.keys()]}

    @router.get("/api/publish-summary")
    def api_publish_summary(request: Request) -> Any:
        _require_operator(request)
        return _publish_summary(request.app.state)

    @router.get("/api/products/{sku}")
    def api_product(request: Request, sku: str) -> Any:
        _require_operator(request)
        detail = _product_detail(request.app.state, sku)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown product.")
        return detail

    # --- Approval workspace decisions (Sprint 40, Objective 2) ---
    @router.post("/api/approvals/{sku}/decision")
    def api_approval_decision(request: Request, sku: str, payload: dict | None = None) -> Any:
        _require_operator(request)
        s = request.app.state
        body = payload or {}
        action = str(body.get("action", "")).lower()
        operator = body.get("operator") or "operator"
        notes = body.get("notes")
        product = s.db.get_product_by_sku(sku)
        if not product:
            raise HTTPException(status_code=404, detail="Unknown product.")
        key, cid = product.get("product_key"), product.get("campaign_id")

        def _record(decision: str) -> dict[str, Any]:
            return s.db.set_product_approval({
                "sku": sku, "product_key": key, "campaign_id": cid,
                "decision": decision, "operator": operator, "notes": notes})

        if action == "reject":
            _record("rejected")
            get_state(request.app).add_log(f"Product {sku} REJECTED by {operator}.", "warn")
            return {"sku": sku, "decision": "rejected", "published": None}
        if action == "approve":
            _record("approved")
            get_state(request.app).add_log(f"Product {sku} APPROVED by {operator}.")
            return {"sku": sku, "decision": "approved", "published": None}
        if action in ("approve_and_publish", "retry"):
            if action == "approve_and_publish":
                _record("approved")
            # Publish to Etsy AND Shopify at the same time (Sprint 41.2, Obj 7).
            result = _publish_all_channels(s, cid, key)
            etsy_ok = (result.get("etsy") or {}).get("status") in ("draft", "dry_run")
            get_state(request.app).add_log(
                f"Publish {sku}: etsy={result.get('etsy', {}).get('status')} "
                f"shopify={result.get('shopify', {}).get('status')}.",
                "info" if etsy_ok else "error")
            return {"sku": sku, "decision": "approved", "published": result}
        if action == "publish_shopify":
            result = _publish_shopify(s, cid, key)
            get_state(request.app).add_log(
                f"Shopify publish {sku} -> {result.get('status')}.")
            return {"sku": sku, "channel": "shopify", "published": result}
        if action == "regenerate_mockups":
            get_state(request.app).add_log(f"Regenerating mockups for {sku}…")
            result = _regenerate_mockups(s, cid, key)
            get_state(request.app).add_log(
                f"Mockups for {sku}: {result.get('mockup_status', 'failed')} "
                f"({result.get('passing', 0)}/{result.get('total', 0)} passed).",
                "info" if result.get("ok") else "warn")
            return {"sku": sku, "action": "regenerate_mockups", "result": result}
        raise HTTPException(status_code=400, detail=f"Unknown approval action '{action}'.")

    @router.post("/api/approvals/cleanup")
    def api_approvals_cleanup(request: Request, payload: dict | None = None) -> Any:
        """Archive all unpublished (incomplete) products — start fresh from a
        clean queue without deleting anything or spending AI credit."""
        _require_operator(request)
        dry = bool((payload or {}).get("dry_run"))
        result = _cleanup_incomplete_products(request.app.state, dry_run=dry)
        if not dry:
            get_state(request.app).add_log(
                f"Queue cleanup: archived {result['archived']} unpublished product(s).",
                "warn")
        return result

    # --- Business settings (Sprint 40, Objective 7) ---
    @router.get("/api/business-settings")
    def api_get_business_settings(request: Request) -> Any:
        _require_operator(request)
        s = request.app.state
        return {"settings": BusinessSettings(s.db, s.config).describe()}

    @router.post("/api/business-settings")
    def api_set_business_settings(request: Request, payload: dict | None = None) -> Any:
        _require_operator(request)
        s = request.app.state
        changes = (payload or {}).get("changes", payload or {})
        try:
            values = BusinessSettings(s.db, s.config).update(
                changes, operator=(payload or {}).get("operator"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        get_state(request.app).add_log("Business settings updated by operator.")
        return {"settings": BusinessSettings(s.db, s.config).describe(), "values": values}

    # --- Software Updates & Deployment (Sprint 40.1 / 40.2) ---
    # Small, well-defined surface: check · download · preview · validate ·
    # deploy · rollback · health. The Operations Centre never runs shell.
    @router.get("/api/updates")
    def api_updates(request: Request) -> Any:
        _require_operator(request)
        # Fast page-load view — no network fetch (use Check for Updates for that).
        return request.app.state.deployment.status(check_remote=False)

    @router.get("/api/environment")
    def api_environment(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.deployment.environment()

    # Safe Mode step 1 — Check (contacts GitHub; changes nothing).
    @router.post("/api/updates/check")
    def api_updates_check(request: Request) -> Any:
        _require_operator(request)
        result = request.app.state.deployment.check_updates(fetch=True)
        get_state(request.app).add_log(
            f"Checked for updates: {'update available' if result['update_available'] else 'up to date'} "
            f"({result['behind']} behind).")
        return result

    # Safe Mode step 2 — Download (fetch Git only; nothing applied).
    @router.post("/api/updates/download")
    def api_updates_download(request: Request) -> Any:
        _require_operator(request)
        result = request.app.state.deployment.download()
        get_state(request.app).add_log(f"Downloaded latest code: {result.get('detail')}.")
        return result

    # Release preview — what a deploy WOULD do (read-only).
    @router.get("/api/updates/preview")
    def api_updates_preview(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.deployment.preview()

    # Safe Mode step 3 — Validate (simulate the deploy; changes nothing).
    @router.get("/api/updates/validate")
    def api_updates_validate(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.deployment.simulate()

    @router.get("/api/updates/versions")
    def api_updates_versions(request: Request) -> Any:
        _require_operator(request)
        return {"versions": request.app.state.deployment.available_versions()}

    @router.get("/api/updates/history")
    def api_updates_history(request: Request) -> Any:
        _require_operator(request)
        return {"deployments": request.app.state.deployment.history(25)}

    @router.get("/api/updates/deploy-status")
    def api_updates_deploy_status(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.deployment.deploy_status() or {"status": "idle"}

    # Safe Mode step 4 — Deploy (privileged; locked; runs live in the background).
    @router.post("/api/updates/deploy")
    def api_updates_deploy(request: Request, payload: dict | None = None) -> Any:
        _require_operator(request)
        body = payload or {}
        deployment = request.app.state.deployment
        # Deployment lock — refuse a second concurrent deploy (Objective 4).
        if deployment.is_deploying:
            active = deployment.deploy_status() or {}
            raise HTTPException(status_code=409, detail={
                "message": "Deployment already running.",
                "started_at": active.get("started_at"),
                "operator": active.get("operator"),
                "current_stage": active.get("current_stage")})
        state = get_state(request.app)
        operator = body.get("operator") or "operator"
        notes = body.get("notes")
        allow_dirty = bool(body.get("allow_dirty", False))
        state.add_log(f"Deployment requested by {operator}.")

        def worker() -> None:
            result = deployment.deploy(operator=operator, notes=notes, allow_dirty=allow_dirty)
            lvl = "info" if result.get("ok") else "error"
            tail = "" if result.get("ok") else f" — {result.get('error')}" + (
                " (rolled back)" if result.get("rollback_performed") else "")
            state.add_log(f"Deployment {result.get('status')}{tail}.", lvl)

        threading.Thread(target=worker, name="deploy", daemon=True).start()
        return {"status": "started"}

    @router.post("/api/updates/rollback")
    def api_updates_rollback(request: Request, payload: dict | None = None) -> Any:
        _require_operator(request)
        body = payload or {}
        state = get_state(request.app)
        state.add_log("Rollback requested.")
        result = request.app.state.deployment.rollback(
            operator=body.get("operator") or "operator",
            to_commit=body.get("to_commit"), notes=body.get("notes"))
        state.add_log(f"Rollback {result.get('status')}.",
                      "info" if result.get("ok") else "error")
        return result

    # --- Health dashboard (expanded — Sprint 40.1) ---
    @router.get("/api/health")
    def api_health(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.deployment.health()

    # --- Pipeline tab ---
    @router.get("/api/pipeline")
    def api_pipeline(request: Request) -> Any:
        _require_operator(request)
        return get_state(request.app).run

    # --- Marketing tab ---
    @router.get("/api/marketing")
    def api_marketing(request: Request) -> Any:
        _require_operator(request)
        s = request.app.state
        funnel = s.daily.traffic.funnel()
        dist = s.daily.distribution
        delivery = {
            "posted": s.db.count_marketing_assets_by_status("posted"),
            "pending": len(s.db.list_pending_marketing_assets(limit=1000)),
            "failed": s.db.count_marketing_assets_by_status("failed"),
            "skipped": s.db.count_marketing_assets_by_status("skipped"),
        }
        channel_ready = {ch: dist.can_distribute(ch)
                         for ch in ("instagram", "facebook", "blog", "email")}
        shopify_pubs = [p for p in s.db.list_publications() if p.get("platform") == "shopify"]
        return {
            "channels": {ch: s.db.count_marketing_assets(channel=ch)
                         for ch in ("pinterest", "instagram", "facebook", "blog", "email")},
            "pins_scheduled": len(s.db.list_pin_schedule(status="scheduled")),
            "pins_posted": len(s.db.list_pin_schedule(status="posted")),
            "delivery": delivery,
            "channel_ready": channel_ready,
            "shopify": {"configured": s.daily.shopify.can_publish,
                        "products": len(shopify_pubs)},
            "funnel": funnel,
            "keywords": s.daily.etsy_intelligence.keyword_performance(20),
        }

    # --- Channels: readiness + connection tests (Sprint 41.1) ---
    @router.get("/api/channels")
    def api_channels(request: Request) -> Any:
        _require_operator(request)
        s = request.app.state
        d = s.daily.distribution
        return {"channels": [
            {"key": "etsy", "label": "Etsy", "kind": "sales",
             "configured": bool(getattr(s.daily.etsy, "is_configured", False)),
             "env": "ETSY_CLIENT_ID / ETSY_ACCESS_TOKEN"},
            {"key": "shopify", "label": "Shopify", "kind": "sales",
             "configured": s.daily.shopify.can_publish,
             "env": "SHOPIFY_STORE_DOMAIN / SHOPIFY_ADMIN_TOKEN"},
            {"key": "pinterest", "label": "Pinterest", "kind": "marketing",
             "configured": bool(getattr(s.daily.pinterest, "can_publish", False)),
             "env": "PINTEREST_ACCESS_TOKEN / PINTEREST_BOARD_ID"},
            {"key": "facebook", "label": "Facebook", "kind": "marketing",
             "configured": d.facebook.can_publish, "testable": True,
             "env": "META_PAGE_ACCESS_TOKEN / FACEBOOK_PAGE_ID"},
            {"key": "instagram", "label": "Instagram", "kind": "marketing",
             "configured": d.instagram.can_publish, "testable": True,
             "env": "META_PAGE_ACCESS_TOKEN / INSTAGRAM_USER_ID"},
            {"key": "email", "label": "Email", "kind": "marketing",
             "configured": d.email.can_publish, "testable": True,
             "env": "SMTP_HOST / EMAIL_FROM / EMAIL_TO"},
            {"key": "blog", "label": "Blog", "kind": "marketing",
             "configured": d.can_distribute("blog"),
             "env": "SHOPIFY_* + shopify.blog_id"},
        ]}

    @router.post("/api/channels/test")
    def api_channels_test(request: Request) -> Any:
        _require_operator(request)
        s = request.app.state
        d = s.daily.distribution
        state = get_state(request.app)
        state.add_log("Testing channel connections…")
        results = {
            "shopify": s.daily.shopify.connector.test_connection(),
            "facebook": d.facebook.test_connection(),
            "instagram": d.instagram.test_connection(),
            "email": d.email.test_connection(),
        }
        for ch, r in results.items():
            state.add_log(f"  {ch}: {'OK' if r.get('ok') else r.get('detail')}",
                          "info" if r.get("ok") else "warn")
        return results

    @router.post("/api/channels/email/test-send")
    def api_channels_email_test(request: Request) -> Any:
        _require_operator(request)
        r = request.app.state.daily.distribution.email.send_test()
        get_state(request.app).add_log(
            f"Test email: {'sent' if r.get('ok') else r.get('reason')}",
            "info" if r.get("ok") else "warn")
        return r

    @router.post("/api/channels/distribute")
    def api_channels_distribute(request: Request) -> Any:
        _require_operator(request)
        r = request.app.state.daily.distribution.distribute()
        get_state(request.app).add_log(
            f"Distribution run: {r['posted']} posted, {r['skipped']} skipped, "
            f"{r['failed']} failed.")
        return r

    @router.post("/api/channels/retry")
    def api_channels_retry(request: Request, payload: dict | None = None) -> Any:
        _require_operator(request)
        channel = (payload or {}).get("channel")
        r = request.app.state.daily.distribution.retry_failed(channel)
        get_state(request.app).add_log(
            f"Retried {r.get('requeued', 0)} failed delivery(ies): {r['posted']} posted.")
        return r

    # --- Integration Manager (Sprint 41.1) ---
    @router.get("/api/integrations")
    def api_integrations(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.integrations.describe()

    @router.get("/api/integrations/{key}")
    def api_integration(request: Request, key: str, reveal: int = 0) -> Any:
        _require_operator(request)
        detail = request.app.state.integrations.detail(key, reveal=bool(reveal))
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown integration.")
        return detail

    @router.post("/api/integrations/{key}/test")
    def api_integration_test(request: Request, key: str) -> Any:
        _require_operator(request)
        r = request.app.state.integrations.test(key)
        get_state(request.app).add_log(
            f"Integration test {key}: {'OK' if r.get('ok') else r.get('detail')}",
            "info" if r.get("ok") else "warn")
        return r

    @router.post("/api/integrations/{key}/save")
    def api_integration_save(request: Request, key: str, payload: dict | None = None) -> Any:
        _require_operator(request)
        body = payload or {}
        try:
            card = request.app.state.integrations.save(
                key, body.get("values", {}), operator=body.get("operator") or "operator")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        get_state(request.app).add_log(f"Integration {key} credentials updated.")
        return card

    @router.post("/api/integrations/{key}/action/{action}")
    def api_integration_action(request: Request, key: str, action: str) -> Any:
        _require_operator(request)
        s = request.app.state
        state = get_state(request.app)
        if key == "email" and action == "send_test":
            r = s.daily.distribution.email.send_test()
            state.add_log(f"Test email: {'sent' if r.get('ok') else r.get('reason')}")
            s.db.insert_integration_event({"integration": "email", "kind": "publish",
                                           "status": "ok" if r.get("ok") else "failed",
                                           "detail": "test email"})
            return r
        if key == "shopify" and action == "publish_test_product":
            listing = {"title": "ONASSIS Test Product", "description": "Connection test.",
                       "tags": ["test"], "price": 1.0, "product_id": "onassis-test"}
            try:
                r = s.daily.shopify.connector.publish_product(listing, active=False)
                state.add_log(f"Shopify test product created: {r.get('product_id')}")
                s.db.insert_integration_event({"integration": "shopify", "kind": "publish",
                                               "status": "ok", "detail": "test product"})
                return {"ok": True, **r}
            except Exception as exc:  # noqa: BLE001
                s.db.insert_integration_event({"integration": "shopify", "kind": "publish",
                                               "status": "failed", "detail": str(exc)})
                return {"ok": False, "detail": str(exc)}
        if key == "shopify" and action == "list_blogs":
            try:
                blogs = s.daily.shopify.connector.list_blogs()
                return {"ok": True, "blogs": blogs}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "detail": str(exc)}
        if key == "etsy" and action == "reconnect_oauth":
            return {"ok": True, "redirect": "/etsy/oauth/login"}
        if key == "pinterest" and action == "reconnect_oauth":
            resolved = s.integrations.resolve("pinterest")
            if not (resolved.get("app_id") and resolved.get("redirect_uri")):
                return {"ok": False, "detail": "Set the App ID, App Secret and Redirect "
                        "URI first (Pinterest, production app), then Reconnect OAuth."}
            return {"ok": True, "redirect": "/pinterest/oauth/login"}
        raise HTTPException(status_code=400,
                            detail=f"No action '{action}' for '{key}'.")

    # --- Approval queue (auto / needs-review / blocked) ---
    @router.get("/api/approvals")
    def api_approvals(request: Request) -> Any:
        _require_operator(request)
        return _approvals(request.app.state)

    # --- System tab ---
    @router.get("/api/system")
    def api_system(request: Request) -> Any:
        _require_operator(request)
        s = request.app.state
        config = s.config
        return {
            "git_commit": _git_commit(),
            "environment": getattr(config, "environment", "development"),
            "service": _service_light("onassis"),
            "deployments": s.db.list_daily_runs(10),
            "env_masked": _masked_env(config),
            "logs": _recent_logs(),
            "config_sections": sorted(k for k in vars(config)
                                      if isinstance(getattr(config, k), dict)),
        }

    # --- CMO: strategy + campaign calendar (Sprint 42 Phase 3) ---
    @router.get("/api/cmo")
    def api_cmo(request: Request) -> Any:
        _require_operator(request)
        cmo = request.app.state.cmo
        return {"strategy": cmo.strategy(), "calendar": cmo.calendar_view(),
                "budget": cmo.budget_allocation(100.0)}

    @router.post("/api/cmo/schedule")
    def api_cmo_schedule(request: Request) -> Any:
        _require_operator(request)
        r = request.app.state.cmo.schedule_pending()
        get_state(request.app).add_log(
            f"CMO scheduled {r.get('scheduled', 0)} marketing asset(s).")
        return r

    @router.get("/api/marketing/learning")
    def api_marketing_learning(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.marketing_learning.digest()

    # --- Commercial Intelligence (Sprint 42 Phase 1) ---
    @router.get("/api/commercial")
    def api_commercial(request: Request) -> Any:
        _require_operator(request)
        c = request.app.state.commercial
        return {"ceo": c.ceo_commercial(), "channels": c.channel_performance(),
                "attribution": c.attribution()}

    @router.get("/api/commercial/products")
    def api_commercial_products(request: Request) -> Any:
        _require_operator(request)
        return {"products": request.app.state.commercial.product_analytics()}

    # --- CFO: AI cost dashboard, ROAI, optimisation (Sprint 42.2) ---
    @router.get("/api/cfo")
    def api_cfo(request: Request) -> Any:
        _require_operator(request)
        cfo = request.app.state.cfo
        return {"dashboard": cfo.dashboard(), "roai": cfo.roai(),
                "optimisation": cfo.optimisation_report()}

    @router.get("/api/cfo/product/{sku}")
    def api_cfo_product(request: Request, sku: str) -> Any:
        _require_operator(request)
        return {"sku": sku,
                "requests": request.app.state.db.ai_cost_by_product(sku)}

    # --- Catalogue & Portfolio (Sprint 44) ---
    @router.get("/api/catalogue")
    def api_catalogue(request: Request) -> Any:
        _require_operator(request)
        cm = request.app.state.catalogue
        return {**cm.catalogue_dashboard(),
                "retirement_candidates": cm.retirement_candidates()}

    @router.get("/api/collections")
    def api_collections(request: Request) -> Any:
        _require_operator(request)
        return {"collections": request.app.state.catalogue.collection_dashboard()}

    @router.post("/api/catalogue/sync-gelato")
    def api_sync_gelato(request: Request, payload: dict | None = None) -> Any:
        """Pull the real Gelato product catalogue into ONASSIS (Sprint 46) so the
        product list auto-expands with verified UIDs — no manual entry."""
        _require_operator(request)
        from onassis.gelato_catalogue import GelatoCatalogueSync, GelatoError
        state = request.app.state
        body = payload or {}
        sync = GelatoCatalogueSync(state.config, state.db)
        if not sync.is_configured:
            return {"ok": False, "detail": "Gelato API key not set (GELATO_API_KEY)."}
        try:
            result = sync.sync(catalogs=body.get("catalogs") or None,
                               per_catalog=int(body.get("per_catalog", 1)),
                               available=bool(body.get("available", True)))
        except GelatoError as exc:  # network / API error — reported, never fatal
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, **result, "total": state.db.count_gelato_catalogue()}

    @router.get("/api/catalogue/gelato")
    def api_gelato_catalogue(request: Request) -> Any:
        _require_operator(request)
        db = request.app.state.db
        return {"products": db.list_gelato_catalogue(),
                "count": db.count_gelato_catalogue(),
                "available": db.count_gelato_catalogue(available_only=True)}

    # --- Short-form video content engine (Sprint 48) ---
    @router.get("/api/content/reels")
    def api_content_reels(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.content.dashboard()

    @router.post("/api/content/reels/build")
    def api_build_reels(request: Request, payload: dict | None = None) -> Any:
        """Generate a queue of short-form clips (runs in the background — rendering
        video takes time). Products land in the exports/reels queue as drafts."""
        _require_operator(request)
        state = get_state(request.app)
        if state.is_running:
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        body = payload or {}
        limit = int(body.get("limit", 20))
        engine = request.app.state.content
        state.begin_run()
        state.add_log(f"CONTENT: building up to {limit} short-form clip(s).")

        def worker() -> None:
            handler = _RunLogHandler(state)
            root = logging.getLogger("onassis")
            root.addHandler(handler)
            try:
                result = engine.build_batch(limit=limit)
                state.end_run("completed", result)
                state.add_log(
                    f"CONTENT: built {result['built']} clip(s); "
                    f"{result.get('skipped', 0)} product(s) already had them, "
                    f"{result.get('no_package', 0)} missing a listing package.")
            except Exception as exc:  # never crash the server on a render failure
                state.end_run("failed", {"error": str(exc)})
                state.add_log(f"CONTENT build crashed: {exc}", "error")
                log.exception("Content build failed")
            finally:
                root.removeHandler(handler)

        threading.Thread(target=worker, name="content-build", daemon=True).start()
        return {"status": "started", "limit": limit}

    @router.post("/api/content/reels/distribute")
    def api_distribute_reels(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.content.distribute()

    @router.get("/api/content/blog")
    def api_blog_status(request: Request) -> Any:
        """Why the Shopify blog is or isn't publishing — the exact diagnosis,
        plus the actual article list (title, status, clickable storefront/admin
        links) so the operator can SEE and verify what was posted."""
        _require_operator(request)
        s = request.app.state
        return {**_blog_status(s), "articles": _blog_articles(s)}

    @router.post("/api/content/blog/generate")
    def api_generate_blog(request: Request, payload: dict | None = None) -> Any:
        """Generate SEO blog articles for products that don't have any (no LLM
        cost, deterministic). Then 'Publish blog now' ships them to Shopify."""
        _require_operator(request)
        s = request.app.state
        limit = int((payload or {}).get("limit", 1000))   # cover the whole catalogue
        result = s.content.generate_blog(limit=limit)
        # Explain a zero so a click never looks like it did nothing.
        made = result.get("generated", 0)
        if made:
            note = f"generated {made} article set(s)."
        elif result.get("skipped_existing"):
            note = (f"nothing new — all {result.get('skipped_existing')} product(s) "
                    "already have articles. Click 'Publish blog now' to post them.")
        elif not result.get("products"):
            note = "no active products to write about yet."
        else:
            note = "no eligible products."
        result["message"] = note
        get_state(request.app).add_log(f"Blog: {note}")
        return {**result, "diagnosis": _blog_status(s)}

    @router.post("/api/content/blog/publish")
    def api_publish_blog(request: Request, payload: dict | None = None) -> Any:
        """Publish all pending Shopify blog articles now (ignores the schedule).
        Returns the exact per-article outcome — the Shopify URL on success, or the
        error on failure. ``generate: true`` first backfills missing articles."""
        _require_operator(request)
        s = request.app.state
        if (payload or {}).get("generate"):
            s.content.generate_blog(limit=1000)   # cover the whole backlog
        # Un-stick articles that failed or were skipped earlier (e.g. Shopify
        # wasn't connected, or an earlier bad URL) so a retry actually posts them.
        requeued = s.db.reset_failed_marketing_assets(channel="blog", include_skipped=True)
        pending = s.db.list_marketing_assets(channel="blog")
        pending = [a for a in pending if (a.get("status") or "pending") == "pending"]
        result = s.daily.distribution.distribute(channels=["blog"], due_on="2999-12-31")
        # Read back each blog asset's outcome so the operator sees exactly what
        # happened (URL published, or the reason it didn't).
        details = []
        for a in s.db.list_marketing_assets(channel="blog")[:25]:
            details.append({"product_key": a.get("product_key"),
                            "status": a.get("status") or "pending",
                            "url": a.get("delivery_ref"),
                            "error": a.get("delivery_error")})
        get_state(request.app).add_log(
            f"Blog publish: {result.get('posted', 0)} posted, "
            f"{result.get('skipped', 0)} skipped, {result.get('failed', 0)} failed.")
        return {**result, "attempted": len(pending), "requeued": requeued,
                "details": details, "diagnosis": _blog_status(s)}

    @router.post("/api/content/blog/rewrite-live")
    def api_rewrite_live_blog(request: Request) -> Any:
        """Rewrite existing live blog articles in place — Shopify product link,
        featured image and HTML body — bringing older posts up to date."""
        _require_operator(request)
        s = request.app.state
        result = s.content.rewrite_live_blog_articles()
        get_state(request.app).add_log(
            f"Blog rewrite: {result.get('rewritten', 0)} updated, "
            f"{result.get('skipped', 0)} unchanged of {result.get('live_count', 0)} "
            f"on blog {result.get('blog_id') or '?'}. {result.get('reason', '')}")
        return {**result, "diagnosis": _blog_status(s), "articles": _blog_articles(s)}

    @router.post("/api/content/blog/fill-schedule")
    def api_fill_blog_schedule(request: Request, payload: dict | None = None) -> Any:
        """Fill the forward blog schedule (evergreen) so there are always upcoming
        posts — cycles the catalogue's content across the next horizon of days."""
        _require_operator(request)
        s = request.app.state
        per_day = (payload or {}).get("per_day")
        result = s.content.refill_blog_schedule(
            per_day=int(per_day) if per_day else None)
        get_state(request.app).add_log(
            f"Blog schedule filled: +{result.get('created', 0)} post(s), "
            f"{result.get('scheduled', 0)} queued through {result.get('last')}.")
        return {**result, "diagnosis": _blog_status(s), "articles": _blog_articles(s)}

    @router.get("/api/content/blog/diagnose")
    def api_diagnose_blog(request: Request) -> Any:
        """Read the live Shopify truth — every blog + article counts — so we can
        see WHERE the posts actually landed when 'there's just no blog'."""
        _require_operator(request)
        s = request.app.state
        try:
            return {"ok": True, **s.daily.shopify.connector.blog_diagnostics()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @router.post("/api/content/blog/republish")
    def api_republish_blog(request: Request) -> Any:
        """Make every article on the store's blog visible now — recovery for posts
        stuck as hidden/scheduled (a future published_at from clock skew)."""
        _require_operator(request)
        s = request.app.state
        try:
            result = s.daily.shopify.connector.republish_hidden()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "diagnosis": _blog_status(s)}
        get_state(request.app).add_log(f"Blog visibility fix: {result.get('note', '')}")
        return {"ok": True, **result, "diagnosis": _blog_status(s)}

    @router.post("/api/content/blog/schedule")
    def api_schedule_blog(request: Request, payload: dict | None = None) -> Any:
        """Generate articles for the whole catalogue and schedule the full lot to
        drip out over future days (``per_day`` a day, default 2) — so the daily
        marketing run publishes a steady stream instead of dumping them at once."""
        _require_operator(request)
        s = request.app.state
        per_day = int((payload or {}).get("per_day", 2))
        start = (payload or {}).get("start")
        result = s.content.schedule_blog_backlog(per_day=per_day, start=start)
        get_state(request.app).add_log(
            f"Blog backlog scheduled: {result.get('scheduled', 0)} article set(s), "
            f"{result.get('per_day')}/day through {result.get('last_date')}.")
        return {**result, "diagnosis": _blog_status(s)}

    @router.post("/api/run-marketing")
    def api_run_marketing(request: Request) -> Any:
        """Run the promotion push only (pins + due channel assets) — decoupled
        from product creation, so marketing goes out daily without a production
        run. Cheap: no new products, no LLM/image cost."""
        _require_operator(request)
        state = get_state(request.app)
        if state.mode != RUNNING:
            raise HTTPException(status_code=409,
                                detail=f"Business is {state.mode} — resume it first.")
        if state.is_running:
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        daily = request.app.state.daily
        state.begin_run()
        state.add_log("MARKETING PUSH requested — pinning + channel distribution.")

        def worker() -> None:
            handler = _RunLogHandler(state)
            root = logging.getLogger("onassis")
            root.addHandler(handler)
            try:
                result = daily.run_marketing()
                state.end_run("completed", result)
                state.add_log(f"MARKETING PUSH finished: {result.get('pins_posted', 0)} "
                              "pin(s) posted.")
            except Exception as exc:  # never crash the server on a push failure
                state.end_run("failed", {"error": str(exc)})
                state.add_log(f"MARKETING PUSH crashed: {exc}", "error")
                log.exception("Marketing push failed")
            finally:
                root.removeHandler(handler)

        threading.Thread(target=worker, name="run-marketing", daemon=True).start()
        return {"status": "started"}

    @router.post("/api/traffic/pin-all")
    def api_pin_all(request: Request, payload: dict | None = None) -> Any:
        """Pin every product to the Pinterest board in one go (runs in the
        background — bulk posting is slow and rate-limited)."""
        _require_operator(request)
        state = get_state(request.app)
        if state.is_running:
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        traffic = request.app.state.traffic
        if not traffic.pinterest.can_publish:
            return {"ok": False, "detail": "Pinterest not connected — set the access "
                    "token and board id on Integrations → Pinterest."}
        body = payload or {}
        limit = body.get("limit")
        limit = int(limit) if limit not in (None, "") else None
        require_link = bool(body.get("require_link", False))
        state.begin_run()
        state.add_log("PINTEREST: pinning all products to the board…")

        def worker() -> None:
            handler = _RunLogHandler(state)
            root = logging.getLogger("onassis")
            root.addHandler(handler)
            try:
                result = traffic.publish_all_products(limit=limit, require_link=require_link)
                state.end_run("completed", result)
                state.add_log(f"PINTEREST: posted {result['posted']}/{result['total']} "
                              f"pin(s) ({result['failed']} failed, {result['no_image']} "
                              "without a hero image).")
            except Exception as exc:  # never crash the server on a bulk-pin failure
                state.end_run("failed", {"error": str(exc)})
                state.add_log(f"PINTEREST bulk pin crashed: {exc}", "error")
                log.exception("Pinterest bulk pin failed")
            finally:
                root.removeHandler(handler)

        threading.Thread(target=worker, name="pin-all", daemon=True).start()
        return {"status": "started", "limit": limit}

    @router.post("/api/catalogue/compile")
    def api_compile_catalogue(request: Request, payload: dict | None = None) -> Any:
        """Build a whole catalogue in one event (Sprint 46) — runs in the
        background, bounded by a $ budget cap; products land as private drafts."""
        _require_operator(request)
        state = get_state(request.app)
        if state.mode != RUNNING:
            raise HTTPException(status_code=409,
                                detail=f"Business is {state.mode} — resume it first.")
        if state.is_running:
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        body = payload or {}
        budget = body.get("budget_usd")
        budget = float(budget) if budget not in (None, "") else None
        max_products = body.get("max_products")
        max_products = int(max_products) if max_products not in (None, "") else None
        mode = str(body.get("mode") or "auto_draft")
        daily = request.app.state.daily
        state.begin_run()
        state.add_log(f"COMPILE CATALOGUE requested — budget=${budget}, "
                      f"max={max_products}, mode={mode}.")

        def worker() -> None:
            handler = _RunLogHandler(state)
            root = logging.getLogger("onassis")
            root.addHandler(handler)
            try:
                from onassis.catalogue_compiler import CatalogueCompiler
                result = CatalogueCompiler(
                    request.app.state.config, request.app.state.db, daily).compile(
                    budget_usd=budget, max_products=max_products, mode=mode)
                state.end_run("completed", result)
                state.add_log(f"COMPILE finished: {result['built']} product(s), "
                              f"${result['spend_usd']} spent (stopped: {result['stopped']}).")
            except Exception as exc:  # never crash the server on a compile failure
                state.end_run("failed", {"error": str(exc)})
                state.add_log(f"COMPILE crashed: {exc}", "error")
                log.exception("Catalogue compile failed")
            finally:
                root.removeHandler(handler)

        threading.Thread(target=worker, name="compile-catalogue", daemon=True).start()
        return {"status": "started", "budget_usd": budget,
                "max_products": max_products, "mode": mode}

    # --- Marketing distribution via Make.com (Sprint 43) ---
    @router.get("/api/distribution")
    def api_distribution(request: Request) -> Any:
        _require_operator(request)
        return request.app.state.distribution.dashboard()

    @router.post("/api/distribution/send/{sku}")
    def api_distribution_send(request: Request, sku: str) -> Any:
        _require_operator(request)
        s = request.app.state
        product = s.db.get_product_by_sku(sku)
        if not product:
            raise HTTPException(status_code=404, detail="Unknown product.")
        result = s.distribution.distribute(product.get("campaign_id"),
                                           product.get("product_key"), product_id=sku)
        get_state(request.app).add_log(
            f"Distribution for {sku} -> Make.com: {result.get('status')}.",
            "info" if result.get("ok") else "error")
        return result

    @router.post("/api/distribution/{camp_id}/retry")
    def api_distribution_retry(request: Request, camp_id: int) -> Any:
        _require_operator(request)
        result = request.app.state.distribution.retry(camp_id)
        get_state(request.app).add_log(
            f"Retried marketing campaign #{camp_id}: {result.get('status')}.")
        return result

    @router.post("/api/distribution/feedback")
    def api_distribution_feedback(request: Request, payload: dict | None = None) -> Any:
        # Make.com posts per-channel statuses back here (Obj 5). Operator-authed;
        # configure the API key header in the Make scenario.
        _require_operator(request)
        body = payload or {}
        statuses = {k: v for k, v in body.items()
                    if k not in ("campaign_id", "product_id")}
        return request.app.state.distribution.record_feedback(
            body.get("campaign_id"), statuses, product_id=body.get("product_id"))

    # --- Production health dashboard (Sprint 41.2, Obj 13) ---
    @router.get("/api/production-health")
    def api_production_health(request: Request) -> Any:
        _require_operator(request)
        return _production_health(request.app.state)

    # --- Self-healing reconciliation (Sprint 41.2, Obj 1/2) ---
    @router.get("/api/system/reconcile")
    def api_reconcile_status(request: Request) -> Any:
        _require_operator(request)
        return getattr(request.app.state, "reconcile_summary", {"repaired": 0})

    @router.post("/api/system/reconcile")
    def api_reconcile_run(request: Request) -> Any:
        _require_operator(request)
        from onassis.self_healing import reconcile
        s = request.app.state
        summary = reconcile(s.config, s.db)
        s.reconcile_summary = summary
        get_state(request.app).add_log(
            f"Self-healing reconcile: {summary.get('repaired', 0)} issue(s) repaired.")
        return summary

    # --- Control actions (business + ops scripts) ---
    @router.post("/api/control/{action}")
    def api_control(request: Request, action: str) -> Any:
        _require_operator(request)
        state = get_state(request.app)
        if action == "pause":
            state.mode = PAUSED
            state.add_log("Business PAUSED by operator.", "warn")
            return {"mode": state.mode}
        if action == "resume":
            state.mode = RUNNING
            state.add_log("Business RESUMED by operator.")
            return {"mode": state.mode}
        if action == "emergency-stop":
            state.mode = STOPPED
            state.add_log("EMERGENCY STOP engaged — no new runs will start.", "error")
            return {"mode": state.mode}
        # Lifecycle actions go through the Deployment Service — the Operations
        # Centre never executes shell commands directly (Sprint 40.1).
        deployment = request.app.state.deployment
        if action == "backup":
            state.add_log("Backup requested.")
            return deployment.backup()
        if action == "health":
            return deployment.health()
        if action == "restart":
            state.add_log("Service restart requested.")
            return deployment.restart()
        if action == "deploy":
            state.add_log("Deploy requested via control — running full deployment.")
            return deployment.deploy(operator="operator")
        if action == "rollback":
            state.add_log("Rollback requested via control.")
            return deployment.rollback(operator="operator")
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")

    return router


# =====================================================================
# Helpers (read-only aggregation)
# =====================================================================

def _run_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result.get(k) for k in (
        "status", "products_launched", "products_live", "pins_posted",
        "assets_created", "first_draft_at")}


def _product_context(state: Any) -> dict[str, Any]:
    """Load the lookup tables the product dashboard needs, once."""
    db = state.db
    return {
        "perf": {p["product_key"]: p for p in db.list_product_performance()},
        "approvals": {a["sku"]: a for a in db.list_product_approvals()},
        "campaigns": {c["id"]: c for c in db.list_campaigns()},
    }


def _product_row(state: Any, p: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """One fully-resolved product dashboard row (Sprint 40, Objective 6).

    The status comes from the Product Status Engine — the single source of truth
    — never from the ``active`` flag. Etsy status reflects the real publication.
    """
    db = state.db
    sku = p.get("sku")
    key = p.get("product_key")
    cid = p.get("campaign_id")
    pf = ctx["perf"].get(key, {})
    approval = ctx["approvals"].get(sku)
    pub = db.get_latest_publication(cid, "etsy", product_id=sku) if cid else None
    marketing = db.list_marketing_assets(product_key=key) if key else []
    units = int(pf.get("units_sold", 0) or 0)
    st = derive_product_status(product=p, publication=pub, approval=approval,
                               marketing_count=len(marketing), units_sold=units)

    # Etsy facts (views/url) come from the imported listing, when there is one.
    views, listing_url = 0, ""
    if st.listing_id and is_valid_listing_id(st.listing_id):
        try:
            listing = db.get_etsy_listing(int(st.listing_id))
        except (TypeError, ValueError):
            listing = None
        if listing:
            views = int(listing.get("views", 0) or 0)
            listing_url = listing.get("url") or ""

    campaign = ctx["campaigns"].get(cid, {})
    collection = collection_name(None, campaign) if campaign.get("name") else ""
    marketing_status = ("live" if marketing else
                        ("pending" if st.status in ("live", "marketing", "tracking")
                         else "none"))
    # Shopify (second sales channel) status for this product.
    shop_pub = db.get_latest_publication(cid, "shopify", product_id=sku) if cid else None
    shopify_state = _pub_label(shop_pub, live_word="Published")
    return {
        "sku": sku, "name": p.get("name") or key, "product_key": key,
        "type": key, "campaign_id": cid, "campaign": campaign.get("name") or "",
        "collection": collection,
        "status": st.status, "status_label": st.label,
        "reason": st.reason, "retryable": st.retryable,
        "etsy_status": st.label, "listing_id": st.listing_id, "listing_url": listing_url,
        "shopify_status": shopify_state["label"], "shopify_state": shopify_state["status"],
        "on_etsy": st.status in ("draft_created", "live", "marketing", "tracking"),
        "on_shopify": shopify_state["status"] in ("draft", "live"),
        "any_failed": st.status == "failed" or shopify_state["status"] == "failed",
        "approval": (approval or {}).get("decision", "awaiting"),
        "operator": (approval or {}).get("operator") or "",
        "marketing_status": marketing_status,
        "views": views,
        "units": units, "sales": units,
        "revenue": float(pf.get("gross_revenue", 0) or 0),
        "net_profit": float(pf.get("net_profit", 0) or 0),
        "launched_at": p.get("launched_at") or p.get("created_at"),
        "updated_at": (approval or {}).get("updated_at") or p.get("launched_at")
                      or p.get("created_at"),
    }


def _ensure_listing_package(state: Any, campaign_id: int, product_key: str) -> dict[str, Any]:
    """Build the per-product listing package on demand if it isn't on disk yet.

    The daily cycle only builds packages for a capped number of products per day
    (``portfolio.max_new_listings_per_day``), so a CEO-launched product an
    operator wants to publish *now* may have no package. Building here (real
    artwork + gallery + listing.json, exactly as the cycle does) turns
    "No listing package found — build it first" into a working publish, and
    produces the mock-ups that power the card preview. Idempotent: a no-op when
    the package already exists."""
    daily = state.daily
    folder = _exports_dir(state) / str(campaign_id) / str(product_key)
    if (folder / "listing.json").exists():
        return {"ok": True, "built": False}
    scores = [s for s in state.db.list_product_scores(campaign_id)
              if s.get("product_key") == product_key]
    # Prefer the CEO score; if none exists (an operator-approved product from a
    # cycle that never scored it), build a spec from the catalogue/product record
    # — the operator's approval is the gate here, not a CEO score.
    spec = scores[0] if scores else _spec_from_product(state, campaign_id, product_key)
    if spec is None:
        return {"ok": False, "reason": "No product found to build a listing from."}
    design_package = None
    opp = spec.get("opportunity_id")
    if opp:
        try:
            design_package = daily.design.get_package(opp)
        except Exception:  # design read is best-effort; the factory has fallbacks
            design_package = None
    try:
        from onassis.ai_accounting import cost_context
        with cost_context(product_id=f"{campaign_id}-{product_key}",
                          campaign_id=campaign_id, stage="Publish Products"):
            pkg = daily.listing_factory.export_product(campaign_id, spec,
                                                       design_package=design_package)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"Listing build failed: {exc}"}
    if pkg.get("status") != "ready":
        return {"ok": False, "reason": pkg.get("reason") or "Listing build did not complete."}
    return {"ok": True, "built": True}


def _cleanup_incomplete_products(state: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Archive every ACTIVE product that never actually published — the backlog
    of incomplete/placeholder products from failed cycles — so the operator can
    start fresh from a clean queue without burning AI credit trying to complete
    them. Nothing is deleted (archive = active 0); genuinely published products
    (a real Etsy/Shopify draft or live listing) are always kept."""
    db = state.db
    to_archive: list[dict[str, Any]] = []
    for p in db.list_products():
        if not p.get("active", 1):
            continue
        sku, cid = p.get("sku"), p.get("campaign_id")
        etsy = db.get_latest_publication(cid, "etsy", product_id=sku) if cid else None
        shop = db.get_latest_publication(cid, "shopify", product_id=sku) if cid else None
        # A product counts as "published" only when it has a REAL listing — a
        # draft/live status with a VALID listing id (matching what the operator
        # sees). A draft record with no valid id is a failed attempt, not a live
        # listing, so it should be archived.
        published = any(
            (pub or {}).get("status") in ("draft", "published", "live")
            and is_valid_listing_id((pub or {}).get("listing_id"))
            for pub in (etsy, shop))
        if not published:
            to_archive.append({"sku": sku, "name": p.get("name") or p.get("product_key")})
    if not dry_run:
        for item in to_archive:
            db.set_product_active(item["sku"], False)
    return {"archived": len(to_archive), "products": to_archive, "dry_run": dry_run}


def _blog_status(state: Any) -> dict[str, Any]:
    """Diagnose Shopify blog publishing: connection, Blog ID, asset counts, and
    the exact reason nothing is posting."""
    shop = state.config.shopify or {}
    assets = state.db.list_marketing_assets(channel="blog")
    by = {"posted": 0, "skipped": 0, "failed": 0, "pending": 0}
    last_reason = None
    scheduled_dates: list[str] = []
    for a in assets:
        st = a.get("status") or "pending"
        by[st] = by.get(st, 0) + 1
        if st in ("skipped", "failed") and a.get("delivery_error"):
            last_reason = a["delivery_error"]
        # A pending asset with a future date is scheduled to drip out later.
        if st == "pending" and a.get("scheduled_date"):
            scheduled_dates.append(a["scheduled_date"])
    try:
        connected = bool(state.daily.shopify.connector.can_publish)
    except Exception:
        connected = False
    blog_id = shop.get("blog_id")
    if not connected:
        diagnosis = "Shopify is not connected — connect it on Integrations → Shopify."
    elif not blog_id:
        diagnosis = ("No Blog ID set — go to Integrations → Shopify, click 'List blogs', "
                     "and set the Blog ID. This is the usual cause.")
    elif not assets:
        diagnosis = ("Shopify + Blog ID are set, but no blog articles have been generated "
                     "yet (marketing generates them for live products).")
    elif by["posted"]:
        diagnosis = f"{by['posted']} article(s) published."
    else:
        diagnosis = last_reason or "Blog articles are queued — click 'Publish blog now'."
    scheduled_dates.sort()
    schedule = {"count": len(scheduled_dates),
                "next": scheduled_dates[0] if scheduled_dates else None,
                "last": scheduled_dates[-1] if scheduled_dates else None}
    return {"connected": connected, "blog_id": str(blog_id) if blog_id else None,
            "assets": len(assets), **by, "last_reason": last_reason,
            "scheduled": schedule, "diagnosis": diagnosis}


def _blog_articles(state: Any, limit: int = 200) -> list[dict[str, Any]]:
    """The blog article list for the dashboard — one row per generated article
    set, with its status and the actual storefront/admin links so the operator
    can click through and confirm it is live (the missing 'can I see it?' bit).

    A posted article's storefront URLs are stored on the asset (delivery_ref,
    ' | '-joined); a not-yet-posted one shows its titles and a 'pending' badge."""
    shop = state.config.shopify or {}
    domain = (shop.get("store_domain") or "").replace("https://", "").strip("/")
    rows: list[dict[str, Any]] = []
    for a in state.db.list_marketing_assets(channel="blog")[:limit]:
        payload = a.get("payload") or {}
        articles = payload.get("articles") or ([payload] if payload else [])
        titles = [x.get("title") for x in articles if x.get("title")]
        ref = a.get("delivery_ref") or ""
        urls = [u.strip() for u in ref.split("|") if u.strip().startswith("http")]
        rows.append({
            "product_key": a.get("product_key"),
            "titles": titles or [a.get("product_key") or "Untitled"],
            "count": len(articles),
            "status": a.get("status") or "pending",
            "scheduled_date": a.get("scheduled_date"),   # YYYY-MM-DD or None (=asap)
            "posted_at": a.get("delivered_at"),
            "urls": urls,
            "admin": (f"https://{domain}/admin/articles" if domain else ""),
            "error": a.get("delivery_error"),
        })
    return rows


def _buildable_keys(state: Any) -> set[str]:
    """The product keys ONASSIS can actually build a listing for — i.e. those
    with a matching Gelato catalogue product. A product outside this set (the
    generic ``product`` key from a broken cycle) can never produce a listing."""
    try:
        return {c["key"] for c in state.daily.expansion.catalogue(include_unavailable=True)}
    except Exception:
        return set()


def _spec_from_product(state: Any, campaign_id: int, product_key: str) -> dict[str, Any] | None:
    """Build a listing-build spec from the catalogue definition + product record
    when there is no CEO product score (an operator-approved product). Returns
    None only when there is genuinely no product to build from."""
    try:
        cat = {c["key"]: c for c in state.daily.expansion.catalogue(include_unavailable=True)}
    except Exception:
        cat = {}
    entry = cat.get(product_key) or {}
    prod = next((p for p in state.db.list_products()
                 if p.get("campaign_id") == campaign_id
                 and p.get("product_key") == product_key), None)
    if not entry and prod is None:
        return None
    spec = {
        "product_key": product_key,
        "product_name": (prod or {}).get("name") or entry.get("name") or product_key,
        "production_cost": (prod or {}).get("production_cost") or entry.get("production_cost"),
        "retail_price": entry.get("retail_price"),
    }
    return spec


def _regenerate_mockups(state: Any, campaign_id: int, product_key: str) -> dict[str, Any]:
    """Rebuild a product's listing package from scratch — new artwork + gallery —
    so a failed/placeholder mockup can be replaced (P1, Regenerate Mockups).

    Deletes the existing package folder first so the build cannot reuse a stale
    placeholder, then re-runs the on-demand build and reports the mockup gate."""
    import shutil as _shutil
    from onassis.mockup_gate import listing_mockup_status

    folder = _exports_dir(state) / str(campaign_id) / str(product_key)
    if folder.exists():
        try:
            _shutil.rmtree(folder)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"Could not clear old package: {exc}"}
    built = _ensure_listing_package(state, campaign_id, product_key)
    if not built["ok"]:
        return {"ok": False, "reason": built["reason"]}
    listing = state.daily._listing_json(campaign_id, product_key)
    from onassis.mockup_gate import allow_local_for
    mq = listing_mockup_status(listing, allow_local=allow_local_for(state.config))
    return {"ok": mq["ok"], "mockup_status": mq["status"], "reason": mq["reason"],
            "passing": mq.get("passing", 0), "total": mq.get("total", 0)}


def _publish_shopify(state: Any, campaign_id: int, product_key: str) -> dict[str, Any]:
    """Publish a single product to Shopify from its listing package (with help)."""
    from onassis.failure_help import annotate

    daily = state.daily
    built = _ensure_listing_package(state, campaign_id, product_key)
    if not built["ok"]:
        return annotate({"status": "failed", "reason": built["reason"]})
    listing = daily._listing_json(campaign_id, product_key)
    if not listing:
        return annotate({"status": "failed", "reason": "No listing package to publish."})
    images = daily._product_images_dir(campaign_id, product_key)
    result = daily.shopify.publish(campaign_id, product_key, listing, images_dir=images)
    return annotate(result)


def _publish_all_channels(state: Any, campaign_id: int, product_key: str) -> dict[str, Any]:
    """Publish a product to Etsy and Shopify together; each is independent.

    Builds the listing package first if the product doesn't have one yet, so an
    operator can publish a launched product straight from the Approval Workspace."""
    from onassis.failure_help import annotate

    built = _ensure_listing_package(state, campaign_id, product_key)
    if not built["ok"]:
        reason = f"Could not prepare listing package: {built['reason']}"
        fail = annotate({"status": "failed", "reason": reason})
        return {"etsy": fail, "shopify": fail}
    etsy = annotate(state.daily.publisher.publish(campaign_id, product_key=product_key))
    shopify = _publish_shopify(state, campaign_id, product_key)
    return {"etsy": etsy, "shopify": shopify}


def _channel_stats(pubs: list[dict[str, Any]], *, live_word: str = "Live",
                   blog_articles: int | None = None) -> dict[str, Any]:
    """A symmetric per-sales-channel success panel (Sprint 42.1, Obj 3 & 9).

    Both Etsy and Shopify (and any future channel — Amazon, eBay) expose the
    same shape so the dashboard treats every channel identically."""
    published = sum(1 for p in pubs if p.get("status") in ("published", "live"))
    drafts = sum(1 for p in pubs if p.get("status") == "draft")
    failed = sum(1 for p in pubs if p.get("status") == "failed")
    ok, fail = published + drafts, failed
    # Timestamps of successful publishes, oldest→newest, for cadence + last publish.
    ts = sorted((p.get("created_at") or "") for p in pubs
                if p.get("status") in ("draft", "published", "live") and p.get("created_at"))
    avg_secs = None
    if len(ts) >= 2:
        gaps = []
        for a, b in zip(ts, ts[1:]):
            try:
                da = datetime.fromisoformat(a.replace("Z", "+00:00"))
                dbt = datetime.fromisoformat(b.replace("Z", "+00:00"))
                gaps.append((dbt - da).total_seconds())
            except (ValueError, TypeError):
                continue
        if gaps:
            avg_secs = round(sum(gaps) / len(gaps))
    return {
        "products_published": published,
        "drafts_created": drafts,
        "blog_articles": blog_articles,
        "failed": failed,
        "retry_queue": failed,          # failed publishes await a retry
        "success_rate": round(ok / (ok + fail) * 100) if (ok + fail) else None,
        "last_publish": ts[-1] if ts else None,
        "avg_publish_interval_secs": avg_secs,
        "live_word": live_word,
    }


def _production_health(state: Any) -> dict[str, Any]:
    """The operational heartbeat (Sprint 41.2, Obj 13; Sprint 42.1 Obj 3/9)."""
    db = state.db
    rows = _products(state)
    waiting = sum(1 for r in rows if r["status"] == "awaiting_approval")
    publishing = sum(1 for r in rows if r["status"] == "approved")
    pubs = db.list_publications()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _today(p: dict[str, Any]) -> bool:
        return (p.get("created_at") or "")[:10] == today

    def _rate(lst: list[dict[str, Any]]) -> int | None:
        ok = sum(1 for p in lst if p.get("status") in ("draft", "published", "live"))
        fail = sum(1 for p in lst if p.get("status") == "failed")
        return round(ok / (ok + fail) * 100) if (ok + fail) else None

    etsy = [p for p in pubs if p.get("platform") == "etsy"]
    shop = [p for p in pubs if p.get("platform") == "shopify"]
    # Verified Shopify Blog articles = marketing blog assets actually delivered.
    blog_published = sum(1 for a in db.list_marketing_assets(channel="blog")
                         if (a.get("status") or "").lower() == "posted")
    report = {}
    try:
        report = state.report.build() or {}
    except Exception:  # report is best-effort
        report = {}
    return {
        "products_waiting": waiting,
        "products_publishing": publishing,
        "published_today": sum(1 for p in pubs if _today(p)
                               and p.get("status") in ("draft", "published", "live")),
        "failed_today": sum(1 for p in pubs if _today(p) and p.get("status") == "failed"),
        "retries": sum(1 for p in pubs if int(p.get("attempts", 1) or 1) > 1),
        "success_rate": _rate(pubs),
        "etsy_success": _rate(etsy),
        "shopify_success": _rate(shop),
        "marketing_published": db.count_marketing_assets_by_status("posted"),
        "revenue_today": (report.get("revenue") or {}).get("today"),
        "profit_today": (report.get("profit") or {}).get("today_net"),
        # Symmetric per-channel success panels — Etsy ↔ Shopify (future: Amazon/eBay).
        "channels": {
            "etsy": _channel_stats(etsy, live_word="Live"),
            "shopify": _channel_stats(shop, live_word="Published",
                                      blog_articles=blog_published),
        },
    }


def _pub_label(pub: dict[str, Any] | None, *, live_word: str = "Live") -> dict[str, Any]:
    """Map a publication row to a channel status cell."""
    if not pub:
        return {"status": "not_created", "label": "—"}
    st = (pub.get("status") or "").lower()
    lid = pub.get("listing_id")
    if st in ("draft", "published") and is_valid_listing_id(lid):
        return {"status": "draft", "label": "Draft Created" if st == "draft" else live_word,
                "ref": str(lid)}
    if st == "live" and is_valid_listing_id(lid):
        return {"status": "live", "label": live_word, "ref": str(lid)}
    if st == "failed":
        return {"status": "failed", "label": "Failed"}
    return {"status": st or "unknown", "label": (st or "unknown").title()}


def _delivery_label(asset: dict[str, Any] | None, *, sent_word: str = "Posted") -> dict[str, Any]:
    if not asset:
        return {"status": "not_created", "label": "—"}
    st = (asset.get("status") or "pending").lower()
    return {
        "posted": {"status": "posted", "label": sent_word, "ref": asset.get("delivery_ref")},
        "skipped": {"status": "skipped", "label": "Skipped",
                    "ref": asset.get("delivery_error")},
        "failed": {"status": "failed", "label": "Failed", "ref": asset.get("delivery_error")},
        "pending": {"status": "pending", "label": "Generated"},
    }.get(st, {"status": st, "label": st.title()})


def _product_channels(state: Any, product: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-product Sales Channels matrix — one glance shows where a product
    exists: Etsy · Shopify · Pinterest · Facebook · Instagram · Email · Blog."""
    db = state.db
    cid = product.get("campaign_id")
    key = product.get("product_key")
    sku = product.get("sku")
    etsy = db.get_latest_publication(cid, "etsy", product_id=sku) if cid else None
    shopify = db.get_latest_publication(cid, "shopify", product_id=sku) if cid else None
    pins = db.list_pin_schedule(product_key=key) if key else []
    posted_pins = [p for p in pins if p.get("status") == "posted"]
    pin_cell = ({"status": "posted", "label": f"Posted ({len(posted_pins)})"} if posted_pins
                else ({"status": "pending", "label": f"Scheduled ({len(pins)})"} if pins
                      else {"status": "not_created", "label": "—"}))
    latest_asset: dict[str, dict[str, Any]] = {}
    for a in db.list_marketing_assets(product_key=key) if key else []:
        latest_asset.setdefault(a["channel"], a)   # list is newest-first
    rows = [
        {"channel": "Etsy", **_pub_label(etsy)},
        {"channel": "Shopify", **_pub_label(shopify, live_word="Published")},
        {"channel": "Shopify Blog", **_delivery_label(latest_asset.get("blog"),
                                                      sent_word="Published")},
        {"channel": "Pinterest", **pin_cell},
        {"channel": "Facebook", **_delivery_label(latest_asset.get("facebook"))},
        {"channel": "Instagram", **_delivery_label(latest_asset.get("instagram"))},
        {"channel": "TikTok", **_delivery_label(latest_asset.get("tiktok"),
                                                sent_word="Posted")},
        {"channel": "Email", **_delivery_label(latest_asset.get("email"), sent_word="Sent")},
    ]
    return rows


# Sales-channel filters (Sprint 41.2, Obj 7) — distinct from lifecycle filters.
_CHANNEL_FILTERS = {
    "etsy": lambda r: r["on_etsy"],
    "shopify": lambda r: r["on_shopify"],
    "both": lambda r: r["on_etsy"] and r["on_shopify"],
    "failed": lambda r: r["any_failed"],
}


def _products(state: Any, filter_bucket: str | None = None) -> list[dict[str, Any]]:
    ctx = _product_context(state)
    rows = [_product_row(state, p, ctx) for p in state.db.list_products()]
    if filter_bucket and filter_bucket != "all":
        if filter_bucket in _CHANNEL_FILTERS:
            rows = [r for r in rows if _CHANNEL_FILTERS[filter_bucket](r)]
        else:
            rows = [r for r in rows if matches_filter(r["status"], filter_bucket)]
    return rows


def _publish_summary(state: Any) -> dict[str, Any]:
    """The honest publish summary (Sprint 40, Objective 5) — real state, not
    'Completed'. Products by lifecycle, real drafts, failures, marketing reach,
    scheduled pins and a revenue forecast."""
    rows = _products(state)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    approved = sum(by_status.get(s, 0) for s in ("approved",))
    awaiting = by_status.get("awaiting_approval", 0)
    drafts_created = sum(by_status.get(s, 0) for s in ("draft_created", "publishing",
                                                       "marketing", "tracking", "live"))
    drafts_failed = by_status.get("failed", 0)
    db = state.db
    pins_scheduled = len(db.list_pin_schedule(status="scheduled"))
    marketing_assets = sum(db.count_marketing_assets(channel=ch)
                           for ch in ("pinterest", "instagram", "facebook", "blog", "email"))
    # Revenue forecast: per-unit expected profit of launched, priced products.
    forecast = 0.0
    for s in db.list_product_scores():
        if s.get("launched"):
            forecast += float(s.get("expected_profit", 0) or 0)
    return {
        "products_created": len(rows),
        "products_approved": approved,
        "products_awaiting_review": awaiting,
        "drafts_created": drafts_created,
        "drafts_failed": drafts_failed,
        "marketing_assets": marketing_assets,
        "pinterest_posts_scheduled": pins_scheduled,
        "revenue_forecast": round(forecast, 2),
        "by_status": by_status,
    }


def _product_detail(state: Any, sku: str) -> dict[str, Any] | None:
    """The Product Detail Drawer payload (Sprint 40, Objective 3)."""
    db = state.db
    product = db.get_product_by_sku(sku)
    if not product:
        return None
    ctx = _product_context(state)
    row = _product_row(state, product, ctx)
    key = product.get("product_key")
    cid = product.get("campaign_id")
    scores = [sc for sc in db.list_product_scores(cid) if sc.get("product_key") == key]
    compliance = db.get_compliance_for_campaign(cid) if cid else None
    return {
        "product": product,
        "row": row,
        "channels": _product_channels(state, product),
        "score": scores[0] if scores else None,
        "ceo_reasoning": (scores[0].get("reasoning") if scores else "") or "",
        "compliance": compliance,
        "marketing": db.list_marketing_assets(product_key=key) if key else [],
        "reviews": db.list_portfolio_reviews(sku),
        "approval": db.get_product_approval(sku),
        "approval_history": db.list_approval_history(sku),
        "publication": db.get_latest_publication(cid, "etsy", product_id=sku) if cid else None,
    }


def _exports_dir(state: Any) -> Path:
    base = Path((state.config.listing or {}).get("exports_dir", "exports"))
    if not base.is_absolute():
        base = ROOT_DIR / base
    return base


def _package_assets(state: Any, campaign_id: Any, product_key: str | None,
                    opportunity_id: str | None = None) -> dict[str, Any]:
    """Resolve a product package's real artwork/mock-up/listing assets to
    /exports URLs (best-effort — missing files degrade to empty, so this is
    offline-safe). Powers the Approval Workspace cards and Detail Drawer.

    Hero fallback chain (Sprint 42.1, Obj 1): primary mock-up → per-product
    master artwork → **design master artwork** (produced at the Artwork stage,
    before a product is published, so awaiting-approval cards still get a real
    preview instead of the placeholder) → empty (UI shows "Generating preview…")."""
    out: dict[str, Any] = {"has_artwork": False, "has_mockups": False,
                           "artwork_url": "", "hero_url": "", "mockups": [],
                           "listing_title": "", "seo_score": None,
                           "listing_package_url": "",
                           "mockup_status": "none", "mockup_ok": False,
                           "mockup_reason": ""}
    if campaign_id is None or not product_key:
        return out
    folder = _exports_dir(state) / str(campaign_id) / str(product_key)
    rel = f"/exports/{campaign_id}/{product_key}"
    artwork = folder / "master_artwork.png"
    if artwork.exists():
        out["has_artwork"] = True
        out["artwork_url"] = f"{rel}/master_artwork.png"
    listing_path = folder / "listing.json"
    if listing_path.exists():
        out["listing_package_url"] = f"{rel}/listing.json"
        try:
            import json as _json
            listing = _json.loads(listing_path.read_text(encoding="utf-8"))
        except Exception:
            listing = {}
        out["listing_title"] = listing.get("title") or ""
        out["seo_score"] = listing.get("seo_score")
        images = listing.get("images") or []
        mockups = [f"{rel}/images/{img['filename']}" for img in images
                   if (folder / "images" / img.get("filename", "")).exists()]
        out["mockups"] = mockups
        out["has_mockups"] = bool(mockups)
        out["hero_url"] = mockups[0] if mockups else out["artwork_url"]
        # Mockup Quality Gate status for the card (P1).
        from onassis.mockup_gate import allow_local_for, listing_mockup_status
        mq = listing_mockup_status(listing, allow_local=allow_local_for(state.config))
        out["mockup_status"] = mq["status"]
        out["mockup_ok"] = mq["ok"]
        out["mockup_reason"] = mq["reason"]
    else:
        out["hero_url"] = out["artwork_url"]
    # Pre-publish fallback: the design master artwork (exports/opportunities/<id>/).
    if not out["hero_url"] and opportunity_id:
        subdir = (state.config.design or {}).get("subdir", "opportunities")
        design_master = _exports_dir(state) / subdir / str(opportunity_id) / "master_artwork.png"
        if design_master.exists():
            url = f"/exports/{subdir}/{opportunity_id}/master_artwork.png"
            out["artwork_url"] = out["artwork_url"] or url
            out["hero_url"] = url
            out["has_artwork"] = True
    return out


# The operator actions each card offers, gated by the product's lifecycle status.
def _card_actions(status: str) -> list[str]:
    base = ["view_artwork", "view_mockups", "edit_listing", "regenerate_mockups"]
    if status == "awaiting_approval":
        return ["approve", "approve_and_publish", "reject", *base]
    if status == "approved":
        return ["approve_and_publish", "reject", *base]
    if status == "failed":
        return ["retry", "reject", *base]
    if status in ("draft_created", "live", "marketing", "tracking"):
        return ["view_listing", *base]
    return base


def _approval_card(state: Any, row: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """One operational approval card (Sprint 40, Objective 2)."""
    db = state.db
    cid = row["campaign_id"]
    key = row["product_key"]
    scores = [sc for sc in db.list_product_scores(cid) if sc.get("product_key") == key]
    score = scores[0] if scores else {}
    assets = _package_assets(state, cid, key, opportunity_id=score.get("opportunity_id"))
    compliance = db.get_compliance_for_campaign(cid) if cid else None
    # Real confidence only — never a spurious 0% when nothing was calculated.
    raw_conf = score.get("composite_score")
    confidence = round(float(raw_conf) / 100.0, 3) if raw_conf else None
    # Publish + retry history across both sales channels (Obj 3).
    pub_history, retries = [], 0
    for platform in ("etsy", "shopify"):
        pub = db.get_latest_publication(cid, platform, product_id=row["sku"]) if cid else None
        if pub:
            attempts = int(pub.get("attempts", 1) or 1)
            retries += max(0, attempts - 1)
            pub_history.append({"channel": platform, "status": pub.get("status"),
                                "listing_id": pub.get("listing_id"),
                                "attempts": attempts,
                                "reason": pub.get("failure_reason"),
                                "at": pub.get("created_at")})
    # Hero is never blank — fall back to artwork, then a placeholder flag.
    hero = assets["hero_url"] or assets["artwork_url"]
    # Per-channel status pills for the card (Sprint 42.1, Obj 6).
    etsy_pub = db.get_latest_publication(cid, "etsy", product_id=row["sku"]) if cid else None
    etsy_cell = _pub_label(etsy_pub)
    return {
        "sku": row["sku"], "name": row["name"], "type": key or "product",
        "campaign_id": cid, "campaign": row["campaign"] or "",
        "collection": row.get("collection") or "",
        "status": row["status"], "status_label": row["status_label"],
        "workflow_stage": row["status_label"],
        "hero_url": hero, "has_hero": bool(hero),
        "artwork_url": assets["artwork_url"],
        "mockups": assets["mockups"], "has_artwork": assets["has_artwork"],
        "has_mockups": assets["has_mockups"],
        "listing_title": assets["listing_title"] or row["name"] or row["sku"],
        "listing_package_url": assets["listing_package_url"],
        "seo_score": assets["seo_score"],
        "mockup_status": assets["mockup_status"], "mockup_ok": assets["mockup_ok"],
        "mockup_reason": assets["mockup_reason"],
        "confidence": confidence,
        "ceo_rationale": (score.get("reasoning") or "").strip() or "—",
        "compliance": (compliance or {}).get("verdict") or "PENDING",
        "compliance_score": (compliance or {}).get("compliance_score"),
        "approval": row["approval"], "operator": row["operator"] or "—",
        "listing_id": row["listing_id"], "listing_url": row["listing_url"],
        "etsy_status": etsy_cell["label"], "etsy_state": etsy_cell["status"],
        "shopify_status": row["shopify_status"], "shopify_state": row["shopify_state"],
        "publish_history": pub_history, "retry_history": retries,
        "last_updated": row["updated_at"],
        "reason": row["reason"] or "", "retryable": row["retryable"],
        "actions": _card_actions(row["status"]),
    }


def _approvals(state: Any) -> dict[str, Any]:
    """The Approval Workspace (Sprint 40, Objective 2): operational product cards
    the operator can act on, plus the legacy compliance summary buckets.

    ``queue`` holds the products that need a human decision (awaiting / failed),
    ``ready`` those the operator approved and can publish, and the auto/needs/
    blocked buckets summarise the compliance verdicts as before."""
    threshold = float((state.config.compliance or {}).get("auto_approve_confidence", 0.85))
    try:
        threshold = float(BusinessSettings(state.db, state.config).get("auto_approval_threshold"))
    except Exception:
        pass

    ctx = _product_context(state)
    # Archived products (active=0) are removed from the workspace — the operator
    # cleared them, so they must vanish from queue/ready/published, not linger.
    # (The Product Status Engine derives status independently of the active flag,
    # so without this filter an archived product still shows as "ready".)
    products = [p for p in state.db.list_products() if p.get("active", 1)]
    active_cids = {p.get("campaign_id") for p in products if p.get("campaign_id")}
    active_keys = {p.get("product_key") for p in products if p.get("product_key")}
    rows = [_product_row(state, p, ctx) for p in products]
    # A product whose type has no matching Gelato catalogue product (e.g. the
    # generic "product" key from a broken cycle) can NEVER build a listing, so it
    # must not sit in "Ready to publish" fooling the operator into publishing it.
    # Flag it as un-buildable and route it to the queue for archiving instead.
    buildable = _buildable_keys(state)
    queue, ready, published = [], [], []
    _non_published = ("awaiting_approval", "approved", "failed")
    for r in rows:
        broken = (r["status"] in _non_published
                  and r.get("product_key") not in buildable)
        if broken:
            r["status"] = "failed"
            r["status_label"] = "Cannot build — unrecognised product type"
            r["reason"] = (
                f"Product type '{r.get('product_key') or '—'}' has no matching "
                "Gelato product, so no listing can be built. Archive it (Clear "
                "pending) to remove it from the queue.")
            r["retryable"] = False
        if r["status"] in ("awaiting_approval", "failed"):
            card = _approval_card(state, r, ctx)
            if broken:  # never offer approve/publish/retry — it can only be archived
                card["actions"] = [a for a in card["actions"]
                                   if a not in ("retry", "approve", "approve_and_publish")]
                card["buildable"] = False
            queue.append(card)
        elif r["status"] == "approved":
            ready.append(_approval_card(state, r, ctx))
        elif r["status"] in ("draft_created", "live", "marketing", "tracking"):
            published.append(_approval_card(state, r, ctx))

    # Legacy compliance-derived buckets (the summary strip). Bound to campaigns
    # that still have an ACTIVE product — so clearing the queue clears these too,
    # instead of leaving orphaned "Prepare Etsy listing…" cards from dead runs.
    blocked, review, auto = [], [], []
    for r in state.db.list_compliance_reports()[:100]:
        if r.get("campaign_id") not in active_cids:
            continue
        verdict = (r.get("verdict") or "").upper()
        item = {"subject": r.get("subject"), "verdict": verdict,
                "score": r.get("compliance_score"), "campaign_id": r.get("campaign_id")}
        if verdict == "REJECT":
            blocked.append(item)
        elif verdict == "APPROVE_WITH_CHANGES":
            review.append(item)
        else:
            auto.append(item)
    for d in state.db.list_protection_decisions(decision="REJECT")[:40]:
        if d.get("product_key") not in active_keys:
            continue
        review.append({"subject": f"{d['action']} · {d.get('product_key') or ''}",
                       "verdict": "PROTECTION_HOLD", "score": d.get("confidence"),
                       "reason": d.get("reason")})
    return {"queue": queue, "ready": ready, "published": published,
            "auto_approved": auto[:20], "needs_review": review[:20],
            "blocked": blocked[:20], "confidence_threshold": threshold}


def _masked_env(config: Any) -> list[dict[str, str]]:
    def mask(v: str | None) -> str:
        if not v:
            return "—"
        v = str(v)
        return (v[:4] + "…" + v[-4:]) if len(v) > 10 else "set"
    return [
        {"key": "ONASSIS_ENV", "value": getattr(config, "environment", "development")},
        {"key": "ANTHROPIC_API_KEY", "value": mask(config.anthropic_api_key)},
        {"key": "OPENAI_API_KEY", "value": mask((config.image or {}).get("api_key"))},
        {"key": "ETSY_CLIENT_ID", "value": mask((config.etsy or {}).get("client_id"))},
        {"key": "GELATO_API_KEY", "value": mask((config.gelato or {}).get("api_key"))},
        {"key": "PINTEREST_ACCESS_TOKEN",
         "value": mask((config.pinterest or {}).get("access_token"))},
        {"key": "ONASSIS_API_KEY", "value": mask((config.security or {}).get("api_key"))},
    ]


def _recent_logs(limit: int = 60) -> list[str]:
    path = ROOT_DIR / "logs" / "onassis.log"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except Exception:
        return []
