from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hydrogram import Client

from src.config import DATABASE_PATH
from src.db import (
    UpsertStats,
    connect,
    init_schema,
    upsert_group,
    upsert_messages,
    upsert_topic,
)
from src.message_map import message_to_record
from src.topic_messages import fetch_forum_topic_messages


@dataclass(frozen=True)
class IngestResult:
    chat_id: int
    topic_id: int
    fetched: int
    inserted: int
    updated: int
    skipped: int
    date_min_utc: str | None
    date_max_utc: str | None


async def ingest_topic_messages(
    client: Client,
    chat_id: int,
    topic_id: int,
    *,
    limit: int = 100,
    db_path: Path = DATABASE_PATH,
) -> IngestResult:
    messages = await fetch_forum_topic_messages(
        client, chat_id, topic_id, limit=limit
    )

    chat_title: str | None = None
    try:
        chat = await client.get_chat(chat_id)
        chat_title = chat.title
    except Exception:
        chat_title = None

    topic_title: str | None = None
    from hydrogram.raw.functions.channels import GetForumTopicsByID
    from hydrogram.raw.types import ForumTopic

    try:
        channel = await client.resolve_peer(chat_id)
        by_id = await client.invoke(
            GetForumTopicsByID(channel=channel, topics=[topic_id])
        )
        for raw_topic in by_id.topics:
            if isinstance(raw_topic, ForumTopic):
                topic_title = raw_topic.title
                break
    except Exception:
        topic_title = None

    records = [
        message_to_record(message, chat_id=chat_id, topic_id=topic_id)
        for message in messages
    ]

    conn = connect(db_path)
    try:
        init_schema(conn)
        upsert_group(conn, chat_id, chat_title)
        upsert_topic(conn, chat_id, topic_id, topic_title)
        stats: UpsertStats = upsert_messages(conn, records)
        conn.commit()
    finally:
        conn.close()

    dates = sorted(record.message_date_utc for record in records)
    return IngestResult(
        chat_id=chat_id,
        topic_id=topic_id,
        fetched=len(messages),
        inserted=stats.inserted,
        updated=stats.updated,
        skipped=stats.skipped,
        date_min_utc=dates[0] if dates else None,
        date_max_utc=dates[-1] if dates else None,
    )


def print_ingest_result(result: IngestResult) -> None:
    print(f"Chat ID:     {result.chat_id}")
    print(f"Topic ID:    {result.topic_id}")
    print(f"Fetched:     {result.fetched}")
    print(f"Inserted:    {result.inserted}")
    print(f"Updated:     {result.updated}")
    print(f"Skipped:     {result.skipped}")
    if result.date_min_utc and result.date_max_utc:
        print(f"Date range:  {result.date_min_utc} .. {result.date_max_utc} (UTC)")
    else:
        print("Date range:  (no messages)")


class IngestError(Exception):
    pass
