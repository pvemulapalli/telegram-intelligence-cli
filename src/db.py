from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4
DEFAULT_OVERLAP_IDS = 10


@dataclass(frozen=True)
class MessageRecord:
    chat_id: int
    message_id: int
    topic_id: int | None
    sender_id: int | None
    sender_display_name: str | None
    message_date_utc: str
    text: str | None
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    message_link: str | None
    content_kind: str | None = None


@dataclass(frozen=True)
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0

    def __add__(self, other: UpsertStats) -> UpsertStats:
        return UpsertStats(
            inserted=self.inserted + other.inserted,
            updated=self.updated + other.updated,
            skipped=self.skipped + other.skipped,
        )


@dataclass(frozen=True)
class TopicMessageStats:
    message_count: int
    latest_message_id: int | None
    latest_message_date_utc: str | None
    oldest_message_id: int | None


@dataclass(frozen=True)
class SyncCheckpoint:
    chat_id: int
    topic_id: int
    history_complete: bool
    last_sync_utc: str | None
    last_sync_status: str | None
    resume_offset_id: int | None
    overlap_message_ids: int
    latest_stored_message_id: int | None
    latest_stored_message_date_utc: str | None
    window_cutoff_utc: str | None = None
    window_months: int | None = None
    window_complete: bool = False
    full_history_complete: bool = False


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS topics (
            chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            title TEXT,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (chat_id, topic_id),
            FOREIGN KEY (chat_id) REFERENCES groups (chat_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            topic_id INTEGER,
            sender_id INTEGER,
            sender_display_name TEXT,
            message_date_utc TEXT NOT NULL,
            text TEXT,
            reply_to_message_id INTEGER,
            reply_to_top_message_id INTEGER,
            message_link TEXT,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id),
            UNIQUE (chat_id, message_id),
            FOREIGN KEY (chat_id) REFERENCES groups (chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_topic
            ON messages (chat_id, topic_id, message_date_utc);

        CREATE TABLE IF NOT EXISTS sync_checkpoints (
            chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            history_complete INTEGER NOT NULL DEFAULT 0,
            last_sync_utc TEXT,
            last_sync_status TEXT,
            resume_offset_id INTEGER,
            overlap_message_ids INTEGER NOT NULL DEFAULT 10,
            latest_stored_message_id INTEGER,
            latest_stored_message_date_utc TEXT,
            PRIMARY KEY (chat_id, topic_id),
            FOREIGN KEY (chat_id) REFERENCES groups (chat_id)
        );
        """
    )
    _migrate_schema(conn)
    from src.search_index import ensure_fts_backfill_once, ensure_fts_schema

    ensure_fts_schema(conn)
    ensure_fts_backfill_once(conn)
    conn.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "content_kind" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN content_kind TEXT")

    checkpoint_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(sync_checkpoints)").fetchall()
    }
    if checkpoint_cols:
        if "window_cutoff_utc" not in checkpoint_cols:
            conn.execute(
                "ALTER TABLE sync_checkpoints ADD COLUMN window_cutoff_utc TEXT"
            )
        if "window_months" not in checkpoint_cols:
            conn.execute(
                "ALTER TABLE sync_checkpoints ADD COLUMN window_months INTEGER"
            )
        if "window_complete" not in checkpoint_cols:
            conn.execute(
                "ALTER TABLE sync_checkpoints ADD COLUMN window_complete INTEGER NOT NULL DEFAULT 0"
            )
        if "full_history_complete" not in checkpoint_cols:
            conn.execute(
                "ALTER TABLE sync_checkpoints ADD COLUMN full_history_complete INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            UPDATE sync_checkpoints
            SET window_complete = history_complete
            WHERE window_complete = 0 AND history_complete = 1
            """
        )
        conn.execute(
            """
            UPDATE sync_checkpoints
            SET full_history_complete = history_complete
            WHERE full_history_complete = 0
              AND history_complete = 1
              AND window_months IS NULL
            """
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def upsert_group(conn: sqlite3.Connection, chat_id: int, title: str | None) -> None:
    conn.execute(
        """
        INSERT INTO groups (chat_id, title, updated_at_utc)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = COALESCE(excluded.title, groups.title),
            updated_at_utc = excluded.updated_at_utc
        """,
        (chat_id, title, utc_now_iso()),
    )


def upsert_topic(
    conn: sqlite3.Connection,
    chat_id: int,
    topic_id: int,
    title: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO topics (chat_id, topic_id, title, updated_at_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, topic_id) DO UPDATE SET
            title = COALESCE(excluded.title, topics.title),
            updated_at_utc = excluded.updated_at_utc
        """,
        (chat_id, topic_id, title, utc_now_iso()),
    )


_COMPARABLE_MESSAGE_FIELDS = (
    "topic_id",
    "sender_id",
    "sender_display_name",
    "message_date_utc",
    "text",
    "reply_to_message_id",
    "reply_to_top_message_id",
    "message_link",
    "content_kind",
)


def _merge_value(existing: object | None, incoming: object | None) -> object | None:
    if incoming is None and existing is not None:
        return existing
    return incoming


def upsert_message(conn: sqlite3.Connection, record: MessageRecord) -> UpsertStats:
    row = conn.execute(
        """
        SELECT topic_id, sender_id, sender_display_name, message_date_utc, text,
               reply_to_message_id, reply_to_top_message_id, message_link, content_kind
        FROM messages
        WHERE chat_id = ? AND message_id = ?
        """,
        (record.chat_id, record.message_id),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO messages (
                chat_id, message_id, topic_id, sender_id, sender_display_name,
                message_date_utc, text, reply_to_message_id, reply_to_top_message_id,
                message_link, content_kind, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.chat_id,
                record.message_id,
                record.topic_id,
                record.sender_id,
                record.sender_display_name,
                record.message_date_utc,
                record.text,
                record.reply_to_message_id,
                record.reply_to_top_message_id,
                record.message_link,
                record.content_kind,
                utc_now_iso(),
            ),
        )
        _sync_fts(conn, record.chat_id, record.message_id, record.text, record.content_kind)
        return UpsertStats(inserted=1)

    existing = dict(row)
    merged = {
        "topic_id": _merge_value(existing["topic_id"], record.topic_id),
        "sender_id": _merge_value(existing["sender_id"], record.sender_id),
        "sender_display_name": _merge_value(
            existing["sender_display_name"], record.sender_display_name
        ),
        "message_date_utc": _merge_value(
            existing["message_date_utc"], record.message_date_utc
        ),
        "text": _merge_value(existing["text"], record.text),
        "reply_to_message_id": _merge_value(
            existing["reply_to_message_id"], record.reply_to_message_id
        ),
        "reply_to_top_message_id": _merge_value(
            existing["reply_to_top_message_id"], record.reply_to_top_message_id
        ),
        "message_link": _merge_value(existing["message_link"], record.message_link),
        "content_kind": _merge_value(existing["content_kind"], record.content_kind),
    }
    if all(existing[field] == merged[field] for field in _COMPARABLE_MESSAGE_FIELDS):
        return UpsertStats(skipped=1)

    conn.execute(
        """
        UPDATE messages SET
            topic_id = ?,
            sender_id = ?,
            sender_display_name = ?,
            message_date_utc = ?,
            text = ?,
            reply_to_message_id = ?,
            reply_to_top_message_id = ?,
            message_link = ?,
            content_kind = ?,
            updated_at_utc = ?
        WHERE chat_id = ? AND message_id = ?
        """,
        (
            merged["topic_id"],
            merged["sender_id"],
            merged["sender_display_name"],
            merged["message_date_utc"],
            merged["text"],
            merged["reply_to_message_id"],
            merged["reply_to_top_message_id"],
            merged["message_link"],
            merged["content_kind"],
            utc_now_iso(),
            record.chat_id,
            record.message_id,
        ),
    )
    _sync_fts(
        conn,
        record.chat_id,
        record.message_id,
        merged["text"],
        merged["content_kind"],
    )
    return UpsertStats(updated=1)


def _sync_fts(
    conn: sqlite3.Connection,
    chat_id: int,
    message_id: int,
    text: object | None,
    content_kind: object | None,
) -> None:
    from src.search_index import sync_message_fts

    sync_message_fts(
        conn,
        chat_id=chat_id,
        message_id=message_id,
        text=text if isinstance(text, str) else None,
        content_kind=content_kind if isinstance(content_kind, str) else None,
    )


def upsert_messages(
    conn: sqlite3.Connection, records: list[MessageRecord]
) -> UpsertStats:
    total = UpsertStats()
    for record in records:
        total += upsert_message(conn, record)
    return total


def get_topic_message_stats(
    conn: sqlite3.Connection, chat_id: int, topic_id: int
) -> TopicMessageStats:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS message_count,
            MAX(message_id) AS latest_message_id,
            MAX(message_date_utc) AS latest_message_date_utc,
            MIN(message_id) AS oldest_message_id
        FROM messages
        WHERE chat_id = ? AND topic_id = ?
        """,
        (chat_id, topic_id),
    ).fetchone()
    return TopicMessageStats(
        message_count=int(row["message_count"]),
        latest_message_id=row["latest_message_id"],
        latest_message_date_utc=row["latest_message_date_utc"],
        oldest_message_id=row["oldest_message_id"],
    )


def get_sync_checkpoint(
    conn: sqlite3.Connection, chat_id: int, topic_id: int
) -> SyncCheckpoint | None:
    row = conn.execute(
        """
        SELECT chat_id, topic_id, history_complete, last_sync_utc, last_sync_status,
               resume_offset_id, overlap_message_ids,
               latest_stored_message_id, latest_stored_message_date_utc,
               window_cutoff_utc, window_months, window_complete, full_history_complete
        FROM sync_checkpoints
        WHERE chat_id = ? AND topic_id = ?
        """,
        (chat_id, topic_id),
    ).fetchone()
    if row is None:
        return None
    return SyncCheckpoint(
        chat_id=row["chat_id"],
        topic_id=row["topic_id"],
        history_complete=bool(row["history_complete"]),
        last_sync_utc=row["last_sync_utc"],
        last_sync_status=row["last_sync_status"],
        resume_offset_id=row["resume_offset_id"],
        overlap_message_ids=int(row["overlap_message_ids"]),
        latest_stored_message_id=row["latest_stored_message_id"],
        latest_stored_message_date_utc=row["latest_stored_message_date_utc"],
        window_cutoff_utc=row["window_cutoff_utc"],
        window_months=row["window_months"],
        window_complete=bool(row["window_complete"]),
        full_history_complete=bool(row["full_history_complete"]),
    )


def save_sync_checkpoint(conn: sqlite3.Connection, checkpoint: SyncCheckpoint) -> None:
    conn.execute(
        """
        INSERT INTO sync_checkpoints (
            chat_id, topic_id, history_complete, last_sync_utc, last_sync_status,
            resume_offset_id, overlap_message_ids,
            latest_stored_message_id, latest_stored_message_date_utc,
            window_cutoff_utc, window_months, window_complete, full_history_complete
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, topic_id) DO UPDATE SET
            history_complete = excluded.history_complete,
            last_sync_utc = excluded.last_sync_utc,
            last_sync_status = excluded.last_sync_status,
            resume_offset_id = excluded.resume_offset_id,
            overlap_message_ids = excluded.overlap_message_ids,
            latest_stored_message_id = excluded.latest_stored_message_id,
            latest_stored_message_date_utc = excluded.latest_stored_message_date_utc,
            window_cutoff_utc = excluded.window_cutoff_utc,
            window_months = excluded.window_months,
            window_complete = excluded.window_complete,
            full_history_complete = excluded.full_history_complete
        """,
        (
            checkpoint.chat_id,
            checkpoint.topic_id,
            int(checkpoint.window_complete or checkpoint.history_complete),
            checkpoint.last_sync_utc,
            checkpoint.last_sync_status,
            checkpoint.resume_offset_id,
            checkpoint.overlap_message_ids,
            checkpoint.latest_stored_message_id,
            checkpoint.latest_stored_message_date_utc,
            checkpoint.window_cutoff_utc,
            checkpoint.window_months,
            int(checkpoint.window_complete),
            int(checkpoint.full_history_complete),
        ),
    )


def list_sync_status_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            sc.chat_id,
            sc.topic_id,
            t.title AS topic_title,
            g.title AS chat_title,
            sc.history_complete,
            sc.window_cutoff_utc,
            sc.window_months,
            sc.window_complete,
            sc.full_history_complete,
            sc.last_sync_utc,
            sc.last_sync_status,
            sc.latest_stored_message_id,
            sc.latest_stored_message_date_utc,
            (
                SELECT COUNT(*)
                FROM messages m
                WHERE m.chat_id = sc.chat_id AND m.topic_id = sc.topic_id
            ) AS message_count
        FROM sync_checkpoints sc
        LEFT JOIN topics t ON t.chat_id = sc.chat_id AND t.topic_id = sc.topic_id
        LEFT JOIN groups g ON g.chat_id = sc.chat_id
        ORDER BY sc.last_sync_utc DESC, sc.chat_id, sc.topic_id
        """
    ).fetchall()


def compute_incremental_min_id(latest_message_id: int, overlap_message_ids: int) -> int:
    if latest_message_id <= 0:
        return 0
    return max(0, latest_message_id - overlap_message_ids)
