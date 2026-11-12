from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.db import (
    MessageRecord,
    SyncCheckpoint,
    TopicMessageStats,
    compute_incremental_min_id,
    connect,
    get_sync_checkpoint,
    init_schema,
    upsert_group,
    upsert_message,
    upsert_messages,
)
from src.sync import plan_sync, window_policy_matches
from src.sync_window import cutoff_utc_iso, cutoff_utc_from_months


class PlanSyncTests(unittest.TestCase):
    def test_initial_when_empty(self) -> None:
        stats = TopicMessageStats(0, None, None, None)
        plan = plan_sync(stats, None, overlap_message_ids=10)
        self.assertEqual(plan.mode, "initial")
        self.assertEqual(plan.min_id, 0)

    def test_resume_when_history_incomplete(self) -> None:
        stats = TopicMessageStats(50, 500, "2026-01-01T00:00:00+00:00", 1)
        checkpoint = SyncCheckpoint(
            chat_id=-1,
            topic_id=2,
            history_complete=False,
            last_sync_utc=None,
            last_sync_status="in_progress",
            resume_offset_id=123,
            overlap_message_ids=10,
            latest_stored_message_id=500,
            latest_stored_message_date_utc=None,
        )
        plan = plan_sync(stats, checkpoint, overlap_message_ids=10)
        self.assertEqual(plan.mode, "resume_initial")
        self.assertEqual(plan.start_offset_id, 123)

    def test_incremental_when_history_complete(self) -> None:
        stats = TopicMessageStats(9, 5500, "2026-09-06T12:00:00+00:00", 101)
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=101,
            history_complete=True,
            last_sync_utc="2026-01-01T00:00:00+00:00",
            last_sync_status="success",
            resume_offset_id=None,
            overlap_message_ids=10,
            latest_stored_message_id=5500,
            latest_stored_message_date_utc=None,
        )
        plan = plan_sync(stats, checkpoint, overlap_message_ids=10)
        self.assertEqual(plan.mode, "incremental")
        self.assertEqual(plan.min_id, compute_incremental_min_id(5500, 10))


class OverlapAndDedupTests(unittest.TestCase):
    def test_compute_incremental_min_id(self) -> None:
        self.assertEqual(compute_incremental_min_id(5500, 10), 5490)
        self.assertEqual(compute_incremental_min_id(5, 10), 0)

    def test_overlap_edit_updates_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            init_schema(conn)
            upsert_group(conn, -100, "G")
            base = MessageRecord(
                chat_id=-100,
                message_id=5500,
                topic_id=101,
                sender_id=1,
                sender_display_name="A",
                message_date_utc="2026-09-06T12:00:00+00:00",
                text="original",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            )
            upsert_message(conn, base)
            edited = replace(base, text="edited")
            stats = upsert_message(conn, edited)
            conn.commit()
            text = conn.execute(
                "SELECT text FROM messages WHERE message_id = 5500"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(stats.updated, 1)
        self.assertEqual(text, "edited")

    def test_duplicate_second_insert_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            init_schema(conn)
            upsert_group(conn, -100, "G")
            record = MessageRecord(
                chat_id=-100,
                message_id=1,
                topic_id=101,
                sender_id=None,
                sender_display_name=None,
                message_date_utc="2026-01-01T00:00:00+00:00",
                text="x",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            )
            first = upsert_messages(conn, [record])
            second = upsert_messages(conn, [record])
            conn.close()
        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.skipped, 1)

    def test_preserve_existing_text_when_incoming_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            init_schema(conn)
            upsert_group(conn, -100, "G")
            base = MessageRecord(
                chat_id=-100,
                message_id=2,
                topic_id=101,
                sender_id=1,
                sender_display_name="A",
                message_date_utc="2026-01-01T00:00:00+00:00",
                text="keep me",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            )
            upsert_message(conn, base)
            sparse = replace(base, text=None, sender_display_name=None)
            stats = upsert_message(conn, sparse)
            row = conn.execute(
                "SELECT text, sender_display_name FROM messages WHERE message_id = 2"
            ).fetchone()
            conn.close()
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(row["text"], "keep me")
        self.assertEqual(row["sender_display_name"], "A")


class WindowPlanSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(6, when=datetime(2026, 10, 18, tzinfo=timezone.utc))
        )
        self.stats = TopicMessageStats(0, None, None, None)

    def test_window_backfill_when_no_checkpoint(self) -> None:
        plan = plan_sync(
            self.stats,
            None,
            overlap_message_ids=10,
            window_months=6,
            window_cutoff_utc=self.cutoff,
        )
        self.assertEqual(plan.mode, "window_backfill")

    def test_resume_window_when_same_policy_incomplete(self) -> None:
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=False,
            last_sync_utc=None,
            last_sync_status="in_progress",
            resume_offset_id=999,
            overlap_message_ids=10,
            latest_stored_message_id=1000,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=self.cutoff,
            window_months=6,
            window_complete=False,
            full_history_complete=False,
        )
        plan = plan_sync(
            self.stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=6,
            window_cutoff_utc=self.cutoff,
        )
        self.assertEqual(plan.mode, "resume_window")
        self.assertEqual(plan.start_offset_id, 999)

    def test_incremental_after_window_complete(self) -> None:
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=True,
            last_sync_utc="2026-01-01T00:00:00+00:00",
            last_sync_status="success",
            resume_offset_id=None,
            overlap_message_ids=10,
            latest_stored_message_id=500,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=self.cutoff,
            window_months=6,
            window_complete=True,
            full_history_complete=False,
        )
        stats = TopicMessageStats(10, 500, "2026-09-01T00:00:00+00:00", 1)
        plan = plan_sync(
            stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=6,
            window_cutoff_utc=self.cutoff,
        )
        self.assertEqual(plan.mode, "incremental")
        self.assertEqual(plan.min_id, 490)

    def test_smaller_months_restarts_window_backfill(self) -> None:
        other_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(3, when=datetime(2026, 10, 18, tzinfo=timezone.utc))
        )
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=True,
            last_sync_utc=None,
            last_sync_status="success",
            resume_offset_id=None,
            overlap_message_ids=10,
            latest_stored_message_id=500,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=self.cutoff,
            window_months=6,
            window_complete=True,
            full_history_complete=False,
        )
        plan = plan_sync(
            self.stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=3,
            window_cutoff_utc=other_cutoff,
        )
        self.assertEqual(plan.mode, "window_backfill")

    def test_incremental_when_cutoff_advances_next_day(self) -> None:
        stored_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(12, when=datetime(2026, 10, 17, tzinfo=timezone.utc))
        )
        next_day_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(12, when=datetime(2026, 10, 18, tzinfo=timezone.utc))
        )
        self.assertNotEqual(stored_cutoff, next_day_cutoff)
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=True,
            last_sync_utc="2026-10-17T12:00:00+00:00",
            last_sync_status="success",
            resume_offset_id=None,
            overlap_message_ids=10,
            latest_stored_message_id=900,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=stored_cutoff,
            window_months=12,
            window_complete=True,
            full_history_complete=False,
        )
        stats = TopicMessageStats(100, 900, "2026-10-17T00:00:00+00:00", 1)
        plan = plan_sync(
            stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=12,
            window_cutoff_utc=next_day_cutoff,
        )
        self.assertEqual(plan.mode, "incremental")
        self.assertEqual(plan.min_id, 890)

    def test_incremental_many_days_later_same_months(self) -> None:
        stored_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(12, when=datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
        later_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(12, when=datetime(2026, 10, 18, tzinfo=timezone.utc))
        )
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=True,
            last_sync_utc="2026-01-01T00:00:00+00:00",
            last_sync_status="success",
            resume_offset_id=None,
            overlap_message_ids=10,
            latest_stored_message_id=500,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=stored_cutoff,
            window_months=12,
            window_complete=True,
            full_history_complete=False,
        )
        stats = TopicMessageStats(50, 500, "2026-09-01T00:00:00+00:00", 1)
        plan = plan_sync(
            stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=12,
            window_cutoff_utc=later_cutoff,
        )
        self.assertEqual(plan.mode, "incremental")

    def test_expand_window_months_triggers_backfill(self) -> None:
        six_month_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(6, when=datetime(2026, 10, 18, tzinfo=timezone.utc))
        )
        twelve_month_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(12, when=datetime(2026, 10, 18, tzinfo=timezone.utc))
        )
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=True,
            last_sync_utc="2026-01-01T00:00:00+00:00",
            last_sync_status="success",
            resume_offset_id=None,
            overlap_message_ids=10,
            latest_stored_message_id=500,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=six_month_cutoff,
            window_months=6,
            window_complete=True,
            full_history_complete=False,
        )
        plan = plan_sync(
            self.stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=12,
            window_cutoff_utc=twelve_month_cutoff,
        )
        self.assertEqual(plan.mode, "window_backfill")
        self.assertEqual(plan.min_id, 0)

    def test_resume_window_when_cutoff_advances_mid_backfill(self) -> None:
        old_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(12, when=datetime(2026, 10, 17, tzinfo=timezone.utc))
        )
        new_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(12, when=datetime(2026, 10, 18, tzinfo=timezone.utc))
        )
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=False,
            last_sync_utc=None,
            last_sync_status="in_progress",
            resume_offset_id=777,
            overlap_message_ids=10,
            latest_stored_message_id=1000,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=old_cutoff,
            window_months=12,
            window_complete=False,
            full_history_complete=False,
        )
        plan = plan_sync(
            self.stats,
            checkpoint,
            overlap_message_ids=10,
            window_months=12,
            window_cutoff_utc=new_cutoff,
        )
        self.assertEqual(plan.mode, "resume_window")
        self.assertEqual(plan.start_offset_id, 777)

    def test_window_policy_matches_on_months_only(self) -> None:
        checkpoint = SyncCheckpoint(
            chat_id=-100,
            topic_id=1,
            history_complete=False,
            last_sync_utc=None,
            last_sync_status=None,
            resume_offset_id=0,
            overlap_message_ids=10,
            latest_stored_message_id=None,
            latest_stored_message_date_utc=None,
            window_cutoff_utc=self.cutoff,
            window_months=6,
        )
        other_cutoff = cutoff_utc_iso(
            cutoff_utc_from_months(6, when=datetime(2026, 11, 1, tzinfo=timezone.utc))
        )
        self.assertTrue(window_policy_matches(checkpoint, window_months=6))
        self.assertTrue(
            window_policy_matches(
                checkpoint, window_months=6, window_cutoff_utc=other_cutoff
            )
        )
        self.assertFalse(window_policy_matches(checkpoint, window_months=12))

    def test_incremental_plan_does_not_drop_existing_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = connect(db_path)
            init_schema(conn)
            upsert_group(conn, -100, "G")
            old = MessageRecord(
                chat_id=-100,
                message_id=100,
                topic_id=1,
                sender_id=1,
                sender_display_name="A",
                message_date_utc="2025-01-01T00:00:00+00:00",
                text="historical",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            )
            upsert_message(conn, old)
            conn.commit()
            count_before = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = -100"
            ).fetchone()[0]

            checkpoint = SyncCheckpoint(
                chat_id=-100,
                topic_id=1,
                history_complete=True,
                last_sync_utc="2026-01-01T00:00:00+00:00",
                last_sync_status="success",
                resume_offset_id=None,
                overlap_message_ids=10,
                latest_stored_message_id=500,
                latest_stored_message_date_utc=None,
                window_cutoff_utc=cutoff_utc_iso(
                    cutoff_utc_from_months(
                        12, when=datetime(2026, 1, 1, tzinfo=timezone.utc)
                    )
                ),
                window_months=12,
                window_complete=True,
                full_history_complete=False,
            )
            stats = TopicMessageStats(2, 500, "2026-09-01T00:00:00+00:00", 100)
            plan = plan_sync(
                stats,
                checkpoint,
                overlap_message_ids=10,
                window_months=12,
                window_cutoff_utc=cutoff_utc_iso(
                    cutoff_utc_from_months(
                        12, when=datetime(2026, 10, 18, tzinfo=timezone.utc)
                    )
                ),
            )
            self.assertEqual(plan.mode, "incremental")

            count_after = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = -100"
            ).fetchone()[0]
            text = conn.execute(
                "SELECT text FROM messages WHERE message_id = 100"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(count_before, count_after)
        self.assertEqual(text, "historical")


class CheckpointRecoveryTests(unittest.TestCase):
    def test_checkpoint_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            init_schema(conn)
            upsert_group(conn, -100, "Chat")
            from src.db import save_sync_checkpoint

            cp = SyncCheckpoint(
                chat_id=-100,
                topic_id=101,
                history_complete=False,
                last_sync_utc="2026-01-01T00:00:00+00:00",
                last_sync_status="in_progress",
                resume_offset_id=38850,
                overlap_message_ids=10,
                latest_stored_message_id=5500,
                latest_stored_message_date_utc="2026-09-06T12:00:00+00:00",
            )
            save_sync_checkpoint(conn, cp)
            conn.commit()
            loaded = get_sync_checkpoint(conn, -100, 101)
            conn.close()
        assert loaded is not None
        self.assertFalse(loaded.history_complete)
        self.assertEqual(loaded.resume_offset_id, 38850)
        self.assertEqual(loaded.last_sync_status, "in_progress")

    def test_window_checkpoint_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            init_schema(conn)
            upsert_group(conn, -100, "Chat")
            from src.db import save_sync_checkpoint

            cp = SyncCheckpoint(
                chat_id=-100,
                topic_id=101,
                history_complete=False,
                last_sync_utc="2026-01-01T00:00:00+00:00",
                last_sync_status="in_progress",
                resume_offset_id=38850,
                overlap_message_ids=10,
                latest_stored_message_id=5500,
                latest_stored_message_date_utc="2026-09-06T12:00:00+00:00",
                window_cutoff_utc="2026-04-18T00:00:00+00:00",
                window_months=6,
                window_complete=False,
                full_history_complete=False,
            )
            save_sync_checkpoint(conn, cp)
            conn.commit()
            loaded = get_sync_checkpoint(conn, -100, 101)
            conn.close()
        assert loaded is not None
        self.assertEqual(loaded.window_months, 6)
        self.assertEqual(loaded.window_cutoff_utc, "2026-04-18T00:00:00+00:00")
        self.assertFalse(loaded.window_complete)
        self.assertFalse(loaded.full_history_complete)


if __name__ == "__main__":
    unittest.main()
