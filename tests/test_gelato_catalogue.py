"""Tests for the Gelato Catalogue Sync (Sprint 46) — real UIDs, no network."""

from __future__ import annotations

from typing import Any

from onassis.gelato_catalogue import GelatoCatalogueClient, GelatoCatalogueSync


class _Resp:
    def __init__(self, data: Any, status: int = 200):
        self._data = data
        self.status_code = status
        self.text = str(data)

    def json(self) -> Any:
        return self._data


class FakeTransport:
    """A fake httpx-shaped transport returning canned Gelato catalog responses."""

    CATALOGS = [
        {"catalogUid": "posters", "title": "Posters"},
        {"catalogUid": "mugs", "title": "Mugs"},
        {"catalogUid": "cushions", "title": "Cushions"},
        {"catalogUid": "aprons", "title": "Aprons"},
    ]
    PRODUCTS = {
        "posters": [{"productUid": "posters_pf_matte_ptp_210gsm", "productAttributes": {"size": "A2"}}],
        "mugs": [{"productUid": "mugs_11oz_white", "productAttributes": {"size": "11oz"}}],
        "cushions": [{"productUid": "cushions_45x45_cotton"}],
        "aprons": [{"productUid": "aprons_cotton_natural"}],
    }

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        assert headers and headers.get("X-API-KEY") == "gk-test"
        return _Resp({"data": self.CATALOGS})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(url)
        cuid = url.rstrip("/").split("/catalogs/")[1].split("/")[0]
        return _Resp({"products": self.PRODUCTS.get(cuid, [])})


def _sync(config, db) -> GelatoCatalogueSync:
    client = GelatoCatalogueClient("gk-test", transport=FakeTransport())
    return GelatoCatalogueSync(config, db, client=client)


def test_sync_stores_real_product_uids_mapped_to_categories(config, db):
    result = _sync(config, db).sync()
    assert result["synced"] == 4 and result["catalogs"] == 4
    rows = {r["product_key"]: r for r in db.list_gelato_catalogue()}
    # Real Gelato productUids are stored (not placeholders).
    assert rows["mugs"]["product_uid"] == "mugs_11oz_white"
    assert rows["posters"]["product_uid"] == "posters_pf_matte_ptp_210gsm"
    # Mapped to catalogue categories via category_of.
    assert rows["mugs"]["category"] == "Mugs"
    assert rows["cushions"]["category"] == "Cushions"
    assert rows["aprons"]["category"] == "Aprons"
    # Synced products are available immediately (the UID is real/verified).
    assert all(r["available"] == 1 for r in rows.values())
    # Cost/price seeded and priced with a markup.
    assert rows["mugs"]["production_cost"] > 0 and rows["mugs"]["retail_price"] > rows["mugs"]["production_cost"]


def test_sync_is_idempotent(config, db):
    _sync(config, db).sync()
    _sync(config, db).sync()
    assert db.count_gelato_catalogue() == 4       # refreshed, not duplicated


def test_sync_can_restrict_to_named_catalogs(config, db):
    result = _sync(config, db).sync(catalogs=["mugs", "aprons"])
    assert result["synced"] == 2
    keys = {r["product_key"] for r in db.list_gelato_catalogue()}
    assert keys == {"mugs", "aprons"}


def test_synced_catalogue_feeds_the_expansion_engine(config, db):
    """Once synced, the Expansion Engine builds from the Gelato catalogue —
    including categories the config list never had (Cushions, Aprons)."""
    from onassis.expansion import RevenueExpansionEngine
    config.expansion = {**config.expansion, "categories": []}   # full catalogue
    _sync(config, db).sync()
    engine = RevenueExpansionEngine(config, db)
    keys = {p["key"] for p in engine.catalogue()}
    assert {"mugs", "posters", "cushions", "aprons"} <= keys
    # The synced mug carries its real Gelato UID for fulfilment.
    mug = next(p for p in engine.catalogue() if p["key"] == "mugs")
    assert mug["gelato_uid"] == "mugs_11oz_white"


def test_no_sync_leaves_the_config_catalogue_untouched(config, db):
    """With nothing synced, the Expansion catalogue is the config list as before."""
    from onassis.expansion import RevenueExpansionEngine
    config.expansion = {**config.expansion, "categories": []}   # full catalogue
    engine = RevenueExpansionEngine(config, db)
    assert len(engine.catalogue()) == 10          # the phase-1 ten, unchanged


def test_sync_endpoint_reports_when_gelato_is_not_configured(config, tmp_path):
    """The sync endpoint never crashes with no API key — it reports it honestly."""
    from fastapi.testclient import TestClient

    from onassis.api import create_app
    config.environment = "development"
    config.gelato = {}                            # no api_key
    client = TestClient(create_app(config))
    r = client.post("/operations/api/catalogue/sync-gelato", json={}).json()
    assert r["ok"] is False and "GELATO_API_KEY" in r["detail"]
    # The read endpoint works and is empty.
    g = client.get("/operations/api/catalogue/gelato").json()
    assert g["count"] == 0 and g["products"] == []


def test_unavailable_synced_product_is_excluded_unless_requested(config, db):
    _sync(config, db).sync()
    db.set_gelato_product_available("aprons", False)
    from onassis.expansion import RevenueExpansionEngine
    engine = RevenueExpansionEngine(config, db)
    assert "aprons" not in {p["key"] for p in engine.catalogue()}
    assert "aprons" in {p["key"] for p in engine.catalogue(include_unavailable=True)}
