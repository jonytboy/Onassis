"""Meta connectors — publish the marketing kit's Facebook and Instagram assets.

Both use the Meta Graph API and share one gated, injectable client (like the
Pinterest connector): without ``META_PAGE_ACCESS_TOKEN`` (+ a page/IG-user id)
they are safe no-ops, and the HTTP client is injectable so tests never hit the
network.

* Facebook — an organic Page post (message + link) via ``/{page-id}/feed``.
* Instagram — a feed post via the two-step container→publish flow. IG requires a
  publicly reachable ``image_url``; without one the post is cleanly skipped
  (recorded as skipped, never failed).
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.logger import get_logger

log = get_logger(__name__)


def mint_page_token(app_id: str, app_secret: str, short_token: str,
                    page_id: str | None = None, api_version: str = "v21.0",
                    client: Any | None = None) -> dict[str, Any]:
    """Turn a short-lived user token into a NEVER-EXPIRING Page access token: a
    Page token derived from a long-lived user token doesn't expire. Returns
    ``{ok, page_token, page_name, pages}`` — ``pages`` lists every managed Page so
    the operator can spot the right id."""
    client = client or MetaGraphClient(access_token=None, api_version=api_version)
    long_user = client.exchange_long_lived(app_id, app_secret, short_token)
    if not long_user:
        return {"ok": False, "error": "No long-lived token returned.", "pages": []}
    pages = client.list_pages(long_user)
    listing = [{"id": str(p.get("id")), "name": p.get("name")} for p in pages]
    match = None
    if page_id:
        match = next((p for p in pages if str(p.get("id")) == str(page_id)), None)
    elif len(pages) == 1:
        match = pages[0]
    if not match:
        return {"ok": False, "pages": listing,
                "error": ("No Page matched — pick an id from the list and pass it."
                          if page_id else "Multiple Pages — specify which id.")}
    return {"ok": True, "page_token": match.get("access_token"),
            "page_id": str(match.get("id")), "page_name": match.get("name"),
            "pages": listing}


class _MetaBase:
    def __init__(self, config: Config, client: Any | None = None) -> None:
        self.config = config
        self.cfg = config.meta or {}
        self._client = client

    def _c(self) -> Any:
        if self._client is None:
            self._client = MetaGraphClient(
                access_token=self.cfg.get("page_access_token"),
                api_version=self.cfg.get("api_version", "v21.0"))
        return self._client


class FacebookPublisher(_MetaBase):
    name = "facebook"

    @property
    def can_publish(self) -> bool:
        return self._client is not None or bool(
            self.cfg.get("page_access_token") and self.cfg.get("facebook_page_id"))

    def post(self, message: str, link: str | None = None) -> dict[str, Any]:
        """Publish an organic Page post. Returns ``{ok, ref}``."""
        if not self.can_publish:
            return {"ok": False, "skipped": True, "reason": "Facebook not configured."}
        res = self._c().page_feed(self.cfg.get("facebook_page_id"), message, link)
        return {"ok": True, "ref": str(res.get("id", ""))}

    def post_video(self, video_url: str, message: str = "") -> dict[str, Any]:
        """Publish a video to the Page from a public mp4 URL. Returns ``{ok, ref}``."""
        if not self.can_publish:
            return {"ok": False, "skipped": True, "reason": "Facebook not configured."}
        if not video_url:
            return {"ok": False, "skipped": True, "reason": "No video URL."}
        res = self._c().page_video(self.cfg.get("facebook_page_id"), video_url, message)
        return {"ok": True, "ref": str(res.get("id", ""))}

    def test_connection(self) -> dict[str, Any]:
        if not self.can_publish:
            return {"ok": False, "configured": False,
                    "detail": "Set META_PAGE_ACCESS_TOKEN + FACEBOOK_PAGE_ID."}
        try:
            node = self._c().get_node(self.cfg.get("facebook_page_id"), "name")
            return {"ok": True, "configured": True,
                    "detail": f"Connected to page {node.get('name', '')}".strip()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "detail": str(exc)}


class InstagramPublisher(_MetaBase):
    name = "instagram"

    @property
    def can_publish(self) -> bool:
        return self._client is not None or bool(
            self.cfg.get("page_access_token") and self.cfg.get("instagram_user_id"))

    def post(self, caption: str, image_url: str | None = None) -> dict[str, Any]:
        """Publish an IG feed image post (container → publish). Needs a public
        ``image_url``; without one it is cleanly skipped."""
        if not self.can_publish:
            return {"ok": False, "skipped": True, "reason": "Instagram not configured."}
        if not image_url:
            return {"ok": False, "skipped": True,
                    "reason": "Instagram needs a public image URL."}
        client = self._c()
        ig_user = self.cfg.get("instagram_user_id")
        container = client.ig_create_media(ig_user, image_url, caption)
        creation_id = container.get("id")
        if not creation_id:
            raise RuntimeError(f"Instagram media container failed: {container!r}")
        res = client.ig_publish_media(ig_user, creation_id)
        return {"ok": True, "ref": str(res.get("id", ""))}

    def test_connection(self) -> dict[str, Any]:
        if not self.can_publish:
            return {"ok": False, "configured": False,
                    "detail": "Set META_PAGE_ACCESS_TOKEN + INSTAGRAM_USER_ID."}
        try:
            node = self._c().get_node(self.cfg.get("instagram_user_id"), "username")
            return {"ok": True, "configured": True,
                    "detail": f"Connected to @{node.get('username', '')}".strip()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "detail": str(exc)}


class MetaGraphClient:
    """Minimal Meta Graph API client (Facebook + Instagram). Injectable."""

    def __init__(self, access_token: str | None, api_version: str = "v21.0",
                 timeout: float = 30.0) -> None:
        self.access_token = access_token
        self.base = f"https://graph.facebook.com/{api_version}"
        self.timeout = timeout

    def page_feed(self, page_id: str, message: str, link: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message, "access_token": self.access_token}
        if link:
            body["link"] = link
        return self._post(f"/{page_id}/feed", body)

    def page_video(self, page_id: str, video_url: str, description: str) -> dict[str, Any]:
        return self._post(f"/{page_id}/videos", {
            "file_url": video_url, "description": description,
            "access_token": self.access_token})

    def ig_create_media(self, ig_user_id: str, image_url: str,
                        caption: str) -> dict[str, Any]:
        return self._post(f"/{ig_user_id}/media", {
            "image_url": image_url, "caption": caption,
            "access_token": self.access_token})

    def ig_publish_media(self, ig_user_id: str, creation_id: str) -> dict[str, Any]:
        return self._post(f"/{ig_user_id}/media_publish", {
            "creation_id": creation_id, "access_token": self.access_token})

    def get_node(self, node_id: str, fields: str) -> dict[str, Any]:
        import httpx

        resp = httpx.get(f"{self.base}/{node_id}",
                         params={"fields": fields, "access_token": self.access_token},
                         timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Meta GET /{node_id} HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    def debug_token(self, input_token: str, app_id: str,
                    app_secret: str) -> dict[str, Any]:
        """Inspect a token: its type (USER/PAGE), granted scopes and expiry."""
        import httpx

        resp = httpx.get(f"{self.base}/debug_token", params={
            "input_token": input_token,
            "access_token": f"{app_id}|{app_secret}"}, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Meta debug_token HTTP {resp.status_code}: {resp.text}")
        return (resp.json() or {}).get("data", {})

    def exchange_long_lived(self, app_id: str, app_secret: str,
                            short_token: str) -> str:
        """Exchange a short-lived user token for a long-lived one (~60 days)."""
        import httpx

        resp = httpx.get(f"{self.base}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id,
            "client_secret": app_secret, "fb_exchange_token": short_token},
            timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Meta token exchange HTTP {resp.status_code}: {resp.text}")
        return resp.json().get("access_token", "")

    def list_pages(self, user_token: str) -> list[dict[str, Any]]:
        """The Pages this user manages, each with its own Page access token."""
        import httpx

        resp = httpx.get(f"{self.base}/me/accounts", params={
            "access_token": user_token, "fields": "id,name,access_token"},
            timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Meta GET /me/accounts HTTP {resp.status_code}: {resp.text}")
        return resp.json().get("data", [])

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        resp = httpx.post(f"{self.base}{path}", data=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Meta POST {path} HTTP {resp.status_code}: {resp.text}")
        return resp.json()
