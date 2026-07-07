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
    ("Publish Products", "Listing + Etsy Draft + Gallery"),
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
        "anthropic": _cred_light(bool(config.anthropic_api_key), bool(config.anthropic_api_key),
                                 "Anthropic"),
        "openai": _cred_light(bool((config.image or {}).get("api_key")),
                              bool((config.image or {}).get("api_key")), "OpenAI"),
        "etsy": _cred_light(etsy_cfg, bool(getattr(etsy, "is_configured", False)), "Etsy"),
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
            result = s.daily.publisher.publish(cid, product_key=key)
            ok = result.get("status") in ("draft", "dry_run")
            lvl = "info" if ok else "error"
            get_state(request.app).add_log(
                f"Publish {sku} -> {result.get('status')}"
                f"{': ' + result.get('reason', '') if not ok else ''}.", lvl)
            return {"sku": sku, "decision": "approved", "published": result}
        raise HTTPException(status_code=400, detail=f"Unknown approval action '{action}'.")

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
    marketing_status = ("live" if marketing else
                        ("pending" if st.status in ("live", "marketing", "tracking")
                         else "none"))
    return {
        "sku": sku, "name": p.get("name") or key, "product_key": key,
        "type": key, "campaign_id": cid, "campaign": campaign.get("name") or "",
        "status": st.status, "status_label": st.label,
        "reason": st.reason, "retryable": st.retryable,
        "etsy_status": st.label, "listing_id": st.listing_id, "listing_url": listing_url,
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
        {"channel": "Pinterest", **pin_cell},
        {"channel": "Facebook", **_delivery_label(latest_asset.get("facebook"))},
        {"channel": "Instagram", **_delivery_label(latest_asset.get("instagram"))},
        {"channel": "Email", **_delivery_label(latest_asset.get("email"), sent_word="Sent")},
        {"channel": "Blog", **_delivery_label(latest_asset.get("blog"), sent_word="Published")},
    ]
    return rows


def _products(state: Any, filter_bucket: str | None = None) -> list[dict[str, Any]]:
    ctx = _product_context(state)
    rows = [_product_row(state, p, ctx) for p in state.db.list_products()]
    if filter_bucket and filter_bucket != "all":
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


def _package_assets(state: Any, campaign_id: Any, product_key: str | None) -> dict[str, Any]:
    """Resolve a product package's real artwork/mock-up/listing assets to
    /exports URLs (best-effort — missing files degrade to empty, so this is
    offline-safe). Powers the Approval Workspace cards and Detail Drawer."""
    out: dict[str, Any] = {"has_artwork": False, "has_mockups": False,
                           "artwork_url": "", "hero_url": "", "mockups": [],
                           "listing_title": "", "seo_score": None,
                           "listing_package_url": ""}
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
    else:
        out["hero_url"] = out["artwork_url"]
    return out


# The operator actions each card offers, gated by the product's lifecycle status.
def _card_actions(status: str) -> list[str]:
    base = ["view_artwork", "view_mockups", "edit_listing"]
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
    assets = _package_assets(state, cid, key)
    scores = [sc for sc in db.list_product_scores(cid) if sc.get("product_key") == key]
    score = scores[0] if scores else {}
    compliance = db.get_compliance_for_campaign(cid) if cid else None
    confidence = float(score.get("composite_score", 0) or 0) / 100.0
    return {
        "sku": row["sku"], "name": row["name"], "type": key,
        "campaign_id": cid, "campaign": row["campaign"],
        "status": row["status"], "status_label": row["status_label"],
        "hero_url": assets["hero_url"], "artwork_url": assets["artwork_url"],
        "mockups": assets["mockups"], "has_artwork": assets["has_artwork"],
        "has_mockups": assets["has_mockups"],
        "listing_title": assets["listing_title"] or row["name"],
        "listing_package_url": assets["listing_package_url"],
        "seo_score": assets["seo_score"],
        "confidence": round(confidence, 3),
        "ceo_rationale": (score.get("reasoning") or "").strip(),
        "compliance": (compliance or {}).get("verdict") or "PENDING",
        "compliance_score": (compliance or {}).get("compliance_score"),
        "approval": row["approval"], "operator": row["operator"],
        "listing_id": row["listing_id"], "listing_url": row["listing_url"],
        "reason": row["reason"], "retryable": row["retryable"],
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
    rows = [_product_row(state, p, ctx) for p in state.db.list_products()]
    queue, ready, published = [], [], []
    for r in rows:
        if r["status"] in ("awaiting_approval", "failed"):
            queue.append(_approval_card(state, r, ctx))
        elif r["status"] == "approved":
            ready.append(_approval_card(state, r, ctx))
        elif r["status"] in ("draft_created", "live", "marketing", "tracking"):
            published.append(_approval_card(state, r, ctx))

    # Legacy compliance-derived buckets (kept for the summary strip).
    blocked, review, auto = [], [], []
    for r in state.db.list_compliance_reports()[:50]:
        verdict = (r.get("verdict") or "").upper()
        item = {"subject": r.get("subject"), "verdict": verdict,
                "score": r.get("compliance_score"), "campaign_id": r.get("campaign_id")}
        if verdict == "REJECT":
            blocked.append(item)
        elif verdict == "APPROVE_WITH_CHANGES":
            review.append(item)
        else:
            auto.append(item)
    for d in state.db.list_protection_decisions(decision="REJECT")[:20]:
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
