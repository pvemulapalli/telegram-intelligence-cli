from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from hydrogram import Client
from hydrogram.errors import RPCError
from hydrogram.raw.functions.channels import GetForumTopics
from hydrogram.raw.types import ForumTopic, ForumTopicDeleted
from hydrogram.raw.types.messages import ForumTopics

PAGE_LIMIT = 100
MAX_PAGES = 500

OffsetTriple = tuple[int, int, int]
RawTopic = ForumTopic | ForumTopicDeleted
@dataclass(frozen=True)
class TopicRow:
    topic_id: int
    title: str
    closed: str
    pinned: str
    status: str


@dataclass(frozen=True)
class TopicPage:
    """One page from channels.GetForumTopics (for tests and pagination)."""

    count: int
    topics: tuple[RawTopic, ...]
    message_dates: dict[int, int]
    order_by_create_date: bool
    offsets: OffsetTriple


def parse_topic(topic: RawTopic) -> TopicRow:
    if isinstance(topic, ForumTopicDeleted):
        return TopicRow(
            topic_id=topic.id,
            title="(deleted topic)",
            closed="—",
            pinned="—",
            status="deleted",
        )

    title = topic.title
    if topic.id == 1 and not title:
        title = "General"

    closed = "yes" if topic.closed else "no"
    pinned = "yes" if topic.pinned else "no"

    status_parts: list[str] = []
    if topic.hidden:
        status_parts.append("hidden")
    if topic.short:
        status_parts.append("short")
    status = ", ".join(status_parts) if status_parts else "active"

    return TopicRow(
        topic_id=topic.id,
        title=title,
        closed=closed,
        pinned=pinned,
        status=status,
    )


def message_dates_from_forum_topics(result: ForumTopics) -> dict[int, int]:
    dates: dict[int, int] = {}
    for message in result.messages:
        message_id = getattr(message, "id", None)
        message_date = getattr(message, "date", None)
        if isinstance(message_id, int) and isinstance(message_date, int):
            dates[message_id] = message_date
    return dates


def offset_date_for_topic(
    topic: RawTopic,
    *,
    order_by_create_date: bool,
    message_dates: dict[int, int],
) -> int:
    if isinstance(topic, ForumTopicDeleted):
        return 0
    if order_by_create_date:
        return topic.date
    return message_dates.get(topic.top_message, topic.date)


def next_page_offsets(
    last_topic: RawTopic,
    *,
    order_by_create_date: bool,
    message_dates: dict[int, int],
) -> OffsetTriple:
    if isinstance(last_topic, ForumTopicDeleted):
        return last_topic.id, 0, 0
    offset_topic = last_topic.id
    offset_id = last_topic.top_message
    offset_date = offset_date_for_topic(
        last_topic,
        order_by_create_date=order_by_create_date,
        message_dates=message_dates,
    )
    return offset_topic, offset_id, offset_date


def register_topics(
    registry: dict[int, RawTopic],
    page_topics: Iterable[RawTopic],
) -> int:
    """Merge topics into registry; return count of newly seen topic IDs."""
    new_count = 0
    for topic in page_topics:
        topic_id = topic.id
        if topic_id not in registry:
            registry[topic_id] = topic
            new_count += 1
            continue
        existing = registry[topic_id]
        if isinstance(existing, ForumTopicDeleted) and isinstance(topic, ForumTopic):
            registry[topic_id] = topic
        elif isinstance(existing, ForumTopic) and isinstance(topic, ForumTopic):
            if existing.short and not topic.short:
                registry[topic_id] = topic
    return new_count


def paginate_forum_topic_pages(
    pages: Iterable[TopicPage],
) -> dict[int, RawTopic]:
    """Merge topic pages the same way the live fetch loop does (offline tests)."""
    registry: dict[int, RawTopic] = {}
    for page in pages:
        if not page.topics:
            break
        new_count = register_topics(registry, page.topics)
        if new_count == 0:
            break
    return registry


def collect_forum_topics_from_pages(
    pages: Iterable[TopicPage],
) -> list[TopicRow]:
    registry = paginate_forum_topic_pages(pages)
    rows = [parse_topic(topic) for topic in registry.values()]
    rows.sort(key=lambda row: (row.title.casefold(), row.topic_id))
    return rows


def _topic_page_from_result(
    result: ForumTopics,
    offsets: OffsetTriple,
) -> TopicPage:
    return TopicPage(
        count=result.count,
        topics=tuple(result.topics),
        message_dates=message_dates_from_forum_topics(result),
        order_by_create_date=bool(result.order_by_create_date),
        offsets=offsets,
    )


async def fetch_forum_topics(
    client: Client,
    chat_id: int,
    *,
    debug: bool = False,
) -> list[TopicRow]:
    channel = await client.resolve_peer(chat_id)
    offsets: OffsetTriple = (0, 0, 0)
    registry: dict[int, RawTopic] = {}
    seen_request_offsets: set[OffsetTriple] = set()
    max_reported_count = 0

    def log(line: str) -> None:
        if debug:
            print(f"[topics debug] {line}")

    for page_index in range(1, MAX_PAGES + 1):
        offset_topic, offset_id, offset_date = offsets
        if offsets in seen_request_offsets:
            log(f"page {page_index}: duplicate offset request {offsets}; stopping")
            break
        seen_request_offsets.add(offsets)

        result = await client.invoke(
            GetForumTopics(
                channel=channel,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=PAGE_LIMIT,
            )
        )
        max_reported_count = max(max_reported_count, result.count)
        page = _topic_page_from_result(result, offsets)
        new_count = register_topics(registry, page.topics) if page.topics else 0

        log(
            f"page {page_index}: offsets=(topic={offset_topic}, id={offset_id}, date={offset_date}) "
            f"returned={len(page.topics)} total_count={page.count} "
            f"new_ids={new_count} unique={len(registry)} "
            f"order_by_create_date={page.order_by_create_date}"
        )

        if not page.topics:
            log("empty topics list; stopping")
            break

        if new_count == 0:
            log("no new topic IDs on this page; stopping")
            break

        last = page.topics[-1]
        next_offsets = next_page_offsets(
            last,
            order_by_create_date=page.order_by_create_date,
            message_dates=page.message_dates,
        )
        if next_offsets == offsets:
            log(f"offsets unchanged after last topic {last.id}; stopping")
            break

        offsets = next_offsets

    if debug and max_reported_count and len(registry) < max_reported_count:
        log(
            f"note: collected {len(registry)} unique topic IDs but API reported "
            f"count up to {max_reported_count}; no further pages were returned"
        )

    rows = [parse_topic(topic) for topic in registry.values()]
    rows.sort(key=lambda row: (row.title.casefold(), row.topic_id))
    return rows


def print_topics(rows: list[TopicRow], chat_id: int) -> None:
    if not rows:
        print(f"No forum topics returned for chat {chat_id}.")
        return

    title_w = max(len("Title"), max(len(r.title) for r in rows))
    title_w = min(title_w, 40)

    header = (
        f"{'ID':>6}  {'Title':<{title_w}}  {'Closed':<6}  {'Pinned':<6}  Status"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        title = row.title if len(row.title) <= title_w else row.title[: title_w - 1] + "…"
        print(
            f"{row.topic_id:>6}  {title:<{title_w}}  {row.closed:<6}  "
            f"{row.pinned:<6}  {row.status}"
        )
    print(f"\n{len(rows)} topic(s) listed for chat {chat_id}.")


def forum_error_hint(exc: RPCError) -> str | None:
    text = str(exc).upper()
    if "CHANNEL_FORUM_MISSING" in text:
        return "This chat is not a forum-enabled supergroup."
    if "CHANNEL_PRIVATE" in text:
        return "You do not have access to this chat (not a member or invalid ID)."
    if "CHANNEL_INVALID" in text or "PEER_ID_INVALID" in text:
        return "Invalid chat ID. Use an ID from the `groups` command."
    return None


def sync_access_hint(exc: RPCError) -> str | None:
    text = str(exc).upper()
    hint = forum_error_hint(exc)
    if hint is not None:
        return hint
    if "USER_NOT_PARTICIPANT" in text:
        return (
            "You are not a member of this supergroup, or it is not accessible "
            "with your account."
        )
    if "CHAT_ID_INVALID" in text or "CHAT_INVALID" in text:
        return "Invalid chat ID. Use an ID from the `groups` command."
    if "CHANNEL_PRIVATE" in text:
        return "You do not have access to this chat (not a member or invalid ID)."
    return None
