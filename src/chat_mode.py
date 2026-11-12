from __future__ import annotations

from hydrogram import Client, enums

from src.sync import SyncError


async def resolve_supergroup_sync_mode(client: Client, chat_id: int) -> bool:
    """
    Return True when ``chat_id`` is a forum-enabled supergroup.

    Raises SyncError for unsupported or non-supergroup chats.
    """
    chat = await client.get_chat(chat_id)
    chat_type = chat.type

    if chat_type == enums.ChatType.CHANNEL:
        raise SyncError(
            "Broadcast channels are not supported. "
            "Use sync-group only with a supergroup from the groups command."
        )
    if chat_type == enums.ChatType.GROUP:
        raise SyncError(
            "Basic groups are not supported. "
            "Use a supergroup listed by the groups command."
        )
    if chat_type != enums.ChatType.SUPERGROUP:
        raise SyncError(
            f"Unsupported chat type for sync-group ({chat_type!r}). "
            "Use a supergroup from the groups command."
        )
    return bool(chat.is_forum)
