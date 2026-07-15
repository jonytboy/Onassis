"""Tests for the Operations Centre — the browser control room."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from onassis.api import create_app


@pytest.fixture
def client(config, tmp_path):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    config.environment = "development"          # security off -> open
    return TestClient(create_app(config))


def _prod_client(config, tmp_path, key="ops-key"):
    config.listing = {**(config.listing or {}), "exports_dir": str(tmp_path / "exports")}
    config.environment = "production"
    config.security = {"api_key": key}
    return TestClient(create_app(config)), key


# --- Shell + status board --------------------------------------------

def test_shell_renders_with_run_business(client):
    r = client.get("/operations")
    assert r.status_code == 200
    assert "RUN BUSINESS" in r.text
    assert "ONASSIS" in r.text and "Operations Centre" in r.text


def test_status_board_has_all_traffic_lights(client):
    s = client.get("/operations/api/status").json()
    lights = s["lights"]
    for k in ("environment", "server", "service", "api", "disk", "memory", "database",
              "anthropic", "openai", "etsy", "shopify", "gelato", "pinterest", "facebook",
              "instagram", "https"):
        assert k in lights and lights[k]["status"] in {"green", "amber", "red", "grey"}
    assert s["overall"]["status"] in {"green", "amber", "red"}
    assert "git_commit" in s and "business_mode" in s


def test_status_board_flags_ai_billing_failure(client):
    from onassis.ai_accounting import cost_context, record_llm, set_recorder
    db = client.app.state.db
    client.app.state.config.anthropic_api_key = "sk-test"     # configured
    set_recorder(db)
    with cost_context(stage="x"):
        record_llm(provider="anthropic", model="claude-opus-4-8", input_tokens=0,
                   output_tokens=0, duration_ms=1, ok=False,
                   detail="Your credit balance is too low to access the Anthropic API")
    light = client.get("/operations/api/status").json()["lights"]["anthropic"]
    assert light["status"] == "red" and "credit" in light["detail"].lower()


def test_uncredentialled_integrations_are_red_or_grey(client):
    lights = client.get("/operations/api/status").json()["lights"]
    # No Pinterest/Gelato creds in the test config -> red; FB/IG not built -> grey.
    assert lights["gelato"]["status"] in {"red", "amber"}
    assert lights["facebook"]["status"] == "grey"
    assert lights["instagram"]["status"] == "grey"


# --- Data tabs -------------------------------------------------------

def test_business_products_marketing_approvals_system(client):
    b = client.get("/operations/api/business").json()
    for k in ("revenue_yesterday", "profit_yesterday", "roi", "recommendation"):
        assert k in b
    assert client.get("/operations/api/products").json()["products"] == []
    m = client.get("/operations/api/marketing").json()
    assert "channels" in m and "funnel" in m
    a = client.get("/operations/api/approvals").json()
    assert set(a) >= {"auto_approved", "needs_review", "blocked", "confidence_threshold"}
    sysd = client.get("/operations/api/system").json()
    assert "git_commit" in sysd and "env_masked" in sysd
    # Secrets are masked, never returned raw.
    assert all(e["value"] in ("—", "set") or "…" in e["value"] or e["key"] == "ONASSIS_ENV"
               for e in sysd["env_masked"])


# --- RUN BUSINESS (faked cycle) --------------------------------------

def test_run_business_executes_and_reports(client):
    app = client.app
    app.state.daily.run = lambda mode="production": {"status": "completed",
                                                     "products_launched": 2}
    assert client.post("/operations/api/run-business").json()["status"] == "started"
    # The worker runs in a thread; poll the run state until it finishes.
    for _ in range(40):
        run = client.get("/operations/api/run").json()
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "completed"
    assert run["summary"]["products_launched"] == 2
    # The live log captured the run.
    logs = client.get("/operations/api/logs").json()["logs"]
    assert any("RUN BUSINESS" in x["line"] for x in logs)


def test_run_business_blocked_when_stopped(client):
    client.post("/operations/api/control/emergency-stop")
    r = client.post("/operations/api/run-business")
    assert r.status_code == 409


# --- Business controls ----------------------------------------------

def test_pause_resume_emergency_stop(client):
    assert client.post("/operations/api/control/pause").json()["mode"] == "paused"
    assert client.get("/operations/api/status").json()["business_mode"] == "paused"
    assert client.post("/operations/api/control/resume").json()["mode"] == "running"
    assert client.post("/operations/api/control/emergency-stop").json()["mode"] == "emergency_stop"


def test_unknown_control_action_is_400(client):
    assert client.post("/operations/api/control/nonsense").status_code == 400


# --- Auth (production) ------------------------------------------------

def test_shell_is_public_but_api_requires_key_in_production(config, tmp_path):
    cl, key = _prod_client(config, tmp_path)
    assert cl.get("/operations").status_code == 200                 # shell is public
    assert cl.get("/operations/api/status").status_code == 401      # data needs the key
    assert cl.get("/operations/api/status", headers={"X-API-Key": key}).status_code == 200
    # Query-param key works too (for browser EventSource-style calls).
    assert cl.get(f"/operations/api/status?key={key}").status_code == 200


def test_legacy_operations_endpoints_stay_globally_protected(config, tmp_path):
    cl, key = _prod_client(config, tmp_path)
    # /operations/status (the OperationsManager, not the centre) is NOT exempt.
    assert cl.get("/operations/status").status_code == 401
    assert cl.get("/operations/status", headers={"X-API-Key": key}).status_code == 200


# --- Sprint 40: Product Operations & Commercial Workflow -------------

def _seed_launched_product(db, *, key="ceramic_mug", draft=False):
    """A campaign with one CEO-approved, launched product (optionally drafted)."""
    brief_id = db.insert_brief({"brief_date": "2026-07-01", "theme": "Salt", "keywords": []})
    cid = db.insert_campaign({"name": "Salt Air", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": cid, "verdict": "APPROVE",
                                 "reasoning": "ok", "compliance_score": 92})
    sku = f"{cid}-{key}"
    db.insert_product({"sku": sku, "name": key, "campaign_id": cid, "product_key": key})
    db.insert_product_score({"campaign_id": cid, "product_key": key, "product_name": key,
                             "launched": 1, "composite_score": 88, "expected_profit": 9.5,
                             "ceo_verdict": "APPROVE", "reasoning": "Strong margin."})
    if draft:
        db.insert_publication({"platform": "etsy", "product_id": sku, "campaign_id": cid,
                               "listing_id": "555", "mode": "draft", "status": "draft"})
    return cid, sku


def test_products_status_reflects_real_lifecycle_not_active_flag(client):
    db = client.app.state.db
    cid, sku = _seed_launched_product(db)                 # launched, no draft
    _, sku2 = _seed_launched_product(db, key="poster", draft=True)  # real Etsy draft

    rows = {r["sku"]: r for r in client.get("/operations/api/products").json()["products"]}
    # The launched-but-undrafted product is Awaiting Approval, never "published".
    assert rows[sku]["status"] == "awaiting_approval"
    assert rows[sku]["status"] != "published"
    # Only the product Etsy actually drafted shows Draft Created.
    assert rows[sku2]["status"] == "draft_created"
    assert rows[sku2]["listing_id"] == "555"


def test_products_filter_buckets(client):
    db = client.app.state.db
    _seed_launched_product(db, key="mug")
    _seed_launched_product(db, key="poster", draft=True)
    awaiting = client.get("/operations/api/products?filter=awaiting_approval").json()
    assert all(r["status"] == "awaiting_approval" for r in awaiting["products"])
    drafts = client.get("/operations/api/products?filter=draft_created").json()
    assert all(r["status"] in ("draft_created", "publishing") for r in drafts["products"])


def test_publish_summary_is_honest(client):
    db = client.app.state.db
    _seed_launched_product(db, key="mug")
    _seed_launched_product(db, key="poster", draft=True)
    summary = client.get("/operations/api/publish-summary").json()
    assert summary["products_created"] == 2
    assert summary["products_awaiting_review"] == 1
    assert summary["drafts_created"] == 1
    assert "revenue_forecast" in summary


def test_approval_workspace_has_operational_cards(client):
    db = client.app.state.db
    cid, sku = _seed_launched_product(db)
    a = client.get("/operations/api/approvals").json()
    assert "queue" in a and a["queue"]
    card = a["queue"][0]
    assert card["sku"] == sku
    assert "approve" in card["actions"] and "approve_and_publish" in card["actions"]
    assert "ceo_rationale" in card and card["compliance"] == "APPROVE"


def test_approve_and_reject_decisions_persist(client):
    db = client.app.state.db
    cid, sku = _seed_launched_product(db)
    r = client.post(f"/operations/api/approvals/{sku}/decision",
                    json={"action": "approve", "operator": "jony"})
    assert r.json()["decision"] == "approved"
    assert db.get_product_approval(sku)["decision"] == "approved"
    # Status now reads Approved (ready to publish).
    rows = {x["sku"]: x for x in client.get("/operations/api/products").json()["products"]}
    assert rows[sku]["status"] == "approved"
    # Reject flips it and is recorded in history.
    client.post(f"/operations/api/approvals/{sku}/decision",
                json={"action": "reject", "operator": "jony", "notes": "off-brand"})
    assert db.get_product_approval(sku)["decision"] == "rejected"
    assert len(db.list_approval_history(sku)) == 2


def test_unknown_approval_action_is_400(client):
    db = client.app.state.db
    _, sku = _seed_launched_product(db)
    assert client.post(f"/operations/api/approvals/{sku}/decision",
                       json={"action": "nonsense"}).status_code == 400


def test_business_settings_get_and_update(client):
    got = client.get("/operations/api/business-settings").json()["settings"]
    keys = {s["key"] for s in got}
    assert {"products_per_campaign", "auto_approval_threshold", "auto_publish",
            "pinterest_enabled"} <= keys
    # Update a couple and read them back.
    r = client.post("/operations/api/business-settings",
                    json={"changes": {"products_per_campaign": 5,
                                      "auto_approval_threshold": 0.9}})
    assert r.status_code == 200
    values = {s["key"]: s["value"] for s in r.json()["settings"]}
    assert values["products_per_campaign"] == 5
    assert values["auto_approval_threshold"] == 0.9
    # Persisted across requests.
    again = {s["key"]: s["value"]
             for s in client.get("/operations/api/business-settings").json()["settings"]}
    assert again["products_per_campaign"] == 5


def test_product_sales_channels_matrix(client):
    db = client.app.state.db
    cid, sku = _seed_launched_product(db, key="mug", draft=True)   # Etsy draft
    db.insert_publication({"platform": "shopify", "product_id": sku, "campaign_id": cid,
                           "listing_id": "99001", "mode": "live", "status": "live"})
    db.insert_marketing_asset({"campaign_id": cid, "product_key": "mug", "channel": "facebook",
                               "payload": {}})
    db.insert_marketing_asset({"campaign_id": cid, "product_key": "mug", "channel": "email",
                               "payload": {}})
    # Mark deliveries.
    fb = db.list_marketing_assets(channel="facebook")[0]
    em = db.list_marketing_assets(channel="email")[0]
    db.set_marketing_asset_delivery(fb["id"], "posted", ref="fb1")
    db.set_marketing_asset_delivery(em["id"], "skipped", error="disabled")

    detail = client.get(f"/operations/api/products/{sku}").json()
    channels = {c["channel"]: c for c in detail["channels"]}
    assert channels["Etsy"]["label"] == "Draft Created"
    assert channels["Shopify"]["label"] == "Published"
    assert channels["Facebook"]["label"] == "Posted"
    assert channels["Email"]["label"] == "Skipped"
    assert channels["Instagram"]["label"] == "—"          # never created


def test_channels_readiness_endpoint(client):
    chans = {c["key"]: c for c in client.get("/operations/api/channels").json()["channels"]}
    assert set(chans) >= {"etsy", "shopify", "facebook", "instagram", "email", "blog", "pinterest"}
    # Nothing credentialled in the test config.
    assert chans["shopify"]["configured"] is False


def test_channel_connection_tests(client, monkeypatch):
    # Inject fakes so the "test connection" calls never hit the network.
    s = client.app.state
    s.daily.shopify.connector._client = type("C", (), {
        "get_shop": lambda self: {"shop": {"name": "My Store"}}})()
    s.daily.shopify.connector.cfg = {"store_domain": "x.myshopify.com", "admin_token": "t"}
    s.daily.distribution.email._transport = type("T", (), {"test": lambda self: True})()
    s.daily.distribution.email.cfg = {"smtp_host": "smtp.x", "from_address": "a@x", "to_address": "b@x"}
    r = client.post("/operations/api/channels/test").json()
    assert r["shopify"]["ok"] is True and "My Store" in r["shopify"]["detail"]
    assert r["email"]["ok"] is True
    assert r["facebook"]["configured"] is False           # not set → clean report


def test_cmo_endpoints(client):
    c = client.get("/operations/api/cmo").json()
    assert "strategy" in c and "calendar" in c and "budget" in c
    assert len(c["calendar"]) == 14
    db = client.app.state.db
    db.insert_marketing_asset({"campaign_id": 1, "product_key": "mug", "channel": "email",
                               "payload": {"subject": "s", "body": "b"}})
    r = client.post("/operations/api/cmo/schedule").json()
    assert r["scheduled"] == 1


def test_commercial_dashboard_endpoints(client):
    c = client.get("/operations/api/commercial").json()
    assert "ceo" in c and "channels" in c and "attribution" in c
    for k in ("revenue_today", "profit_today", "orders", "average_order_value",
              "net_margin", "recommendations"):
        assert k in c["ceo"]
    assert isinstance(client.get("/operations/api/commercial/products").json()["products"], list)


def test_catalogue_and_collections_endpoints(client):
    db = client.app.state.db
    _seed_launched_product(db, key="ceramic_mug")
    _seed_launched_product(db, key="premium_tshirt")
    cat = client.get("/operations/api/catalogue").json()
    assert cat["mode"] == "build"
    assert any(c["category"] == "Mugs" for c in cat["categories"])
    assert "health" in cat and "retirement_candidates" in cat
    cols = client.get("/operations/api/collections").json()["collections"]
    assert isinstance(cols, list) and cols and cols[0]["products"] >= 1


def test_distribution_endpoints(client):
    db = client.app.state.db
    cid, sku = _seed_launched_product(db, key="mug")
    db.insert_marketing_asset({"campaign_id": cid, "product_key": "mug", "channel": "facebook",
                               "listing_url": "https://etsy.com/l/1", "payload": {"body": "x"}})
    d = client.get("/operations/api/distribution").json()
    assert d["provider"] == "Make.com" and "campaigns_sent" in d and "retry_queue" in d
    # Not configured → send is a safe failure, archived for retry.
    r = client.post(f"/operations/api/distribution/send/{sku}").json()
    assert r["ok"] is False and r["status"] == "failed"
    rec = db.latest_distribution_for_product(sku)
    assert rec is not None and rec["package"]["campaign_id"] == cid


def test_cfo_ai_cost_endpoint(client):
    db = client.app.state.db
    from onassis.ai_accounting import cost_context, record_image, set_recorder
    set_recorder(db)
    with cost_context(stage="Generate Master Artwork", product_id="1-mug", campaign_id=1):
        record_image(provider="openai", model="gpt-image-1", quality="high", images=2)
    c = client.get("/operations/api/cfo").json()
    assert "dashboard" in c and "roai" in c and "optimisation" in c
    assert c["dashboard"]["ai_spend_today"] > 0
    for k in ("cost_per_product", "cost_per_published", "cost_per_sale", "breakdown"):
        assert k in c["dashboard"]


def test_production_health_dashboard(client):
    db = client.app.state.db
    _seed_launched_product(db, key="mug")                 # awaiting
    _seed_launched_product(db, key="poster", draft=True)  # etsy draft today
    h = client.get("/operations/api/production-health").json()
    for k in ("products_waiting", "products_publishing", "published_today",
              "failed_today", "retries", "success_rate", "etsy_success",
              "shopify_success", "marketing_published"):
        assert k in h
    assert h["products_waiting"] >= 1
    assert h["published_today"] >= 1


def test_production_health_symmetric_channel_panels(client):
    """Sprint 42.1 Obj 3/9 — Etsy and Shopify expose the same scorecard shape."""
    db = client.app.state.db
    cid, sku = _seed_launched_product(db, key="mug", draft=True)   # Etsy draft
    db.insert_publication({"platform": "shopify", "product_id": sku, "campaign_id": cid,
                           "listing_id": "99001", "mode": "live", "status": "live"})
    db.insert_marketing_asset({"campaign_id": cid, "product_key": "mug", "channel": "blog",
                               "payload": {}})
    blog = db.list_marketing_assets(channel="blog")[0]
    db.set_marketing_asset_delivery(blog["id"], "posted", ref="https://shop/blogs/1/x")
    h = client.get("/operations/api/production-health").json()
    chans = h["channels"]
    # Symmetric shape — both channels carry the same keys.
    fields = {"products_published", "drafts_created", "blog_articles", "failed",
              "retry_queue", "success_rate", "last_publish", "avg_publish_interval_secs"}
    assert fields <= set(chans["etsy"]) and fields <= set(chans["shopify"])
    assert chans["etsy"]["drafts_created"] == 1
    assert chans["shopify"]["products_published"] == 1
    assert chans["shopify"]["blog_articles"] == 1     # verified Shopify Blog article
    assert chans["etsy"]["blog_articles"] is None     # Etsy has no blog channel


def test_publish_builds_missing_listing_package_on_demand(client, monkeypatch):
    """Sprint 42.1 — a launched product with no package builds one on publish
    instead of failing 'No listing package found — build it first'."""
    from onassis import operations_centre as oc
    db = client.app.state.db
    state = client.app.state
    cid, sku = _seed_launched_product(db, key="mug")           # launched, no package
    calls = {"n": 0}

    def _fake_export(campaign_id, spec, design_package=None):
        calls["n"] += 1
        folder = oc._exports_dir(state) / str(campaign_id) / spec["product_key"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "listing.json").write_text(
            '{"title": "Mug", "product_id": "%d-mug"}' % campaign_id, encoding="utf-8")
        return {"status": "ready"}

    monkeypatch.setattr(state.daily.listing_factory, "export_product", _fake_export)
    r = oc._ensure_listing_package(state, cid, "mug")
    assert r["ok"] is True and r["built"] is True and calls["n"] == 1
    # Idempotent: the package now exists, so no second build.
    r2 = oc._ensure_listing_package(state, cid, "mug")
    assert r2["ok"] is True and r2["built"] is False and calls["n"] == 1


def test_cleanup_archives_unpublished_keeps_published(client):
    db = client.app.state.db
    cid, sku_pending = _seed_launched_product(db, key="mug")           # never published
    cid2, sku_live = _seed_launched_product(db, key="poster", draft=True)  # real Etsy draft
    # Dry run reports what would be archived, changes nothing.
    preview = client.post("/operations/api/approvals/cleanup", json={"dry_run": True}).json()
    assert preview["dry_run"] is True and preview["archived"] >= 1
    assert all(p.get("active", 1) for p in db.list_products())
    # A draft publication with NO valid listing id is a failed attempt, not a
    # real listing — it must still be archived.
    cid3, sku_fake = _seed_launched_product(db, key="tote")
    db.insert_publication({"platform": "etsy", "product_id": sku_fake, "campaign_id": cid3,
                           "listing_id": None, "mode": "draft", "status": "draft"})
    # Real run archives the unpublished + fake-draft products, keeps the real one.
    r = client.post("/operations/api/approvals/cleanup", json={}).json()
    assert r["archived"] >= 2
    active = {p["sku"]: p for p in db.list_products() if p.get("active", 1)}
    assert sku_live in active
    assert sku_pending not in active and sku_fake not in active


def test_archived_products_vanish_from_the_approval_workspace(client):
    """Once a product is archived (active=0) it must disappear from the
    Approval Workspace — queue/ready/published. Otherwise 'Clear pending'
    archives the backlog but the operator still sees it, and a second cleanup
    reports 0 because everything is already inactive (the real-world bug)."""
    db = client.app.state.db
    cid, sku = _seed_launched_product(db, key="mug")           # awaiting → queue
    workspace = client.get("/operations/api/approvals").json()
    assert any(c["sku"] == sku for c in workspace["queue"])
    # Archive it exactly as the cleanup does.
    assert db.set_product_active(sku, False) is True
    # A second cleanup finds nothing new to archive (already inactive)...
    r = client.post("/operations/api/approvals/cleanup", json={}).json()
    assert r["archived"] == 0
    # ...and the product is GONE from every bucket, not lingering as "ready".
    after = client.get("/operations/api/approvals").json()
    all_skus = {c["sku"] for c in after["queue"] + after["ready"] + after["published"]}
    assert sku not in all_skus


def test_legacy_compliance_buckets_clear_with_the_queue(client):
    """The Auto-approved / Needs-review / Blocked summary strip is bound to
    active campaigns — archiving a campaign's product removes its stale
    'Prepare Etsy listing…' compliance card too, instead of orphaning it."""
    db = client.app.state.db
    cid, sku = _seed_launched_product(db, key="mug")   # seeds an APPROVE report
    before = client.get("/operations/api/approvals").json()
    assert any(a["campaign_id"] == cid
               for a in before["auto_approved"] + before["needs_review"] + before["blocked"])
    db.set_product_active(sku, False)
    after = client.get("/operations/api/approvals").json()
    assert not any(a.get("campaign_id") == cid
                   for a in after["auto_approved"] + after["needs_review"] + after["blocked"])


def test_publish_builds_from_product_when_no_ceo_score(client, monkeypatch):
    """An operator-approved product with no CEO score still builds a listing
    (the operator's approval is the gate), instead of 'No CEO-approved score'."""
    from onassis import operations_centre as oc
    db = client.app.state.db
    state = client.app.state
    brief_id = db.insert_brief({"brief_date": "2026-07-01", "theme": "S", "keywords": []})
    cid = db.insert_campaign({"name": "Salt Air", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": cid, "verdict": "APPROVE",
                                 "reasoning": "ok", "compliance_score": 92})
    # A product with NO product_score row.
    db.insert_product({"sku": f"{cid}-product", "name": "Salt & Stillness Body Oil",
                       "campaign_id": cid, "product_key": "product"})

    def _fake_export(campaign_id, spec, design_package=None):
        assert spec["product_name"] == "Salt & Stillness Body Oil"
        folder = oc._exports_dir(state) / str(campaign_id) / spec["product_key"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "listing.json").write_text('{"title": "x"}', encoding="utf-8")
        return {"status": "ready"}

    monkeypatch.setattr(state.daily.listing_factory, "export_product", _fake_export)
    r = oc._ensure_listing_package(state, cid, "product")
    assert r["ok"] is True and r["built"] is True


def test_approval_card_falls_back_to_design_artwork_preview(client, tmp_path):
    """Sprint 42.1 Obj 1 — an awaiting product with no per-product listing
    package still shows a preview: the design master artwork."""
    db = client.app.state.db
    opp = "OPP-abc12345"
    brief_id = db.insert_brief({"brief_date": "2026-07-01", "theme": "Salt", "keywords": []})
    cid = db.insert_campaign({"name": "Salt Air", "brief_id": brief_id})
    db.insert_compliance_report({"campaign_id": cid, "verdict": "APPROVE",
                                 "reasoning": "ok", "compliance_score": 92})
    sku = f"{cid}-mug"
    db.insert_product({"sku": sku, "name": "mug", "campaign_id": cid, "product_key": "mug"})
    db.insert_product_score({"campaign_id": cid, "product_key": "mug", "product_name": "mug",
                             "launched": 1, "composite_score": 88, "opportunity_id": opp,
                             "ceo_verdict": "APPROVE", "reasoning": "Strong."})
    # Write the design master artwork where the Artwork stage would put it.
    exports = tmp_path / "exports" / "opportunities" / opp
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "master_artwork.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    card = next(c for c in client.get("/operations/api/approvals").json()["queue"]
                if c["sku"] == sku)
    assert card["has_hero"] is True
    assert card["hero_url"] == f"/exports/opportunities/{opp}/master_artwork.png"


def test_approval_card_has_collection_and_channel_states(client):
    """Sprint 42.1 Obj 6/8 — cards carry collection name + per-channel pills."""
    db = client.app.state.db
    cid, sku = _seed_launched_product(db, key="mug")   # awaiting decision
    card = next(c for c in client.get("/operations/api/approvals").json()["queue"]
                if c["sku"] == sku)
    assert card["collection"] == "Salt Air Collection"
    assert card["confidence"] is not None              # 88 → calculated, not "Calculating…"
    assert card["etsy_state"] == "not_created" and card["shopify_state"] == "not_created"
    assert "hero_url" in card and "has_hero" in card


def test_reconcile_endpoint_repairs(client):
    db = client.app.state.db
    db.insert_publication({"platform": "etsy", "product_id": "1-x", "campaign_id": 1,
                           "listing_id": "None", "mode": "draft", "status": "draft"})
    r = client.post("/operations/api/system/reconcile").json()
    assert r["broken_publications_fixed"] == 1
    assert client.get("/operations/api/system/reconcile").json()["repaired"] == 1


def test_channel_filters(client):
    db = client.app.state.db
    _seed_launched_product(db, key="mug", draft=True)     # on etsy
    both = client.get("/operations/api/products?filter=etsy").json()["products"]
    assert all(r["on_etsy"] for r in both)
    assert "shopify_status" in both[0]


def test_approval_cards_have_complete_metadata(client):
    db = client.app.state.db
    _seed_launched_product(db, key="mug")
    card = client.get("/operations/api/approvals").json()["queue"][0]
    for k in ("hero_url", "has_hero", "type", "confidence", "seo_score", "compliance",
              "workflow_stage", "publish_history", "retry_history", "last_updated",
              "shopify_status"):
        assert k in card
    # Confidence is a real number here (score seeded at 88) — never a spurious 0.
    assert card["confidence"] == 0.88
    assert card["ceo_rationale"] != ""    # never blank/null


def test_approve_and_publish_targets_both_channels(client):
    db = client.app.state.db
    cid, sku = _seed_launched_product(db, key="mug")
    r = client.post(f"/operations/api/approvals/{sku}/decision",
                    json={"action": "approve_and_publish", "operator": "jony"}).json()
    published = r["published"]
    assert "etsy" in published and "shopify" in published
    # Shopify not configured in test -> a clear, human-readable status (not a crash).
    assert published["shopify"]["status"] in ("not_configured", "failed")
    assert "help" in published["shopify"]


def test_integrations_dashboard(client):
    d = client.get("/operations/api/integrations").json()
    assert set(d["categories"]) == {"AI", "Commerce", "Marketing", "Production"}
    assert "summary" in d and "encryption" in d


def test_integration_detail_and_save_and_test(client):
    # Save credentials via the API, then a fake tester confirms the connection.
    r = client.post("/operations/api/integrations/shopify/save",
                    json={"values": {"store_domain": "x.myshopify.com", "client_id": "cid123456",
                                     "client_secret": "sec1234567890"}, "operator": "jony"})
    assert r.status_code == 200 and r.json()["configured"] is True
    # Inject a passing tester and test.
    client.app.state.integrations._testers["shopify"] = \
        lambda c, res, db: {"ok": True, "configured": True, "detail": "Connected to X"}
    t = client.post("/operations/api/integrations/shopify/test").json()
    assert t["ok"] is True and t["health"] == "healthy"
    # Detail shows masked secret + the audit events.
    detail = client.get("/operations/api/integrations/shopify").json()
    tok = {f["key"]: f["value"] for f in detail["fields"]}["client_secret"]
    assert tok != "sec1234567890" and "…" in tok
    assert any(e["kind"] == "credential_update" for e in detail["events"])
    # Reveal returns the raw secret.
    revealed = client.get("/operations/api/integrations/shopify?reveal=1").json()
    assert {f["key"]: f["value"] for f in revealed["fields"]}["client_secret"] == "sec1234567890"


def test_integration_save_rejects_bad_field(client):
    r = client.post("/operations/api/integrations/shopify/save",
                    json={"values": {"nope": "x"}})
    assert r.status_code == 400


def test_integration_unknown_key_404(client):
    assert client.get("/operations/api/integrations/nonsense").status_code == 404


def test_business_settings_validation_rejects_out_of_range(client):
    r = client.post("/operations/api/business-settings",
                    json={"changes": {"auto_approval_threshold": 5}})  # > 1.0
    assert r.status_code == 400


# --- Sprint 40.1: Software Updates & Deployment ----------------------

def _inject_fake_deployment(client, tmp_path, **git):
    """Replace the app's DeploymentService with one driven by a fake Git runner,
    so update/deploy endpoints never touch the real repository."""
    from onassis.deployment import DeploymentService
    from tests.test_deployment import FakeGit
    s = client.app.state
    dep = DeploymentService(s.config, s.db, runner=FakeGit(branch="main", **git),
                            root=tmp_path)
    dep.branch = "main"
    dep.backup_dir = tmp_path / "dep_backups"
    s.deployment = dep
    return dep


def test_software_updates_status(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, behind=0)
    r = client.get("/operations/api/updates").json()
    assert "current_version" in r and "git" in r and "health" in r
    assert r["updates"]["update_available"] is False


def test_check_for_updates_endpoint(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, behind=1,
                            notes=[{"sha": "abc", "subject": "New feature"}])
    r = client.post("/operations/api/updates/check").json()
    assert r["update_available"] is True and r["behind"] == 1
    assert r["release_notes"][0]["subject"] == "New feature"


def test_validate_endpoint(client, tmp_path):
    _inject_fake_deployment(client, tmp_path)
    v = client.get("/operations/api/updates/validate").json()   # now a simulation
    assert v["ok"] is True
    assert any(c["name"] == "Correct branch" for c in v["validate"]["checks"])


def _wait_deploy(client, tries=60):
    """Deploy runs in a background thread; poll deploy-status until it settles."""
    for _ in range(tries):
        st = client.get("/operations/api/updates/deploy-status").json()
        if st.get("status") not in ("running", "idle"):
            return st
        time.sleep(0.05)
    return client.get("/operations/api/updates/deploy-status").json()


def test_one_click_deploy_and_history(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, head="old000000000",
                            remote="new111111111", behind=1)
    started = client.post("/operations/api/updates/deploy",
                          json={"operator": "jony"}).json()
    assert started["status"] == "started"
    st = _wait_deploy(client)
    assert st["status"] == "success"
    hist = client.get("/operations/api/updates/history").json()["deployments"]
    assert hist[0]["operator"] == "jony" and hist[0]["status"] == "success"


def test_deploy_auto_rollback_reported_to_operator(client, tmp_path):
    dep = _inject_fake_deployment(client, tmp_path, head="old000000000",
                                  remote="new111111111", behind=1)
    dep._health_hook = lambda: {"ok": False, "detail": "db down"}
    client.post("/operations/api/updates/deploy")
    st = _wait_deploy(client)
    assert st["status"] == "failed"
    assert st["result"]["rollback_performed"] is True
    assert "health check failed" in st["result"]["error"]


def test_environment_awareness_endpoint(client, tmp_path):
    _inject_fake_deployment(client, tmp_path)
    e = client.get("/operations/api/environment").json()
    for k in ("environment", "branch", "commit", "git_status",
              "database_version", "application_version"):
        assert k in e
    assert e["database_version"] == 47


def test_download_fetches_only(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, behind=2)
    r = client.post("/operations/api/updates/download").json()
    assert r["fetched"] is True and r["behind"] == 2


def test_release_preview(client, tmp_path):
    dep = _inject_fake_deployment(client, tmp_path, head="a", remote="b", behind=1)
    # Fake diff: database.py + a file changed → migration yes, restart yes.
    dep._run.diff_files = ["onassis/database.py", "onassis/api.py"]
    p = client.get("/operations/api/updates/preview").json()
    assert p["files_changed"] == 2 and p["database_migration"] is True
    assert p["restart_required"] is True and p["estimated_deployment"].startswith("~")


def test_validate_is_a_simulation(client, tmp_path):
    _inject_fake_deployment(client, tmp_path)
    v = client.get("/operations/api/updates/validate").json()
    assert "validate" in v and "preview" in v and "would_deploy" in v


def test_self_healing_message_on_dirty_repo(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, clean=False)
    v = client.get("/operations/api/updates/validate").json()
    assert v["would_deploy"] is False
    assert "local code modifications" in v["validate"]["remediation"]


def test_available_versions_for_rollback(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, head="old000000000",
                            remote="new111111111", behind=1)
    client.post("/operations/api/updates/deploy")
    _wait_deploy(client)
    versions = client.get("/operations/api/updates/versions").json()["versions"]
    assert versions and "commit" in versions[0]


def test_rollback_endpoint(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, head="old000000000",
                            remote="new111111111", behind=1)
    client.post("/operations/api/updates/deploy")
    _wait_deploy(client)                        # let the deploy finish first
    r = client.post("/operations/api/updates/rollback", json={"operator": "jony"}).json()
    assert r["ok"] is True and r["status"] == "rolled_back"


def test_health_dashboard_endpoint(client, tmp_path):
    _inject_fake_deployment(client, tmp_path)
    h = client.get("/operations/api/health").json()
    for k in ("application", "database", "disk", "memory", "cpu", "python_version",
              "restart_required", "pending_updates"):
        assert k in h


def test_control_deploy_routes_through_service_not_shell(client, tmp_path):
    _inject_fake_deployment(client, tmp_path, head="old000000000",
                            remote="new111111111", behind=1)
    r = client.post("/operations/api/control/deploy").json()
    assert r["status"] == "success" and r["action"] == "deploy"
