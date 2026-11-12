from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.db import (
    MessageRecord,
    connect,
    init_schema,
    upsert_group,
    upsert_message,
    upsert_topic,
)
from src.message_map import message_to_record
from src.topic_messages import message_belongs_to_topic


class SchemaTests(unittest.TestCase):
    def test_init_schema_creates_messages_unique_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "test.db")
            init_schema(conn)
            indexes = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
            ).fetchone()[0]
            self.assertIn("UNIQUE (chat_id, message_id)", indexes)
            conn.close()


class DuplicateIngestionTests(unittest.TestCase):
    def test_repeated_upsert_skips_unchanged_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = connect(db)
            init_schema(conn)
            upsert_group(conn, -1001, "Test Group")
            record = MessageRecord(
                chat_id=-1001,
                message_id=42,
                topic_id=101,
                sender_id=1,
                sender_display_name="Alice",
                message_date_utc="2026-01-01T12:00:00+00:00",
                text="FAQ entry",
                reply_to_message_id=101,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            )
            first = upsert_message(conn, record)
            second = upsert_message(conn, record)
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND message_id = ?",
                (-1001, 42),
            ).fetchone()[0]
            conn.close()
        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(count, 1)

    def test_upsert_updates_when_text_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = connect(db)
            init_schema(conn)
            upsert_group(conn, -1001, "G")
            base = MessageRecord(
                chat_id=-1001,
                message_id=7,
                topic_id=101,
                sender_id=2,
                sender_display_name="Bob",
                message_date_utc="2026-01-02T08:00:00+00:00",
                text="v1",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link="https://t.me/example/7",
                content_kind="text",
            )
            upsert_message(conn, base)
            changed = replace(base, text="v2")
            stats = upsert_message(conn, changed)
            conn.commit()
            text = conn.execute(
                "SELECT text FROM messages WHERE message_id = 7"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(stats.updated, 1)
        self.assertEqual(text, "v2")


class TopicAssignmentTests(unittest.TestCase):
    def test_message_belongs_to_topic_uses_thread_id(self) -> None:
        class Msg:
            id = 100
            message_thread_id = 101
            reply_to_top_message_id = None
            reply_to_message_id = None

        self.assertTrue(message_belongs_to_topic(Msg(), 101))
        self.assertFalse(message_belongs_to_topic(Msg(), 999))

    def test_message_to_record_preserves_reply_fields(self) -> None:
        class User:
            id = 99
            first_name = "Pat"
            last_name = None
            username = None

        class Chat:
            id = -1001234567890

        class Msg:
            id = 5500
            message_thread_id = 101
            from_user = User()
            sender_chat = None
            service = None
            date = __import__("datetime").datetime(
                2026, 9, 6, 3, 18, 36, tzinfo=__import__("datetime").timezone.utc
            )
            chat = Chat()
            text = "Where can I find the onboarding FAQ?"
            caption = None
            reply_to_message_id = 101
            reply_to_top_message_id = None

            @property
            def link(self) -> str:
                return "https://t.me/c/1234567890/5500"

        record = message_to_record(Msg(), chat_id=-1001234567890, topic_id=101)
        self.assertEqual(record.topic_id, 101)
        self.assertEqual(record.reply_to_message_id, 101)
        self.assertEqual(record.sender_id, 99)
        self.assertIn("onboarding FAQ", record.text or "")


if __name__ == "__main__":
    unittest.main()
