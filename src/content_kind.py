from __future__ import annotations

from hydrogram import types

CONTENT_TEXT = "text"
CONTENT_CAPTION = "caption"
CONTENT_MEDIA_ONLY = "media-only"
CONTENT_SERVICE = "service"
CONTENT_UNKNOWN = "unknown"


def classify_content_kind(message: types.Message) -> str:
    if getattr(message, "service", None) is not None:
        return CONTENT_SERVICE
    if getattr(message, "text", None):
        return CONTENT_TEXT
    if getattr(message, "caption", None):
        return CONTENT_CAPTION
    if any(
        getattr(message, field, None)
        for field in (
            "media",
            "photo",
            "video",
            "document",
            "audio",
            "voice",
            "video_note",
            "sticker",
            "animation",
            "poll",
        )
    ):
        return CONTENT_MEDIA_ONLY
    return CONTENT_UNKNOWN
