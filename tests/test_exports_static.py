"""Tests for the /exports static mount — Gelato-fetchable artwork, images only."""

from __future__ import annotations

from fastapi.testclient import TestClient

from onassis.api import create_app
from onassis.exports_static import exports_dir


def _app(config, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    app = create_app(config)
    # Write a product's artwork + its private manifest, as the Listing Factory does.
    folder = tmp_path / "exports" / "1" / "ceramic_mug"
    (folder / "images").mkdir(parents=True, exist_ok=True)
    (folder / "print_file.png").write_bytes(b"\x89PNG\r\n\x1a\nprintfile")
    (folder / "images" / "hero.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (folder / "listing.json").write_text('{"price": 24.0}', encoding="utf-8")
    return TestClient(app)


def test_print_file_is_served_over_http(config, tmp_path):
    client = _app(config, tmp_path)
    r = client.get("/exports/1/ceramic_mug/print_file.png")
    assert r.status_code == 200
    assert r.content == b"\x89PNG\r\n\x1a\nprintfile"
    assert r.headers["content-type"].startswith("image/")


def test_gallery_images_are_served(config, tmp_path):
    client = _app(config, tmp_path)
    assert client.get("/exports/1/ceramic_mug/images/hero.jpg").status_code == 200


def test_internal_json_manifests_are_never_exposed(config, tmp_path):
    client = _app(config, tmp_path)
    assert client.get("/exports/1/ceramic_mug/listing.json").status_code == 404


def test_no_directory_listing(config, tmp_path):
    client = _app(config, tmp_path)
    assert client.get("/exports/1/ceramic_mug/").status_code == 404
    assert client.get("/exports/").status_code == 404


def test_path_traversal_is_blocked(config, tmp_path):
    client = _app(config, tmp_path)
    # An attempt to escape the exports root must not read arbitrary files.
    r = client.get("/exports/../config.yaml")
    assert r.status_code in (404, 400)
    assert b"gelato" not in r.content.lower()


def test_missing_file_is_404(config, tmp_path):
    client = _app(config, tmp_path)
    assert client.get("/exports/1/ceramic_mug/nope.png").status_code == 404


def test_url_matches_the_gelato_connector_shape(config, tmp_path):
    # GELATO_FILE_BASE_URL=https://server/exports + the connector's path segment
    # resolves to exactly the file this mount serves.
    client = _app(config, tmp_path)
    campaign_id, product_key = 1, "ceramic_mug"
    path = f"/exports/{campaign_id}/{product_key}/print_file.png"
    assert client.get(path).status_code == 200
    assert exports_dir(config).name == "exports"
