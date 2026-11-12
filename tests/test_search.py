from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.content_kind import CONTENT_MEDIA_ONLY, CONTENT_TEXT
from src.db import MessageRecord, connect, init_schema, upsert_group, upsert_message, upsert_topic
from src.message_links import telegram_message_link
from src.search import fetch_message_context, search_messages
from src.search_index import (
    SearchQueryError,
    backfill_messages_fts,
    is_searchable,
    prepare_fts_query,
    sync_message_fts,
)


class FtsQueryTests(unittest.TestCase):
    def test_empty_query_returns_none(self) -> None:
        self.assertIsNone(prepare_fts_query("   "))

    def test_quotes_are_escaped(self) -> None:
        q = prepare_fts_query('foo "bar" baz')
        assert q is not None
        self.assertIn('""bar""', q)
        self.assertIn(" AND ", q)

    def test_punctuation_token_quoted(self) -> None:
        q = prepare_fts_query("example alerts!")
        assert q is not None
        self.assertIn('"alerts!"', q)


class SearchIndexTests(unittest.TestCase):
    def _conn(self) -> tuple:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "test.db")
        init_schema(conn)
        upsert_group(conn, -100, "G")
        upsert_topic(conn, -100, 101, "Questions")
        return tmp, conn

    def test_backfill_indexes_text_only(self) -> None:
        tmp, conn = self._conn()
        upsert_message(
            conn,
            MessageRecord(
                chat_id=-100,
                message_id=1,
                topic_id=101,
                sender_id=1,
                sender_display_name="A",
                message_date_utc="2026-10-01T00:00:00+00:00",
                text="example alerts setup guide",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind=CONTENT_TEXT,
            ),
        )
        upsert_message(
            conn,
            MessageRecord(
                chat_id=-100,
                message_id=2,
                topic_id=101,
                sender_id=1,
                sender_display_name="A",
                message_date_utc="2026-10-02T00:00:00+00:00",
                text=None,
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind=CONTENT_MEDIA_ONLY,
            ),
        )
        conn.commit()
        added = backfill_messages_fts(conn)
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        conn.close()
        tmp.cleanup()
        self.assertGreaterEqual(added, 0)
        self.assertEqual(count, 1)

    def test_update_syncs_index(self) -> None:
        tmp, conn = self._conn()
        record = MessageRecord(
            chat_id=-100,
            message_id=10,
            topic_id=101,
            sender_id=1,
            sender_display_name="A",
            message_date_utc="2026-10-01T00:00:00+00:00",
            text="alpha",
            reply_to_message_id=None,
            reply_to_top_message_id=None,
            message_link=None,
            content_kind=CONTENT_TEXT,
        )
        upsert_message(conn, record)
        conn.commit()
        updated = MessageRecord(
            chat_id=-100,
            message_id=10,
            topic_id=101,
            sender_id=1,
            sender_display_name="A",
            message_date_utc="2026-10-01T00:00:00+00:00",
            text="beta gamma",
            reply_to_message_id=None,
            reply_to_top_message_id=None,
            message_link=None,
            content_kind=CONTENT_TEXT,
        )
        upsert_message(conn, updated)
        conn.commit()
        hits = search_messages(conn, chat_id=-100, query="gamma", limit=5)
        conn.close()
        tmp.cleanup()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].message_id, 10)

    def test_search_topic_filter(self) -> None:
        tmp, conn = self._conn()
        upsert_topic(conn, -100, 1, "General")
        for mid, tid, text in (
            (1, 101, "needle in Questions"),
            (2, 1, "needle in General"),
        ):
            upsert_message(
                conn,
                MessageRecord(
                    chat_id=-100,
                    message_id=mid,
                    topic_id=tid,
                    sender_id=1,
                    sender_display_name="A",
                    message_date_utc="2026-10-01T00:00:00+00:00",
                    text=text,
                    reply_to_message_id=None,
                    reply_to_top_message_id=None,
                    message_link=None,
                    content_kind=CONTENT_TEXT,
                ),
            )
        conn.commit()
        all_hits = search_messages(conn, chat_id=-100, query="needle", limit=10)
        faq_hits = search_messages(
            conn, chat_id=-100, query="needle", limit=10, topic_id=101
        )
        conn.close()
        tmp.cleanup()
        self.assertEqual(len(all_hits), 2)
        self.assertEqual(len(faq_hits), 1)
        self.assertEqual(faq_hits[0].topic_id, 101)

    def test_context_includes_reply_parent(self) -> None:
        tmp, conn = self._conn()
        upsert_message(
            conn,
            MessageRecord(
                chat_id=-100,
                message_id=100,
                topic_id=101,
                sender_id=1,
                sender_display_name="Parent",
                message_date_utc="2026-10-01T00:00:00+00:00",
                text="parent text",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind=CONTENT_TEXT,
            ),
        )
        upsert_message(
            conn,
            MessageRecord(
                chat_id=-100,
                message_id=101,
                topic_id=101,
                sender_id=2,
                sender_display_name="Child",
                message_date_utc="2026-10-01T01:00:00+00:00",
                text="child text",
                reply_to_message_id=100,
                reply_to_top_message_id=100,
                message_link=None,
                content_kind=CONTENT_TEXT,
            ),
        )
        conn.commit()
        ctx = fetch_message_context(conn, chat_id=-100, message_id=101, before=0, after=0)
        ids = [row.message_id for row in ctx]
        conn.close()
        tmp.cleanup()
        self.assertIn(100, ids)
        self.assertIn(101, ids)


class LinkTests(unittest.TestCase):
    def test_builds_supergroup_link(self) -> None:
        link = telegram_message_link(-1001234567890, 50001)
        self.assertEqual(link, "https://t.me/c/1234567890/50001")

    def test_prefers_stored_link(self) -> None:
        stored = "https://t.me/c/1234567890/1/50001"
        self.assertEqual(
            telegram_message_link(-1001234567890, 50001, stored_link=stored),
            stored,
        )


class SearchSafetyTests(unittest.TestCase):
    def test_is_searchable_excludes_media_only(self) -> None:
        self.assertFalse(is_searchable(None, CONTENT_MEDIA_ONLY))
        self.assertTrue(is_searchable("hello", CONTENT_TEXT))

    def test_empty_search_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = connect(Path(tmpdir) / "test.db")
            init_schema(conn)
            with self.assertRaises(SearchQueryError):
                search_messages(conn, chat_id=-100, query="   ", limit=5)
            conn.close()


if __name__ == "__main__":
    unittest.main()
