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
from typing import Any, Callable

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
        """Configured when we have a store + a way to get a token: either the new
        Client ID/Secret (client-credentials grant) or a legacy Admin token."""
        c = self.cfg
        has_client = bool(c.get("client_id") and c.get("client_secret"))
        has_token = bool(c.get("admin_token") or c.get("access_token"))
        return bool(c.get("store_domain") and (has_client or has_token))

    @property
    def can_publish(self) -> bool:
        return self._client is not None or self.is_configured

    def _c(self) -> Any:
        if self._client is None:
            c = self.cfg
            domain = c.get("store_domain")
            version = c.get("api_version", "2024-10")
            static = c.get("admin_token") or c.get("access_token")
            if static:  # legacy static Admin token — still supported
                self._client = ShopifyAdminClient(
                    store_domain=domain, access_token=static, api_version=version)
            else:       # new apps: obtain a token via the client-credentials grant
                from onassis.connectors.shopify_oauth import ShopifyTokenProvider

                provider = ShopifyTokenProvider(
                    store_domain=domain, client_id=c.get("client_id"),
                    client_secret=c.get("client_secret"), api_version=version)
                self._client = ShopifyAdminClient(
                    store_domain=domain, token_provider=provider.valid_access_token,
                    api_version=version)
        return self._client

    def test_connection(self) -> dict[str, Any]:
        """Read-only auth check — confirms the store + token work (GET shop.json)."""
        if not self.can_publish:
            return {"ok": False, "configured": False,
                    "detail": "Set Store URL + Client ID + Client Secret."}
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

    def product_details(self, product_id: str) -> dict[str, Any]:
        """Storefront URL + first image ``src`` for a published product (cached).
        Lets the blog link to the Shopify product and reuse its image as the
        article's featured image instead of the theme's placeholder."""
        cache = getattr(self, "_prod_cache", None)
        if cache is None:
            cache = self._prod_cache = {}
        pid = str(product_id)
        if pid in cache:
            return cache[pid]
        out = {"url": "", "image_url": ""}
        try:
            p = (self._c().get_product(pid) or {}).get("product") or {}
            handle = p.get("handle") or ""
            domain = self.cfg.get("store_domain") or ""
            if domain and handle:
                out["url"] = f"https://{domain}/products/{handle}"
            imgs = p.get("images") or []
            if imgs:
                out["image_url"] = imgs[0].get("src") or ""
            elif p.get("image"):
                out["image_url"] = (p["image"] or {}).get("src") or ""
        except Exception as exc:  # best-effort — never block the blog on this
            log.warning("Shopify product details fetch failed (%s): %s", pid, exc)
        cache[pid] = out
        return out

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

    def list_blogs(self) -> list[dict[str, Any]]:
        """Fetch the store's blogs so the operator can pick one (no manual id)."""
        blogs = (self._c().list_blogs() or {}).get("blogs", [])
        return [{"id": str(b.get("id")), "title": b.get("title", "")} for b in blogs]

    def blog_diagnostics(self) -> dict[str, Any]:
        """Read the ground truth from Shopify so 'it says posted but there's no
        blog' can be pinned down: every blog on the store with its article count,
        which blog is selected, and the selected blog's articles with their real
        published state + storefront URL. This reveals the usual culprit — posts
        landing on a DIFFERENT blog than the one the storefront theme shows."""
        client = self._c()
        domain = self.cfg.get("store_domain") or ""
        selected = str(self.cfg.get("blog_id") or "")
        blogs_raw = (client.list_blogs() or {}).get("blogs", [])
        blogs = []
        for b in blogs_raw:
            bid = str(b.get("id"))
            try:
                arts = (client.list_articles(bid) or {}).get("articles", [])
                count = len(arts)
            except Exception:
                count = None
            blogs.append({"id": bid, "title": b.get("title", ""),
                          "handle": b.get("handle", ""), "articles": count,
                          "selected": bid == selected})
        sel_articles = []
        if selected:
            try:
                for a in (client.list_articles(selected) or {}).get("articles", [])[:25]:
                    handle = a.get("handle") or ""
                    bhandle = self._blog_handle(selected) or selected
                    sel_articles.append({
                        "id": a.get("id"), "title": a.get("title"),
                        "published": a.get("published"),
                        "published_at": a.get("published_at"),
                        "visible": self._is_visible(a),
                        "url": (f"https://{domain}/blogs/{bhandle}/{handle}"
                                if (domain and handle) else "")})
            except Exception as exc:  # noqa: BLE001
                sel_articles = [{"error": str(exc)}]
        return {"store_domain": domain, "selected_blog_id": selected,
                "blogs": blogs, "selected_articles": sel_articles}

    def republish_hidden(self, blog_id: str | None = None) -> dict[str, Any]:
        """Make every article on the blog visible NOW — recovery for posts that
        were created with a future ``published_at`` (a server clock ahead of
        Shopify's) and are stuck as hidden/scheduled, so 'there's just no blog'.

        For each article that isn't currently visible (unpublished, or a
        ``published_at`` that isn't in the past) it PUTs ``published: true`` and
        clears ``published_at`` so Shopify re-stamps it to now. Returns
        ``{checked, fixed, already_live}``."""
        blog_id = str(blog_id or self.cfg.get("blog_id") or "")
        if not blog_id:
            raise RuntimeError("No Shopify blog selected.")
        client = self._c()
        articles = (client.list_articles(blog_id) or {}).get("articles", [])
        domain = self.cfg.get("store_domain") or ""
        checked, fixed, live = 0, 0, 0
        details = []
        for a in articles:
            checked += 1
            was_live = self._is_visible(a)
            if not was_live:
                client.update_article(blog_id, str(a.get("id")),
                                      {"article": {"id": a.get("id"), "published": True,
                                                   "published_at": None}})
                fixed += 1
            else:
                live += 1
            details.append({
                "id": a.get("id"), "title": a.get("title"),
                "was_visible": was_live, "published_at": a.get("published_at"),
                "admin_url": (f"https://{domain}/admin/blogs/{blog_id}/articles/{a.get('id')}"
                              if domain else "")})
        # 0 articles on this blog while our records say we posted some ⇒ the wrong
        # blog is selected (the posts went to a different blog). Name that clearly.
        note = ("No articles exist on the selected blog (id %s) — the posts likely "
                "went to a DIFFERENT blog. Re-pick the blog on Integrations → Shopify."
                % blog_id) if checked == 0 else (
                    f"{fixed} re-published, {live} already live.")
        return {"checked": checked, "fixed": fixed, "already_live": live,
                "blog_id": blog_id, "note": note, "details": details[:50]}

    def rewrite_blog_articles(self, articles_by_title: dict[str, dict[str, Any]],
                              *, blog_id: str | None = None) -> dict[str, Any]:
        """Update EXISTING live articles in place with a fresh body/image/links —
        matched by title. Never creates or deletes; articles with no match are
        left untouched. For fixing posts published before a content change (e.g.
        adding the Shopify product link + featured image). Returns
        ``{checked, rewritten, skipped, details}``."""
        blog_id = str(blog_id or self.cfg.get("blog_id") or "")
        if not blog_id:
            raise RuntimeError("No Shopify blog selected.")

        def _norm(t: str | None) -> str:
            return " ".join((t or "").split()).strip().lower()

        client = self._c()
        want = {_norm(k): v for k, v in articles_by_title.items()}
        live = (client.list_articles(blog_id) or {}).get("articles", [])
        checked = rewritten = skipped = 0
        details = []
        for a in live:
            checked += 1
            match = want.get(_norm(a.get("title")))
            if not match:
                skipped += 1
                continue
            art = {"id": a.get("id"), "body_html": match.get("body") or "",
                   "tags": ", ".join(match.get("keywords") or [])}
            if match.get("image"):
                art["image"] = {"src": match["image"]}
            try:
                client.update_article(blog_id, str(a.get("id")), {"article": art})
                rewritten += 1
                details.append({"id": a.get("id"), "title": a.get("title"), "ok": True})
            except Exception as exc:  # noqa: BLE001 — record, keep going
                skipped += 1
                details.append({"id": a.get("id"), "title": a.get("title"),
                                "ok": False, "error": str(exc)})
        return {"checked": checked, "rewritten": rewritten, "skipped": skipped,
                "blog_id": blog_id, "details": details[:50]}

    @staticmethod
    def _is_visible(article: dict[str, Any]) -> bool:
        """An article is live when it's published and its published_at is not in
        the future. A missing/None published_at with published truthy counts live."""
        from datetime import datetime, timezone

        if article.get("published") is False:
            return False
        pub = article.get("published_at")
        if not pub:
            return bool(article.get("published", True))
        try:
            when = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when <= datetime.now(timezone.utc)
        except ValueError:
            return True   # unparseable → assume live, don't churn it

    def publish_article(self, article: dict[str, Any]) -> dict[str, Any]:
        """Publish a blog article to the store's blog and return a verifiable
        result ``{ok, id, handle, url, verified}``. ``article`` needs a
        ``title`` and ``body``; ``blog_id`` falls back to config.

        After creation the article is fetched back (Sprint 42.1, Obj 5) to
        confirm it really exists on the store — a created id alone isn't proof
        of a live post — and to capture the canonical URL. A failed verification
        is reported (``ok`` stays False), never silently skipped."""
        blog_id = article.get("blog_id") or self.cfg.get("blog_id")
        if not blog_id:
            raise RuntimeError("No Shopify blog selected — pick one on the "
                               "Integrations page (Shopify → list blogs).")
        client = self._c()
        body = {"title": article.get("title") or "New post",
                "body_html": article.get("body") or "",
                "tags": ", ".join(article.get("keywords") or []),
                # published: true publishes immediately — Shopify stamps
                # published_at itself. Do NOT send our own published_at:
                # if the server clock is even slightly ahead of Shopify's,
                # Shopify reads it as a FUTURE time and hides the article
                # as 'scheduled' (the whole blog looks empty).
                "published": True}
        # A featured image so the theme shows the product, not its placeholder.
        img = article.get("image")
        if img:
            body["image"] = {"src": img}
        resp = client.create_article(str(blog_id), {"article": body})
        art = (resp or {}).get("article") or {}
        art_id = art.get("id")
        if not art_id:
            return {"ok": False, "id": "", "handle": "", "url": "",
                    "verified": False, "error": "Shopify returned no article id."}
        # Verify the article is retrievable — proof it exists on the store.
        verified, verify_error = True, ""
        try:
            confirmed = (client.get_article(str(blog_id), str(art_id)) or {}).get("article") or {}
            if confirmed.get("id"):
                art = {**art, **confirmed}   # canonical fields (url/handle) win
            else:
                verified, verify_error = False, "Article not found after publish."
        except Exception as exc:            # verification is best-effort but reported
            verified, verify_error = False, f"Verification failed: {exc}"
        handle = art.get("handle") or ""
        domain = self.cfg.get("store_domain") or ""
        # Storefront blog URLs use the blog HANDLE, not its numeric id — using the
        # id gives a 404 that looks like "it didn't post" even though it did. We
        # build the URL from the handle ourselves rather than trusting the
        # Admin-API ``url`` field, which points at the numeric-id path.
        blog_handle = self._blog_handle(str(blog_id)) or str(blog_id)
        url = (f"https://{domain}/blogs/{blog_handle}/{handle}"
               if (domain and handle) else "")
        admin_url = (f"https://{domain}/admin/blogs/{blog_id}/articles/{art_id}"
                     if domain else "")
        return {"ok": bool(art_id) and verified, "id": str(art_id),
                "handle": handle, "url": url, "admin_url": admin_url,
                "verified": verified, "error": verify_error}

    def _blog_handle(self, blog_id: str) -> str | None:
        """The blog's handle (for storefront URLs), cached from list_blogs."""
        cache = getattr(self, "_blog_handles", None)
        if cache is None:
            cache = self._blog_handles = {}
        if blog_id in cache:
            return cache[blog_id]
        try:
            for b in (self._c().list_blogs() or {}).get("blogs", []):
                cache[str(b.get("id"))] = b.get("handle")
        except Exception:  # URL nicety only — never break publishing on it
            pass
        return cache.get(blog_id)


class ShopifyAdminClient:
    """Minimal Shopify Admin REST client. Injectable for tests.

    Accepts either a static ``access_token`` (legacy Admin token) or a
    ``token_provider`` callable ``provider(force: bool) -> token`` (the
    client-credentials flow). On a 401 it refreshes via the provider once and
    retries, so an expired token heals itself transparently.
    """

    def __init__(self, store_domain: str | None, *, access_token: str | None = None,
                 token_provider: Callable[..., str] | None = None,
                 api_version: str = "2024-10", timeout: float = 30.0) -> None:
        self.store_domain = (store_domain or "").replace("https://", "").strip("/")
        self._access_token = access_token
        self._token_provider = token_provider
        self.api_version = api_version
        self.timeout = timeout

    def _token(self, force: bool = False) -> str:
        if self._token_provider is not None:
            return self._token_provider(force)
        return self._access_token or ""

    @property
    def _base(self) -> str:
        return f"https://{self.store_domain}/admin/api/{self.api_version}"

    def _headers(self, token: str) -> dict[str, str]:
        return {"X-Shopify-Access-Token": token or "",
                "Content-Type": "application/json"}

    def get_shop(self) -> dict[str, Any]:
        return self._request("GET", "/shop.json", None)

    def create_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/products.json", payload)

    def get_product(self, product_id: str) -> dict[str, Any]:
        return self._request("GET", f"/products/{product_id}.json", None)

    def update_product(self, product_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._put(f"/products/{product_id}.json", payload)

    def add_product_image(self, product_id: str, image_path: str, *, position: int = 1,
                          alt_text: str | None = None) -> dict[str, Any]:
        data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        body = {"image": {"attachment": data, "position": position,
                          "alt": alt_text or ""}}
        return self._post(f"/products/{product_id}/images.json", body)

    def list_blogs(self) -> dict[str, Any]:
        return self._request("GET", "/blogs.json", None)

    def create_article(self, blog_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/blogs/{blog_id}/articles.json", payload)

    def get_article(self, blog_id: str, article_id: str) -> dict[str, Any]:
        return self._request("GET", f"/blogs/{blog_id}/articles/{article_id}.json", None)

    def list_articles(self, blog_id: str) -> dict[str, Any]:
        return self._request("GET", f"/blogs/{blog_id}/articles.json?limit=250", None)

    def update_article(self, blog_id: str, article_id: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
        return self._put(f"/blogs/{blog_id}/articles/{article_id}.json", payload)

    # --- HTTP -------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def _put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", path, body)

    def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        url = f"{self._base}{path}"
        resp = httpx.request(method, url, headers=self._headers(self._token()),
                             json=body, timeout=self.timeout)
        # An expired token → refresh once and retry (client-credentials flow).
        if resp.status_code == 401 and self._token_provider is not None:
            resp = httpx.request(method, url, headers=self._headers(self._token(force=True)),
                                 json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Shopify {method} {path} HTTP {resp.status_code}: {resp.text}")
        return resp.json()
