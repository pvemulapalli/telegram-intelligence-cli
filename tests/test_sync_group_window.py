from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.db import SyncCheckpoint, TopicMessageStats
from src.main import _build_parser
from src.sync import SyncError, plan_sync, window_policy_matches
from src.sync_group import resolve_sync_group_window
from src.sync_window import (
    cutoff_utc_from_days,
    cutoff_utc_from_months,
    window_policy_key_for_days,
)


class ResolveSyncGroupWindowTests(unittest.TestCase):
    def test_default_months(self) -> None:
        window = resolve_sync_group_window(months=None, days=None)
        self.assertEqual(window.policy_key, 6)
        self.assertEqual(
            window.cutoff,
            cutoff_utc_from_months(6),
        )

    def test_explicit_months(self) -> None:
        window = resolve_sync_group_window(months=12, days=None)
        self.assertEqual(window.policy_key, 12)

    def test_days_pilot(self) -> None:
        window = resolve_sync_group_window(months=None, days=1)
        self.assertEqual(window.policy_key, -1)
        self.assertEqual(
            window.cutoff,
            cutoff_utc_from_days(1),
        )

    def test_rejects_both(self) -> None:
        with self.assertRaises(SyncError):
            resolve_sync_group_window(months=6, days=1)

    def test_rejects_non_positive_days(self) -> None:
        with self.assertRaises(SyncError):
            resolve_sync_group_window(days=0)


class DayPilotThenMonthsTests(unittest.TestCase):
    def test_policy_key_differs_from_six_months(self) -> None:
        self.assertEqual(window_policy_key_for_days(1), -1)
        self.assertFalse(window_policy_matches(
            SyncCheckpoint(
                chat_id=-100,
                topic_id=0,
                history_complete=True,
                last_sync_utc=None,
                last_sync_status="success",
                resume_offset_id=None,
                overlap_message_ids=10,
                latest_stored_message_id=500,
                latest_stored_message_date_utc=None,
                window_cutoff_utc="2026-11-11T00:00:00+00:00",
                window_months=-1,
                window_complete=True,
                full_history_complete=False,
            ),
            window_months=6,
        ))

    def test_days_pilot_then_full_months_triggers_backfill(self) -> None:
        anchor = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        pilot_cutoff = cutoff_utc_from_days(1, when=anchor).isoformat()
        full_cutoff = cutoff_utc_from_months(6, when=anchor).isoformat()
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=0,
            history_complete=True,
            last_sync_utc="2026-11-12T12:00:00+00:00",
            last_sync_status="success",
            resume_offset_id=None,
            overlap_message_ids=10,
            latest_stored_message_id=900,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=pilot_cutoff,
            window_months=-1,
            window_complete=True,
            full_history_complete=False,
        )
        stats = TopicMessageStats(10, 900, "2026-11-12T00:00:00+00:00", 1)
        plan = plan_sync(
            stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=6,
            window_cutoff_utc=full_cutoff,
        )
        self.assertEqual(plan.mode, "window_backfill")
        self.assertEqual(plan.min_id, 0)


class SyncGroupCliTests(unittest.TestCase):
    def test_days_only(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["sync-group", "--chat-id", "-100", "--days", "1"])
        self.assertEqual(args.days, 1)
        self.assertIsNone(args.months)

    def test_default_months_when_omitted(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["sync-group", "--chat-id", "-100"])
        self.assertIsNone(args.months)
        self.assertIsNone(args.days)
        window = resolve_sync_group_window(months=args.months, days=args.days)
        self.assertEqual(window.policy_key, 6)

    def test_both_flags_rejected_at_runtime(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["sync-group", "--chat-id", "-100", "--months", "6", "--days", "1"]
        )
        with self.assertRaises(SystemExit):
            if args.months is not None and args.days is not None:
                parser.error("Cannot use both --months and --days.")


if __name__ == "__main__":
    unittest.main()
