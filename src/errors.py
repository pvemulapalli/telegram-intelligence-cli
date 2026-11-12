from __future__ import annotations

import sys

from hydrogram.errors import (
    AuthKeyUnregistered,
    FloodWait,
    RPCError,
    SessionPasswordNeeded,
    SessionRevoked,
    Unauthorized,
)


def exit_with_message(message: str, *, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def format_rpc_error(exc: RPCError) -> str:
    if isinstance(exc, FloodWait):
        seconds = getattr(exc, "value", None)
        if isinstance(seconds, int) and seconds > 0:
            return (
                f"Telegram rate limit: wait {seconds} second(s) before retrying.\n"
                f"Details: {exc}"
            )
        return f"Telegram rate limit (FloodWait). Details: {exc}"

    if isinstance(exc, SessionPasswordNeeded):
        return (
            "Two-step verification is enabled on this account. "
            "Re-run the CLI; Hydrogram will prompt for your cloud password."
        )

    if isinstance(exc, (AuthKeyUnregistered, SessionRevoked, Unauthorized)):
        return (
            "Telegram session is invalid or expired. "
            f"Delete the session file in sessions/ and authenticate again.\n"
            f"Details: {exc}"
        )

    return str(exc)
