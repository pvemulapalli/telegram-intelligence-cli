from __future__ import annotations

from pathlib import Path

from src.config import DATABASE_PATH
from src.db import connect, init_schema


def _preview(text: str | None, width: int = 60) -> str:
    if not text:
        return ""
    one_line = " ".join(text.split())
    if len(one_line) <= width:
        return one_line
    return one_line[: width - 1] + "…"


def _reply_label(
    reply_to_message_id: int | None,
    reply_to_top_message_id: int | None,
) -> str:
    parts: list[str] = []
    if reply_to_top_message_id is not None:
        parts.append(f"top={reply_to_top_message_id}")
    if reply_to_message_id is not None:
        parts.append(f"reply={reply_to_message_id}")
    return ", ".join(parts) if parts else "—"


def print_inspect(
    chat_id: int,
    topic_id: int,
    *,
    limit: int = 10,
    db_path: Path = DATABASE_PATH,
) -> None:
    conn = connect(db_path)
    try:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT message_id, message_date_utc, sender_display_name,
                   text, reply_to_message_id, reply_to_top_message_id
            FROM messages
            WHERE chat_id = ? AND topic_id = ?
            ORDER BY message_date_utc DESC, message_id DESC
            LIMIT ?
            """,
            (chat_id, topic_id, limit),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(
            f"No messages in SQLite for chat {chat_id}, topic {topic_id}. "
            "Run ingest first."
        )
        return

    print(f"SQLite preview (chat {chat_id}, topic {topic_id}, newest {limit}):")
    header = f"{'ID':>8}  {'Date (UTC)':<20}  {'Author':<16}  {'Reply':<18}  Preview"
    print(header)
    print("-" * len(header))
    for row in rows:
        author = row["sender_display_name"] or "—"
        if len(author) > 16:
            author = author[:15] + "…"
        date_short = row["message_date_utc"].replace("T", " ")[:19]
        reply = _reply_label(
            row["reply_to_message_id"],
            row["reply_to_top_message_id"],
        )
        preview = _preview(row["text"])
        print(
            f"{row['message_id']:>8}  {date_short:<20}  {author:<16}  "
            f"{reply:<18}  {preview}"
        )
