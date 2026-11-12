from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

from hydrogram import types

DEFAULT_SYNC_GROUP_MONTHS = 6


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def subtract_calendar_months(when: datetime, months: int) -> datetime:
    """Move backward by ``months`` calendar months, clamping the day to month length."""
    if months < 0:
        raise ValueError("months must be non-negative")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)

    year = when.year
    month = when.month - months
    while month <= 0:
        month += 12
        year -= 1

    last_day = calendar.monthrange(year, month)[1]
    day = min(when.day, last_day)
    return when.replace(
        year=year,
        month=month,
        day=day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def cutoff_utc_from_months(
    months: int,
    *,
    when: datetime | None = None,
) -> datetime:
    """UTC cutoff for “messages on or after this instant” backfill windows."""
    anchor = when or utc_now()
    return subtract_calendar_months(anchor, months)


def cutoff_utc_from_days(
    days: int,
    *,
    when: datetime | None = None,
) -> datetime:
    """UTC cutoff for the last ``days`` calendar days (midnight UTC boundary)."""
    if days < 0:
        raise ValueError("days must be non-negative")
    anchor = when or utc_now()
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    else:
        anchor = anchor.astimezone(timezone.utc)
    base = anchor - timedelta(days=days)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)


def window_policy_key_for_days(days: int) -> int:
    """
    Checkpoint ``window_months`` value for a day-based window.

    Negative values distinguish day windows from calendar-month windows so a
  later ``--months`` run does not match a pilot ``--days`` checkpoint.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    return -days


def format_window_policy_label(policy_months: int) -> str:
    if policy_months < 0:
        days = abs(policy_months)
        noun = "day" if days == 1 else "days"
        return f"last {days} calendar {noun}"
    noun = "month" if policy_months == 1 else "months"
    return f"last {policy_months} calendar {noun}"


def cutoff_utc_iso(cutoff: datetime) -> str:
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def message_datetime_utc(message: types.Message) -> datetime:
    dt = message.date
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_message_date_utc(iso_value: str) -> datetime:
    text = iso_value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def message_on_or_after_cutoff(message: types.Message, cutoff: datetime) -> bool:
    return message_datetime_utc(message) >= cutoff.astimezone(timezone.utc)


def split_page_at_cutoff(
    page: list[types.Message],
    cutoff: datetime,
) -> tuple[list[types.Message], bool]:
    """
    Split a newest-first page at ``cutoff``.

    Returns in-window messages (date >= cutoff) and whether pagination should stop
    because an older-than-cutoff message was seen.
    """
    in_window: list[types.Message] = []
    for message in page:
        if message_on_or_after_cutoff(message, cutoff):
            in_window.append(message)
        else:
            return in_window, True
    return in_window, False
