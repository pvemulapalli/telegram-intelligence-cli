from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.sync_window import (
    cutoff_utc_from_months,
    message_on_or_after_cutoff,
    split_page_at_cutoff,
    subtract_calendar_months,
)


class _FakeMessage:
    def __init__(self, message_id: int, date: datetime) -> None:
        self.id = message_id
        self.date = date


class CalendarMonthTests(unittest.TestCase):
    def test_subtract_six_months_clamps_day(self) -> None:
        anchor = datetime(2026, 3, 31, 15, 30, tzinfo=timezone.utc)
        cutoff = subtract_calendar_months(anchor, 6)
        self.assertEqual(cutoff, datetime(2025, 9, 30, 0, 0, tzinfo=timezone.utc))

    def test_cutoff_at_run_start_is_midnight_utc(self) -> None:
        when = datetime(2026, 10, 18, 16, 48, tzinfo=timezone.utc)
        cutoff = cutoff_utc_from_months(6, when=when)
        self.assertEqual(cutoff, datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc))


class CutoffPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cutoff = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)

    def test_all_messages_in_window(self) -> None:
        page = [
            _FakeMessage(3, datetime(2026, 5, 1, tzinfo=timezone.utc)),
            _FakeMessage(2, datetime(2026, 4, 15, tzinfo=timezone.utc)),
            _FakeMessage(1, datetime(2026, 4, 1, tzinfo=timezone.utc)),
        ]
        kept, stop = split_page_at_cutoff(page, self.cutoff)
        self.assertEqual([m.id for m in kept], [3, 2, 1])
        self.assertFalse(stop)

    def test_stops_at_first_older_message(self) -> None:
        page = [
            _FakeMessage(4, datetime(2026, 5, 1, tzinfo=timezone.utc)),
            _FakeMessage(3, datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc)),
            _FakeMessage(2, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]
        kept, stop = split_page_at_cutoff(page, self.cutoff)
        self.assertEqual([m.id for m in kept], [4])
        self.assertTrue(stop)

    def test_on_cutoff_boundary_is_kept(self) -> None:
        msg = _FakeMessage(1, self.cutoff)
        self.assertTrue(message_on_or_after_cutoff(msg, self.cutoff))


if __name__ == "__main__":
    unittest.main()
