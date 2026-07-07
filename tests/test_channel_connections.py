"""Connection-test tooling for each channel (Sprint 41.1)."""

from __future__ import annotations

from onassis.connectors.email_sender import EmailSender
from onassis.connectors.shopify import ShopifyConnector
from onassis.connectors.social import FacebookPublisher, InstagramPublisher


# --- Shopify ---------------------------------------------------------

def test_shopify_test_connection_ok(config):
    config.shopify = {"store_domain": "x.myshopify.com", "admin_token": "t"}
    client = type("C", (), {"get_shop": lambda self: {"shop": {"name": "My Store"}}})()
    r = ShopifyConnector(config, client=client).test_connection()
    assert r["ok"] is True and "My Store" in r["detail"]


def test_shopify_test_connection_reports_auth_error(config):
    config.shopify = {"store_domain": "x.myshopify.com", "admin_token": "bad"}

    def boom(self):
        raise RuntimeError("Shopify GET /shop.json HTTP 401: unauthorized")
    client = type("C", (), {"get_shop": boom})()
    r = ShopifyConnector(config, client=client).test_connection()
    assert r["ok"] is False and r["configured"] is True and "401" in r["detail"]


def test_shopify_test_connection_not_configured(config):
    config.shopify = {}
    r = ShopifyConnector(config).test_connection()
    assert r["ok"] is False and r["configured"] is False


# --- Meta (Facebook / Instagram) ------------------------------------

def test_facebook_test_connection_ok(config):
    config.meta = {"page_access_token": "t", "facebook_page_id": "123"}
    client = type("C", (), {"get_node": lambda self, i, f: {"name": "Local Celebrity"}})()
    r = FacebookPublisher(config, client=client).test_connection()
    assert r["ok"] is True and "Local Celebrity" in r["detail"]


def test_instagram_test_connection_ok(config):
    config.meta = {"page_access_token": "t", "instagram_user_id": "999"}
    client = type("C", (), {"get_node": lambda self, i, f: {"username": "localceleb"}})()
    r = InstagramPublisher(config, client=client).test_connection()
    assert r["ok"] is True and "localceleb" in r["detail"]


def test_meta_not_configured(config):
    config.meta = {}
    assert FacebookPublisher(config).test_connection()["configured"] is False
    assert InstagramPublisher(config).test_connection()["configured"] is False


# --- Email (SMTP) ----------------------------------------------------

def test_email_test_connection_ok(config):
    config.email = {"smtp_host": "smtp.x", "from_address": "a@x", "to_address": "b@x"}
    transport = type("T", (), {"test": lambda self: True})()
    r = EmailSender(config, transport=transport).test_connection()
    assert r["ok"] is True


def test_email_send_test_uses_transport(config):
    config.email = {"smtp_host": "smtp.x", "from_address": "a@x", "to_address": "b@x"}
    sent = {}

    class T:
        def send(self, *, sender, to, subject, body):
            sent.update(to=to, subject=subject)
            return "mid-1"
    r = EmailSender(config, transport=T()).send_test()
    assert r["ok"] is True and sent["to"] == "b@x" and "test" in sent["subject"].lower()


def test_email_not_configured_is_safe(config):
    config.email = {}
    assert EmailSender(config).test_connection()["configured"] is False
    assert EmailSender(config).send_test()["skipped"] is True
