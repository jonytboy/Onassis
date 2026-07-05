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

from onassis.config import ROOT_DIR
from onassis.logger import get_logger
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
                state.end_run(result.get("status", status), _run_summary(result))
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

    # --- Products tab ---
    @router.get("/api/products")
    def api_products(request: Request) -> Any:
        _require_operator(request)
        return {"products": _products(request.app.state)}

    @router.get("/api/products/{sku}")
    def api_product(request: Request, sku: str) -> Any:
        _require_operator(request)
        s = request.app.state
        product = s.db.get_product_by_sku(sku)
        if not product:
            raise HTTPException(status_code=404, detail="Unknown product.")
        key = product.get("product_key")
        scores = [sc for sc in s.db.list_product_scores() if sc.get("product_key") == key]
        return {
            "product": product,
            "reviews": s.db.list_portfolio_reviews(sku),
            "marketing": s.db.list_marketing_assets(product_key=key),
            "scores": scores[:1],
            "protection": s.db.list_protection_decisions()[:5],
        }

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
        return {
            "channels": {ch: s.db.count_marketing_assets(channel=ch)
                         for ch in ("pinterest", "instagram", "facebook", "blog", "email")},
            "pins_scheduled": len(s.db.list_pin_schedule(status="scheduled")),
            "pins_posted": len(s.db.list_pin_schedule(status="posted")),
            "funnel": funnel,
            "keywords": s.daily.etsy_intelligence.keyword_performance(20),
        }

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
        if action in ("backup", "health", "deploy", "rollback", "restart"):
            return _run_script(action, state)
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")

    return router


# =====================================================================
# Helpers (read-only aggregation)
# =====================================================================

def _run_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result.get(k) for k in (
        "status", "products_launched", "products_live", "pins_posted",
        "assets_created", "first_draft_at")}


def _products(state: Any) -> list[dict[str, Any]]:
    perf = {p["product_key"]: p for p in state.db.list_product_performance()}
    rows: list[dict[str, Any]] = []
    for p in state.db.list_products():
        key = p.get("product_key")
        pf = perf.get(key, {})
        rows.append({
            "sku": p.get("sku"), "name": p.get("name") or key, "product_key": key,
            "status": "archived" if not p.get("active", 1) else "published",
            "units": int(pf.get("units_sold", 0) or 0),
            "net_profit": float(pf.get("net_profit", 0) or 0),
            "launched_at": p.get("launched_at") or p.get("created_at"),
        })
    return rows


def _approvals(state: Any) -> dict[str, Any]:
    """The manual-approval queue: auto-approved / needs-review / blocked, derived
    from the compliance verdicts + protection confidence the system already
    records. ONASSIS does 99%; the operator only sees what needs a human."""
    threshold = float((state.config.compliance or {}).get("auto_approve_confidence", 0.85))
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
    # Low-confidence protection decisions also want a human look.
    for d in state.db.list_protection_decisions(decision="REJECT")[:20]:
        review.append({"subject": f"{d['action']} · {d.get('product_key') or ''}",
                       "verdict": "PROTECTION_HOLD", "score": d.get("confidence"),
                       "reason": d.get("reason")})
    return {"auto_approved": auto[:20], "needs_review": review[:20],
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


def _run_script(name: str, state: OperationsState) -> dict[str, Any]:
    """Run an operations script (backup/health/deploy/rollback) or restart the
    service, capturing output for the console. Bounded timeout; never blocks."""
    if name == "restart":
        cmd = ["sudo", "systemctl", "restart", "onassis"]
    else:
        script = ROOT_DIR / f"{name}.sh"
        if not script.exists():
            raise HTTPException(status_code=404, detail=f"{name}.sh not found.")
        cmd = ["bash", str(script)]
        if name == "rollback":
            cmd.append("-y")
    state.add_log(f"Running: {' '.join(cmd)}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                             cwd=str(ROOT_DIR))
    except subprocess.TimeoutExpired:
        state.add_log(f"{name}: timed out.", "error")
        return {"action": name, "ok": False, "detail": "timed out"}
    for line in (out.stdout or "").splitlines()[-40:]:
        state.add_log(line)
    if out.returncode != 0:
        for line in (out.stderr or "").splitlines()[-10:]:
            state.add_log(line, "error")
    return {"action": name, "ok": out.returncode == 0, "returncode": out.returncode,
            "output": (out.stdout or "")[-4000:], "error": (out.stderr or "")[-2000:]}
