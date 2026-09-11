"""Tests for the Personaliser — engine, Etsy-unlock, and the buyer web flow."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from onassis.personaliser import (PRODUCTS, render_tier1, transform_photo,
                                  upscale_for_print, verify_order)

SAMPLES = {
    "place-poster": {"place": "Positano, Italy", "lat": 40.628, "lon": 14.485,
                     "date": "2024-06-14", "names": "Sam & Ellie", "message": "where it began"},
    "star-map": {"place": "London", "lat": 51.5, "lon": -0.13, "date": "2024-06-21",
                 "time": "22:30", "message": "the night we said yes"},
    "birth-stats": {"name": "Isla Rose", "date": "2024-03-08", "time": "06:42",
                    "weight": "3.4 kg", "length": "51 cm", "place": "Bristol"},
    "invite": {"names": "Sam & Ellie", "date": "2025-06-14", "time": "15:00",
               "venue": "Ravello", "message": "dinner & dancing"},
}


# --- Tier 1 renderers ----------------------------------------------------

@pytest.mark.parametrize("key", list(SAMPLES))
def test_every_tier1_product_renders_a_poster(key):
    img = render_tier1(key, SAMPLES[key], size=320)
    assert img.size == (320, int(320 * 1.4))


def test_preview_is_watermarked_and_final_is_not():
    pv = render_tier1("invite", SAMPLES["invite"], size=320, preview=True)
    fin = render_tier1("invite", SAMPLES["invite"], size=320)
    assert pv.tobytes() != fin.tobytes()            # the PREVIEW stamp differs


def test_registry_has_both_tiers():
    tiers = {p.tier for p in PRODUCTS.values()}
    assert tiers == {1, 2}
    assert all(p.styles for p in PRODUCTS.values() if p.tier == 2)


# --- Tier 2: AI-from-photo ----------------------------------------------

def _png_bytes(size=(64, 64)) -> bytes:
    """A photo-like PNG (random pixels, so it doesn't compress to a few bytes)."""
    import os
    b = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(b, "PNG")
    return b.getvalue()


def test_transform_photo_needs_a_provider_with_edit(config):
    config.image = {"backend": "local"}                 # dev renderer: no edit()
    with pytest.raises(RuntimeError, match="not configured"):
        transform_photo(config, _png_bytes(), "pet-portrait", "oil")


def test_transform_photo_returns_variations_and_propagates_errors(config, monkeypatch):
    from onassis import personaliser as P

    class _Backend:
        name = "openai"; model = "gpt-image-1"; quality = "high"
        def __init__(self, fail=False): self.fail = fail
        def edit(self, image_bytes, prompt, *, n=3, size="1024x1536"):
            if self.fail:
                raise RuntimeError("boom")
            assert "exact animal" in prompt            # style prompt reached the model
            return [_png_bytes()] * n

    monkeypatch.setattr("onassis.connectors.image_backend.build_image_backend",
                        lambda cfg: _Backend())
    out = transform_photo(config, _png_bytes(), "pet-portrait", "oil", n=3)
    assert len(out) == 3
    monkeypatch.setattr("onassis.connectors.image_backend.build_image_backend",
                        lambda cfg: _Backend(fail=True))
    with pytest.raises(RuntimeError):
        transform_photo(config, _png_bytes(), "pet-portrait", "oil")


def test_upscale_for_print_reaches_target_long_edge():
    out = upscale_for_print(_png_bytes((100, 150)), target_long_edge=600)
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 600


# --- Unlocking by Etsy order --------------------------------------------

def test_verify_order_demo_code_and_empty(config, db):
    assert verify_order(config, db, "DEMO") == (True, "demo")
    ok, why = verify_order(config, db, "")
    assert ok is False and "order number" in why


def test_verify_order_looks_up_etsy_receipt(config, db, monkeypatch):
    class _Client:
        def __init__(self, found): self.found = found
        def get_receipt(self, rid):
            if not self.found:
                raise RuntimeError("404")
            return {"receipt_id": int(rid)}

    class _Engine:
        def __init__(self, found): self.client = _Client(found); self.is_configured = True

    import onassis.etsy_automation as EA
    monkeypatch.setattr(EA, "EtsyAutomationEngine", lambda c, d: _Engine(found=True))
    assert verify_order(config, db, "3412345678") == (True, "etsy")
    monkeypatch.setattr(EA, "EtsyAutomationEngine", lambda c, d: _Engine(found=False))
    ok, why = verify_order(config, db, "999")
    assert ok is False and "couldn't find" in why


# --- The buyer web flow (end to end, tier 1) -----------------------------

@pytest.fixture
def client(config, db, tmp_path):
    from fastapi.testclient import TestClient

    from onassis.personaliser_web import build_personaliser_router
    from fastapi import FastAPI
    config.personaliser = {"out_dir": str(tmp_path / "out"), "render_px": 400}
    app = FastAPI()
    app.include_router(build_personaliser_router(config, db))
    return TestClient(app)


def test_index_and_product_pages(client):
    r = client.get("/make")
    assert r.status_code == 200 and "Custom Pet Portrait" in r.text
    r = client.get("/make/place-poster")
    assert r.status_code == 200 and "Etsy order number" in r.text
    assert client.get("/make/nope").status_code == 404


def test_unlock_rejects_a_bad_order_and_accepts_demo(client):
    r = client.post("/make/invite/unlock", json={"order_ref": "not-an-order"})
    assert r.status_code == 403
    r = client.post("/make/invite/unlock", json={"order_ref": "DEMO"})
    assert r.status_code == 200 and r.json()["token"]


def test_full_tier1_flow_preview_finalize_download(client):
    token = client.post("/make/place-poster/unlock", json={"order_ref": "DEMO"}).json()["token"]
    # Preview is a watermarked PNG.
    r = client.post("/make/place-poster/preview",
                    json={"token": token, "fields": SAMPLES["place-poster"]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    # Finalize renders the full file and hands back a download link.
    r = client.post("/make/place-poster/finalize",
                    json={"token": token, "fields": SAMPLES["place-poster"]})
    assert r.status_code == 200
    dl = r.json()["download"]
    r = client.get(dl)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.size[0] == 400                          # render_px honoured


def test_preview_without_unlock_is_refused(client):
    r = client.post("/make/invite/preview", json={"token": "nope", "fields": {}})
    assert r.status_code == 403


def test_tier2_preview_stores_variations_and_finalize_picks_one(client, monkeypatch):
    from onassis import personaliser_web as W
    monkeypatch.setattr(W, "transform_photo",
                        lambda cfg, photo, key, style: [_png_bytes((200, 300))] * 3)
    token = client.post("/make/pet-portrait/unlock", json={"order_ref": "DEMO"}).json()["token"]
    photo = "data:image/png;base64," + base64.b64encode(_png_bytes((300, 300))).decode()
    r = client.post("/make/pet-portrait/preview",
                    json={"token": token, "fields": {"style": "oil"}, "photo": photo})
    assert r.status_code == 200 and len(r.json()["variations"]) == 3
    r = client.post("/make/pet-portrait/finalize", json={"token": token, "choice": 1})
    assert r.status_code == 200
    r = client.get(r.json()["download"])
    assert r.status_code == 200
    assert max(Image.open(io.BytesIO(r.content)).size) == 2400   # upscaled for print


# --- Plumbing --------------------------------------------------------------

def test_personaliser_session_roundtrip(db):
    db.insert_personaliser_session({"token": "t1", "product": "invite",
                                    "order_ref": "DEMO", "fields": {"a": 1}})
    s = db.get_personaliser_session("t1")
    assert s["product"] == "invite" and s["fields"] == {"a": 1} and s["status"] == "unlocked"
    db.update_personaliser_session("t1", status="done", file_path="/x.png")
    s = db.get_personaliser_session("t1")
    assert s["status"] == "done" and s["file_path"] == "/x.png"
    assert db.get_personaliser_session("missing") is None


def test_etsy_get_receipt_hits_the_receipt_endpoint(monkeypatch):
    from onassis.connectors import etsy_client as ec
    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"receipt_id": 77}

    def _get(url, headers=None, params=None, timeout=None, **_):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(ec.httpx, "get", _get)
    c = ec.EtsyDraftClient(api_key="k", shop_id="9", access_token="t")
    assert c.get_receipt("77")["receipt_id"] == 77
    assert captured["url"].endswith("/shops/9/receipts/77")


def test_image_backend_edit_posts_multipart_and_decodes(monkeypatch):
    from onassis.connectors import image_backend as ib
    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [{"b64_json": base64.b64encode(b"img1").decode()},
                             {"b64_json": base64.b64encode(b"img2").decode()}]}

    def _post(url, headers=None, data=None, files=None, timeout=None, **_):
        captured.update(url=url, data=data, has_image="image" in (files or {}))
        return _Resp()

    import httpx                       # image_backend imports httpx inside edit()
    monkeypatch.setattr(httpx, "post", _post)
    b = ib.OpenAIImageBackend("key")
    out = b.edit(b"photo", "make it a painting", n=2)
    assert out == [b"img1", b"img2"]
    assert captured["url"].endswith("/images/edits") and captured["has_image"]
    assert captured["data"]["n"] == 2
