"""The Channel Distributor — ships the marketing kit beyond Pinterest.

The Marketing Engine already generates Instagram, Facebook, Blog and Email
assets for every live product and stores them; until now only Pinterest was ever
distributed (by the Traffic Engine). This manager closes that gap: it picks up
**undelivered** marketing assets and dispatches each to its channel connector,
recording the outcome (posted / failed / skipped) back on the asset so nothing
is sent twice and the dashboard can show real reach.

Every connector is gated + injectable, so with no credentials the distributor is
a safe, honest no-op (assets are recorded ``skipped`` with a reason) and it is
fully offline-testable. Operator channel toggles (Business Settings) are honoured.
"""

from __future__ import annotations

from typing import Any

from onassis.business_settings import BusinessSettings
from onassis.config import Config
from onassis.connectors.email_sender import EmailSender
from onassis.connectors.shopify import ShopifyConnector
from onassis.connectors.social import FacebookPublisher, InstagramPublisher
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# Pinterest is distributed by the Traffic Engine; these are the channels this
# manager owns.
CHANNELS = ["instagram", "facebook", "blog", "email"]


class ChannelDistributor:
    def __init__(self, config: Config, db: Database, *,
                 instagram: Any | None = None, facebook: Any | None = None,
                 email: Any | None = None, shopify: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.instagram = instagram or InstagramPublisher(config)
        self.facebook = facebook or FacebookPublisher(config)
        self.email = email or EmailSender(config)
        self.shopify = shopify or ShopifyConnector(config, db)   # blog target
        self.settings = BusinessSettings(db, config)

    # --- Capability -------------------------------------------------

    def can_distribute(self, channel: str) -> bool:
        return {
            "instagram": self.instagram.can_publish,
            "facebook": self.facebook.can_publish,
            "email": self.email.can_publish,
            "blog": self.shopify.can_publish and bool((self.config.shopify or {}).get("blog_id")),
        }.get(channel, False)

    def _enabled(self, channel: str) -> bool:
        """Respect the operator's channel toggles (Business Settings)."""
        try:
            if not self.settings.get("marketing_enabled"):
                return False
            if channel == "facebook":
                return bool(self.settings.get("facebook_enabled"))
            if channel == "email":
                return bool(self.settings.get("email_enabled"))
        except Exception:  # settings are advisory — never block on a read error
            return True
        return True

    # --- Distribute -------------------------------------------------

    def distribute(self, limit: int = 100, due_on: str | None = None,
                   channels: list[str] | None = None) -> dict[str, Any]:
        """Deliver every undelivered IG/FB/Blog/Email asset that is due today or
        earlier (or unscheduled). Idempotent. ``channels`` restricts to a subset
        (e.g. ['blog'] to publish only the first-party Shopify blog)."""
        from datetime import datetime, timezone

        chans = channels or CHANNELS
        due_on = due_on or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assets = self.db.list_pending_marketing_assets(
            channels=chans, due_on=due_on, limit=limit)
        counts = {"posted": 0, "failed": 0, "skipped": 0}
        by_channel: dict[str, dict[str, int]] = {c: {"posted": 0, "failed": 0, "skipped": 0}
                                                 for c in chans}
        for asset in assets:
            outcome = self._dispatch(asset)
            self.db.set_marketing_asset_delivery(
                asset["id"], outcome["status"], ref=outcome.get("ref"),
                error=outcome.get("error"))
            counts[outcome["status"]] += 1
            by_channel.setdefault(asset["channel"], {"posted": 0, "failed": 0, "skipped": 0})
            by_channel[asset["channel"]][outcome["status"]] += 1
        if assets:
            log.info("Distribution: %d asset(s) — %d posted, %d skipped, %d failed.",
                     len(assets), counts["posted"], counts["skipped"], counts["failed"])
        return {"processed": len(assets), **counts, "by_channel": by_channel}

    def retry_failed(self, channel: str | None = None) -> dict[str, Any]:
        """Re-queue and re-send failed deliveries — a failure is never silently
        dropped; it can always be retried."""
        requeued = self.db.reset_failed_marketing_assets(channel)
        result = self.distribute() if requeued else {"processed": 0, "posted": 0,
                                                     "skipped": 0, "failed": 0}
        result["requeued"] = requeued
        return result

    def _dispatch(self, asset: dict[str, Any]) -> dict[str, Any]:
        channel = asset["channel"]
        payload = asset.get("payload") or {}
        url = asset.get("listing_url") or ""
        if not self._enabled(channel):
            return {"status": "skipped", "error": f"{channel} disabled in settings"}
        try:
            if channel == "facebook":
                post = payload.get("post") or {}
                r = self.facebook.post(post.get("body") or "", post.get("link") or url)
            elif channel == "instagram":
                caption = (payload.get("captions") or [""])[0]
                r = self.instagram.post(caption, image_url=payload.get("image_url"))
            elif channel == "email":
                r = self.email.send(payload.get("subject") or "", payload.get("body") or "")
            elif channel == "blog":
                r = self._publish_blog(payload)
            else:
                r = {"ok": False, "skipped": True, "reason": f"no distributor for {channel}"}
        except Exception as exc:  # a channel failure is recorded, never fatal
            log.warning("Distribution to %s failed: %s", channel, exc)
            return {"status": "failed", "error": str(exc)}
        if r.get("ok"):
            return {"status": "posted", "ref": r.get("ref")}
        if r.get("skipped"):
            return {"status": "skipped", "error": r.get("reason")}
        return {"status": "failed", "error": r.get("reason")}

    def _publish_blog(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.can_distribute("blog"):
            if not self.shopify.can_publish:
                reason = "Shopify is not connected — cannot publish the blog."
            else:
                reason = ("No Shopify blog selected — set the Blog ID on the "
                          "Integrations → Shopify page (use 'List blogs').")
            return {"ok": False, "skipped": True, "reason": reason}
        # One product can generate multiple SEO articles (launch, gift guide,
        # lifestyle, …). Publish each and record their URLs.
        articles = payload.get("articles") or [payload]
        refs = []
        for a in articles:
            res = self.shopify.publish_article(a)
            if not res.get("ok"):
                # Never silently skip — report why (unverified / no id).
                reason = res.get("error") or "Shopify returned no article id."
                return {"ok": False, "reason": reason}
            refs.append(res.get("url") or res.get("id"))
        return {"ok": True, "ref": " | ".join(r for r in refs if r)}
