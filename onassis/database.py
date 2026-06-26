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


def _row_to_brief(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["keywords"] = json.loads(data.get("keywords") or "[]")
    return data


def _row_to_content(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json.loads(data.get("metadata") or "{}")
    return data
