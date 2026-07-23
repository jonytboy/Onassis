"""Production security hardening for the ONASSIS API.

This is a deployment concern only — it adds **no business logic** and changes no
endpoint. A single middleware wraps the app and, when security is active,
enforces:

* **API-key authentication** on every route except the public ones (``/``,
  ``/health``, ``/exports/*``). Missing/incorrect key → ``401``.
* **Per-IP rate limiting** on protected routes → ``429`` when exceeded.
* **Security headers** on every response, and the ``Server`` banner removed.
* **Access logging** (IP · method · path · status · timestamp) via the app's
  rotating log handler.

Security is **auto-enabled in production** (``ONASSIS_ENV=production``) and off in
development so local work and the test-suite are frictionless; ``security.enabled``
in config can force it either way. Swagger (``/docs``, ``/openapi.json``,
``/redoc``) is not on the public allow-list, so in production it is reachable only
with a valid key — public but unauthenticated access gets ``401``.

The public ``/exports/*`` mount stays open so Gelato can fetch print files, and
``/health`` stays open for load-balancer probes.
"""

from __future__ import annotations

import hmac
import time
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from onassis.logger import get_logger

log = get_logger(__name__)
access_log = get_logger("onassis.access")

# Publicly reachable without a key (success criteria: Gelato + health probes).
# The Operations Centre shell (/operations) and its data/action API
# (/operations/api/*) are exempt from the GLOBAL middleware because the centre
# does its OWN operator-key check and polls frequently (so the shared rate
# limiter must not throttle it). The legacy /operations/check|status|report
# endpoints are NOT exempt — they stay behind the global key.
_PUBLIC_EXACT = {
    "/",
    "/health",
    "/operations",
    "/operations/",
    "/etsy/oauth/login",
    "/etsy/oauth/callback",
    "/etsy/oauth/status",
    "/pinterest/oauth/login",
    "/pinterest/oauth/callback",
}
_PUBLIC_PREFIXES = ("/exports", "/operations/api", "/operations/static")

_DEFAULT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # Prevent clickjacking without blocking Swagger's own resources.
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def security_enabled(config: Any) -> bool:
    """Security is on in production, off in development — unless forced by config."""
    sec = getattr(config, "security", None) or {}
    if sec.get("enabled") is not None:
        return bool(sec.get("enabled"))
    return getattr(config, "environment", "development") == "production"


class SecurityMiddleware(BaseHTTPMiddleware):
    """One middleware: auth + rate-limit + headers + access logging."""

    def __init__(self, app, *, config: Any,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(app)
        sec = getattr(config, "security", None) or {}
        self.enabled = security_enabled(config)
        self.production = getattr(config, "environment", "development") == "production"
        self.api_key = sec.get("api_key") or None
        self.header_name = sec.get("api_key_header", "X-API-Key")
        self.rate_limit = int(sec.get("rate_limit_requests", 120))
        self.window = int(sec.get("rate_limit_window_seconds", 60))
        self._clock = clock
        self._hits: dict[str, tuple[float, int]] = {}
        if self.enabled and not self.api_key:
            log.critical("SECURITY ENABLED BUT NO API KEY SET (ONASSIS_API_KEY). "
                         "All protected endpoints will return 401 until a key is set.")

    async def dispatch(self, request, call_next) -> Response:
        path = request.url.path
        ip = self._client_ip(request)
        response: Response | None = None

        if self.enabled and not self._is_public(path):
            if not self._within_rate_limit(ip):
                response = self._deny(429, "Rate limit exceeded.")
            elif not self._authorised(request):
                response = self._deny(401, "Unauthorised.")

        if response is None:
            response = await call_next(request)

        self._harden(response)
        access_log.info("%s %s %s -> %s", ip, request.method, path,
                        response.status_code)
        return response

    # --- Auth -------------------------------------------------------

    def _authorised(self, request) -> bool:
        if not self.api_key:
            return False  # fail closed: no key configured -> nobody is authorised
        provided = request.headers.get(self.header_name) or self._bearer(request)
        if not provided:
            return False
        # Constant-time comparison — no early-exit timing side channel.
        return hmac.compare_digest(str(provided), str(self.api_key))

    @staticmethod
    def _bearer(request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    # --- Rate limiting (fixed window, per IP) -----------------------

    def _within_rate_limit(self, ip: str) -> bool:
        now = self._clock()
        start, count = self._hits.get(ip, (now, 0))
        if now - start >= self.window:
            start, count = now, 0
        count += 1
        self._hits[ip] = (start, count)
        return count <= self.rate_limit

    # --- Response hardening -----------------------------------------

    def _harden(self, response: Response) -> None:
        for name, value in _DEFAULT_HEADERS.items():
            response.headers.setdefault(name, value)
        if self.production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        # Hide the server banner where practical.
        if "server" in response.headers:
            del response.headers["server"]

    # --- Helpers ----------------------------------------------------

    @staticmethod
    def _is_public(path: str) -> bool:
        return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)

    @staticmethod
    def _client_ip(request) -> str:
        # Behind a trusted reverse proxy / TLS terminator, use the first hop.
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _deny(self, status: int, detail: str) -> Response:
        return JSONResponse({"detail": detail}, status_code=status)


def install_security(app: Any, config: Any) -> bool:
    """Attach the security middleware. Returns whether security is active.

    In development it is inert (pass-through + headers), so tests and local work
    are unaffected; in production it authenticates, rate-limits and hardens.
    """
    app.add_middleware(SecurityMiddleware, config=config)
    active = security_enabled(config)
    log.info("API security %s (env=%s).", "ENABLED" if active else "disabled (dev)",
             getattr(config, "environment", "development"))
    return active
