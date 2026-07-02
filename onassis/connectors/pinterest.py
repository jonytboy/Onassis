"""Pinterest connector — analytics (read) and promotion (write).

Read: emits canonical metric snapshots (impressions, saves, outbound clicks,
CTR) for the Analytics Collector, matching the Etsy source's ``fetch_metrics()``
contract.

Write: ``publish_pins()`` posts pins that promote a **live** product, each
linking back to its Etsy listing — the free, high-intent traffic channel this
niche needs. It needs credentials (``PINTEREST_ACCESS_TOKEN`` + a board id);
without them it is a safe no-op, so the daily cycle runs unchanged until the
shop is connected. The HTTP client is injectable, so tests never touch the
network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from onassis.config import Config
from onassis.logger import get_logger

log = get_logger(__name__)


class PinterestConnector:
    """Pinterest analytics source (read) + auto-promotion (write)."""

    name = "pinterest"

    def __init__(self, config: Config, pin_client: Any | None = None) -> None:
        self.config = config
        self.cfg = config.pinterest or {}
        self.board_id = self.cfg.get("board_id")
        self.max_pins = int(self.cfg.get("max_pins_per_product", 3))
        self._pin_client = pin_client

    @property
    def is_configured(self) -> bool:
        return bool(self.cfg.get("access_token"))

    @property
    def can_publish(self) -> bool:
        """Auto-promotion needs a token AND a target board (or an injected client)."""
        return self._pin_client is not None or bool(
            self.cfg.get("access_token") and self.board_id)

    def fetch_metrics(self) -> list[dict[str, Any]]:
        if not self.is_configured:
            return []  # safe no-op until credentials are provided
        # A live implementation would call the Pinterest v5 analytics API here
        # and map the response onto metric rows (impressions/saves/outbound
        # clicks/ctr). Kept out of scope (no credentials available).
        log.info("Pinterest configured but live metric fetch is not implemented yet.")
        return []

    # --- Promotion (write) ------------------------------------------

    def publish_pins(self, pins: list[dict[str, Any]]) -> dict[str, Any]:
        """Post pins that promote a live product. Each pin dict needs a ``title``,
        ``description``, ``link`` (the Etsy listing URL) and an ``image_path``.

        Best-effort per pin: one failure is counted, not fatal. Returns
        ``{posted, failed, skipped, results}``.
        """
        if not pins:
            return {"posted": 0, "failed": 0, "skipped": 0, "results": []}
        if not self.can_publish:
            return {"posted": 0, "failed": 0, "skipped": len(pins),
                    "reason": "Pinterest not configured for publishing.", "results": []}
        client = self._client()
        posted, failed, results = 0, 0, []
        for pin in pins[: self.max_pins]:
            try:
                res = client.create_pin(
                    board_id=self.board_id, title=(pin.get("title") or "")[:100],
                    description=(pin.get("description") or "")[:500],
                    link=pin.get("link"), image_path=pin.get("image_path"),
                    alt_text=pin.get("alt_text"))
                posted += 1
                results.append({"status": "posted", "pin_id": res.get("id"),
                                "link": pin.get("link")})
            except Exception as exc:  # keep going; record the miss
                failed += 1
                results.append({"status": "failed", "reason": str(exc)})
                log.warning("Pinterest pin failed (%s): %s", pin.get("link"), exc)
        log.info("Pinterest: posted %d/%d pin(s).", posted, len(pins[: self.max_pins]))
        return {"posted": posted, "failed": failed,
                "skipped": max(0, len(pins) - self.max_pins), "results": results}

    def _client(self) -> Any:
        if self._pin_client is None:
            self._pin_client = PinterestPinClient(
                access_token=self.cfg.get("access_token"),
                base_url=self.cfg.get("base_url", "https://api.pinterest.com/v5"),
            )
        return self._pin_client


class PinterestPinClient:
    """Minimal Pinterest v5 pin-creation client (write). Injectable for tests."""

    def __init__(self, access_token: str | None,
                 base_url: str = "https://api.pinterest.com/v5",
                 timeout: float = 30.0) -> None:
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def create_pin(self, *, board_id: str, title: str, description: str,
                   link: str | None, image_path: str | None,
                   alt_text: str | None = None) -> dict[str, Any]:
        import base64

        import httpx

        media: dict[str, Any] = {"source_type": "image_base64"}
        if image_path and Path(image_path).exists():
            data = Path(image_path).read_bytes()
            media["content_type"] = "image/jpeg"
            media["data"] = base64.b64encode(data).decode("ascii")
        body = {"board_id": board_id, "title": title, "description": description,
                "alt_text": (alt_text or title)[:500], "media_source": media}
        if link:
            body["link"] = link
        resp = httpx.post(f"{self.base_url}/pins",
                          headers={"Authorization": f"Bearer {self.access_token}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Pinterest createPin HTTP {resp.status_code}: {resp.text}")
        return resp.json()
