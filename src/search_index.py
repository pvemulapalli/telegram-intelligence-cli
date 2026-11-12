from __future__ import annotations

import re
import sqlite3

from src.content_kind import CONTENT_MEDIA_ONLY

_FTS_TABLE = "messages_fts"


def ensure_fts_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5(
            searchable_text,
            chat_id UNINDEXED,
            message_id UNINDEXED,
            tokenize='unicode61'
        )
        """
    )


def is_searchable(text: str | None, content_kind: str | None) -> bool:
    if content_kind == CONTENT_MEDIA_ONLY:
        return False
    if text is None:
        return False
    return bool(text.strip())


def sync_message_fts(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    message_id: int,
    text: str | None,
    content_kind: str | None,
) -> None:
    ensure_fts_schema(conn)
    conn.execute(
        f"DELETE FROM {_FTS_TABLE} WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    if not is_searchable(text, content_kind):
        return
    conn.execute(
        f"""
        INSERT INTO {_FTS_TABLE} (searchable_text, chat_id, message_id)
        VALUES (?, ?, ?)
        """,
        (text.strip(), chat_id, message_id),
    )


def backfill_messages_fts(conn: sqlite3.Connection) -> int:
    """Index stored messages missing from FTS. Returns rows added."""
    ensure_fts_schema(conn)
    before = conn.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE}").fetchone()[0]
    conn.execute(
        f"""
        INSERT INTO {_FTS_TABLE} (searchable_text, chat_id, message_id)
        SELECT trim(m.text), m.chat_id, m.message_id
        FROM messages m
        WHERE m.text IS NOT NULL AND trim(m.text) != ''
          AND (m.content_kind IS NULL OR m.content_kind != ?)
          AND NOT EXISTS (
            SELECT 1 FROM {_FTS_TABLE} f
            WHERE f.chat_id = m.chat_id AND f.message_id = m.message_id
          )
        """,
        (CONTENT_MEDIA_ONLY,),
    )
    after = conn.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE}").fetchone()[0]
    return int(after) - int(before)


def ensure_fts_backfill_once(conn: sqlite3.Connection) -> int:
    """Run one-time backfill after upgrade; safe to call repeatedly."""
    ensure_fts_schema(conn)
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'fts_backfill'"
    ).fetchone()
    if row is not None and row[0] == "done":
        return 0
    added = backfill_messages_fts(conn)
    conn.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('fts_backfill', 'done')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """
    )
    return added


def prepare_fts_query(user_query: str) -> str | None:
    """
    Turn free text into a safe FTS5 MATCH expression (AND of quoted terms).

    Avoids treating user punctuation as FTS5 operators.
    """
    stripped = user_query.strip()
    if not stripped:
        return None
    tokens = re.findall(r"\S+", stripped)
    if not tokens:
        return None
    parts: list[str] = []
    for token in tokens:
        escaped = token.replace('"', '""')
        parts.append(f'"{escaped}"')
    return " AND ".join(parts)


class SearchQueryError(Exception):
    pass
