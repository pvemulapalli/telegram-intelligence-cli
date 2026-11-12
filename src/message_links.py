from __future__ import annotations


def telegram_message_link(
    chat_id: int,
    message_id: int,
    *,
    stored_link: str | None = None,
) -> str | None:
    if stored_link:
        return stored_link
    chat_str = str(chat_id)
    if chat_str.startswith("-100"):
        internal = chat_str[4:]
    elif chat_id < 0:
        internal = chat_str.lstrip("-")
    else:
        return None
    if not internal.isdigit():
        return None
    return f"https://t.me/c/{internal}/{message_id}"
