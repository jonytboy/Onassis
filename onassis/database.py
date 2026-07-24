"""SQLite persistence layer for ONASSIS.

A single :class:`Database` class owns the schema and all queries. Keeping
SQL in one place means the rest of the app never touches the driver, and
swapping SQLite for Postgres later is a localized change.

Schema (v0.1)
-------------
briefs
    One row per daily content brief produced by the Content Director.
content_items
    Many rows per brief — the posts/captions/prompts the Content Creator
    generated, each tagged with platform and content type.

The two tables are linked by ``content_items.brief_id -> briefs.id``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from onassis.logger import get_logger

log = get_logger(__name__)

# Bump whenever the schema changes (new table / column). Surfaced in the
# Operations Centre "Environment" panel so an operator can see at a glance
# whether the running database matches the code they expect.
SCHEMA_VERSION = 49

_SCHEMA = """
CREATE TABLE IF NOT EXISTS briefs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    brief_date    TEXT    NOT NULL,
    theme         TEXT    NOT NULL,
    tone          TEXT,
    audience      TEXT,
    objective     TEXT,
    keywords      TEXT,                 -- JSON-encoded list[str]
    payload       TEXT    NOT NULL      -- full brief as JSON, for forward-compat
);

CREATE TABLE IF NOT EXISTS content_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id      INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    platform      TEXT    NOT NULL,     -- pinterest | instagram | facebook | image
    content_type  TEXT    NOT NULL,     -- post | caption | image_prompt
    title         TEXT,
    body          TEXT    NOT NULL,
    metadata      TEXT,                 -- JSON-encoded dict (hashtags, etc.)
    status        TEXT    NOT NULL DEFAULT 'draft',  -- draft | published (future)
    FOREIGN KEY (brief_id) REFERENCES briefs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_content_brief ON content_items(brief_id);
CREATE INDEX IF NOT EXISTS idx_content_platform ON content_items(platform);

CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    theme         TEXT,
    story         TEXT,
    status        TEXT    NOT NULL DEFAULT 'Draft',  -- Draft | Scheduled | Live | Complete
    brief_id      INTEGER NOT NULL UNIQUE,           -- the brief this campaign wraps (1:1)
    FOREIGN KEY (brief_id) REFERENCES briefs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_campaign_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS knowledge (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id       INTEGER NOT NULL UNIQUE,        -- one knowledge record per campaign
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    hypothesis        TEXT    NOT NULL,
    variables         TEXT,                            -- JSON-encoded list[str]
    predicted_outcome TEXT,
    confidence        INTEGER,                         -- 0-100
    success_metrics   TEXT,                            -- JSON-encoded list[str]
    recommendation    TEXT,
    status            TEXT    NOT NULL DEFAULT 'predicted',  -- predicted | validated | revised
    actual_outcome    TEXT,                            -- NULL until analytics observes (future)
    observed_metrics  TEXT,                            -- JSON dict, filled by future analytics
    payload           TEXT    NOT NULL,                -- full record as JSON, forward-compat
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_knowledge_status ON knowledge(status);

CREATE TABLE IF NOT EXISTS proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    agent_name       TEXT    NOT NULL,
    requested_action TEXT    NOT NULL,
    estimated_cost   REAL    NOT NULL DEFAULT 0,
    expected_benefit REAL    NOT NULL DEFAULT 0,
    confidence       INTEGER,                          -- 0-100
    risks            TEXT,                             -- JSON-encoded list[str]
    reasoning        TEXT,
    campaign_id      INTEGER,                          -- optional link
    status           TEXT    NOT NULL DEFAULT 'submitted',  -- submitted|approved|rejected|needs_info
    payload          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    proposal_id   INTEGER NOT NULL,
    authority     TEXT    NOT NULL,     -- CEO | Compliance
    verdict       TEXT    NOT NULL,     -- APPROVE | REJECT | REQUEST_MORE_INFO
    reasoning     TEXT    NOT NULL,
    policy_checks TEXT,                 -- JSON
    payload       TEXT    NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compliance_reports (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT    NOT NULL,
    proposal_id             INTEGER,            -- nullable (campaign-level reviews)
    campaign_id             INTEGER,
    subject                 TEXT,               -- short description of what was reviewed
    compliance_score        INTEGER,            -- 0-100
    trademark_risk          INTEGER,            -- 0-100
    copyright_risk          INTEGER,
    platform_risk           INTEGER,
    brand_consistency_score INTEGER,
    verdict                 TEXT    NOT NULL,    -- APPROVE | REJECT | REQUEST_MORE_INFO
    reasoning               TEXT    NOT NULL,
    corrections             TEXT,               -- JSON-encoded list[str] (lower-risk alternatives)
    payload                 TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_proposal ON decisions(proposal_id);
CREATE INDEX IF NOT EXISTS idx_compliance_verdict ON compliance_reports(verdict);

CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    entry_date  TEXT    NOT NULL,          -- YYYY-MM-DD, for daily budgeting
    kind        TEXT    NOT NULL,          -- revenue | cost
    category    TEXT    NOT NULL,          -- ai | advertising | cogs | sale | other
    amount      REAL    NOT NULL,          -- positive magnitude
    campaign_id INTEGER,
    product_id  TEXT,
    brand       TEXT,
    marketplace TEXT,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger(kind);
CREATE INDEX IF NOT EXISTS idx_ledger_category ON ledger(category);
CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger(entry_date);

-- Per-request AI cost accounting (Sprint 42.2). One row per AI API call so no
-- AI cost is invisible: LLM completions and image generations alike.
CREATE TABLE IF NOT EXISTS ai_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    request_date  TEXT    NOT NULL,          -- YYYY-MM-DD, for daily rollups
    provider      TEXT    NOT NULL,          -- anthropic | openai | ...
    model         TEXT    NOT NULL,
    kind          TEXT    NOT NULL,          -- llm | image
    stage         TEXT,                       -- workflow stage (research, artwork, ...)
    product_id    TEXT,                       -- sku / product reference (when known)
    campaign_id   INTEGER,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    images        INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_requests_date ON ai_requests(request_date);
CREATE INDEX IF NOT EXISTS idx_ai_requests_product ON ai_requests(product_id);
CREATE INDEX IF NOT EXISTS idx_ai_requests_stage ON ai_requests(stage);

-- Marketing distribution campaigns (Sprint 43). One row per product campaign
-- sent to Make.com: the full package is archived so a failed send can be
-- retried WITHOUT regenerating content, and per-channel status is recorded.
CREATE TABLE IF NOT EXISTS distribution_campaigns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    campaign_id    INTEGER,
    product_id     TEXT,
    product_key    TEXT,
    collection     TEXT,
    status         TEXT    NOT NULL DEFAULT 'generated',  -- generated|sent|published|partial|failed
    package        TEXT,                                   -- JSON campaign package (for retry)
    channel_status TEXT,                                   -- JSON {channel: status}
    retry_count    INTEGER NOT NULL DEFAULT 0,
    last_attempt   TEXT,
    sent_at        TEXT,
    failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_distcamp_product ON distribution_campaigns(product_id);
CREATE INDEX IF NOT EXISTS idx_distcamp_status ON distribution_campaigns(status);

-- Gelato product catalogue (Sprint 46). Synced from Gelato's Product Catalog
-- API so ONASSIS builds from REAL product UIDs automatically instead of a hand-
-- maintained list. One row per buildable product type.
CREATE TABLE IF NOT EXISTS gelato_catalogue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at       TEXT    NOT NULL,
    product_uid     TEXT    NOT NULL UNIQUE,     -- Gelato productUid (real fulfilment id)
    catalog_uid     TEXT,                         -- e.g. posters | mugs | apparel
    title           TEXT,
    category        TEXT,                         -- catalogue category (category_of)
    product_key     TEXT    NOT NULL,             -- our internal product type key
    production_cost REAL    NOT NULL DEFAULT 0,
    retail_price    REAL    NOT NULL DEFAULT 0,
    base_brand_fit  INTEGER NOT NULL DEFAULT 78,
    base_commercial INTEGER NOT NULL DEFAULT 76,
    base_conversion REAL    NOT NULL DEFAULT 0.025,
    attributes      TEXT,                         -- JSON of Gelato product attributes
    available       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_gelato_cat_category ON gelato_catalogue(category);
CREATE INDEX IF NOT EXISTS idx_gelato_cat_key ON gelato_catalogue(product_key);

-- Short-form video content (Sprint 48). One row per generated TikTok/Reel clip:
-- the mp4 path plus caption/hashtags/sound, its posting status, and engagement
-- (which becomes the 'what actually converts' signal once posted).
CREATE TABLE IF NOT EXISTS short_form_content (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    campaign_id    INTEGER,
    product_id     TEXT,
    product_key    TEXT,
    fmt            TEXT,                               -- style_slide|product_in_use|gifting
    path           TEXT,
    caption        TEXT,
    hashtags       TEXT,                               -- JSON list
    sound          TEXT,
    duration_s     REAL    NOT NULL DEFAULT 0,
    listing_url    TEXT,
    status         TEXT    NOT NULL DEFAULT 'queued',  -- queued|distributed|posted|failed
    distributed_at TEXT,
    delivery_ref   TEXT,
    views          INTEGER NOT NULL DEFAULT 0,
    likes          INTEGER NOT NULL DEFAULT 0,
    shares         INTEGER NOT NULL DEFAULT 0,
    clicks         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shortform_status ON short_form_content(status);
CREATE INDEX IF NOT EXISTS idx_shortform_fmt ON short_form_content(fmt);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    sku             TEXT    UNIQUE,
    name            TEXT,
    campaign_id     INTEGER,
    brand           TEXT,
    marketplace     TEXT,
    production_cost REAL    NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    product_key     TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    occurred_at      TEXT    NOT NULL,        -- when the sale happened (ISO)
    sale_date        TEXT    NOT NULL,        -- YYYY-MM-DD, for day/month rollups
    order_ref        TEXT,                    -- external marketplace order id
    product_id       TEXT,                    -- sku / product reference
    campaign_id      INTEGER,
    platform         TEXT,                    -- etsy | pinterest | ...
    sale_price       REAL    NOT NULL DEFAULT 0,
    currency         TEXT    NOT NULL DEFAULT 'GBP',
    quantity         INTEGER NOT NULL DEFAULT 1,
    -- cost breakdown
    ai_cost          REAL    NOT NULL DEFAULT 0,
    advertising_cost REAL    NOT NULL DEFAULT 0,
    production_cost  REAL    NOT NULL DEFAULT 0,
    marketplace_fees REAL    NOT NULL DEFAULT 0,
    payment_fees     REAL    NOT NULL DEFAULT 0,
    other_costs      REAL    NOT NULL DEFAULT 0,
    -- calculated economics (stored for fast rollups; computed at insert)
    gross_revenue    REAL    NOT NULL DEFAULT 0,
    total_cost       REAL    NOT NULL DEFAULT 0,
    gross_profit     REAL    NOT NULL DEFAULT 0,
    net_profit       REAL    NOT NULL DEFAULT 0,
    profit_margin    REAL    NOT NULL DEFAULT 0,
    roi              REAL    NOT NULL DEFAULT 0,
    shipping_address TEXT                          -- JSON recipient (for fulfilment)
);

CREATE INDEX IF NOT EXISTS idx_orders_sale_date ON orders(sale_date);
CREATE INDEX IF NOT EXISTS idx_orders_campaign ON orders(campaign_id);
CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product_id);

CREATE TABLE IF NOT EXISTS etsy_listings (
    listing_id   INTEGER PRIMARY KEY,          -- Etsy's listing id (dedupe key)
    imported_at  TEXT    NOT NULL,
    product_id   TEXT,                          -- linked product (sku)
    campaign_id  INTEGER,                       -- linked campaign
    title        TEXT,
    state        TEXT,
    price        REAL,
    currency     TEXT,
    url          TEXT,
    num_favorers INTEGER NOT NULL DEFAULT 0,
    views        INTEGER NOT NULL DEFAULT 0,
    created_ts   TEXT,
    raw          TEXT
);

CREATE TABLE IF NOT EXISTS listing_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at     TEXT    NOT NULL,
    listing_id      INTEGER NOT NULL,
    stat_date       TEXT    NOT NULL,           -- YYYY-MM-DD snapshot
    views           INTEGER NOT NULL DEFAULT 0,
    visits          INTEGER NOT NULL DEFAULT 0,
    favourites      INTEGER NOT NULL DEFAULT 0,
    orders          INTEGER NOT NULL DEFAULT 0,
    revenue         REAL    NOT NULL DEFAULT 0,
    conversion_rate REAL    NOT NULL DEFAULT 0,
    UNIQUE (listing_id, stat_date)
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    resource   TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listing_stats_listing ON listing_stats(listing_id);

CREATE TABLE IF NOT EXISTS publications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    platform       TEXT    NOT NULL,        -- etsy
    product_id     TEXT,
    campaign_id    INTEGER NOT NULL,
    listing_id     TEXT,                     -- external listing id (when created)
    mode           TEXT    NOT NULL,         -- dry_run | draft | live
    status         TEXT    NOT NULL,         -- draft | published | dry_run | failed
    attempts       INTEGER NOT NULL DEFAULT 1,
    failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_publications_campaign ON publications(campaign_id);
CREATE INDEX IF NOT EXISTS idx_publications_status ON publications(status);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    snapshot_date TEXT    NOT NULL,        -- YYYY-MM-DD
    platform      TEXT    NOT NULL,        -- etsy | pinterest
    product_id    TEXT,
    campaign_id   INTEGER,
    metric        TEXT    NOT NULL,        -- views | favourites | orders | revenue | ...
    value         REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_product ON metric_snapshots(product_id, metric);
CREATE INDEX IF NOT EXISTS idx_metrics_campaign ON metric_snapshots(campaign_id, metric);
CREATE INDEX IF NOT EXISTS idx_metrics_date ON metric_snapshots(snapshot_date);

CREATE TABLE IF NOT EXISTS experiments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    product_id       TEXT,
    campaign_id      INTEGER,
    hypothesis       TEXT    NOT NULL,
    variable         TEXT    NOT NULL,     -- title | thumbnail | mockup | price | keywords | ...
    expected_outcome TEXT,
    success_metric   TEXT,
    start_date       TEXT    NOT NULL,
    end_date         TEXT,
    status           TEXT    NOT NULL DEFAULT 'active',  -- active | completed | abandoned
    result           TEXT,                 -- win | loss | inconclusive
    learning         TEXT,
    baseline_value   REAL,
    result_value     REAL,
    confidence       REAL,                 -- statistical confidence (0-100) where computed
    promoted         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
CREATE INDEX IF NOT EXISTS idx_experiments_product ON experiments(product_id, variable);

CREATE TABLE IF NOT EXISTS daily_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    mode             TEXT    NOT NULL,        -- production | dry_run
    status           TEXT    NOT NULL,        -- completed | completed_with_failures
    duration_seconds REAL    NOT NULL,
    stages           TEXT    NOT NULL         -- JSON list of stage results
);

CREATE TABLE IF NOT EXISTS operations_reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    mode             TEXT    NOT NULL,
    status           TEXT    NOT NULL,        -- healthy | aborted | completed | completed_with_failures
    overall_health   TEXT    NOT NULL,        -- healthy | degraded | critical
    runtime_seconds  REAL    NOT NULL,
    errors           INTEGER NOT NULL DEFAULT 0,
    warnings         INTEGER NOT NULL DEFAULT 0,
    ceo_notified     INTEGER NOT NULL DEFAULT 0,
    system           TEXT    NOT NULL,        -- JSON
    business         TEXT    NOT NULL,        -- JSON
    recommendations  TEXT    NOT NULL,        -- JSON
    preflight        TEXT    NOT NULL         -- JSON
);

CREATE TABLE IF NOT EXISTS opportunities (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at            TEXT    NOT NULL,
    opportunity_id        TEXT    NOT NULL UNIQUE,   -- e.g. OPP-ab12cd34
    brand                 TEXT,
    theme                 TEXT,
    target_customer       TEXT,
    emotional_angle       TEXT,
    product_type          TEXT,
    search_intent         TEXT,
    seasonal_relevance    TEXT,
    commercial_score      INTEGER NOT NULL DEFAULT 0,  -- 0-100
    originality_score     INTEGER NOT NULL DEFAULT 0,  -- 0-100
    brand_fit_score       INTEGER NOT NULL DEFAULT 0,  -- 0-100
    estimated_demand      INTEGER NOT NULL DEFAULT 0,  -- 0-100
    estimated_competition INTEGER NOT NULL DEFAULT 0,  -- 0-100
    confidence            INTEGER NOT NULL DEFAULT 0,  -- 0-100
    product_name          TEXT,
    concept               TEXT,                        -- one sentence
    colour_palette        TEXT,                        -- JSON array
    typography_style      TEXT,
    illustration_style    TEXT,
    photography_style     TEXT,
    mockup_style          TEXT,
    expected_value        REAL    NOT NULL DEFAULT 0,  -- ranking score
    dedupe_key            TEXT    NOT NULL,            -- normalised concept fingerprint
    status                TEXT    NOT NULL DEFAULT 'backlog',  -- backlog | selected | rejected
    selected_by           TEXT,
    selected_at           TEXT,
    payload               TEXT                         -- JSON (raw generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunities_dedupe ON opportunities(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_opportunities_rank ON opportunities(status, expected_value);

CREATE TABLE IF NOT EXISTS product_scores (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at             TEXT    NOT NULL,
    campaign_id            INTEGER,
    opportunity_id         TEXT,
    product_key            TEXT    NOT NULL,
    product_name           TEXT,
    brand_fit              REAL    NOT NULL DEFAULT 0,   -- 0-100
    commercial_suitability REAL    NOT NULL DEFAULT 0,   -- 0-100
    estimated_conversion   REAL    NOT NULL DEFAULT 0,   -- 0-100 (score of the rate)
    expected_profit        REAL    NOT NULL DEFAULT 0,   -- per-unit net profit
    production_cost        REAL    NOT NULL DEFAULT 0,
    retail_price           REAL    NOT NULL DEFAULT 0,
    historical_performance REAL    NOT NULL DEFAULT 0,   -- 0-100
    composite_score        REAL    NOT NULL DEFAULT 0,   -- 0-100
    ceo_verdict            TEXT,
    launched               INTEGER NOT NULL DEFAULT 0,
    reasoning              TEXT
);

CREATE INDEX IF NOT EXISTS idx_product_scores_campaign ON product_scores(campaign_id);

-- One launch per design (campaign): the whole approved product set is approved
-- for publication in a single action.
CREATE TABLE IF NOT EXISTS launches (
    campaign_id  INTEGER PRIMARY KEY,
    status       TEXT    NOT NULL,          -- launch_ready | launched
    policy       TEXT    NOT NULL,          -- manual | scheduled | automatic
    products     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    approved_at  TEXT,
    approved_by  TEXT
);

-- Learned performance per product type (updated from real sales history).
CREATE TABLE IF NOT EXISTS product_performance (
    product_key   TEXT    PRIMARY KEY,
    units_sold    INTEGER NOT NULL DEFAULT 0,
    orders        INTEGER NOT NULL DEFAULT 0,
    gross_revenue REAL    NOT NULL DEFAULT 0,
    net_profit    REAL    NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL
);

-- Market Intelligence: scored keyword/niche signals (research before invention).
CREATE TABLE IF NOT EXISTS market_signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    run_at            TEXT    NOT NULL,        -- groups one research report
    brand             TEXT,
    keyword           TEXT    NOT NULL,
    product_type      TEXT,
    theme             TEXT,
    demand            INTEGER NOT NULL DEFAULT 0,
    competition       INTEGER NOT NULL DEFAULT 0,
    opportunity_score INTEGER NOT NULL DEFAULT 0,
    opportunity       TEXT,                    -- VERY HIGH | HIGH | MEDIUM | LOW
    avg_selling_price REAL    NOT NULL DEFAULT 0,
    est_monthly_sales INTEGER NOT NULL DEFAULT 0,
    competitor_count  INTEGER NOT NULL DEFAULT 0,
    review_count      INTEGER NOT NULL DEFAULT 0,
    payload           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_run ON market_signals(run_at);

-- Portfolio lifecycle: every listing gets a review window, then a verdict
-- (KEEP / IMPROVE / RETIRE). Append-only — each 30-day review adds a row so the
-- history of a product's judgements is preserved.
CREATE TABLE IF NOT EXISTS portfolio_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    sku           TEXT    NOT NULL,
    product_key   TEXT,
    campaign_id   INTEGER,
    age_days      INTEGER NOT NULL DEFAULT 0,
    views         INTEGER NOT NULL DEFAULT 0,
    favourites    INTEGER NOT NULL DEFAULT 0,
    units         INTEGER NOT NULL DEFAULT 0,
    net_profit    REAL    NOT NULL DEFAULT 0,
    conversion    REAL    NOT NULL DEFAULT 0,
    ctr           REAL    NOT NULL DEFAULT 0,
    decision      TEXT    NOT NULL,          -- KEEP | IMPROVE | RETIRE
    reason        TEXT
);
CREATE INDEX IF NOT EXISTS idx_portfolio_reviews_sku ON portfolio_reviews(sku);

-- Thumbnail (hero image) A/B candidates. Four heroes are generated per product,
-- scored, and one is chosen; impressions/clicks accrue so the winning STYLE is
-- learned over time (CTR feeds the next product's variant prior).
CREATE TABLE IF NOT EXISTS thumbnails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    campaign_id   INTEGER,
    product_key   TEXT,
    sku           TEXT,
    variant       TEXT    NOT NULL,          -- white_background | lifestyle | close_crop | in_use
    filename      TEXT,
    quality_score REAL    NOT NULL DEFAULT 0,
    prior         REAL    NOT NULL DEFAULT 0,
    score         REAL    NOT NULL DEFAULT 0,
    chosen        INTEGER NOT NULL DEFAULT 0,
    impressions   INTEGER NOT NULL DEFAULT 0,
    clicks        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_thumbnails_variant ON thumbnails(variant);
CREATE INDEX IF NOT EXISTS idx_thumbnails_product ON thumbnails(product_key);

-- Marketing assets produced for a published product (one row per channel).
-- Every asset carries the Etsy listing link it drives traffic back to.
CREATE TABLE IF NOT EXISTS marketing_assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    campaign_id  INTEGER,
    product_key  TEXT,
    listing_id   TEXT,
    listing_url  TEXT,
    channel      TEXT    NOT NULL,          -- pinterest | instagram | facebook | blog | email
    payload      TEXT    NOT NULL           -- JSON asset bundle for the channel
);
CREATE INDEX IF NOT EXISTS idx_marketing_product ON marketing_assets(product_key);
CREATE INDEX IF NOT EXISTS idx_marketing_channel ON marketing_assets(channel);

-- Traffic Engine: the Pinterest posting schedule (5-10 pins/day across boards,
-- keywords and seasons). Each pin drives traffic back to its Etsy listing.
CREATE TABLE IF NOT EXISTS pin_schedule (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    pin_key        TEXT    UNIQUE,           -- stable per-pin dedupe key
    campaign_id    INTEGER,
    product_key    TEXT,
    listing_id     TEXT,
    listing_url    TEXT,
    board          TEXT,
    keyword        TEXT,
    season         TEXT,
    aspect_ratio   TEXT,
    title          TEXT,
    description    TEXT,
    scheduled_date TEXT,                     -- YYYY-MM-DD (may be a FUTURE date)
    status         TEXT    NOT NULL DEFAULT 'scheduled',  -- scheduled | posted | failed
    pin_ref        TEXT,
    posted_at      TEXT,
    image_path     TEXT,                     -- the hero image attached to the pin
    impressions    INTEGER NOT NULL DEFAULT 0,
    clicks         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pin_schedule_date ON pin_schedule(scheduled_date);
CREATE INDEX IF NOT EXISTS idx_pin_schedule_status ON pin_schedule(status);

-- Traffic funnel: Impressions -> Clicks -> Visits -> Sales, per product per day.
CREATE TABLE IF NOT EXISTS traffic_funnel (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    funnel_date  TEXT    NOT NULL,           -- YYYY-MM-DD
    product_key  TEXT,
    listing_id   TEXT,
    source       TEXT    NOT NULL DEFAULT 'pinterest',
    impressions  INTEGER NOT NULL DEFAULT 0,
    clicks       INTEGER NOT NULL DEFAULT 0,
    visits       INTEGER NOT NULL DEFAULT 0,
    sales        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_funnel_date ON traffic_funnel(funnel_date);
CREATE INDEX IF NOT EXISTS idx_funnel_product ON traffic_funnel(product_key);

-- Gelato fulfilment: one row per paid Etsy order sent to production. Tracks the
-- Gelato order id, production status, tracking, and the ACTUAL production cost
-- (which replaces the estimate in the ledger). order_ref is UNIQUE so an order
-- is never fulfilled twice.
CREATE TABLE IF NOT EXISTS fulfilments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT,
    order_ref        TEXT    UNIQUE,      -- canonical order ref (dedupe key)
    order_id         INTEGER,            -- orders.id
    product_id       TEXT,               -- sku ("<campaign>-<product_key>")
    product_key      TEXT,
    gelato_uid       TEXT,
    gelato_order_id  TEXT,
    status           TEXT    NOT NULL DEFAULT 'pending',  -- pending|created|failed|in_production|shipped|delivered|canceled
    tracking_number  TEXT,
    tracking_url     TEXT,
    carrier          TEXT,
    estimated_cost   REAL    NOT NULL DEFAULT 0,
    actual_cost      REAL,               -- NULL until Gelato reports it
    cost_booked      INTEGER NOT NULL DEFAULT 0,  -- 1 once the ledger adjustment is made
    currency         TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    shipped_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fulfilments_status ON fulfilments(status);

-- Etsy change audit: every write ONASSIS makes to a live Etsy listing is logged
-- here (field, old -> new, why, source, result) so every automated change is
-- traceable.
CREATE TABLE IF NOT EXISTS etsy_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    listing_id   TEXT,
    product_key  TEXT,
    field        TEXT    NOT NULL,          -- title|description|tags|price|quantity|images|state
    old_value    TEXT,
    new_value    TEXT,
    reason       TEXT,
    source       TEXT,                      -- learning|portfolio|manual|...
    status       TEXT    NOT NULL,          -- applied | failed | skipped
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_etsy_changes_listing ON etsy_changes(listing_id);

-- Etsy search-term intelligence: which queries surfaced/were clicked for a
-- listing (from a Shop-Stats provider). Fed to keyword optimisation.
CREATE TABLE IF NOT EXISTS etsy_search_terms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    snapshot_date TEXT    NOT NULL,
    term          TEXT    NOT NULL,
    listing_id    TEXT,
    product_key   TEXT,
    impressions   INTEGER NOT NULL DEFAULT 0,
    clicks        INTEGER NOT NULL DEFAULT 0,
    orders        INTEGER NOT NULL DEFAULT 0,
    position      REAL,
    source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_terms_term ON etsy_search_terms(term);
CREATE INDEX IF NOT EXISTS idx_search_terms_date ON etsy_search_terms(snapshot_date);

-- Financial Protection audit: every protection decision (approve/reject) with
-- the full commercial reasoning — a complete audit trail of what was allowed and
-- what was stopped, and why.
CREATE TABLE IF NOT EXISTS protection_decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT    NOT NULL,
    action               TEXT    NOT NULL,   -- launch|price_change|discount|advertising|portfolio_optimisation
    product_key          TEXT,
    listing_id           TEXT,
    expected_revenue     REAL    NOT NULL DEFAULT 0,
    expected_costs       REAL    NOT NULL DEFAULT 0,
    estimated_costs      REAL    NOT NULL DEFAULT 0,   -- portion from conservative defaults
    risk_reserve_percent REAL    NOT NULL DEFAULT 0,
    risk_reserve_amount  REAL    NOT NULL DEFAULT 0,
    protected_profit     REAL    NOT NULL DEFAULT 0,
    gross_margin         REAL    NOT NULL DEFAULT 0,
    contribution_margin  REAL    NOT NULL DEFAULT 0,
    confidence           REAL    NOT NULL DEFAULT 0,
    decision             TEXT    NOT NULL,   -- APPROVE | REJECT
    reason               TEXT
);
CREATE INDEX IF NOT EXISTS idx_protection_decision ON protection_decisions(decision);

-- Operator approval workspace (Sprint 40): the human's explicit decision on a
-- product's listing. One row per product (sku); the full decision trail lives in
-- approval_history. Distinct from the CEO/Compliance verdicts — this is the
-- operator saying "publish it" (or not) from the Operations Centre.
CREATE TABLE IF NOT EXISTS product_approvals (
    sku          TEXT    PRIMARY KEY,
    product_key  TEXT,
    campaign_id  INTEGER,
    decision     TEXT    NOT NULL DEFAULT 'awaiting',  -- awaiting | approved | rejected
    operator     TEXT,
    notes        TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

-- Append-only audit of every approval action (who / what / when / why).
CREATE TABLE IF NOT EXISTS approval_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    sku          TEXT    NOT NULL,
    product_key  TEXT,
    campaign_id  INTEGER,
    decision     TEXT    NOT NULL,
    operator     TEXT,
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_history_sku ON approval_history(sku);

-- Business settings (Sprint 40): operator-editable configuration overlaid on
-- config.yaml so the business can be tuned from the UI without editing files.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,          -- JSON-encoded value
    updated_at  TEXT    NOT NULL,
    updated_by  TEXT
);

-- Deployment history (Sprint 40.1): one row per deploy/rollback initiated from
-- the Operations Centre. A full, auditable trail of what changed, by whom, how
-- long it took, and whether it succeeded or rolled back.
CREATE TABLE IF NOT EXISTS deployments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    action            TEXT    NOT NULL,          -- deploy | rollback
    version           TEXT,
    from_commit       TEXT,
    to_commit         TEXT,
    branch            TEXT,
    operator          TEXT,
    duration_seconds  REAL    NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL,          -- success | failed | rolled_back
    rollback_performed INTEGER NOT NULL DEFAULT 0,
    steps             TEXT,                       -- JSON list of step results
    notes             TEXT,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployments_created ON deployments(created_at);

-- Integration activity + audit (Sprint 41.1): a per-connector event log —
-- connection tests, publishes, syncs, auth failures and credential updates. The
-- Operations Centre reads this for each integration's health, activity and logs.
CREATE TABLE IF NOT EXISTS integration_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    integration  TEXT    NOT NULL,       -- anthropic|openai|etsy|shopify|...
    kind         TEXT    NOT NULL,       -- test|connection|publish|sync|error|auth|credential_update
    status       TEXT    NOT NULL,       -- ok|failed
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_integration_events ON integration_events(integration, id);

-- Marketing learning (Sprint 42 Phase 5): a snapshot of each channel's measured
-- effectiveness per run, so the marketing loop can compare over time and improve
-- (generate → launch → measure → compare → learn → improve → launch again).
CREATE TABLE IF NOT EXISTS marketing_learnings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    snapshot_date TEXT    NOT NULL,
    channel       TEXT    NOT NULL,
    clicks        INTEGER NOT NULL DEFAULT 0,
    sales         INTEGER NOT NULL DEFAULT 0,
    effectiveness REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_marketing_learnings ON marketing_learnings(channel, id);
"""


def _utcnow() -> str:
    """ISO-8601 UTC timestamp, used for all created_at columns."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin, well-typed wrapper around a SQLite database file."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        log.debug("Database ready at %s", self.db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with sane defaults, committing on success."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Idempotent additive column migrations (CREATE TABLE IF NOT EXISTS
            # can't add columns to a pre-existing table).
            for table, column, decl in (("products", "product_key", "TEXT"),
                                        ("products", "launched_at", "TEXT"),
                                        ("orders", "shipping_address", "TEXT"),
                                        ("pin_schedule", "image_path", "TEXT"),
                                        ("pin_schedule", "impressions", "INTEGER DEFAULT 0"),
                                        ("pin_schedule", "clicks", "INTEGER DEFAULT 0"),
                                        # Sprint 41: channel delivery tracking.
                                        ("marketing_assets", "status", "TEXT DEFAULT 'pending'"),
                                        ("marketing_assets", "delivered_at", "TEXT"),
                                        ("marketing_assets", "delivery_ref", "TEXT"),
                                        ("marketing_assets", "delivery_error", "TEXT"),
                                        # Sprint 42 Phase 3: campaign calendar.
                                        ("marketing_assets", "scheduled_date", "TEXT")):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError:
                    pass  # column already present
            # Stamp the schema version (PRAGMA can't be parameterised).
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    # --- Briefs -----------------------------------------------------

    def insert_brief(self, brief: dict[str, Any]) -> int:
        """Persist a content brief and return its new row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO briefs
                    (created_at, brief_date, theme, tone, audience, objective, keywords, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    brief.get("brief_date", ""),
                    brief.get("theme", ""),
                    brief.get("tone", ""),
                    brief.get("audience", ""),
                    brief.get("objective", ""),
                    json.dumps(brief.get("keywords", [])),
                    json.dumps(brief),
                ),
            )
            brief_id = int(cur.lastrowid)
        log.info("Stored brief #%s (theme=%r)", brief_id, brief.get("theme"))
        return brief_id

    def get_brief(self, brief_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
        return _row_to_brief(row) if row else None

    def get_recent_briefs(self, limit: int = 30) -> list[dict[str, Any]]:
        """Return the most recent briefs (newest first).

        Used by the Content Director to avoid repeating recent campaigns.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM briefs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_brief(r) for r in rows]

    # --- Content items ---------------------------------------------

    def insert_content_items(self, brief_id: int, items: list[dict[str, Any]]) -> int:
        """Bulk-insert generated content. Returns the number stored."""
        rows = [
            (
                brief_id,
                _utcnow(),
                item["platform"],
                item["content_type"],
                item.get("title"),
                item["body"],
                json.dumps(item.get("metadata", {})),
                item.get("status", "draft"),
            )
            for item in items
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO content_items
                    (brief_id, created_at, platform, content_type, title, body, metadata, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        log.info("Stored %d content item(s) for brief #%s", len(rows), brief_id)
        return len(rows)

    def get_content_for_brief(self, brief_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM content_items WHERE brief_id = ? ORDER BY id", (brief_id,)
            ).fetchall()
        return [_row_to_content(r) for r in rows]

    def count_content(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM content_items").fetchone()[0])

    # --- Campaigns --------------------------------------------------

    def insert_campaign(self, campaign: dict[str, Any]) -> int:
        """Persist a campaign and return its new row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO campaigns (created_at, name, theme, story, status, brief_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    campaign.get("name", ""),
                    campaign.get("theme", ""),
                    campaign.get("story", ""),
                    campaign.get("status", "Draft"),
                    campaign["brief_id"],
                ),
            )
            campaign_id = int(cur.lastrowid)
        log.info("Stored campaign #%s (%r)", campaign_id, campaign.get("name"))
        return campaign_id

    def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_campaign_by_brief(self, brief_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM campaigns WHERE brief_id = ?", (brief_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_campaigns(self) -> list[dict[str, Any]]:
        """Return all campaigns, newest first."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def update_campaign_status(self, campaign_id: int, status: str) -> bool:
        """Set a campaign's status. Returns True if a row was updated."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id)
            )
            return cur.rowcount > 0

    def get_briefs_without_campaign(self) -> list[dict[str, Any]]:
        """Briefs that don't yet have a campaign (used for backfill)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT b.* FROM briefs b
                LEFT JOIN campaigns c ON c.brief_id = b.id
                WHERE c.id IS NULL
                ORDER BY b.id
                """
            ).fetchall()
        return [_row_to_brief(r) for r in rows]

    # --- Knowledge (the Brain) --------------------------------------

    def insert_knowledge(self, knowledge: dict[str, Any]) -> int:
        """Persist a knowledge record and return its new row id."""
        now = _utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO knowledge
                    (campaign_id, created_at, updated_at, hypothesis, variables,
                     predicted_outcome, confidence, success_metrics, recommendation,
                     status, actual_outcome, observed_metrics, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    knowledge["campaign_id"],
                    now,
                    now,
                    knowledge.get("hypothesis", ""),
                    json.dumps(knowledge.get("variables", [])),
                    knowledge.get("predicted_outcome", ""),
                    knowledge.get("confidence"),
                    json.dumps(knowledge.get("success_metrics", [])),
                    knowledge.get("recommendation", ""),
                    knowledge.get("status", "predicted"),
                    knowledge.get("actual_outcome"),
                    json.dumps(knowledge.get("observed_metrics", {})),
                    json.dumps(knowledge),
                ),
            )
            knowledge_id = int(cur.lastrowid)
        log.info("Stored knowledge #%s for campaign #%s", knowledge_id, knowledge["campaign_id"])
        return knowledge_id

    def get_knowledge(self, knowledge_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)
            ).fetchone()
        return _row_to_knowledge(row) if row else None

    def get_knowledge_for_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
        return _row_to_knowledge(row) if row else None

    def list_knowledge(self) -> list[dict[str, Any]]:
        """All knowledge records, newest first."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM knowledge ORDER BY id DESC").fetchall()
        return [_row_to_knowledge(r) for r in rows]

    def update_knowledge(self, knowledge_id: int, fields: dict[str, Any]) -> bool:
        """Update selected columns on a knowledge record (analytics hook).

        Only a whitelist of columns may be updated; list/dict values are
        JSON-encoded. ``updated_at`` is refreshed automatically. Returns True
        if a row changed.
        """
        allowed = {
            "hypothesis",
            "variables",
            "predicted_outcome",
            "confidence",
            "success_metrics",
            "recommendation",
            "status",
            "actual_outcome",
            "observed_metrics",
        }
        sets: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets[key] = json.dumps(value) if isinstance(value, (list, dict)) else value
        if not sets:
            return False

        sets["updated_at"] = _utcnow()
        columns = ", ".join(f"{k} = ?" for k in sets)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE knowledge SET {columns} WHERE id = ?",
                (*sets.values(), knowledge_id),
            )
            return cur.rowcount > 0

    def get_campaigns_without_knowledge(self) -> list[dict[str, Any]]:
        """Campaigns that don't yet have a knowledge record (used for backfill)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.* FROM campaigns c
                LEFT JOIN knowledge k ON k.campaign_id = c.id
                WHERE k.id IS NULL
                ORDER BY c.id
                """
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Governance: proposals --------------------------------------

    def insert_proposal(self, proposal: dict[str, Any]) -> int:
        """Persist a proposal and return its new row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO proposals
                    (created_at, agent_name, requested_action, estimated_cost,
                     expected_benefit, confidence, risks, reasoning, campaign_id,
                     status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    proposal.get("agent_name", ""),
                    proposal.get("requested_action", ""),
                    float(proposal.get("estimated_cost", 0) or 0),
                    float(proposal.get("expected_benefit", 0) or 0),
                    proposal.get("confidence"),
                    json.dumps(proposal.get("risks", [])),
                    proposal.get("reasoning", ""),
                    proposal.get("campaign_id"),
                    proposal.get("status", "submitted"),
                    json.dumps(proposal),
                ),
            )
            proposal_id = int(cur.lastrowid)
        log.info("Stored proposal #%s from %s", proposal_id, proposal.get("agent_name"))
        return proposal_id

    def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return _row_to_proposal(row) if row else None

    def list_proposals(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM proposals ORDER BY id DESC").fetchall()
        return [_row_to_proposal(r) for r in rows]

    def update_proposal_status(self, proposal_id: int, status: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id)
            )
            return cur.rowcount > 0

    # --- Governance: decisions (CEO + Compliance verdicts) ----------

    def insert_decision(self, decision: dict[str, Any]) -> int:
        """Persist a decision (with written reasoning) and return its row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO decisions
                    (created_at, proposal_id, authority, verdict, reasoning,
                     policy_checks, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    decision["proposal_id"],
                    decision.get("authority", ""),
                    decision.get("verdict", ""),
                    decision.get("reasoning", ""),
                    json.dumps(decision.get("policy_checks", [])),
                    json.dumps(decision),
                ),
            )
            decision_id = int(cur.lastrowid)
        log.info(
            "Stored %s decision #%s for proposal #%s: %s",
            decision.get("authority"),
            decision_id,
            decision["proposal_id"],
            decision.get("verdict"),
        )
        return decision_id

    def get_decisions_for_proposal(self, proposal_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE proposal_id = ? ORDER BY id", (proposal_id,)
            ).fetchall()
        return [_row_to_decision(r) for r in rows]

    def list_decisions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM decisions ORDER BY id DESC").fetchall()
        return [_row_to_decision(r) for r in rows]

    # --- Governance: compliance reports -----------------------------

    def insert_compliance_report(self, report: dict[str, Any]) -> int:
        """Persist a compliance report and return its row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO compliance_reports
                    (created_at, proposal_id, campaign_id, subject, compliance_score,
                     trademark_risk, copyright_risk, platform_risk,
                     brand_consistency_score, verdict, reasoning, corrections, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    report.get("proposal_id"),
                    report.get("campaign_id"),
                    report.get("subject", ""),
                    report.get("compliance_score"),
                    report.get("trademark_risk"),
                    report.get("copyright_risk"),
                    report.get("platform_risk"),
                    report.get("brand_consistency_score"),
                    report.get("verdict", ""),
                    report.get("reasoning", ""),
                    json.dumps(report.get("corrections", [])),
                    json.dumps(report),
                ),
            )
            report_id = int(cur.lastrowid)
        log.info(
            "Stored compliance report #%s: %s (score %s)",
            report_id,
            report.get("verdict"),
            report.get("compliance_score"),
        )
        return report_id

    def get_compliance_for_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM compliance_reports WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
        return _row_to_compliance(row) if row else None

    def get_compliance_for_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM compliance_reports WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
                (campaign_id,),
            ).fetchone()
        return _row_to_compliance(row) if row else None

    def list_compliance_reports(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM compliance_reports ORDER BY id DESC"
            ).fetchall()
        return [_row_to_compliance(r) for r in rows]

    def get_recent_compliance_rejections(self, limit: int = 20) -> list[dict[str, Any]]:
        """Past rejections/changes — the Compliance Director's learning memory."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM compliance_reports
                WHERE verdict IN ('REJECT', 'APPROVE_WITH_CHANGES', 'REQUEST_MORE_INFO')
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_compliance(r) for r in rows]

    # --- Profit ledger (costs & revenue) ----------------------------

    def insert_ledger_entry(self, entry: dict[str, Any]) -> int:
        """Record a cost or revenue entry; returns its row id."""
        now = _utcnow()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ledger
                    (created_at, entry_date, kind, category, amount, campaign_id,
                     product_id, brand, marketplace, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    entry.get("entry_date") or now[:10],
                    entry["kind"],
                    entry.get("category", "other"),
                    float(entry.get("amount", 0) or 0),
                    entry.get("campaign_id"),
                    entry.get("product_id"),
                    entry.get("brand"),
                    entry.get("marketplace"),
                    entry.get("note", ""),
                ),
            )
            entry_id = int(cur.lastrowid)
        log.info(
            "Ledger %s %s %.2f (campaign %s)",
            entry["kind"],
            entry.get("category"),
            float(entry.get("amount", 0) or 0),
            entry.get("campaign_id"),
        )
        return entry_id

    def list_ledger(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM ledger ORDER BY id DESC").fetchall()
        return [_row_to_ledger(r) for r in rows]

    def _sum(self, where: str, params: tuple) -> float:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE {where}", params
            ).fetchone()
        return float(row[0])

    def total_revenue(self) -> float:
        return self._sum("kind = 'revenue'", ())

    def total_cost(self) -> float:
        return self._sum("kind = 'cost'", ())

    def cost_by_category(self, category: str) -> float:
        return self._sum("kind = 'cost' AND category = ?", (category,))

    def ai_cost_on(self, entry_date: str) -> float:
        """AI cost recorded on a given YYYY-MM-DD (for the daily AI budget)."""
        return self._sum(
            "kind = 'cost' AND category = 'ai' AND entry_date = ?", (entry_date,)
        )

    # --- AI cost accounting (Sprint 42.2) ---------------------------

    def insert_ai_request(self, req: dict[str, Any]) -> int:
        """Record one AI API call (LLM completion or image generation)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ai_requests
                    (created_at, request_date, provider, model, kind, stage,
                     product_id, campaign_id, input_tokens, output_tokens, images,
                     duration_ms, cost_usd, ok, detail)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (_utcnow(), req.get("request_date") or _utcnow()[:10],
                 req.get("provider", ""), req.get("model", ""), req.get("kind", "llm"),
                 req.get("stage"), req.get("product_id"), req.get("campaign_id"),
                 int(req.get("input_tokens", 0) or 0), int(req.get("output_tokens", 0) or 0),
                 int(req.get("images", 0) or 0), int(req.get("duration_ms", 0) or 0),
                 float(req.get("cost_usd", 0.0) or 0.0),
                 1 if req.get("ok", True) else 0, req.get("detail")))
            return int(cur.lastrowid)

    def ai_spend_on(self, request_date: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM ai_requests WHERE request_date = ?",
                (request_date,)).fetchone()
        return float(row[0])

    def ai_spend_total(self) -> float:
        """All-time AI spend (USD). Snapshot before/after an operation to measure
        its cost — used by the Catalogue Compiler's budget cap."""
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_requests").fetchone()
        return float(row[0])

    def ai_cost_by_stage(self, request_date: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT stage, COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS n "
               "FROM ai_requests")
        params: tuple = ()
        if request_date:
            sql += " WHERE request_date = ?"
            params = (request_date,)
        sql += " GROUP BY stage ORDER BY cost DESC"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def ai_cost_by_product(self, product_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT stage, model, kind, cost_usd, input_tokens, output_tokens, "
                "images, created_at FROM ai_requests WHERE product_id = ? "
                "ORDER BY id", (product_id,)).fetchall()
        return [dict(r) for r in rows]

    def ai_cost_per_product(self, request_date: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT product_id, COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS calls "
               "FROM ai_requests WHERE product_id IS NOT NULL")
        params: tuple = ()
        if request_date:
            sql += " AND request_date = ?"
            params = (request_date,)
        sql += " GROUP BY product_id ORDER BY cost DESC"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def ai_spend_trend(self, days: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT request_date, COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS calls "
                "FROM ai_requests GROUP BY request_date ORDER BY request_date DESC LIMIT ?",
                (int(days),)).fetchall()
        return [dict(r) for r in rows][::-1]

    def ai_spend_total(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_requests").fetchone()
        return float(row[0])

    def latest_ai_request(self, provider: str | None = None) -> dict[str, Any] | None:
        """The most recent AI request (optionally for one provider) — used to
        surface a provider's live billing/credit health."""
        sql = "SELECT * FROM ai_requests"
        params: tuple = ()
        if provider:
            sql += " WHERE provider = ?"
            params = (provider,)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # --- Marketing distribution campaigns (Sprint 43) ----------------

    def insert_distribution_campaign(self, c: dict[str, Any]) -> int:
        import json as _json
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO distribution_campaigns
                   (created_at, campaign_id, product_id, product_key, collection,
                    status, package, channel_status, retry_count, last_attempt,
                    sent_at, failure_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_utcnow(), c.get("campaign_id"), c.get("product_id"), c.get("product_key"),
                 c.get("collection"), c.get("status", "generated"),
                 _json.dumps(c.get("package") or {}),
                 _json.dumps(c.get("channel_status") or {}),
                 int(c.get("retry_count", 0)), c.get("last_attempt"),
                 c.get("sent_at"), c.get("failure_reason")))
            return int(cur.lastrowid)

    def _distcamp_row(self, row: Any) -> dict[str, Any]:
        import json as _json
        d = dict(row)
        for k in ("package", "channel_status"):
            try:
                d[k] = _json.loads(d.get(k) or "{}")
            except Exception:
                d[k] = {}
        return d

    def get_distribution_campaign(self, camp_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM distribution_campaigns WHERE id = ?",
                               (camp_id,)).fetchone()
        return self._distcamp_row(row) if row else None

    def latest_distribution_for_product(self, product_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM distribution_campaigns WHERE product_id = ? "
                "ORDER BY id DESC LIMIT 1", (product_id,)).fetchone()
        return self._distcamp_row(row) if row else None

    def list_distribution_campaigns(self, *, limit: int = 100,
                                    status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM distribution_campaigns"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id DESC LIMIT ?"
        params = (*params, int(limit))
        with self._connect() as conn:
            return [self._distcamp_row(r) for r in conn.execute(sql, params).fetchall()]

    def update_distribution_campaign(self, camp_id: int, fields: dict[str, Any]) -> None:
        import json as _json
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            if k in ("package", "channel_status"):
                v = _json.dumps(v or {})
            sets.append(f"{k} = ?")
            vals.append(v)
        with self._connect() as conn:
            conn.execute(f"UPDATE distribution_campaigns SET {', '.join(sets)} WHERE id = ?",
                         (*vals, camp_id))

    def count_distribution_campaigns(self, *, status: str | None = None,
                                     on_date: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM distribution_campaigns WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if on_date:
            sql += " AND substr(sent_at,1,10) = ?"
            params.append(on_date)
        with self._connect() as conn:
            return int(conn.execute(sql, tuple(params)).fetchone()[0])

    def net_by_campaign(self) -> list[dict[str, Any]]:
        """Net profit (revenue - cost) grouped by campaign_id."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT campaign_id,
                       COALESCE(SUM(CASE WHEN kind='revenue' THEN amount ELSE 0 END), 0)
                       - COALESCE(SUM(CASE WHEN kind='cost' THEN amount ELSE 0 END), 0)
                       AS net
                FROM ledger
                WHERE campaign_id IS NOT NULL
                GROUP BY campaign_id
                ORDER BY campaign_id
                """
            ).fetchall()
        return [{"campaign_id": r["campaign_id"], "net_profit": float(r["net"])} for r in rows]

    def net_by_product(self) -> list[dict[str, Any]]:
        """Net profit grouped by product_id (products may be a future entity)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT product_id,
                       COALESCE(SUM(CASE WHEN kind='revenue' THEN amount ELSE 0 END), 0)
                       - COALESCE(SUM(CASE WHEN kind='cost' THEN amount ELSE 0 END), 0)
                       AS net
                FROM ledger
                WHERE product_id IS NOT NULL
                GROUP BY product_id
                ORDER BY product_id
                """
            ).fetchall()
        return [{"product_id": r["product_id"], "net_profit": float(r["net"])} for r in rows]

    # --- Products ---------------------------------------------------

    def insert_product(self, product: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO products
                    (created_at, sku, name, campaign_id, brand, marketplace,
                     production_cost, active, product_key, launched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    # `created_at`/`launched_at` may be back-dated by the caller
                    # (e.g. importing history, or ageing a listing in tests).
                    product.get("created_at") or _utcnow(),
                    product.get("sku"),
                    product.get("name", ""),
                    product.get("campaign_id"),
                    product.get("brand"),
                    product.get("marketplace"),
                    float(product.get("production_cost", 0) or 0),
                    1 if product.get("active", True) else 0,
                    product.get("product_key"),
                    product.get("launched_at") or product.get("created_at") or _utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def set_product_active(self, sku: str, active: bool) -> bool:
        """Archive (active=0) or restore (active=1) a product by sku."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE products SET active = ? WHERE sku = ?",
                (1 if active else 0, sku),
            )
            return cur.rowcount > 0

    def get_product(self, product_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None

    def get_product_by_sku(self, sku: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
        return dict(row) if row else None

    def list_products(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def get_products_for_campaign(self, campaign_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM products WHERE campaign_id = ? ORDER BY id", (campaign_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Orders -----------------------------------------------------

    def insert_order(self, order: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO orders
                    (created_at, occurred_at, sale_date, order_ref, product_id,
                     campaign_id, platform, sale_price, currency, quantity,
                     ai_cost, advertising_cost, production_cost, marketplace_fees,
                     payment_fees, other_costs, gross_revenue, total_cost,
                     gross_profit, net_profit, profit_margin, roi, shipping_address)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    order["occurred_at"],
                    order["sale_date"],
                    order.get("order_ref"),
                    order.get("product_id"),
                    order.get("campaign_id"),
                    order.get("platform"),
                    float(order.get("sale_price", 0) or 0),
                    order.get("currency", "GBP"),
                    int(order.get("quantity", 1) or 1),
                    float(order.get("ai_cost", 0) or 0),
                    float(order.get("advertising_cost", 0) or 0),
                    float(order.get("production_cost", 0) or 0),
                    float(order.get("marketplace_fees", 0) or 0),
                    float(order.get("payment_fees", 0) or 0),
                    float(order.get("other_costs", 0) or 0),
                    float(order["gross_revenue"]),
                    float(order["total_cost"]),
                    float(order["gross_profit"]),
                    float(order["net_profit"]),
                    float(order["profit_margin"]),
                    float(order["roi"]),
                    (json.dumps(order["shipping_address"])
                     if isinstance(order.get("shipping_address"), (dict, list))
                     else order.get("shipping_address")),
                ),
            )
            order_id = int(cur.lastrowid)
        log.info(
            "Stored order #%s (%s) net profit %.2f %s",
            order_id,
            order.get("platform"),
            float(order["net_profit"]),
            order.get("currency", "GBP"),
        )
        return order_id

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def list_orders(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def get_orders_on(self, sale_date: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE sale_date = ? ORDER BY id", (sale_date,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_orders_in_month(self, year_month: str) -> list[dict[str, Any]]:
        """``year_month`` is 'YYYY-MM'."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE sale_date LIKE ? ORDER BY id",
                (f"{year_month}-%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_orders_by_platform(self, platform: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE platform = ? ORDER BY id DESC", (platform,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_orders_for_product(self, product_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE product_id = ? ORDER BY id", (product_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_existing_order_refs(self) -> set[str]:
        """All known external order refs — used to never import a duplicate."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT order_ref FROM orders WHERE order_ref IS NOT NULL"
            ).fetchall()
        return {r["order_ref"] for r in rows}

    # --- Etsy: listings, stats, sync cursors ------------------------

    def upsert_etsy_listing(self, listing: dict[str, Any]) -> None:
        """Insert or update a listing snapshot (deduped by listing_id)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO etsy_listings
                    (listing_id, imported_at, product_id, campaign_id, title, state,
                     price, currency, url, num_favorers, views, created_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    imported_at=excluded.imported_at, product_id=excluded.product_id,
                    campaign_id=excluded.campaign_id, title=excluded.title,
                    state=excluded.state, price=excluded.price, currency=excluded.currency,
                    url=excluded.url, num_favorers=excluded.num_favorers,
                    views=excluded.views, created_ts=excluded.created_ts, raw=excluded.raw
                """,
                (
                    listing["listing_id"],
                    _utcnow(),
                    listing.get("product_id"),
                    listing.get("campaign_id"),
                    listing.get("title"),
                    listing.get("state"),
                    listing.get("price"),
                    listing.get("currency"),
                    listing.get("url"),
                    int(listing.get("num_favorers", 0) or 0),
                    int(listing.get("views", 0) or 0),
                    listing.get("created_ts"),
                    json.dumps(listing.get("raw", {})),
                ),
            )

    def get_etsy_listing(self, listing_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM etsy_listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        return _row_to_listing(row) if row else None

    def list_etsy_listings(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM etsy_listings ORDER BY listing_id DESC"
            ).fetchall()
        return [_row_to_listing(r) for r in rows]

    def upsert_listing_stat(self, stat: dict[str, Any]) -> None:
        """Insert or update a daily listing-stat snapshot (deduped per day)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO listing_stats
                    (imported_at, listing_id, stat_date, views, visits, favourites,
                     orders, revenue, conversion_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id, stat_date) DO UPDATE SET
                    imported_at=excluded.imported_at, views=excluded.views,
                    visits=excluded.visits, favourites=excluded.favourites,
                    orders=excluded.orders, revenue=excluded.revenue,
                    conversion_rate=excluded.conversion_rate
                """,
                (
                    _utcnow(),
                    stat["listing_id"],
                    stat["stat_date"],
                    int(stat.get("views", 0) or 0),
                    int(stat.get("visits", 0) or 0),
                    int(stat.get("favourites", 0) or 0),
                    int(stat.get("orders", 0) or 0),
                    float(stat.get("revenue", 0) or 0),
                    float(stat.get("conversion_rate", 0) or 0),
                ),
            )

    def list_listing_stats(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM listing_stats ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_listing_stats_for(self, listing_id: int) -> list[dict[str, Any]]:
        """A listing's stat snapshots, oldest first (for trend analysis)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM listing_stats WHERE listing_id = ? ORDER BY stat_date, id",
                (listing_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_sync_cursor(self, resource: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cursor FROM sync_cursors WHERE resource = ?", (resource,)
            ).fetchone()
        return row["cursor"] if row else None

    def set_sync_cursor(self, resource: str, cursor: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_cursors (resource, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(resource) DO UPDATE SET
                    cursor=excluded.cursor, updated_at=excluded.updated_at
                """,
                (resource, cursor, _utcnow()),
            )

    # --- Publications -----------------------------------------------

    def insert_publication(self, pub: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO publications
                    (created_at, platform, product_id, campaign_id, listing_id,
                     mode, status, attempts, failure_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    pub.get("platform", "etsy"),
                    pub.get("product_id"),
                    pub["campaign_id"],
                    pub.get("listing_id"),
                    pub.get("mode", "draft"),
                    pub["status"],
                    int(pub.get("attempts", 1)),
                    pub.get("failure_reason"),
                ),
            )
            pub_id = int(cur.lastrowid)
        log.info(
            "Recorded publication #%s: campaign %s -> %s (%s)",
            pub_id, pub["campaign_id"], pub["status"], pub.get("mode"),
        )
        return pub_id

    def get_active_publication(
        self, campaign_id: int, platform: str = "etsy", product_id: str | None = None
    ) -> dict[str, Any] | None:
        """The latest non-failed real publication (draft/published) — for
        duplicate prevention. When ``product_id`` is given, dedup is per-product
        (so a design can publish one listing per approved product)."""
        sql = ("SELECT * FROM publications WHERE campaign_id = ? AND platform = ? "
               "AND status IN ('draft', 'published', 'live')")
        params: list[Any] = [campaign_id, platform]
        if product_id is not None:
            sql += " AND product_id = ?"
            params.append(product_id)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def set_publication_status(
        self, publication_id: int, status: str, *, listing_id: str | None = None
    ) -> None:
        """Update a publication's status (e.g. draft -> live on activation)."""
        with self._connect() as conn:
            if listing_id is not None:
                conn.execute(
                    "UPDATE publications SET status = ?, listing_id = ? WHERE id = ?",
                    (status, listing_id, publication_id))
            else:
                conn.execute("UPDATE publications SET status = ? WHERE id = ?",
                             (status, publication_id))

    def count_new_listings_today(self) -> int:
        """New real listings (draft/published/live) created today — for the daily
        portfolio cap that stops ONASSIS flooding Etsy."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM publications "
                "WHERE status IN ('draft', 'published', 'live') "
                "AND date(created_at) = date('now')"
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_publications(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM publications ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def get_latest_publication(
        self, campaign_id: int, platform: str = "etsy", product_id: str | None = None
    ) -> dict[str, Any] | None:
        """The most recent publication for a product **regardless of status** —
        including ``failed``. The Product Status Engine needs the true latest
        attempt (a failed publish must surface as Failed, not be hidden like
        :meth:`get_active_publication` does)."""
        sql = "SELECT * FROM publications WHERE campaign_id = ? AND platform = ?"
        params: list[Any] = [campaign_id, platform]
        if product_id is not None:
            sql += " AND product_id = ?"
            params.append(product_id)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # --- Operator approvals (Sprint 40 workspace) -------------------

    def set_product_approval(self, approval: dict[str, Any]) -> dict[str, Any]:
        """Record the operator's decision on a product and append to the audit
        trail. Upserts the current decision (one row per sku), logs history."""
        now = _utcnow()
        sku = approval["sku"]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO product_approvals
                    (sku, product_key, campaign_id, decision, operator, notes,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    decision = excluded.decision,
                    operator = excluded.operator,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (sku, approval.get("product_key"), approval.get("campaign_id"),
                 approval.get("decision", "awaiting"), approval.get("operator"),
                 approval.get("notes"), now, now),
            )
            conn.execute(
                """
                INSERT INTO approval_history
                    (created_at, sku, product_key, campaign_id, decision, operator, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now, sku, approval.get("product_key"), approval.get("campaign_id"),
                 approval.get("decision", "awaiting"), approval.get("operator"),
                 approval.get("notes")),
            )
        return self.get_product_approval(sku) or {}

    def get_product_approval(self, sku: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM product_approvals WHERE sku = ?", (sku,)).fetchone()
        return dict(row) if row else None

    def list_product_approvals(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM product_approvals").fetchall()
        return [dict(r) for r in rows]

    def delete_orphan_product_approvals(self) -> int:
        """Remove approval rows whose product no longer exists (Sprint 41.2)."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM product_approvals WHERE sku NOT IN "
                "(SELECT sku FROM products WHERE sku IS NOT NULL)")
            return cur.rowcount

    def clamp_product_score_confidence(self) -> int:
        """Clamp impossible composite scores into [0, 100]. Returns rows fixed."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE product_scores SET composite_score = "
                "MIN(100.0, MAX(0.0, composite_score)) "
                "WHERE composite_score < 0 OR composite_score > 100")
            return cur.rowcount

    def list_approval_history(self, sku: str | None = None,
                              limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM approval_history"
        params: list[Any] = []
        if sku is not None:
            sql += " WHERE sku = ?"
            params.append(sku)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- Business settings (Sprint 40 UI-editable config) -----------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    def set_setting(self, key: str, value: Any, updated_by: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, json.dumps(value), _utcnow(), updated_by),
            )

    def all_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (ValueError, TypeError):
                out[r["key"]] = r["value"]
        return out

    # --- Deployments (Sprint 40.1 audit trail) ----------------------

    def insert_deployment(self, dep: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO deployments
                    (created_at, action, version, from_commit, to_commit, branch,
                     operator, duration_seconds, status, rollback_performed, steps,
                     notes, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(), dep.get("action", "deploy"), dep.get("version"),
                    dep.get("from_commit"), dep.get("to_commit"), dep.get("branch"),
                    dep.get("operator"), float(dep.get("duration_seconds", 0) or 0),
                    dep.get("status", "success"),
                    1 if dep.get("rollback_performed") else 0,
                    json.dumps(dep.get("steps", [])), dep.get("notes"), dep.get("error"),
                ),
            )
            return int(cur.lastrowid)

    def list_deployments(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deployments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["steps"] = json.loads(d.get("steps") or "[]")
            except (ValueError, TypeError):
                d["steps"] = []
            out.append(d)
        return out

    # --- Integration events (Sprint 41.1 audit + activity) ----------

    def insert_integration_event(self, event: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO integration_events (created_at, integration, kind, status, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (_utcnow(), event["integration"], event.get("kind", "test"),
                 event.get("status", "ok"), event.get("detail")))
            return int(cur.lastrowid)

    def list_integration_events(self, integration: str | None = None,
                                limit: int = 25) -> list[dict[str, Any]]:
        sql = "SELECT * FROM integration_events"
        params: list[Any] = []
        if integration is not None:
            sql += " WHERE integration = ?"
            params.append(integration)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def last_integration_event(self, integration: str, *, kind: str | None = None,
                               status: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM integration_events WHERE integration = ?"
        params: list[Any] = [integration]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # --- Marketing learning snapshots (Sprint 42 Phase 5) -----------

    def insert_marketing_learnings(self, snapshot_date: str,
                                   rows: list[dict[str, Any]]) -> int:
        data = [(_utcnow(), snapshot_date, r["channel"], int(r.get("clicks", 0) or 0),
                 int(r.get("sales", 0) or 0), float(r.get("effectiveness", 0) or 0))
                for r in rows]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO marketing_learnings "
                "(created_at, snapshot_date, channel, clicks, sales, effectiveness) "
                "VALUES (?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def previous_marketing_effectiveness(self, before_date: str) -> dict[str, float]:
        """The most recent effectiveness per channel recorded before a date."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT channel, effectiveness FROM marketing_learnings m "
                "WHERE snapshot_date < ? AND id = ("
                "  SELECT MAX(id) FROM marketing_learnings WHERE channel = m.channel "
                "  AND snapshot_date < ?) ",
                (before_date, before_date)).fetchall()
        return {r["channel"]: float(r["effectiveness"]) for r in rows}

    def get_last_deployment(self, action: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM deployments"
        params: list[Any] = []
        if action is not None:
            sql += " WHERE action = ?"
            params.append(action)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["steps"] = json.loads(d.get("steps") or "[]")
        except (ValueError, TypeError):
            d["steps"] = []
        return d

    # --- Metric snapshots (append-only history) ---------------------

    def insert_metric_snapshots(self, rows: list[dict[str, Any]]) -> int:
        """Append metric snapshots. History is never overwritten."""
        now = _utcnow()
        data = [
            (
                now,
                r.get("snapshot_date") or now[:10],
                r.get("platform", ""),
                r.get("product_id"),
                r.get("campaign_id"),
                r["metric"],
                float(r.get("value", 0) or 0),
            )
            for r in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO metric_snapshots
                    (created_at, snapshot_date, platform, product_id, campaign_id, metric, value)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                data,
            )
        return len(data)

    def get_metric_series(
        self, *, product_id: str | None = None, campaign_id: int | None = None,
        metric: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if product_id is not None:
            clauses.append("product_id = ?")
            params.append(product_id)
        if campaign_id is not None:
            clauses.append("campaign_id = ?")
            params.append(campaign_id)
        if metric is not None:
            clauses.append("metric = ?")
            params.append(metric)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM metric_snapshots{where} ORDER BY snapshot_date, id",
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_metric_snapshots(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0])

    def distinct_metric_products(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT product_id FROM metric_snapshots WHERE product_id IS NOT NULL"
            ).fetchall()
        return [r["product_id"] for r in rows]

    # --- Experiments ------------------------------------------------

    def insert_experiment(self, exp: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO experiments
                    (created_at, product_id, campaign_id, hypothesis, variable,
                     expected_outcome, success_metric, start_date, end_date, status,
                     result, learning, baseline_value, result_value, confidence, promoted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    exp.get("product_id"),
                    exp.get("campaign_id"),
                    exp["hypothesis"],
                    exp["variable"],
                    exp.get("expected_outcome"),
                    exp.get("success_metric"),
                    exp["start_date"],
                    exp.get("end_date"),
                    exp.get("status", "active"),
                    exp.get("result"),
                    exp.get("learning"),
                    exp.get("baseline_value"),
                    exp.get("result_value"),
                    exp.get("confidence"),
                    1 if exp.get("promoted") else 0,
                ),
            )
            exp_id = int(cur.lastrowid)
        log.info("Started experiment #%s (%s on %s)", exp_id, exp["variable"],
                 exp.get("product_id"))
        return exp_id

    def update_experiment(self, experiment_id: int, fields: dict[str, Any]) -> bool:
        allowed = {"status", "result", "learning", "end_date", "baseline_value",
                   "result_value", "confidence", "promoted"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        columns = ", ".join(f"{k} = ?" for k in sets)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE experiments SET {columns} WHERE id = ?",
                (*sets.values(), experiment_id),
            )
            return cur.rowcount > 0

    def get_experiment(self, experiment_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_experiments(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM experiments ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def list_active_experiments(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM experiments WHERE status = 'active' ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_experiment(self, product_id: str, variable: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM experiments
                WHERE product_id = ? AND variable = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (product_id, variable),
            ).fetchone()
        return dict(row) if row else None

    def get_last_completed_experiment(
        self, product_id: str, variable: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM experiments
                WHERE product_id = ? AND variable = ? AND status = 'completed'
                ORDER BY id DESC LIMIT 1
                """,
                (product_id, variable),
            ).fetchone()
        return dict(row) if row else None

    def list_promoted_learnings(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM experiments WHERE promoted = 1 ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Daily runs -------------------------------------------------

    def insert_daily_run(self, run: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO daily_runs (created_at, mode, status, duration_seconds, stages)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    run.get("mode", "production"),
                    run.get("status", "completed"),
                    float(run.get("duration_seconds", 0) or 0),
                    json.dumps(run.get("stages", [])),
                ),
            )
            return int(cur.lastrowid)

    def get_latest_daily_run(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1").fetchone()
        return _row_to_daily_run(row) if row else None

    def list_daily_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_daily_run(r) for r in rows]

    # --- Operations reports & integrity -----------------------------

    def integrity_ok(self) -> bool:
        """SQLite integrity check — used by the Operations Manager."""
        with self._connect() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"

    def insert_operations_report(self, report: dict[str, Any]) -> int:
        sys_ = report.get("system", {})
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO operations_reports
                    (created_at, mode, status, overall_health, runtime_seconds, errors,
                     warnings, ceo_notified, system, business, recommendations, preflight)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    report.get("mode", "production"),
                    report.get("status", "completed"),
                    sys_.get("overall_health", "healthy"),
                    float(sys_.get("runtime_seconds", 0) or 0),
                    int(sys_.get("errors", 0)),
                    int(sys_.get("warnings", 0)),
                    1 if report.get("ceo_notified") else 0,
                    json.dumps(sys_),
                    json.dumps(report.get("business", {})),
                    json.dumps(report.get("recommendations", [])),
                    json.dumps(report.get("preflight", {})),
                ),
            )
            return int(cur.lastrowid)

    def get_latest_operations_report(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations_reports ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _row_to_operations_report(row) if row else None

    def list_operations_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM operations_reports ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_operations_report(r) for r in rows]

    # --- Opportunities (the product development backlog) ------------

    def insert_opportunity(self, opp: dict[str, Any]) -> int:
        """Persist one product opportunity and return its row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunities
                    (created_at, opportunity_id, brand, theme, target_customer,
                     emotional_angle, product_type, search_intent, seasonal_relevance,
                     commercial_score, originality_score, brand_fit_score,
                     estimated_demand, estimated_competition, confidence,
                     product_name, concept, colour_palette, typography_style,
                     illustration_style, photography_style, mockup_style,
                     expected_value, dedupe_key, status, selected_by, selected_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    opp["opportunity_id"],
                    opp.get("brand"),
                    opp.get("theme"),
                    opp.get("target_customer"),
                    opp.get("emotional_angle"),
                    opp.get("product_type"),
                    opp.get("search_intent"),
                    opp.get("seasonal_relevance"),
                    int(opp.get("commercial_score", 0)),
                    int(opp.get("originality_score", 0)),
                    int(opp.get("brand_fit_score", 0)),
                    int(opp.get("estimated_demand", 0)),
                    int(opp.get("estimated_competition", 0)),
                    int(opp.get("confidence", 0)),
                    opp.get("product_name"),
                    opp.get("concept"),
                    json.dumps(opp.get("colour_palette", [])),
                    opp.get("typography_style"),
                    opp.get("illustration_style"),
                    opp.get("photography_style"),
                    opp.get("mockup_style"),
                    float(opp.get("expected_value", 0) or 0),
                    opp["dedupe_key"],
                    opp.get("status", "backlog"),
                    opp.get("selected_by"),
                    opp.get("selected_at"),
                    json.dumps(opp.get("payload", {})),
                ),
            )
            return int(cur.lastrowid)

    def opportunity_dedupe_keys(self) -> set[str]:
        """All concept fingerprints already in the backlog (for dedup)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT dedupe_key FROM opportunities").fetchall()
        return {r["dedupe_key"] for r in rows}

    def list_opportunities(
        self, status: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Opportunities ranked by expected commercial value (best first)."""
        sql = "SELECT * FROM opportunities"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY expected_value DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_opportunity(r) for r in rows]

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM opportunities WHERE opportunity_id = ?", (opportunity_id,)
            ).fetchone()
        return _row_to_opportunity(row) if row else None

    def count_opportunities(self, status: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM opportunities"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()["n"])

    def update_opportunity(self, opportunity_id: str, fields: dict[str, Any]) -> bool:
        allowed = {"status", "selected_by", "selected_at"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        columns = ", ".join(f"{k} = ?" for k in sets)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE opportunities SET {columns} WHERE opportunity_id = ?",
                (*sets.values(), opportunity_id),
            )
            return cur.rowcount > 0

    # --- Product scores + performance (Revenue Expansion) -----------

    def insert_product_score(self, score: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO product_scores
                    (created_at, campaign_id, opportunity_id, product_key, product_name,
                     brand_fit, commercial_suitability, estimated_conversion,
                     expected_profit, production_cost, retail_price,
                     historical_performance, composite_score, ceo_verdict, launched, reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    score.get("campaign_id"),
                    score.get("opportunity_id"),
                    score["product_key"],
                    score.get("product_name"),
                    float(score.get("brand_fit", 0) or 0),
                    float(score.get("commercial_suitability", 0) or 0),
                    float(score.get("estimated_conversion", 0) or 0),
                    float(score.get("expected_profit", 0) or 0),
                    float(score.get("production_cost", 0) or 0),
                    float(score.get("retail_price", 0) or 0),
                    float(score.get("historical_performance", 0) or 0),
                    float(score.get("composite_score", 0) or 0),
                    score.get("ceo_verdict"),
                    1 if score.get("launched") else 0,
                    score.get("reasoning"),
                ),
            )
            return int(cur.lastrowid)

    def list_product_scores(self, campaign_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM product_scores"
        params: list[Any] = []
        if campaign_id is not None:
            sql += " WHERE campaign_id = ?"
            params.append(campaign_id)
        sql += " ORDER BY composite_score DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- Market intelligence ----------------------------------------

    def insert_market_signal(self, row: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO market_signals
                    (created_at, run_at, brand, keyword, product_type, theme,
                     demand, competition, opportunity_score, opportunity,
                     avg_selling_price, est_monthly_sales, competitor_count,
                     review_count, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(), row.get("run_at") or _utcnow(), row.get("brand"),
                    row["keyword"], row.get("product_type"), row.get("theme"),
                    int(row.get("demand", 0)), int(row.get("competition", 0)),
                    int(row.get("opportunity_score", 0)), row.get("opportunity"),
                    float(row.get("avg_selling_price", 0) or 0),
                    int(row.get("est_monthly_sales", 0) or 0),
                    int(row.get("competitor_count", 0) or 0),
                    int(row.get("review_count", 0) or 0),
                    json.dumps(row.get("payload", {})),
                ),
            )
            return int(cur.lastrowid)

    def top_market_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        """The latest research report's keywords, best opportunity first."""
        with self._connect() as conn:
            latest = conn.execute(
                "SELECT MAX(run_at) AS run_at FROM market_signals").fetchone()
            run_at = latest["run_at"] if latest else None
            if not run_at:
                return []
            rows = conn.execute(
                "SELECT * FROM market_signals WHERE run_at = ? "
                "ORDER BY opportunity_score DESC, demand DESC LIMIT ?",
                (run_at, limit),
            ).fetchall()
        return [_row_to_market(r) for r in rows]

    # --- Gelato catalogue (Sprint 46) -------------------------------

    def upsert_gelato_product(self, row: dict[str, Any]) -> None:
        """Insert or update one synced Gelato product (keyed by product_uid)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gelato_catalogue
                    (synced_at, product_uid, catalog_uid, title, category, product_key,
                     production_cost, retail_price, base_brand_fit, base_commercial,
                     base_conversion, attributes, available)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_uid) DO UPDATE SET
                    synced_at=excluded.synced_at, catalog_uid=excluded.catalog_uid,
                    title=excluded.title, category=excluded.category,
                    product_key=excluded.product_key,
                    production_cost=excluded.production_cost,
                    retail_price=excluded.retail_price,
                    base_brand_fit=excluded.base_brand_fit,
                    base_commercial=excluded.base_commercial,
                    base_conversion=excluded.base_conversion,
                    attributes=excluded.attributes, available=excluded.available
                """,
                (
                    _utcnow(), row["product_uid"], row.get("catalog_uid"),
                    row.get("title"), row.get("category"), row["product_key"],
                    float(row.get("production_cost", 0) or 0),
                    float(row.get("retail_price", 0) or 0),
                    int(row.get("base_brand_fit", 78) or 78),
                    int(row.get("base_commercial", 76) or 76),
                    float(row.get("base_conversion", 0.025) or 0.025),
                    json.dumps(row.get("attributes", {})),
                    1 if row.get("available", True) else 0,
                ),
            )

    def list_gelato_catalogue(self, *, available_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM gelato_catalogue"
        if available_only:
            sql += " WHERE available = 1"
        sql += " ORDER BY category, product_key"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["attributes"] = json.loads(d.get("attributes") or "{}")
            except (TypeError, ValueError):
                d["attributes"] = {}
            out.append(d)
        return out

    def count_gelato_catalogue(self, *, available_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM gelato_catalogue"
        if available_only:
            sql += " WHERE available = 1"
        with self._connect() as conn:
            return int(conn.execute(sql).fetchone()["n"])

    def set_gelato_product_available(self, product_key: str, available: bool) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE gelato_catalogue SET available = ? WHERE product_key = ?",
                (1 if available else 0, product_key))
            return cur.rowcount

    def clear_gelato_catalogue(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM gelato_catalogue")
            return cur.rowcount

    # --- Short-form video content (Sprint 48) -----------------------

    def insert_short_form(self, row: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO short_form_content
                    (created_at, campaign_id, product_id, product_key, fmt, path,
                     caption, hashtags, sound, duration_s, listing_url, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_utcnow(), row.get("campaign_id"), row.get("product_id"),
                 row.get("product_key"), row.get("fmt"), row.get("path"),
                 row.get("caption"), json.dumps(row.get("hashtags", [])),
                 row.get("sound"), float(row.get("duration_s", 0) or 0),
                 row.get("listing_url"), row.get("status", "queued")))
            return int(cur.lastrowid)

    def list_short_form(self, *, status: str | None = None,
                        limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM short_form_content"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["hashtags"] = json.loads(d.get("hashtags") or "[]")
            except (TypeError, ValueError):
                d["hashtags"] = []
            out.append(d)
        return out

    def count_short_form(self, *, status: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM short_form_content"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()["n"])

    def set_short_form_status(self, clip_id: int, status: str,
                              ref: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE short_form_content SET status = ?, delivery_ref = ?, "
                "distributed_at = ? WHERE id = ?",
                (status, ref, _utcnow() if status == "distributed" else None, clip_id))

    def update_short_form_engagement(self, clip_id: int, *, views: int = 0, likes: int = 0,
                                     shares: int = 0, clicks: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE short_form_content SET views=?, likes=?, shares=?, clicks=? "
                "WHERE id = ?", (views, likes, shares, clicks, clip_id))

    def upsert_product_performance(self, perf: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO product_performance
                    (product_key, units_sold, orders, gross_revenue, net_profit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_key) DO UPDATE SET
                    units_sold = excluded.units_sold,
                    orders = excluded.orders,
                    gross_revenue = excluded.gross_revenue,
                    net_profit = excluded.net_profit,
                    updated_at = excluded.updated_at
                """,
                (
                    perf["product_key"],
                    int(perf.get("units_sold", 0)),
                    int(perf.get("orders", 0)),
                    float(perf.get("gross_revenue", 0) or 0),
                    float(perf.get("net_profit", 0) or 0),
                    _utcnow(),
                ),
            )

    def get_product_performance(self, product_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM product_performance WHERE product_key = ?", (product_key,)
            ).fetchone()
        return dict(row) if row else None

    def list_product_performance(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM product_performance ORDER BY net_profit DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Launches (single-approval product-set launch) --------------

    def upsert_launch(self, launch: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO launches
                    (campaign_id, status, policy, products, created_at, approved_at, approved_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    status = excluded.status,
                    policy = excluded.policy,
                    products = excluded.products,
                    approved_at = excluded.approved_at,
                    approved_by = excluded.approved_by
                """,
                (
                    launch["campaign_id"],
                    launch["status"],
                    launch.get("policy", "manual"),
                    int(launch.get("products", 0)),
                    launch.get("created_at") or _utcnow(),
                    launch.get("approved_at"),
                    launch.get("approved_by"),
                ),
            )

    def get_launch(self, campaign_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM launches WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_launches(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM launches"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY campaign_id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # --- Fulfilment (Gelato) ----------------------------------------

    def insert_fulfilment(self, f: dict[str, Any]) -> int | None:
        """Create a fulfilment record. Returns None if the order_ref already has one."""
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO fulfilments
                        (created_at, updated_at, order_ref, order_id, product_id,
                         product_key, gelato_uid, gelato_order_id, status,
                         tracking_number, tracking_url, carrier, estimated_cost,
                         actual_cost, cost_booked, currency, attempts, last_error,
                         shipped_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utcnow(), _utcnow(), f.get("order_ref"), f.get("order_id"),
                        f.get("product_id"), f.get("product_key"), f.get("gelato_uid"),
                        f.get("gelato_order_id"), f.get("status", "pending"),
                        f.get("tracking_number"), f.get("tracking_url"), f.get("carrier"),
                        float(f.get("estimated_cost", 0) or 0),
                        f.get("actual_cost"), 1 if f.get("cost_booked") else 0,
                        f.get("currency"), int(f.get("attempts", 0) or 0),
                        f.get("last_error"), f.get("shipped_at"),
                    ),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def update_fulfilment(self, fulfilment_id: int, fields: dict[str, Any]) -> bool:
        allowed = {"status", "gelato_order_id", "tracking_number", "tracking_url",
                   "carrier", "actual_cost", "cost_booked", "attempts", "last_error",
                   "shipped_at", "estimated_cost", "currency"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        sets["updated_at"] = _utcnow()
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE fulfilments SET {assignments} WHERE id = ?",
                (*sets.values(), fulfilment_id),
            )
            return cur.rowcount > 0

    def get_fulfilment(self, order_ref: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM fulfilments WHERE order_ref = ?", (order_ref,)).fetchone()
        return dict(row) if row else None

    def get_fulfilment_by_id(self, fulfilment_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM fulfilments WHERE id = ?", (fulfilment_id,)).fetchone()
        return dict(row) if row else None

    def list_fulfilments(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM fulfilments"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def fulfilled_order_refs(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT order_ref FROM fulfilments WHERE order_ref IS NOT NULL").fetchall()
        return {r["order_ref"] for r in rows}

    def insert_etsy_change(self, change: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO etsy_changes
                    (created_at, listing_id, product_key, field, old_value, new_value,
                     reason, source, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(), change.get("listing_id"), change.get("product_key"),
                    change["field"],
                    (json.dumps(change["old_value"])
                     if isinstance(change.get("old_value"), (dict, list))
                     else (None if change.get("old_value") is None
                           else str(change.get("old_value")))),
                    (json.dumps(change["new_value"])
                     if isinstance(change.get("new_value"), (dict, list))
                     else (None if change.get("new_value") is None
                           else str(change.get("new_value")))),
                    change.get("reason"), change.get("source"),
                    change.get("status", "applied"), change.get("error"),
                ),
            )
            return int(cur.lastrowid)

    def list_etsy_changes(self, listing_id: str | None = None,
                          limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM etsy_changes"
        params: list[Any] = []
        if listing_id is not None:
            sql += " WHERE listing_id = ?"
            params.append(str(listing_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def insert_protection_decision(self, d: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO protection_decisions
                    (created_at, action, product_key, listing_id, expected_revenue,
                     expected_costs, estimated_costs, risk_reserve_percent,
                     risk_reserve_amount, protected_profit, gross_margin,
                     contribution_margin, confidence, decision, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(), d["action"], d.get("product_key"), d.get("listing_id"),
                    float(d.get("expected_revenue", 0) or 0),
                    float(d.get("expected_costs", 0) or 0),
                    float(d.get("estimated_costs", 0) or 0),
                    float(d.get("risk_reserve_percent", 0) or 0),
                    float(d.get("risk_reserve_amount", 0) or 0),
                    float(d.get("protected_profit", 0) or 0),
                    float(d.get("gross_margin", 0) or 0),
                    float(d.get("contribution_margin", 0) or 0),
                    float(d.get("confidence", 0) or 0),
                    d["decision"], d.get("reason"),
                ),
            )
            return int(cur.lastrowid)

    def list_protection_decisions(self, *, decision: str | None = None,
                                  limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM protection_decisions"
        params: list[Any] = []
        if decision is not None:
            sql += " WHERE decision = ?"
            params.append(decision)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def insert_etsy_search_terms(self, rows: list[dict[str, Any]]) -> int:
        now = _utcnow()
        data = [
            (now, r.get("snapshot_date") or now[:10], r["term"], r.get("listing_id"),
             r.get("product_key"), int(r.get("impressions", 0) or 0),
             int(r.get("clicks", 0) or 0), int(r.get("orders", 0) or 0),
             r.get("position"), r.get("source"))
            for r in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO etsy_search_terms
                    (created_at, snapshot_date, term, listing_id, product_key,
                     impressions, clicks, orders, position, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                data,
            )
        return len(data)

    def list_etsy_search_terms(self, *, term: str | None = None,
                               product_key: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM etsy_search_terms"
        clauses: list[str] = []
        params: list[Any] = []
        if term is not None:
            clauses.append("term = ?")
            params.append(term)
        if product_key is not None:
            clauses.append("product_key = ?")
            params.append(product_key)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def top_search_terms(self, limit: int = 50) -> list[dict[str, Any]]:
        """Aggregate search-term performance across history (impressions/clicks/orders)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT term,
                       SUM(impressions) AS impressions,
                       SUM(clicks) AS clicks,
                       SUM(orders) AS orders
                FROM etsy_search_terms GROUP BY term
                ORDER BY orders DESC, clicks DESC, impressions DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_publication_by_listing_id(self, listing_id: str) -> dict[str, Any] | None:
        """The publication that owns an Etsy listing id (links listing -> product)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM publications WHERE listing_id = ? "
                "AND status IN ('draft','published','live') ORDER BY id DESC LIMIT 1",
                (str(listing_id),),
            ).fetchone()
        return dict(row) if row else None

    # --- Portfolio lifecycle reviews --------------------------------

    def insert_portfolio_review(self, review: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO portfolio_reviews
                    (created_at, sku, product_key, campaign_id, age_days, views,
                     favourites, units, net_profit, conversion, ctr, decision, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    review["sku"],
                    review.get("product_key"),
                    review.get("campaign_id"),
                    int(review.get("age_days", 0)),
                    int(review.get("views", 0)),
                    int(review.get("favourites", 0)),
                    int(review.get("units", 0)),
                    float(review.get("net_profit", 0) or 0),
                    float(review.get("conversion", 0) or 0),
                    float(review.get("ctr", 0) or 0),
                    review["decision"],
                    review.get("reason"),
                ),
            )
            return int(cur.lastrowid)

    def list_portfolio_reviews(self, sku: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM portfolio_reviews"
        params: list[Any] = []
        if sku is not None:
            sql += " WHERE sku = ?"
            params.append(sku)
        sql += " ORDER BY id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_last_portfolio_review(self, sku: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_reviews WHERE sku = ? ORDER BY id DESC LIMIT 1",
                (sku,),
            ).fetchone()
        return dict(row) if row else None

    def count_portfolio_decisions(self, decision: str, since: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM portfolio_reviews WHERE decision = ?"
        params: list[Any] = [decision]
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    # --- Thumbnail (hero) A/B candidates -----------------------------

    def insert_thumbnail(self, thumb: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO thumbnails
                    (created_at, campaign_id, product_key, sku, variant, filename,
                     quality_score, prior, score, chosen, impressions, clicks)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(), thumb.get("campaign_id"), thumb.get("product_key"),
                    thumb.get("sku"), thumb["variant"], thumb.get("filename"),
                    float(thumb.get("quality_score", 0) or 0),
                    float(thumb.get("prior", 0) or 0),
                    float(thumb.get("score", 0) or 0),
                    1 if thumb.get("chosen") else 0,
                    int(thumb.get("impressions", 0) or 0),
                    int(thumb.get("clicks", 0) or 0),
                ),
            )
            return int(cur.lastrowid)

    def list_thumbnails(self, product_key: str | None = None,
                        variant: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM thumbnails"
        clauses: list[str] = []
        params: list[Any] = []
        if product_key is not None:
            clauses.append("product_key = ?")
            params.append(product_key)
        if variant is not None:
            clauses.append("variant = ?")
            params.append(variant)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def record_thumbnail_metrics(self, thumbnail_id: int, *, impressions: int,
                                 clicks: int) -> bool:
        """Accrue impressions/clicks against a chosen thumbnail (CTR learning)."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE thumbnails SET impressions = impressions + ?, "
                "clicks = clicks + ? WHERE id = ?",
                (int(impressions), int(clicks), thumbnail_id),
            )
            return cur.rowcount > 0

    # --- Marketing assets -------------------------------------------

    def insert_marketing_asset(self, asset: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO marketing_assets
                    (created_at, campaign_id, product_key, listing_id, listing_url,
                     channel, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(), asset.get("campaign_id"), asset.get("product_key"),
                    asset.get("listing_id"), asset.get("listing_url"),
                    asset["channel"], json.dumps(asset.get("payload", {})),
                ),
            )
            return int(cur.lastrowid)

    def list_marketing_assets(self, *, product_key: str | None = None,
                              campaign_id: int | None = None,
                              channel: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM marketing_assets"
        clauses: list[str] = []
        params: list[Any] = []
        if product_key is not None:
            clauses.append("product_key = ?")
            params.append(product_key)
        if campaign_id is not None:
            clauses.append("campaign_id = ?")
            params.append(campaign_id)
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["payload"] = json.loads(data.get("payload") or "{}")
            out.append(data)
        return out

    def count_marketing_assets(self, channel: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM marketing_assets"
        params: list[Any] = []
        if channel is not None:
            sql += " WHERE channel = ?"
            params.append(channel)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def list_pending_marketing_assets(self, *, channel: str | None = None,
                                      channels: list[str] | None = None,
                                      due_on: str | None = None,
                                      limit: int = 100) -> list[dict[str, Any]]:
        """Assets not yet delivered to their channel (Sprint 41 distribution).
        ``pending`` or NULL status counts as undelivered. With ``due_on`` (a
        YYYY-MM-DD), only assets scheduled on/before that date (or unscheduled)
        are returned — the campaign calendar (Sprint 42)."""
        sql = ("SELECT * FROM marketing_assets "
               "WHERE (status IS NULL OR status = 'pending')")
        params: list[Any] = []
        if channel is not None:
            sql += " AND channel = ?"
            params.append(channel)
        if channels:
            sql += " AND channel IN (%s)" % ",".join("?" * len(channels))
            params.extend(channels)
        if due_on is not None:
            sql += " AND (scheduled_date IS NULL OR scheduled_date <= ?)"
            params.append(due_on)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["payload"] = json.loads(data.get("payload") or "{}")
            out.append(data)
        return out

    def set_marketing_asset_delivery(self, asset_id: int, status: str, *,
                                     ref: str | None = None,
                                     error: str | None = None) -> None:
        """Record the outcome of a channel delivery (posted/failed/skipped)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE marketing_assets SET status = ?, delivered_at = ?, "
                "delivery_ref = ?, delivery_error = ? WHERE id = ?",
                (status, _utcnow() if status == "posted" else None, ref, error, asset_id))

    def count_marketing_assets_by_status(self, status: str) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM marketing_assets WHERE status = ?",
                (status,)).fetchone()[0])

    def schedule_marketing_asset(self, asset_id: int, date: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE marketing_assets SET scheduled_date = ? WHERE id = ?",
                         (date, asset_id))

    def list_scheduled_marketing(self, *, on_or_after: str | None = None,
                                 limit: int = 200) -> list[dict[str, Any]]:
        """Scheduled (undelivered) assets for the campaign calendar view."""
        sql = ("SELECT * FROM marketing_assets WHERE scheduled_date IS NOT NULL "
               "AND (status IS NULL OR status = 'pending')")
        params: list[Any] = []
        if on_or_after is not None:
            sql += " AND scheduled_date >= ?"
            params.append(on_or_after)
        sql += " ORDER BY scheduled_date ASC, id ASC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.get("payload") or "{}")
            out.append(d)
        return out

    def reset_failed_marketing_assets(self, channel: str | None = None, *,
                                      include_skipped: bool = False) -> int:
        """Re-queue failed deliveries for another attempt (Sprint 42 retry). With
        ``include_skipped`` a 'skipped' asset (e.g. the channel wasn't connected at
        the time) is re-queued too — so re-connecting and retrying actually posts
        articles that were passed over earlier."""
        statuses = ["failed", "skipped"] if include_skipped else ["failed"]
        sql = ("UPDATE marketing_assets SET status = 'pending', delivery_error = NULL "
               "WHERE status IN (%s)" % ",".join("?" * len(statuses)))
        params: list[Any] = list(statuses)
        if channel is not None:
            sql += " AND channel = ?"
            params.append(channel)
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    # --- Traffic: pin schedule --------------------------------------

    def insert_pin_schedule(self, pin: dict[str, Any]) -> int | None:
        """Schedule a pin. Returns None if this pin_key is already scheduled."""
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO pin_schedule
                        (created_at, pin_key, campaign_id, product_key, listing_id,
                         listing_url, board, keyword, season, aspect_ratio, title,
                         description, scheduled_date, status, image_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utcnow(), pin.get("pin_key"), pin.get("campaign_id"),
                        pin.get("product_key"), pin.get("listing_id"),
                        pin.get("listing_url"), pin.get("board"), pin.get("keyword"),
                        pin.get("season"), pin.get("aspect_ratio"), pin.get("title"),
                        pin.get("description"), pin.get("scheduled_date"),
                        pin.get("status", "scheduled"), pin.get("image_path"),
                    ),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None  # already scheduled (unique pin_key)

    def scheduled_pin_keys(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pin_key FROM pin_schedule WHERE pin_key IS NOT NULL").fetchall()
        return {r["pin_key"] for r in rows}

    def last_pinned_dates(self) -> dict[str, str]:
        """product_key → the most recent date it was POSTED (or scheduled). Drives
        evergreen round-robin: least-recently-pinned products go first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT product_key, MAX(COALESCE(posted_at, scheduled_date)) AS last "
                "FROM pin_schedule WHERE product_key IS NOT NULL "
                "GROUP BY product_key").fetchall()
        return {r["product_key"]: (r["last"] or "") for r in rows}

    def list_pin_schedule(self, *, scheduled_date: str | None = None,
                          status: str | None = None,
                          product_key: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM pin_schedule"
        clauses: list[str] = []
        params: list[Any] = []
        if scheduled_date is not None:
            clauses.append("scheduled_date = ?")
            params.append(scheduled_date)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if product_key is not None:
            clauses.append("product_key = ?")
            params.append(product_key)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_pins_scheduled_on(self, scheduled_date: str) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM pin_schedule WHERE scheduled_date = ?",
                (scheduled_date,)).fetchone()[0])

    def due_pins(self, on_or_before: str) -> list[dict[str, Any]]:
        """Scheduled pins whose date has arrived (<= today) — ready to post."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pin_schedule WHERE status = 'scheduled' "
                "AND scheduled_date <= ? ORDER BY scheduled_date, id",
                (on_or_before,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_pin_metrics(self, pin_id: int, *, impressions: int, clicks: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pin_schedule SET impressions = ?, clicks = ? WHERE id = ?",
                (int(impressions), int(clicks), pin_id),
            )
            return cur.rowcount > 0

    def posted_pins(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pin_schedule WHERE status = 'posted' AND pin_ref IS NOT NULL "
                "ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def set_pin_status(self, pin_id: int, status: str, *, pin_ref: str | None = None,
                       posted_at: str | None = None) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pin_schedule SET status = ?, pin_ref = ?, posted_at = ? "
                "WHERE id = ?",
                (status, pin_ref, posted_at or (_utcnow() if status == "posted" else None),
                 pin_id),
            )
            return cur.rowcount > 0

    # --- Traffic: funnel --------------------------------------------

    def insert_traffic_funnel(self, row: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO traffic_funnel
                    (created_at, funnel_date, product_key, listing_id, source,
                     impressions, clicks, visits, sales)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(), row["funnel_date"], row.get("product_key"),
                    row.get("listing_id"), row.get("source", "pinterest"),
                    int(row.get("impressions", 0)), int(row.get("clicks", 0)),
                    int(row.get("visits", 0)), int(row.get("sales", 0)),
                ),
            )
            return int(cur.lastrowid)

    def list_traffic_funnel(self, *, funnel_date: str | None = None,
                            product_key: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM traffic_funnel"
        clauses: list[str] = []
        params: list[Any] = []
        if funnel_date is not None:
            clauses.append("funnel_date = ?")
            params.append(funnel_date)
        if product_key is not None:
            clauses.append("product_key = ?")
            params.append(product_key)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def funnel_totals(self, *, funnel_date: str | None = None) -> dict[str, int]:
        sql = ("SELECT COALESCE(SUM(impressions),0) AS impressions, "
               "COALESCE(SUM(clicks),0) AS clicks, COALESCE(SUM(visits),0) AS visits, "
               "COALESCE(SUM(sales),0) AS sales FROM traffic_funnel")
        params: list[Any] = []
        if funnel_date is not None:
            sql += " WHERE funnel_date = ?"
            params.append(funnel_date)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return {k: int(row[k]) for k in ("impressions", "clicks", "visits", "sales")}

    def learned_ctr_by_variant(self) -> dict[str, float]:
        """Average CTR per hero variant across all history with real impressions."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT variant, SUM(impressions) AS imp, SUM(clicks) AS clk "
                "FROM thumbnails GROUP BY variant"
            ).fetchall()
        out: dict[str, float] = {}
        for r in rows:
            imp = int(r["imp"] or 0)
            if imp > 0:
                out[r["variant"]] = round(int(r["clk"] or 0) / imp, 4)
        return out


def _row_to_brief(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["keywords"] = json.loads(data.get("keywords") or "[]")
    return data


def _row_to_content(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json.loads(data.get("metadata") or "{}")
    return data


def _row_to_knowledge(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["variables"] = json.loads(data.get("variables") or "[]")
    data["success_metrics"] = json.loads(data.get("success_metrics") or "[]")
    data["observed_metrics"] = json.loads(data.get("observed_metrics") or "{}")
    return data


def _row_to_proposal(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["risks"] = json.loads(data.get("risks") or "[]")
    # Surface investment economics stored in the JSON payload (kept there so no
    # schema migration is needed) without overriding column-authoritative fields.
    payload = json.loads(data.get("payload") or "{}")
    for key, value in payload.items():
        data.setdefault(key, value)
    return data


def _row_to_ledger(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _row_to_listing(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["raw"] = json.loads(data.get("raw") or "{}")
    return data


def _row_to_daily_run(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["stages"] = json.loads(data.get("stages") or "[]")
    return data


def _row_to_operations_report(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["system"] = json.loads(data.get("system") or "{}")
    data["business"] = json.loads(data.get("business") or "{}")
    data["recommendations"] = json.loads(data.get("recommendations") or "[]")
    data["preflight"] = json.loads(data.get("preflight") or "{}")
    return data


def _row_to_opportunity(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["colour_palette"] = json.loads(data.get("colour_palette") or "[]")
    data["payload"] = json.loads(data.get("payload") or "{}")
    return data


def _row_to_decision(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["policy_checks"] = json.loads(data.get("policy_checks") or "[]")
    return data


def _row_to_market(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    payload = json.loads(data.get("payload") or "{}")
    # Surface the full scored signal set (payload holds the raw sub-signals).
    return {**payload, **data, "payload": payload}


def _row_to_compliance(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["corrections"] = json.loads(data.get("corrections") or "[]")
    # advisories / blocking_issues / outcome live in the stored payload — restore
    # them so callers see the full commercial report, not just the columns.
    payload = json.loads(data.get("payload") or "{}")
    for key in ("advisories", "blocking_issues", "outcome"):
        if key in payload:
            data[key] = payload[key]
    data.setdefault("advisories", [])
    data.setdefault("blocking_issues", [])
    return data
