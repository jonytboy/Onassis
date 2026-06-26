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
    return data


def _row_to_decision(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["policy_checks"] = json.loads(data.get("policy_checks") or "[]")
    return data


def _row_to_compliance(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["corrections"] = json.loads(data.get("corrections") or "[]")
    return data
