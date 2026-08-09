"""Tests for the Integration Manager (Sprint 41.1)."""

from __future__ import annotations

import pytest

from onassis.integrations import IntegrationManager, REGISTRY, mask


def _mgr(config, db, testers=None):
    return IntegrationManager(config, db, testers=testers)


# --- Registry + views ------------------------------------------------

def test_registry_covers_all_expected_integrations():
    keys = {i.key for i in REGISTRY}
    assert keys == {"anthropic", "openai", "etsy", "shopify", "pinterest",
                    "facebook", "instagram", "tiktok", "email", "gelato", "make"}


def test_describe_groups_and_summarises(config, db):
    d = _mgr(config, db).describe()
    assert set(d["categories"]) == {"AI", "Commerce", "Marketing", "Production"}
    assert sum(d["summary"].values()) == len(REGISTRY)
    assert "encryption" in d


def test_secrets_are_masked_by_default(config, db):
    config.shopify = {"store_domain": "x.myshopify.com", "client_secret": "supersecrettoken"}
    card = _mgr(config, db).detail("shopify")
    fields = {f["key"]: f for f in card["fields"]}
    assert fields["client_secret"]["secret"] is True
    assert fields["client_secret"]["value"] != "supersecrettoken"  # masked
    assert fields["store_domain"]["value"] == "x.myshopify.com"  # non-secret shown


def test_reveal_returns_raw_secret(config, db):
    config.shopify = {"store_domain": "x.myshopify.com", "client_secret": "supersecrettoken"}
    card = _mgr(config, db).detail("shopify", reveal=True)
    assert {f["key"]: f["value"] for f in card["fields"]}["client_secret"] == "supersecrettoken"


def test_mask_helper():
    assert mask(None) == "—"
    assert "…" in mask("supersecrettoken")
    assert mask("abc") == "set"


# --- Save (credential management) ------------------------------------

def test_save_persists_and_overlays_live_config(config, db):
    m = _mgr(config, db)
    m.save("shopify", {"store_domain": "s.myshopify.com", "client_id": "cid",
                       "client_secret": "csecret123"}, operator="jony")
    # Persisted (survives a fresh manager) + configured.
    card = _mgr(config, db).detail("shopify")
    assert card["configured"] is True
    # Live config was overlaid so connectors pick it up.
    assert config.shopify["client_secret"] == "csecret123"
    # Audited.
    assert any(e["kind"] == "credential_update" for e in card["events"])


def test_save_rejects_unknown_field(config, db):
    with pytest.raises(ValueError, match="Unknown field"):
        _mgr(config, db).save("shopify", {"nonsense": "x"})


# --- Test + health ---------------------------------------------------

def test_test_records_event_and_sets_health(config, db):
    config.shopify = {"store_domain": "x.myshopify.com", "client_id": "c", "client_secret": "s"}
    ok_tester = {"shopify": lambda c, r, d: {"ok": True, "configured": True, "detail": "Connected"}}
    m = _mgr(config, db, testers=ok_tester)
    r = m.test("shopify")
    assert r["ok"] is True and r["health"] == "healthy"
    assert db.last_integration_event("shopify", kind="test")["status"] == "ok"


def test_not_configured_reports_cleanly(config, db):
    config.shopify = {}
    r = _mgr(config, db).test("shopify")
    assert r["ok"] is False and r["health"] == "not_configured"


def test_failed_test_sets_failed_health(config, db):
    config.pinterest = {"access_token": "t", "board_id": "b"}
    bad = {"pinterest": lambda c, r, d: {"ok": False, "configured": True, "detail": "HTTP 401"}}
    m = _mgr(config, db, testers=bad)
    m.test("pinterest")
    assert _mgr(config, db).detail("pinterest")["health"] == "failed"


def test_health_warns_when_credentials_change_after_test(config, db):
    config.shopify = {"store_domain": "x.myshopify.com", "client_id": "c", "client_secret": "s"}
    m = _mgr(config, db, testers={"shopify": lambda c, r, d: {"ok": True, "configured": True}})
    m.test("shopify")
    assert _mgr(config, db).detail("shopify")["health"] == "healthy"
    m.save("shopify", {"client_secret": "newsecret"})     # change since last test
    assert _mgr(config, db, testers=m._testers).detail("shopify")["health"] == "warning"


def test_configured_but_untested_is_warning(config, db):
    config.email = {"smtp_host": "smtp.x", "from_address": "a@x", "to_address": "b@x"}
    assert _mgr(config, db).detail("email")["health"] == "warning"


def test_facebook_credentials_apply_to_config_meta(config, db):
    """Regression: the Facebook/Instagram integrations must overlay onto
    config.meta (where the connectors read), not a non-existent config.facebook —
    otherwise saved Page credentials never reach the poster."""
    from onassis.integrations import apply_integration_overrides

    config.meta = {}
    _mgr(config, db).save("facebook", {"page_access_token": "TOK",
                                       "facebook_page_id": "PAGE"})
    config.meta = {}
    apply_integration_overrides(config, db)
    assert config.meta.get("page_access_token") == "TOK"
    assert config.meta.get("facebook_page_id") == "PAGE"


def test_pricing_dials_overlay_onto_config(config, db):
    """Operator pricing dials (Business Settings) flow into config.pricing via
    apply_integration_overrides, so the pricing engine honours them live."""
    from onassis.integrations import apply_integration_overrides
    db.set_setting("business.pricing_adaptive", True)
    db.set_setting("business.adaptive_start_profit", 4.0)
    db.set_setting("business.shipping_cost", 6.5)
    apply_integration_overrides(config, db)
    assert config.pricing["strategy"] == "adaptive"
    assert config.pricing["adaptive_start_profit"] == 4.0
    assert config.pricing["shipping_cost"] == 6.5
