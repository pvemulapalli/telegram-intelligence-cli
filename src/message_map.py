from __future__ import annotations

from datetime import timezone

from hydrogram import types

from src.content_kind import classify_content_kind
from src.db import MessageRecord


def _sender_display_name(message: types.Message) -> tuple[int | None, str | None]:
    if message.from_user:
        user = message.from_user
        parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(part for part in parts if part).strip()
        if not name and user.username:
            name = f"@{user.username}"
        return user.id, name or None
    if message.sender_chat:
        chat = message.sender_chat
        return chat.id, chat.title
    return None, None


def message_to_record(
    message: types.Message,
    *,
    chat_id: int,
    topic_id: int,
) -> MessageRecord:
    sender_id, sender_name = _sender_display_name(message)
    message_dt = message.date
    if message_dt.tzinfo is None:
        message_dt = message_dt.replace(tzinfo=timezone.utc)
    else:
        message_dt = message_dt.astimezone(timezone.utc)

    body = message.text
    if body is None and message.caption:
        body = message.caption

    link: str | None = None
    try:
        link = message.link
    except (ValueError, AttributeError):
        link = None

    assigned_topic = message.message_thread_id
    if assigned_topic is None and message.id == topic_id:
        assigned_topic = topic_id
    if assigned_topic is None:
        assigned_topic = topic_id

    return MessageRecord(
        chat_id=chat_id,
        message_id=message.id,
        topic_id=assigned_topic,
        sender_id=sender_id,
        sender_display_name=sender_name,
        message_date_utc=message_dt.replace(microsecond=0).isoformat(),
        text=body,
        reply_to_message_id=message.reply_to_message_id,
        reply_to_top_message_id=message.reply_to_top_message_id,
        message_link=link,
        content_kind=classify_content_kind(message),
    )
