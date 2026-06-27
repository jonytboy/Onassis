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

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    sku             TEXT    UNIQUE,
    name            TEXT,
    campaign_id     INTEGER,
    brand           TEXT,
    marketplace     TEXT,
    production_cost REAL    NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1
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
    roi              REAL    NOT NULL DEFAULT 0
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
                WHERE verdict IN ('REJECT', 'REQUEST_MORE_INFO')
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
                     production_cost, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    product.get("sku"),
                    product.get("name", ""),
                    product.get("campaign_id"),
                    product.get("brand"),
                    product.get("marketplace"),
                    float(product.get("production_cost", 0) or 0),
                    1 if product.get("active", True) else 0,
                ),
            )
            return int(cur.lastrowid)

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
                     gross_profit, net_profit, profit_margin, roi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self, campaign_id: int, platform: str = "etsy"
    ) -> dict[str, Any] | None:
        """The latest non-failed real publication (draft/published) — for
        duplicate prevention."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM publications
                WHERE campaign_id = ? AND platform = ? AND status IN ('draft', 'published')
                ORDER BY id DESC LIMIT 1
                """,
                (campaign_id, platform),
            ).fetchone()
        return dict(row) if row else None

    def list_publications(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM publications ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

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


def _row_to_decision(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["policy_checks"] = json.loads(data.get("policy_checks") or "[]")
    return data


def _row_to_compliance(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["corrections"] = json.loads(data.get("corrections") or "[]")
    return data
