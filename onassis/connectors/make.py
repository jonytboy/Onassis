"""Make.com distribution connector (Sprint 43).

ONASSIS stays the brain — it generates every marketing asset, decides the
schedule, and measures the outcome. Make.com becomes the single **distribution
engine**: one webhook carries a complete campaign package out, and Make fans it
out to Facebook / Instagram / Pinterest / TikTok / Threads / LinkedIn / email /
Buffer / Mailchimp / any future platform. Adding a channel is Make configuration
— no ONASSIS code change.

Follows the house connector pattern: a ``can_publish`` gate, an injectable HTTP
client (so tests never hit the network), and a safe no-op until the webhook is
configured.
"""

from __future__ import annotations

from typing import Any, Callable

from onassis.logger import get_logger

log = get_logger(__name__)


class MakeConnector:
    """Posts a campaign package to a Make.com webhook."""

    name = "make"

    def __init__(self, config: Any, *, client: Callable[..., Any] | None = None) -> None:
        self.config = config
        self.cfg = dict(getattr(config, "make", None) or {})
        self._client = client            # injectable POST(url, json, headers) -> response

    # --- Gate -------------------------------------------------------

    @property
    def webhook_url(self) -> str:
        return (self.cfg.get("webhook_url") or "").strip()

    @property
    def is_configured(self) -> bool:
        return self.webhook_url.startswith("http")

    # Alias to match the other connectors' gate name.
    can_publish = is_configured

    # --- Actions ----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = (self.cfg.get("api_key") or "").strip()
        if key:
            headers["x-make-apikey"] = key
        return headers

    def _post(self, payload: dict[str, Any]) -> Any:
        if self._client is not None:
            return self._client(self.webhook_url, json=payload, headers=self._headers())
        import httpx
        return httpx.post(self.webhook_url, json=payload, headers=self._headers(),
                          timeout=float(self.cfg.get("timeout", 30)))

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one campaign package. Returns ``{ok, status, detail, response}``.

        Make may reply synchronously with per-channel statuses, or just 200/202;
        either way a 2xx means Make accepted the campaign for distribution."""
        if not self.is_configured:
            return {"ok": False, "status": "not_configured",
                    "detail": "Make.com webhook URL is not set."}
        try:
            resp = self._post(payload)
        except Exception as exc:  # network / DNS / timeout
            log.warning("Make.com webhook POST failed: %s", exc)
            return {"ok": False, "status": "failed", "detail": str(exc)}
        code = getattr(resp, "status_code", 200)
        if code >= 400:
            text = getattr(resp, "text", "")
            return {"ok": False, "status": "failed",
                    "detail": f"Make.com HTTP {code}: {text}"[:300]}
        # Best-effort parse of any per-channel statuses Make returned.
        body: Any = {}
        try:
            body = resp.json() if hasattr(resp, "json") else {}
        except Exception:
            body = {}
        return {"ok": True, "status": "sent", "detail": f"HTTP {code}",
                "response": body if isinstance(body, dict) else {}}

    def test_connection(self) -> dict[str, Any]:
        """A lightweight ping — sends a ``{"test": true}`` probe to the webhook."""
        if not self.is_configured:
            return {"ok": False, "detail": "No Make.com webhook URL configured."}
        r = self.send({"test": True, "source": "onassis", "event": "test_connection"})
        return {"ok": r["ok"], "detail": r.get("detail", "")}
