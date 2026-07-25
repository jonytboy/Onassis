"""TikTok connector — publish the slideshow videos to TikTok via the Content
Posting API (v2), using the same public mp4 URLs the blog/Facebook already use
(PULL_FROM_URL source).

Gated + injectable, exactly like the Meta/Shopify connectors: without a TikTok
access token it is a safe no-op, and the HTTP client is injectable so tests
never touch the network.

TikTok reality (so the operator isn't surprised): the Content Posting API needs
a TikTok for Developers app + an OAuth access token. Until the app passes
TikTok's audit, posts can only be created as ``SELF_ONLY`` (private) — that's the
default ``privacy_level`` here. Once audited, set ``privacy_level`` to
``PUBLIC_TO_EVERYONE`` for public posts. PULL_FROM_URL also requires the mp4's
domain to be verified in the TikTok app.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.logger import get_logger

log = get_logger(__name__)


class TikTokPublisher:
    name = "tiktok"

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self.config = config
        self.cfg = config.tiktok or {}
        self._client = client

    @property
    def can_publish(self) -> bool:
        return self._client is not None or bool(self.cfg.get("access_token"))

    def _c(self) -> Any:
        if self._client is None:
            self._client = TikTokClient(access_token=self.cfg.get("access_token"))
        return self._client

    def post_video(self, video_url: str, caption: str = "") -> dict[str, Any]:
        """Publish a video from a public mp4 URL. Returns ``{ok, ref}`` where ref
        is TikTok's publish_id."""
        if not self.can_publish:
            return {"ok": False, "skipped": True, "reason": "TikTok not configured."}
        if not video_url:
            return {"ok": False, "skipped": True, "reason": "No video URL."}
        privacy = self.cfg.get("privacy_level", "SELF_ONLY")
        res = self._c().init_video_post(video_url, caption, privacy)
        pid = ((res or {}).get("data") or {}).get("publish_id", "")
        return {"ok": bool(pid), "ref": str(pid)}

    def test_connection(self) -> dict[str, Any]:
        if not self.can_publish:
            return {"ok": False, "configured": False,
                    "detail": "Set a TikTok access token (Integrations → TikTok)."}
        try:
            info = self._c().creator_info()
            name = ((info or {}).get("data") or {}).get("creator_nickname", "")
            return {"ok": True, "configured": True,
                    "detail": f"Connected as {name}".strip()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "detail": str(exc)}


class TikTokClient:
    """Minimal TikTok Content Posting API client. Injectable for tests."""

    def __init__(self, access_token: str | None,
                 base: str = "https://open.tiktokapis.com/v2",
                 timeout: float = 60.0) -> None:
        self.access_token = access_token
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token or ''}",
                "Content-Type": "application/json; charset=UTF-8"}

    def creator_info(self) -> dict[str, Any]:
        import httpx

        resp = httpx.post(f"{self.base}/post/publish/creator_info/query/",
                          headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"TikTok creator_info HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    def init_video_post(self, video_url: str, caption: str,
                        privacy_level: str) -> dict[str, Any]:
        import httpx

        body = {
            "post_info": {"title": (caption or "")[:150], "privacy_level": privacy_level,
                          "disable_comment": False, "disable_duet": False,
                          "disable_stitch": False},
            "source_info": {"source": "PULL_FROM_URL", "video_url": video_url}}
        resp = httpx.post(f"{self.base}/post/publish/video/init/",
                          headers=self._headers(), json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"TikTok init HTTP {resp.status_code}: {resp.text}")
        return resp.json()
