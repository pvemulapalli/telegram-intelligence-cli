from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import DATABASE_PATH
from src.db import connect, init_schema
from src.message_links import telegram_message_link
from src.search_index import (
    SearchQueryError,
    ensure_fts_backfill_once,
    prepare_fts_query,
)

__all__ = ["SearchQueryError", "run_search", "print_search_results", "print_message_context"]


@dataclass(frozen=True)
class SearchHit:
    chat_id: int
    message_id: int
    topic_id: int | None
    topic_title: str | None
    message_date_utc: str
    sender_display_name: str | None
    text: str
    excerpt: str
    message_link: str | None
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    rank: float


@dataclass(frozen=True)
class ContextMessage:
    message_id: int
    message_date_utc: str
    sender_display_name: str | None
    text: str | None
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    message_link: str | None
    is_target: bool


def _min_date_utc_for_days(days: int | None) -> str | None:
    if days is None or days <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.replace(microsecond=0).isoformat()


def _make_excerpt(text: str, query: str, width: int = 140) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= width:
        return normalized
    tokens = [t for t in query.split() if t]
    lower = normalized.casefold()
    pos = -1
    for token in tokens:
        idx = lower.find(token.casefold())
        if idx >= 0:
            pos = idx
            break
    if pos < 0:
        return normalized[: width - 1] + "…"
    start = max(0, pos - width // 3)
    end = min(len(normalized), start + width)
    snippet = normalized[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(normalized):
        snippet = snippet + "…"
    return snippet


def search_messages(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    query: str,
    limit: int = 20,
    topic_id: int | None = None,
    days: int | None = None,
) -> list[SearchHit]:
    fts_query = prepare_fts_query(query)
    if fts_query is None:
        raise SearchQueryError("Query is empty. Provide non-whitespace search terms.")

    min_date = _min_date_utc_for_days(days)
    params: list[object] = [fts_query, chat_id]
    topic_clause = ""
    if topic_id is not None:
        topic_clause = "AND m.topic_id = ?"
        params.append(topic_id)
    date_clause = ""
    if min_date is not None:
        date_clause = "AND m.message_date_utc >= ?"
        params.append(min_date)
    params.append(limit)

    sql = f"""
        SELECT
            m.chat_id,
            m.message_id,
            m.topic_id,
            t.title AS topic_title,
            m.message_date_utc,
            m.sender_display_name,
            m.text,
            m.message_link,
            m.reply_to_message_id,
            m.reply_to_top_message_id,
            bm25(messages_fts) AS rank
        FROM messages_fts
        JOIN messages m
          ON m.chat_id = messages_fts.chat_id
         AND m.message_id = messages_fts.message_id
        LEFT JOIN topics t
          ON t.chat_id = m.chat_id AND t.topic_id = m.topic_id
        WHERE messages_fts MATCH ?
          AND m.chat_id = ?
          {topic_clause}
          {date_clause}
        ORDER BY rank ASC, m.message_date_utc DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise SearchQueryError(f"Search failed: {exc}") from exc

    hits: list[SearchHit] = []
    for row in rows:
        body = row["text"] or ""
        hits.append(
            SearchHit(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                topic_id=row["topic_id"],
                topic_title=row["topic_title"],
                message_date_utc=row["message_date_utc"],
                sender_display_name=row["sender_display_name"],
                text=body,
                excerpt=_make_excerpt(body, query),
                message_link=telegram_message_link(
                    int(row["chat_id"]),
                    int(row["message_id"]),
                    stored_link=row["message_link"],
                ),
                reply_to_message_id=row["reply_to_message_id"],
                reply_to_top_message_id=row["reply_to_top_message_id"],
                rank=float(row["rank"]),
            )
        )
    return hits


def fetch_message_context(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    message_id: int,
    before: int = 3,
    after: int = 3,
) -> list[ContextMessage]:
    target = conn.execute(
        """
        SELECT message_id, topic_id, message_date_utc, sender_display_name, text,
               reply_to_message_id, reply_to_top_message_id, message_link
        FROM messages
        WHERE chat_id = ? AND message_id = ?
        """,
        (chat_id, message_id),
    ).fetchone()
    if target is None:
        return []

    topic_id = target["topic_id"]
    collected: dict[int, ContextMessage] = {}

    def row_to_context(row: sqlite3.Row, *, is_target: bool) -> ContextMessage:
        return ContextMessage(
            message_id=int(row["message_id"]),
            message_date_utc=row["message_date_utc"],
            sender_display_name=row["sender_display_name"],
            text=row["text"],
            reply_to_message_id=row["reply_to_message_id"],
            reply_to_top_message_id=row["reply_to_top_message_id"],
            message_link=telegram_message_link(
                chat_id,
                int(row["message_id"]),
                stored_link=row["message_link"],
            ),
            is_target=is_target,
        )

    collected[int(target["message_id"])] = row_to_context(target, is_target=True)

    parent_id = target["reply_to_message_id"]
    if parent_id is not None:
        parent = conn.execute(
            """
            SELECT message_id, message_date_utc, sender_display_name, text,
                   reply_to_message_id, reply_to_top_message_id, message_link
            FROM messages
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, parent_id),
        ).fetchone()
        if parent is not None:
            collected[int(parent["message_id"])] = row_to_context(
                parent, is_target=False
            )

    if topic_id is not None:
        older = conn.execute(
            """
            SELECT message_id, message_date_utc, sender_display_name, text,
                   reply_to_message_id, reply_to_top_message_id, message_link
            FROM messages
            WHERE chat_id = ? AND topic_id = ? AND message_id < ?
            ORDER BY message_id DESC
            LIMIT ?
            """,
            (chat_id, topic_id, message_id, before),
        ).fetchall()
        for row in older:
            collected[int(row["message_id"])] = row_to_context(row, is_target=False)

        newer = conn.execute(
            """
            SELECT message_id, message_date_utc, sender_display_name, text,
                   reply_to_message_id, reply_to_top_message_id, message_link
            FROM messages
            WHERE chat_id = ? AND topic_id = ? AND message_id > ?
            ORDER BY message_id ASC
            LIMIT ?
            """,
            (chat_id, topic_id, message_id, after),
        ).fetchall()
        for row in newer:
            collected[int(row["message_id"])] = row_to_context(row, is_target=False)

    return sorted(collected.values(), key=lambda m: (m.message_date_utc, m.message_id))


def run_search(
    chat_id: int,
    query: str,
    *,
    limit: int = 20,
    topic_id: int | None = None,
    days: int | None = None,
    db_path: Path = DATABASE_PATH,
) -> list[SearchHit]:
    conn = connect(db_path)
    try:
        init_schema(conn)
        ensure_fts_backfill_once(conn)
        conn.commit()
        hits = search_messages(
            conn,
            chat_id=chat_id,
            query=query,
            limit=limit,
            topic_id=topic_id,
            days=days,
        )
        conn.commit()
        return hits
    finally:
        conn.close()


def print_search_results(
    hits: list[SearchHit],
    *,
    query: str,
    context_hint: bool = True,
) -> None:
    if not hits:
        print(f"No matches for: {query!r}")
        return

    print(f"Matches for {query!r} ({len(hits)} shown):")
    print(
        f"{'#':>2}  {'Topic':<22}  {'Date (UTC)':<20}  {'Author':<14}  "
        f"{'Msg ID':>8}  Excerpt"
    )
    print("-" * 110)
    for index, hit in enumerate(hits, start=1):
        topic = hit.topic_title or (f"topic {hit.topic_id}" if hit.topic_id else "—")
        if len(topic) > 22:
            topic = topic[:21] + "…"
        author = hit.sender_display_name or "—"
        if len(author) > 14:
            author = author[:13] + "…"
        date_short = hit.message_date_utc.replace("T", " ")[:19]
        print(
            f"{index:>2}  {topic:<22}  {date_short:<20}  {author:<14}  "
            f"{hit.message_id:>8}  {hit.excerpt}"
        )
        link = hit.message_link or "—"
        reply_bits: list[str] = []
        if hit.reply_to_top_message_id is not None:
            reply_bits.append(f"top={hit.reply_to_top_message_id}")
        if hit.reply_to_message_id is not None:
            reply_bits.append(f"reply={hit.reply_to_message_id}")
        reply_label = ", ".join(reply_bits) if reply_bits else "—"
        print(f"     link: {link}  |  reply: {reply_label}")
    if context_hint:
        sample = hits[0]
        print()
        print(
            "Context (offline): python -m src.main message-context "
            f"--chat-id {sample.chat_id} --message-id <ID> "
            "[--mode thread | --before 5 --after 5]"
        )


def print_message_context(
    chat_id: int,
    message_id: int,
    *,
    mode: str = "chrono",
    before: int = 3,
    after: int = 3,
    max_messages: int = 50,
    db_path: Path = DATABASE_PATH,
) -> None:
    from src.thread_context import fetch_message_thread, print_thread_context_entries

    conn = connect(db_path)
    try:
        init_schema(conn)
        if mode == "thread":
            target = conn.execute(
                "SELECT topic_id FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            entries = fetch_message_thread(
                conn,
                chat_id=chat_id,
                message_id=message_id,
                max_messages=max_messages,
            )
        else:
            target = conn.execute(
                "SELECT topic_id FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            rows = fetch_message_context(
                conn,
                chat_id=chat_id,
                message_id=message_id,
                before=before,
                after=after,
            )
    finally:
        conn.close()

    if mode == "thread":
        topic_id = int(target["topic_id"]) if target else None
        print_thread_context_entries(
            entries,
            chat_id=chat_id,
            message_id=message_id,
            topic_id=topic_id,
        )
        return

    if not rows:
        print(f"No message {message_id} in SQLite for chat {chat_id}.")
        return

    print(f"Context for message {message_id} (chat {chat_id}, mode=chrono):")
    for row in rows:
        marker = ">>" if row.is_target else "  "
        author = row.sender_display_name or "—"
        date_short = row.message_date_utc.replace("T", " ")[:19]
        preview = " ".join((row.text or "").split())
        if len(preview) > 100:
            preview = preview[:99] + "…"
        if not preview:
            preview = "(no text)"
        link = row.message_link or "—"
        print(
            f"{marker} {row.message_id:>8}  {date_short}  {author:<16}  {preview}"
        )
        print(f"          link: {link}")
