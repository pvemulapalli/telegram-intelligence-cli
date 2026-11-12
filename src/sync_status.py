from __future__ import annotations

from pathlib import Path

from src.config import DATABASE_PATH
from src.db import connect, init_schema, list_sync_status_rows


def print_sync_status(db_path: Path = DATABASE_PATH) -> None:
    conn = connect(db_path)
    try:
        init_schema(conn)
        rows = list_sync_status_rows(conn)
    finally:
        conn.close()

    if not rows:
        print("No synchronized topics yet. Run sync for a chat/topic pair first.")
        return

    print(
        f"{'Chat ID':>14}  {'Topic':>6}  {'Title':<20}  {'Count':>5}  "
        f"{'Mo':>2}  {'Window':>6}  {'Full':>4}  Last sync (UTC)"
    )
    print("-" * 100)
    for row in rows:
        title = row["topic_title"] or row["chat_title"] or "—"
        if len(title) > 20:
            title = title[:19] + "…"
        months = row["window_months"]
        if months is None:
            months_display = "—"
        elif months < 0:
            months_display = f"{abs(months)}d"
        else:
            months_display = str(months)
        window = "yes" if row["window_complete"] else "no"
        if months is not None:
            full = "no"
        else:
            full = "yes" if row["full_history_complete"] else "no"
        last_sync = (row["last_sync_utc"] or "—").replace("T", " ")[:19]
        status = row["last_sync_status"] or "—"
        cutoff = row["window_cutoff_utc"]
        cutoff_note = f" cutoff={cutoff}" if cutoff else ""
        print(
            f"{row['chat_id']:>14}  {row['topic_id']:>6}  {title:<20}  "
            f"{row['message_count']:>5}  {months_display:>2}  {window:>6}  {full:>4}  "
            f"{last_sync} ({status}){cutoff_note}"
        )
