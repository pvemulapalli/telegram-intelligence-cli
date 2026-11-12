from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Literal

from hydrogram import types

from src.content_kind import classify_content_kind

GENERAL_TOPIC_ID = 1

SourceKind = Literal["get_replies", "get_history"]


@dataclass(frozen=True)
class GeneralClassification:
    message_id: int
    message_date_utc: str
    message_thread_id: int | None
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    is_service: bool
    content_kind: str
    decision: str
    reason: str


def _message_date_utc(message: types.Message) -> str:
    dt = message.date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def message_belongs_to_general(
    message: types.Message,
    *,
    other_topic_ids: frozenset[int] | None = None,
) -> bool:
    """
    True when a message belongs to the General forum topic.

    Used for pages returned by ``messages.GetReplies`` with ``msg_id=1``.
    ``reply_to_top_message_id`` is *not* the forum topic id; it often points at
    an earlier message inside the same General thread.
    """
    if message.id == GENERAL_TOPIC_ID:
        return True

    thread_id = message.message_thread_id
    if thread_id is not None:
        if thread_id == GENERAL_TOPIC_ID:
            return True
        if other_topic_ids and thread_id in other_topic_ids:
            return False
        return False

    return True


def message_belongs_to_topic(
    message: types.Message,
    topic_id: int,
    *,
    other_topic_ids: frozenset[int] | None = None,
) -> bool:
    """Return True when Hydrogram attributes the message to the given forum topic."""
    if topic_id == GENERAL_TOPIC_ID:
        return message_belongs_to_general(message, other_topic_ids=other_topic_ids)

    if message.id == topic_id:
        return True
    if message.message_thread_id is not None:
        return message.message_thread_id == topic_id
    if message.reply_to_top_message_id is not None:
        return message.reply_to_top_message_id == topic_id
    if message.reply_to_message_id is not None:
        return message.reply_to_message_id == topic_id
    return False


def classify_for_general(
    message: types.Message,
    *,
    other_topic_ids: frozenset[int],
    source: SourceKind,
) -> GeneralClassification:
    thread_id = message.message_thread_id
    is_service = bool(getattr(message, "service", None))

    if thread_id is not None and thread_id != GENERAL_TOPIC_ID:
        if thread_id in other_topic_ids:
            decision, reason = (
                "non_general",
                f"message_thread_id={thread_id} is another forum topic",
            )
        else:
            decision, reason = (
                "non_general",
                f"message_thread_id={thread_id} is not General (1)",
            )
    elif thread_id == GENERAL_TOPIC_ID:
        decision, reason = "general", "message_thread_id=1"
    elif source == "get_replies":
        decision, reason = (
            "general",
            "no message_thread_id; included in GetReplies(msg_id=1) page",
        )
    else:
        decision, reason = (
            "ambiguous",
            "GetHistory: no message_thread_id (may be General or legacy main chat)",
        )

    return GeneralClassification(
        message_id=message.id,
        message_date_utc=_message_date_utc(message),
        message_thread_id=thread_id,
        reply_to_message_id=message.reply_to_message_id,
        reply_to_top_message_id=message.reply_to_top_message_id,
        is_service=is_service,
        content_kind=classify_content_kind(message),
        decision=decision,
        reason=reason,
    )
