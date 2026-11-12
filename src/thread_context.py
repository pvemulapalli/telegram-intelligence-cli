from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.message_links import telegram_message_link

DEFAULT_MAX_THREAD_MESSAGES = 50
DEFAULT_MAX_ANCESTOR_DEPTH = 25
DEFAULT_MAX_DESCENDANT_DEPTH = 8

_MESSAGE_ROW_SQL = """
    SELECT message_id, topic_id, message_date_utc, sender_display_name, text,
           reply_to_message_id, reply_to_top_message_id, message_link
    FROM messages
    WHERE chat_id = ? AND message_id = ?
"""


@dataclass(frozen=True)
class ThreadContextEntry:
    message_id: int | None
    role: str
    message_date_utc: str | None
    sender_display_name: str | None
    text: str | None
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    message_link: str | None
    is_target: bool
    missing_reference_id: int | None = None
    note: str | None = None


def _preview(text: str | None, width: int = 100) -> str:
    if not text:
        return "(no text)"
    one = " ".join(text.split())
    if len(one) <= width:
        return one
    return one[: width - 1] + "…"


def _load_message(
    conn: sqlite3.Connection,
    chat_id: int,
    message_id: int,
    *,
    topic_id: int | None,
) -> sqlite3.Row | None:
    row = conn.execute(_MESSAGE_ROW_SQL, (chat_id, message_id)).fetchone()
    if row is None:
        return None
    if topic_id is not None and row["topic_id"] != topic_id:
        return None
    return row


def _row_to_entry(
    row: sqlite3.Row,
    chat_id: int,
    *,
    role: str,
    is_target: bool,
) -> ThreadContextEntry:
    mid = int(row["message_id"])
    return ThreadContextEntry(
        message_id=mid,
        role=role,
        message_date_utc=row["message_date_utc"],
        sender_display_name=row["sender_display_name"],
        text=row["text"],
        reply_to_message_id=row["reply_to_message_id"],
        reply_to_top_message_id=row["reply_to_top_message_id"],
        message_link=telegram_message_link(
            chat_id, mid, stored_link=row["message_link"]
        ),
        is_target=is_target,
    )


def _missing_entry(
    referenced_id: int,
    *,
    note: str,
) -> ThreadContextEntry:
    return ThreadContextEntry(
        message_id=None,
        role="missing_parent",
        message_date_utc=None,
        sender_display_name=None,
        text=None,
        reply_to_message_id=None,
        reply_to_top_message_id=None,
        message_link=None,
        is_target=False,
        missing_reference_id=referenced_id,
        note=note,
    )


def fetch_message_thread(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    message_id: int,
    max_messages: int = DEFAULT_MAX_THREAD_MESSAGES,
    max_ancestor_depth: int = DEFAULT_MAX_ANCESTOR_DEPTH,
    max_descendant_depth: int = DEFAULT_MAX_DESCENDANT_DEPTH,
) -> list[ThreadContextEntry]:
    target_row = _load_message(conn, chat_id, message_id, topic_id=None)
    if target_row is None:
        return []

    topic_id = target_row["topic_id"]
    target_entry = _row_to_entry(
        target_row, chat_id, role="target", is_target=True
    )

    visited: set[int] = {message_id}
    ancestors: list[ThreadContextEntry] = []

    cursor_row = target_row
    depth = 0
    while (
        depth < max_ancestor_depth
        and len(ancestors) + 1 < max_messages
    ):
        parent_id = cursor_row["reply_to_message_id"]
        if parent_id is None:
            break
        if parent_id in visited:
            break
        visited.add(parent_id)

        parent_row = _load_message(
            conn, chat_id, int(parent_id), topic_id=topic_id
        )
        if parent_row is None:
            ancestors.append(
                _missing_entry(
                    int(parent_id),
                    note="Referenced parent is not in the local archive "
                    "(may be outside the synced window).",
                )
            )
            top_id = cursor_row["reply_to_top_message_id"]
            if (
                top_id is not None
                and int(top_id) not in visited
                and int(top_id) != int(parent_id)
            ):
                hint_row = _load_message(
                    conn, chat_id, int(top_id), topic_id=topic_id
                )
                if hint_row is not None:
                    visited.add(int(top_id))
                    ancestors.append(
                        _row_to_entry(
                            hint_row,
                            chat_id,
                            role="ancestor_hint",
                            is_target=False,
                        )
                    )
            break

        ancestors.append(
            _row_to_entry(parent_row, chat_id, role="ancestor", is_target=False)
        )
        cursor_row = parent_row
        depth += 1

    descendants: list[ThreadContextEntry] = []
    frontier = [message_id]
    desc_depth = 0
    while (
        frontier
        and desc_depth < max_descendant_depth
        and len(ancestors) + 1 + len(descendants) < max_messages
    ):
        next_frontier: list[int] = []
        for parent_msg_id in frontier:
            if len(ancestors) + 1 + len(descendants) >= max_messages:
                break
            child_rows = conn.execute(
                """
                SELECT message_id, topic_id, message_date_utc, sender_display_name,
                       text, reply_to_message_id, reply_to_top_message_id, message_link
                FROM messages
                WHERE chat_id = ? AND topic_id = ? AND reply_to_message_id = ?
                ORDER BY message_date_utc ASC, message_id ASC
                """,
                (chat_id, topic_id, parent_msg_id),
            ).fetchall()
            for child in child_rows:
                cid = int(child["message_id"])
                if cid in visited:
                    continue
                visited.add(cid)
                descendants.append(
                    _row_to_entry(child, chat_id, role="descendant", is_target=False)
                )
                next_frontier.append(cid)
                if len(ancestors) + 1 + len(descendants) >= max_messages:
                    break
        frontier = next_frontier
        desc_depth += 1

    ancestors.reverse()
    return ancestors + [target_entry] + descendants


def format_thread_role_label(entry: ThreadContextEntry) -> str:
    if entry.role == "target":
        return "target"
    if entry.role == "ancestor":
        return "ancestor"
    if entry.role == "ancestor_hint":
        return "ancestor (via reply_to_top_message_id)"
    if entry.role == "descendant":
        return "direct reply"
    if entry.role == "missing_parent":
        return "missing parent"
    return entry.role


def print_thread_context_entries(
    entries: list[ThreadContextEntry],
    *,
    chat_id: int,
    message_id: int,
    topic_id: int | None,
) -> None:
    if not entries:
        print(f"No message {message_id} in SQLite for chat {chat_id}.")
        return

    topic_label = str(topic_id) if topic_id is not None else "—"
    print(
        f"Thread context for message {message_id} "
        f"(chat {chat_id}, topic {topic_label}, mode=thread):"
    )
    print(f"Messages shown: {len(entries)} (reply graph only, same topic)")
    print()

    for entry in entries:
        marker = ">>" if entry.is_target else "  "
        role = format_thread_role_label(entry)
        if entry.role == "missing_parent":
            ref = entry.missing_reference_id
            print(f"{marker} [missing parent]  message_id={ref}  (not in archive)")
            if entry.note:
                print(f"          {entry.note}")
            continue

        author = entry.sender_display_name or "—"
        date_short = (entry.message_date_utc or "—").replace("T", " ")[:19]
        preview = _preview(entry.text)
        link = entry.message_link or "—"
        reply_bits: list[str] = []
        if entry.reply_to_top_message_id is not None:
            reply_bits.append(f"top={entry.reply_to_top_message_id}")
        if entry.reply_to_message_id is not None:
            reply_bits.append(f"reply={entry.reply_to_message_id}")
        reply_label = ", ".join(reply_bits) if reply_bits else "—"
        print(
            f"{marker} [{role}]  {entry.message_id:>8}  {date_short}  "
            f"{author:<16}  {preview}"
        )
        print(f"          link: {link}  |  {reply_label}")
