"""Tests for Make.com marketing distribution (Sprint 43)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from onassis.campaign_distributor import CampaignDistributor
from onassis.connectors.make import MakeConnector


class FakeResponse:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


def _fake_client(response=None, *, boom=False):
    calls = []

    def client(url, json=None, headers=None):
        calls.append({"url": url, "json": json, "headers": headers})
        if boom:
            raise RuntimeError("network down")
        return response or FakeResponse()

    client.calls = calls
    return client


def _configured(config):
    config.make = {"webhook_url": "https://hook.eu1.make.com/abc123",
                   "api_key": "sek", "file_base_url": "https://cdn.host"}
    return config


def _seed_campaign_with_marketing(db):
    brief_id = db.insert_brief({"brief_date": "2026-07-01", "theme": "Salt", "keywords": []})
    cid = db.insert_campaign({"name": "Mediterranean Morning", "brief_id": brief_id})
    sku = f"{cid}-mug"
    db.insert_product({"sku": sku, "name": "Ceramic Mug", "campaign_id": cid, "product_key": "mug"})
    for ch in ("facebook", "instagram", "pinterest", "tiktok", "email", "blog"):
        db.insert_marketing_asset({"campaign_id": cid, "product_key": "mug", "channel": ch,
                                   "listing_url": "https://etsy.com/listing/1",
                                   "payload": {"body": f"{ch} post"}})
    return cid, sku


# --- Connector -------------------------------------------------------

def test_connector_gate_and_headers(config):
    assert MakeConnector(config).is_configured is False        # no webhook
    _configured(config)
    m = MakeConnector(config, client=_fake_client())
    assert m.is_configured is True
    m.send({"hello": "world"})
    sent = m._client.calls[0]
    assert sent["url"].startswith("https://hook.eu1.make.com")
    assert sent["headers"]["x-make-apikey"] == "sek"


def test_send_reports_http_error(config):
    _configured(config)
    m = MakeConnector(config, client=_fake_client(FakeResponse(500, {"error": "boom"})))
    r = m.send({"x": 1})
    assert r["ok"] is False and "500" in r["detail"]


def test_send_not_configured_is_safe(config):
    r = MakeConnector(config).send({"x": 1})
    assert r["ok"] is False and r["status"] == "not_configured"


# --- CampaignDistributor ---------------------------------------------

def test_build_package_has_all_channels_and_manifest(config, db):
    _configured(config)
    cid, sku = _seed_campaign_with_marketing(db)
    dist = CampaignDistributor(config, db, connector=MakeConnector(config, client=_fake_client()))
    pkg = dist.build_package(cid, "mug", product_id=sku)
    assert pkg["campaign_id"] == cid and pkg["product"]["collection"].endswith("Collection")
    for ch in ("facebook", "instagram", "pinterest", "tiktok", "email", "blog"):
        assert ch in pkg
    man = pkg["manifest"]
    assert set(man["target_channels"]) >= {"facebook", "instagram", "pinterest", "tiktok", "email"}
    assert man["utm_campaign"].startswith("onassis_")
    assert "collection" in man and "hero_image" in man


def test_distribute_sends_one_webhook_and_archives(config, db):
    _configured(config)
    cid, sku = _seed_campaign_with_marketing(db)
    client = _fake_client(FakeResponse(200, {"facebook": "posted", "instagram": "scheduled"}))
    dist = CampaignDistributor(config, db, connector=MakeConnector(config, client=client))
    r = dist.distribute(cid, "mug", product_id=sku)
    assert r["ok"] and r["status"] == "published"
    assert r["channels"]["facebook"] == "posted"
    assert len(client.calls) == 1                              # exactly one webhook
    rec = db.latest_distribution_for_product(sku)
    assert rec["status"] == "published" and rec["package"]["campaign_id"] == cid


def test_failed_send_can_retry_without_regeneration(config, db):
    _configured(config)
    cid, sku = _seed_campaign_with_marketing(db)
    boom = CampaignDistributor(config, db,
                               connector=MakeConnector(config, client=_fake_client(boom=True)))
    r = boom.distribute(cid, "mug", product_id=sku)
    assert r["ok"] is False
    rec = db.latest_distribution_for_product(sku)
    assert rec["status"] == "failed" and rec["package"]["campaign_id"] == cid   # package kept

    # Retry reuses the archived package (no rebuild) with a working connector.
    ok = CampaignDistributor(config, db,
                             connector=MakeConnector(config, client=_fake_client()))
    res = ok.retry(rec["id"])
    assert res["ok"] is True
    again = db.get_distribution_campaign(rec["id"])
    assert again["retry_count"] == 1 and again["status"] in ("sent", "published")


def test_record_feedback_updates_channel_status(config, db):
    _configured(config)
    cid, sku = _seed_campaign_with_marketing(db)
    dist = CampaignDistributor(config, db, connector=MakeConnector(config, client=_fake_client()))
    dist.distribute(cid, "mug", product_id=sku)
    fb = dist.record_feedback(cid, {"tiktok": "posted", "email": "sent"}, product_id=sku)
    assert fb["ok"] and fb["channels"]["tiktok"] == "posted"


def test_dashboard_summary(config, db):
    _configured(config)
    cid, sku = _seed_campaign_with_marketing(db)
    dist = CampaignDistributor(config, db, connector=MakeConnector(config, client=_fake_client()))
    dist.distribute(cid, "mug", product_id=sku)
    d = dist.dashboard()
    assert d["provider"] == "Make.com" and d["connected"] is True
    assert d["campaigns_sent"] >= 1 and "retry_queue" in d
