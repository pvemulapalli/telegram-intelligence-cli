from __future__ import annotations

from hydrogram import Client, raw, utils

from src.topic_attribution import GeneralClassification, classify_for_general
from src.topics import fetch_forum_topics

MAX_PAGE_SIZE = 100


async def fetch_general_replies_sample(
    client: Client,
    chat_id: int,
    *,
    limit: int,
) -> list[GeneralClassification]:
    other = await _other_topic_ids(client, chat_id)
    peer = await client.resolve_peer(chat_id)
    collected: list[GeneralClassification] = []
    offset_id = 0
    seen_offsets: set[int] = set()

    while len(collected) < limit:
        if offset_id in seen_offsets:
            break
        seen_offsets.add(offset_id)

        result = await client.invoke(
            raw.functions.messages.GetReplies(
                peer=peer,
                msg_id=1,
                offset_id=offset_id,
                offset_date=0,
                add_offset=0,
                limit=min(MAX_PAGE_SIZE, limit - len(collected)),
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        if not result.messages:
            break
        page = await utils.parse_messages(client, result, replies=0) or []
        if not page:
            break

        for message in page:
            collected.append(
                classify_for_general(
                    message, other_topic_ids=other, source="get_replies"
                )
            )
            if len(collected) >= limit:
                break

        if len(page) < MAX_PAGE_SIZE:
            break
        next_offset = page[-1].id
        if next_offset == offset_id:
            break
        offset_id = next_offset

    return collected


async def fetch_history_sample_for_comparison(
    client: Client,
    chat_id: int,
    *,
    limit: int,
) -> list[GeneralClassification]:
    other = await _other_topic_ids(client, chat_id)
    peer = await client.resolve_peer(chat_id)
    collected: list[GeneralClassification] = []
    offset_id = 0
    seen_offsets: set[int] = set()

    while len(collected) < limit:
        if offset_id in seen_offsets:
            break
        seen_offsets.add(offset_id)

        result = await client.invoke(
            raw.functions.messages.GetHistory(
                peer=peer,
                offset_id=offset_id,
                offset_date=0,
                add_offset=0,
                limit=min(MAX_PAGE_SIZE, limit - len(collected)),
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        if not result.messages:
            break
        page = await utils.parse_messages(client, result) or []
        if not page:
            break

        for message in page:
            collected.append(
                classify_for_general(
                    message, other_topic_ids=other, source="get_history"
                )
            )
            if len(collected) >= limit:
                break

        if len(page) < MAX_PAGE_SIZE:
            break
        next_offset = page[-1].id
        if next_offset == offset_id:
            break
        offset_id = next_offset

    return collected


async def _other_topic_ids(client: Client, chat_id: int) -> frozenset[int]:
    rows = await fetch_forum_topics(client, chat_id, debug=False)
    return frozenset(row.topic_id for row in rows if row.topic_id != 1)


def print_general_diagnostic(
    *,
    chat_id: int,
    get_replies: list[GeneralClassification],
    get_history: list[GeneralClassification],
) -> None:
    print(f"Chat ID: {chat_id}")
    print(
        "General topic uses messages.GetReplies(peer, msg_id=1) — same family as "
        "other forum topics (msg_id=topic_id)."
    )
    print("No message bodies below; only metadata and classification.")
    print()

    _print_section("GetReplies(msg_id=1) sample", get_replies)
    print()
    _print_section(
        "GetHistory sample (mixed topics; for comparison only — not used for sync)",
        get_history,
    )

    replies_counts = _count_decisions(get_replies)
    history_counts = _count_decisions(get_history)
    print()
    print("Summary:")
    print(f"  GetReplies decisions: {replies_counts}")
    print(f"  GetHistory decisions: {history_counts}")
    if replies_counts.get("non_general"):
        print(
            "  WARNING: non-General messages appeared in GetReplies(1); "
            "do not mark window complete until resolved."
        )
    if replies_counts.get("ambiguous"):
        print(
            "  WARNING: ambiguous GetReplies rows; do not mark window complete."
        )


def _count_decisions(rows: list[GeneralClassification]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.decision] = counts.get(row.decision, 0) + 1
    return counts


def _print_section(title: str, rows: list[GeneralClassification]) -> None:
    print(title)
    print(
        f"{'ID':>8}  {'Date (UTC)':<22}  {'Thread':>6}  {'RTop':>6}  {'RTo':>6}  "
        f"{'Svc':>3}  {'Kind':<12}  Decision / reason"
    )
    print("-" * 110)
    for row in rows:
        thread = "—" if row.message_thread_id is None else str(row.message_thread_id)
        rtop = "—" if row.reply_to_top_message_id is None else str(
            row.reply_to_top_message_id
        )
        rto = "—" if row.reply_to_message_id is None else str(row.reply_to_message_id)
        svc = "yes" if row.is_service else "no"
        date = row.message_date_utc.replace("T", " ")[:19]
        print(
            f"{row.message_id:>8}  {date:<22}  {thread:>6}  {rtop:>6}  {rto:>6}  "
            f"{svc:>3}  {row.content_kind:<12}  {row.decision}: {row.reason}"
        )
