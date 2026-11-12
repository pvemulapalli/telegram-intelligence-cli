from __future__ import annotations

from collections.abc import AsyncIterator

from hydrogram import Client, raw, types, utils

from src.topic_attribution import GENERAL_TOPIC_ID, message_belongs_to_topic

__all__ = [
    "GENERAL_TOPIC_ID",
    "TopicMessageError",
    "fetch_forum_topic_messages",
    "iter_forum_topic_message_pages",
    "message_belongs_to_topic",
]

MAX_PAGE_SIZE = 100


class TopicMessageError(Exception):
    """Forum topic message fetch or attribution failed."""


async def _invoke_replies_page(
    client: Client,
    peer: raw.base.InputPeer,
    topic_id: int,
    *,
    offset_id: int,
    min_id: int,
    limit: int,
) -> list[types.Message]:
    result = await client.invoke(
        raw.functions.messages.GetReplies(
            peer=peer,
            msg_id=topic_id,
            offset_id=offset_id,
            offset_date=0,
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=min_id,
            hash=0,
        )
    )
    if not result.messages:
        return []
    page = await utils.parse_messages(client, result, replies=0)
    return page or []


def _validate_page(
    page: list[types.Message],
    topic_id: int,
    *,
    other_topic_ids: frozenset[int] | None = None,
) -> list[types.Message]:
    validated: list[types.Message] = []
    for message in page:
        if not message_belongs_to_topic(
            message, topic_id, other_topic_ids=other_topic_ids
        ):
            raise TopicMessageError(
                f"Message {message.id} could not be attributed to topic {topic_id}. "
                "Refusing to ingest unrelated topic content."
            )
        validated.append(message)
    return validated


async def iter_forum_topic_message_pages(
    client: Client,
    chat_id: int,
    topic_id: int,
    *,
    min_id: int = 0,
    start_offset_id: int = 0,
    other_topic_ids: frozenset[int] | None = None,
) -> AsyncIterator[list[types.Message]]:
    """Yield validated message pages for a forum topic (newest-first from API)."""
    peer = await client.resolve_peer(chat_id)
    offset_id = start_offset_id
    seen_offsets: set[int] = set()

    while True:
        if offset_id in seen_offsets:
            break
        seen_offsets.add(offset_id)

        page = await _invoke_replies_page(
            client,
            peer,
            topic_id,
            offset_id=offset_id,
            min_id=min_id,
            limit=MAX_PAGE_SIZE,
        )
        if not page:
            break

        yield _validate_page(
            page, topic_id, other_topic_ids=other_topic_ids
        )

        if len(page) < MAX_PAGE_SIZE:
            break

        next_offset_id = page[-1].id
        if next_offset_id == offset_id:
            break
        offset_id = next_offset_id


async def fetch_forum_topic_messages(
    client: Client,
    chat_id: int,
    topic_id: int,
    *,
    limit: int,
    other_topic_ids: frozenset[int] | None = None,
) -> list[types.Message]:
    """Fetch up to ``limit`` messages for a forum topic (read-only)."""
    if limit <= 0:
        return []

    collected: list[types.Message] = []
    seen_ids: set[int] = set()

    async for page in iter_forum_topic_message_pages(
        client,
        chat_id,
        topic_id,
        other_topic_ids=other_topic_ids,
    ):
        for message in page:
            if message.id in seen_ids:
                continue
            seen_ids.add(message.id)
            collected.append(message)
            if len(collected) >= limit:
                return collected

    return collected
