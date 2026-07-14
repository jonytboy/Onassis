"""Campaign distribution via Make.com (Sprint 43).

ONASSIS builds ONE complete marketing package per product — every channel's
generated content, the images, and a rich campaign manifest — and sends it in a
single webhook to Make.com, which fans it out to the platforms. The package is
archived so a failed send retries without regenerating anything, and the
per-channel status Make returns is recorded for the dashboard and analytics.

Content generation, scheduling (the CMO), and analytics stay entirely inside
ONASSIS — only the *publishing mechanism* is delegated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from onassis.collections import collection_name
from onassis.connectors.make import MakeConnector
from onassis.logger import get_logger

log = get_logger(__name__)

# The marketing channels ONASSIS generates and Make distributes.
CHANNELS = ["facebook", "instagram", "pinterest", "tiktok", "blog", "email"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CampaignDistributor:
    """Assembles and sends complete marketing campaigns to Make.com."""

    def __init__(self, config: Any, db: Any, *, connector: MakeConnector | None = None) -> None:
        self.config = config
        self.db = db
        self.make = connector or MakeConnector(config)
        self.cfg = dict(getattr(config, "make", None) or {})

    # --- Package assembly (Obj 2 + manifest enhancement) ------------

    def _file_url(self, rel: str) -> str:
        base = (self.cfg.get("file_base_url") or "").rstrip("/")
        if not rel:
            return ""
        return f"{base}{rel}" if base and rel.startswith("/") else rel

    def build_package(self, campaign_id: int | None, product_key: str,
                      product_id: str | None = None) -> dict[str, Any]:
        """Assemble the complete campaign package from generated assets."""
        db = self.db
        product = db.get_product_by_sku(product_id) if product_id else None
        campaign = db.get_campaign(campaign_id) if campaign_id else None
        collection = collection_name(None, campaign) if campaign else ""

        assets = db.list_marketing_assets(product_key=product_key) if product_key else []
        by_channel: dict[str, Any] = {}
        scheduled: dict[str, Any] = {}
        for a in assets:
            by_channel.setdefault(a["channel"], a.get("payload") or {})
            if a.get("scheduled_date"):
                scheduled.setdefault(a["channel"], a["scheduled_date"])

        # Sales-channel URLs (Etsy / Shopify / Shopify blog).
        etsy_pub = (db.get_latest_publication(campaign_id, "etsy", product_id=product_id)
                    if campaign_id else None) or {}
        shop_pub = (db.get_latest_publication(campaign_id, "shopify", product_id=product_id)
                    if campaign_id else None) or {}
        etsy_url = (assets[0].get("listing_url") if assets else "") or ""
        shopify_url = shop_pub.get("listing_url") or ""
        blog_url = ""
        blog_asset = by_channel.get("blog") or {}
        if isinstance(blog_asset, dict):
            blog_url = blog_asset.get("url") or blog_asset.get("blog_url") or ""

        images = self._images(campaign_id, product_key)
        target_channels = [c for c in CHANNELS if c in by_channel]
        utm = f"onassis_{collection.lower().replace(' ', '_')}" if collection else "onassis"

        manifest = {
            "campaign_id": campaign_id, "product_id": product_id,
            "product_key": product_key, "collection": collection,
            "hero_image": images[0] if images else "",
            "secondary_images": images[1:],
            "etsy_url": etsy_url, "shopify_product_url": shopify_url,
            "shopify_blog_url": blog_url,
            "target_channels": target_channels,
            "scheduled_dates": scheduled,
            "utm_campaign": utm,
        }
        package: dict[str, Any] = {
            "source": "onassis", "event": "campaign",
            "campaign_id": campaign_id, "product_id": product_id,
            "product": {
                "title": (product or {}).get("name") or product_key,
                "product_key": product_key,
                "url": etsy_url or shopify_url,
                "collection": collection,
            },
            "images": images,
            "manifest": manifest,
        }
        for ch in CHANNELS:
            if ch in by_channel:
                package[ch] = by_channel[ch]
        return package

    def _images(self, campaign_id: int | None, product_key: str) -> list[str]:
        """Absolute image URLs (hero first) from the product's listing package."""
        if campaign_id is None or not product_key:
            return []
        import json as _json
        from pathlib import Path

        from onassis.config import ROOT_DIR
        base = Path((self.config.listing or {}).get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        folder = base / str(campaign_id) / str(product_key)
        listing_path = folder / "listing.json"
        if not listing_path.exists():
            return []
        try:
            listing = _json.loads(listing_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        rel = f"/exports/{campaign_id}/{product_key}"
        return [self._file_url(f"{rel}/images/{img['filename']}")
                for img in (listing.get("images") or [])
                if (folder / "images" / img.get("filename", "")).exists()]

    # --- Send / retry (Obj 3, 5, 6, 7) ------------------------------

    def distribute(self, campaign_id: int | None, product_key: str,
                   product_id: str | None = None) -> dict[str, Any]:
        """Build + archive + send one campaign to Make.com in a single webhook."""
        pid = product_id or (f"{campaign_id}-{product_key}" if campaign_id else product_key)
        package = self.build_package(campaign_id, product_key, product_id=pid)
        camp_id = self.db.insert_distribution_campaign({
            "campaign_id": campaign_id, "product_id": pid, "product_key": product_key,
            "collection": package["product"]["collection"],
            "status": "generated", "package": package})
        return self._send(camp_id, package)

    def retry(self, camp_id: int) -> dict[str, Any]:
        """Resend an archived campaign WITHOUT regenerating any content."""
        rec = self.db.get_distribution_campaign(camp_id)
        if not rec:
            return {"ok": False, "reason": "No such campaign."}
        self.db.update_distribution_campaign(camp_id, {
            "retry_count": int(rec.get("retry_count", 0)) + 1})
        return self._send(camp_id, rec.get("package") or {})

    def _send(self, camp_id: int, package: dict[str, Any]) -> dict[str, Any]:
        result = self.make.send(package)
        now = _now()
        if not result["ok"]:
            self.db.update_distribution_campaign(camp_id, {
                "status": "failed", "last_attempt": now,
                "failure_reason": result.get("detail")})
            return {"ok": False, "campaign": camp_id, "status": "failed",
                    "reason": result.get("detail")}
        # Make may return per-channel statuses synchronously.
        channel_status = {k: v for k, v in (result.get("response") or {}).items()
                          if k in CHANNELS}
        self.db.update_distribution_campaign(camp_id, {
            "status": "published" if channel_status else "sent",
            "sent_at": now, "last_attempt": now, "failure_reason": None,
            "channel_status": channel_status})
        return {"ok": True, "campaign": camp_id,
                "status": "published" if channel_status else "sent",
                "channels": channel_status}

    # --- Feedback (Obj 5) -------------------------------------------

    def record_feedback(self, campaign_id: int | None, statuses: dict[str, Any],
                        product_id: str | None = None) -> dict[str, Any]:
        """Record the per-channel status Make.com reports back (async webhook)."""
        rec = None
        if product_id:
            rec = self.db.latest_distribution_for_product(product_id)
        if rec is None and campaign_id is not None:
            for c in self.db.list_distribution_campaigns(limit=200):
                if c.get("campaign_id") == campaign_id:
                    rec = c
                    break
        if rec is None:
            return {"ok": False, "reason": "No matching campaign to attach status."}
        merged = {**(rec.get("channel_status") or {}),
                  **{k: v for k, v in statuses.items() if k in CHANNELS}}
        any_fail = any(str(v).lower() in ("failed", "error") for v in merged.values())
        status = "failed" if any_fail else ("published" if merged else rec.get("status"))
        self.db.update_distribution_campaign(rec["id"], {
            "channel_status": merged, "status": status, "last_attempt": _now()})
        return {"ok": True, "campaign": rec["id"], "status": status, "channels": merged}

    # --- Dashboard (Obj 9) ------------------------------------------

    def dashboard(self) -> dict[str, Any]:
        db = self.db
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        recent = db.list_distribution_campaigns(limit=50)
        sent = [c for c in recent if c.get("sent_at")]
        failed = [c for c in recent if c.get("status") == "failed"]
        return {
            "provider": "Make.com",
            "configured": self.make.is_configured,
            "webhook": _mask_webhook(self.make.webhook_url),
            "connected": self.make.is_configured,
            "campaigns_sent": db.count_distribution_campaigns(status="sent")
                              + db.count_distribution_campaigns(status="published"),
            "todays_campaigns": db.count_distribution_campaigns(on_date=today),
            "retry_queue": len(failed),
            "last_success": (sent[0]["sent_at"] if sent else None),
            "last_failure": (failed[0]["last_attempt"] if failed else None),
            "recent": recent[:15],
        }


def _mask_webhook(url: str) -> str:
    if not url:
        return ""
    return url[:28] + "…" if len(url) > 30 else url
