from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = PROJECT_ROOT / "sessions"
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "telegram.db"
SESSION_NAME = "user"

_API_HASH_PATTERN = re.compile(r"^[a-fA-F0-9]{32}$")


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    sessions_dir: Path
    session_name: str


def load_telegram_config() -> TelegramConfig:
    """Load and validate Telegram API credentials from the project .env file."""
    load_dotenv(PROJECT_ROOT / ".env")

    api_id_raw = os.getenv("TELEGRAM_API_ID")
    api_hash_raw = os.getenv("TELEGRAM_API_HASH")

    problems: list[str] = []

    if not api_id_raw or not str(api_id_raw).strip():
        problems.append("TELEGRAM_API_ID is missing or empty.")
    if not api_hash_raw or not str(api_hash_raw).strip():
        problems.append("TELEGRAM_API_HASH is missing or empty.")

    api_id: int | None = None
    api_hash: str | None = None

    if api_id_raw and str(api_id_raw).strip():
        try:
            api_id = int(str(api_id_raw).strip())
        except ValueError:
            problems.append("TELEGRAM_API_ID must be a whole number.")
        else:
            if api_id <= 0:
                problems.append("TELEGRAM_API_ID must be a positive integer.")

    if api_hash_raw and str(api_hash_raw).strip():
        api_hash = str(api_hash_raw).strip()
        if not _API_HASH_PATTERN.fullmatch(api_hash):
            problems.append(
                "TELEGRAM_API_HASH must be a 32-character hexadecimal string."
            )

    if problems:
        hint = (
            "Copy .env.example to .env and set credentials from "
            "https://my.telegram.org/apps"
        )
        raise ConfigError("\n".join(problems + [hint]))

    assert api_id is not None and api_hash is not None
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    return TelegramConfig(
        api_id=api_id,
        api_hash=api_hash.lower(),
        sessions_dir=SESSIONS_DIR,
        session_name=SESSION_NAME,
    )
