"""Shopify connector — publish products to a Shopify store (a second sales
channel alongside Etsy).

When a product is published, ONASSIS now creates it on Shopify **at the same
time** as the Etsy draft, from the same listing package (title, description,
tags, price, gallery images). Products are created **draft** by default (mirroring
Etsy's draft-first safety) and only set ``active`` when the go-live policy allows.

Gated + injectable, exactly like the Pinterest/Gelato connectors: without
``SHOPIFY_STORE_DOMAIN`` + ``SHOPIFY_ADMIN_TOKEN`` it is a safe no-op, and the
HTTP client is injectable so tests never touch the network. A blog-article helper
lets the marketing Blog channel publish to the store's blog.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from onassis.config import Config
from onassis.logger import get_logger

log = get_logger(__name__)


class ShopifyConnector:
    """Create/publish products (and blog articles) on a Shopify store."""

    name = "shopify"

    def __init__(self, config: Config, db: Any | None = None,
                 client: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = config.shopify or {}
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self.cfg.get("store_domain") and self.cfg.get("admin_token"))

    @property
    def can_publish(self) -> bool:
        return self._client is not None or self.is_configured

    def _c(self) -> Any:
        if self._client is None:
            self._client = ShopifyAdminClient(
                store_domain=self.cfg.get("store_domain"),
                admin_token=self.cfg.get("admin_token"),
                api_version=self.cfg.get("api_version", "2024-10"))
        return self._client

    def test_connection(self) -> dict[str, Any]:
        """Read-only auth check — confirms the store + token work (GET shop.json)."""
        if not self.can_publish:
            return {"ok": False, "configured": False,
                    "detail": "Set SHOPIFY_STORE_DOMAIN + SHOPIFY_ADMIN_TOKEN."}
        try:
            shop = (self._c().get_shop() or {}).get("shop", {})
            return {"ok": True, "configured": True,
                    "detail": f"Connected to {shop.get('name') or self.cfg.get('store_domain')}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "detail": str(exc)}

    # --- Product publishing -----------------------------------------

    @staticmethod
    def build_product(listing: dict[str, Any], *, active: bool = False) -> dict[str, Any]:
        """Map an ONASSIS listing package onto a Shopify product payload."""
        tags = ", ".join(str(t) for t in (listing.get("tags") or []))
        price = listing.get("price") or listing.get("retail_price") or 0
        return {"product": {
            "title": (listing.get("title") or listing.get("product_name") or "New product"),
            "body_html": listing.get("description") or "",
            "tags": tags,
            "status": "active" if active else "draft",
            "vendor": listing.get("brand") or "",
            "variants": [{
                "price": f"{float(price):.2f}",
                "sku": listing.get("product_id") or listing.get("product_key") or "",
                "requires_shipping": True,
            }],
        }}

    def publish_product(self, listing: dict[str, Any], *, images_dir: Path | None = None,
                        active: bool = False) -> dict[str, Any]:
        """Create the product on Shopify and upload its gallery images.

        Returns ``{ok, product_id, handle, url, status, images_uploaded, images_failed}``.
        Raises on a hard failure (so the caller can retry/record), but image
        failures are best-effort and never abort a created product.
        """
        client = self._c()
        result = client.create_product(self.build_product(listing, active=active))
        product = (result or {}).get("product") or {}
        product_id = product.get("id")
        if not product_id:
            raise RuntimeError(f"Shopify did not return a product id (got {result!r}).")
        uploads = self._upload_images(client, str(product_id), listing, images_dir)
        handle = product.get("handle") or ""
        domain = self.cfg.get("store_domain") or ""
        url = f"https://{domain}/products/{handle}" if (domain and handle) else ""
        return {"ok": True, "product_id": str(product_id), "handle": handle,
                "url": url, "status": product.get("status", "draft"),
                "images_uploaded": uploads["uploaded"], "images_failed": uploads["failed"]}

    def set_active(self, product_id: str) -> dict[str, Any]:
        """Take a draft product live on Shopify."""
        return self._c().update_product(product_id, {"product": {"id": product_id,
                                                                 "status": "active"}})

    def _upload_images(self, client: Any, product_id: str, listing: dict[str, Any],
                       images_dir: Path | None) -> dict[str, int]:
        images = listing.get("images") or []
        if images_dir is None or not hasattr(client, "add_product_image"):
            return {"uploaded": 0, "failed": 0, "skipped": len(images)}
        uploaded, failed = 0, 0
        for img in images:
            path = images_dir / img.get("filename", "")
            if not path.exists():
                failed += 1
                continue
            try:
                client.add_product_image(product_id, str(path),
                                         position=img.get("order", 1),
                                         alt_text=img.get("alt_text"))
                uploaded += 1
            except Exception as exc:  # keep the product; record the miss
                failed += 1
                log.warning("Shopify image upload failed (%s): %s", path.name, exc)
        return {"uploaded": uploaded, "failed": failed, "skipped": 0}

    # --- Blog (for the marketing Blog channel) ----------------------

    def publish_article(self, article: dict[str, Any]) -> dict[str, Any]:
        """Publish a blog article to the store's blog. ``article`` needs a
        ``title`` and ``body`` (HTML/markdown); ``blog_id`` falls back to config."""
        blog_id = article.get("blog_id") or self.cfg.get("blog_id")
        if not blog_id:
            raise RuntimeError("No Shopify blog_id configured for article publishing.")
        return self._c().create_article(str(blog_id), {
            "article": {"title": article.get("title") or "New post",
                        "body_html": article.get("body") or "",
                        "tags": ", ".join(article.get("keywords") or []),
                        "published": True}})


class ShopifyAdminClient:
    """Minimal Shopify Admin REST client. Injectable for tests."""

    def __init__(self, store_domain: str | None, admin_token: str | None,
                 api_version: str = "2024-10", timeout: float = 30.0) -> None:
        self.store_domain = (store_domain or "").replace("https://", "").strip("/")
        self.admin_token = admin_token
        self.api_version = api_version
        self.timeout = timeout

    @property
    def _base(self) -> str:
        return f"https://{self.store_domain}/admin/api/{self.api_version}"

    def _headers(self) -> dict[str, str]:
        return {"X-Shopify-Access-Token": self.admin_token or "",
                "Content-Type": "application/json"}

    def get_shop(self) -> dict[str, Any]:
        return self._request("GET", "/shop.json", None)

    def create_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/products.json", payload)

    def update_product(self, product_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._put(f"/products/{product_id}.json", payload)

    def add_product_image(self, product_id: str, image_path: str, *, position: int = 1,
                          alt_text: str | None = None) -> dict[str, Any]:
        data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        body = {"image": {"attachment": data, "position": position,
                          "alt": alt_text or ""}}
        return self._post(f"/products/{product_id}/images.json", body)

    def create_article(self, blog_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/blogs/{blog_id}/articles.json", payload)

    # --- HTTP -------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def _put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", path, body)

    def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        resp = httpx.request(method, f"{self._base}{path}", headers=self._headers(),
                             json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Shopify {method} {path} HTTP {resp.status_code}: {resp.text}")
        return resp.json()
