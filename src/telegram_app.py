from __future__ import annotations

from hydrogram import Client

from src.config import TelegramConfig


def build_client(config: TelegramConfig) -> Client:
    """Create a read-only Hydrogram user client (no update handlers)."""
    return Client(
        name=config.session_name,
        api_id=config.api_id,
        api_hash=config.api_hash,
        workdir=str(config.sessions_dir),
        no_updates=True,
    )
