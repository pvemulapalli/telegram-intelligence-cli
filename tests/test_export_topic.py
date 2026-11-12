from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.db import MessageRecord, connect, init_schema, upsert_group, upsert_message, upsert_topic
from src.export_topic import (
    EXPORT_MODE_AFTER,
    EXPORT_MODE_COUNT,
    TopicCatalogEntry,
    TopicExportError,
    allocate_topic_export_path,
    build_export_topics_batch_spec,
    export_topic_by_name,
    export_topic_messages,
    export_topics_batch,
    fetch_latest_topic_messages,
    fetch_topic_messages_after_id,
    load_topic_catalog,
    normalize_topic_label,
    parse_topics_batch_spec,
    render_topic_export_markdown,
    resolve_topic_name,
    resolve_topics_batch,
    topic_name_for_batch_spec,
)
from src.message_links import telegram_message_link

EXAMPLE_CHAT_ID = -1001234567890


def _seed_catalog(conn) -> None:
    upsert_group(conn, EXAMPLE_CHAT_ID, "Example Forum")
    upsert_topic(conn, EXAMPLE_CHAT_ID, 100, "Announcements")
    upsert_topic(conn, EXAMPLE_CHAT_ID, 1, "General")
    upsert_topic(conn, EXAMPLE_CHAT_ID, 101, "Questions")
    upsert_topic(conn, EXAMPLE_CHAT_ID, 102, "Support")
    upsert_topic(conn, EXAMPLE_CHAT_ID, 103, "Crypto")
    upsert_topic(conn, EXAMPLE_CHAT_ID, 99, "Other")


class TopicNameMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(self.tmp.name) / "test.db")
        init_schema(conn)
        _seed_catalog(conn)
        conn.commit()
        self.catalog = load_topic_catalog(conn, EXAMPLE_CHAT_ID)
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_case_insensitive_exact(self) -> None:
        row = resolve_topic_name("crypto", self.catalog)
        self.assertEqual(row.topic_id, 103)

    def test_normalized_emoji_and_punctuation(self) -> None:
        self.assertEqual(
            resolve_topic_name("general", self.catalog).topic_id,
            1,
        )
        self.assertEqual(
            resolve_topic_name("Questions", self.catalog).topic_id,
            101,
        )
        self.assertEqual(
            resolve_topic_name("Support", self.catalog).topic_id,
            102,
        )

    def test_prefix_match_unique(self) -> None:
        archive_only = [
            TopicCatalogEntry(topic_id=104, title="Questions (Archive)"),
        ]
        self.assertEqual(
            resolve_topic_name("Questions", archive_only).topic_id,
            104,
        )

    def test_unknown_and_ambiguous(self) -> None:
        with self.assertRaises(TopicExportError) as ctx:
            resolve_topic_name("Missing Topic", self.catalog)
        self.assertIn("Unknown topic", str(ctx.exception))

        ambiguous_catalog = [
            TopicCatalogEntry(topic_id=1, title="Support Team"),
            TopicCatalogEntry(topic_id=2, title="Support Desk"),
        ]
        with self.assertRaises(TopicExportError) as ctx:
            resolve_topic_name("Support", ambiguous_catalog)
        self.assertIn("Ambiguous", str(ctx.exception))

    def test_normalize_topic_label(self) -> None:
        self.assertEqual(normalize_topic_label("General"), "general")
        self.assertEqual(normalize_topic_label("Support (Archive)"), "support archive")


class ExportTopicsBatchSpecTests(unittest.TestCase):
    def test_topic_name_for_batch_spec(self) -> None:
        self.assertEqual(topic_name_for_batch_spec("General"), "General")
        self.assertEqual(
            topic_name_for_batch_spec("Support (Archive)"),
            "Support (Archive)",
        )
        self.assertEqual(
            topic_name_for_batch_spec("Weird:Name|Here"),
            "Weird Name Here",
        )

    def test_build_export_topics_batch_spec(self) -> None:
        spec = build_export_topics_batch_spec(
            [
                ("Announcements", 28),
                ("Support", 0),
                ("General", 378),
            ]
        )
        self.assertEqual(spec, "Announcements:28|General:378")

    def test_build_export_topics_batch_spec_all_zero(self) -> None:
        self.assertIsNone(build_export_topics_batch_spec([("FAQs", 0)]))

    def test_batch_spec_names_resolve(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "test.db"
        conn = connect(db_path)
        init_schema(conn)
        _seed_catalog(conn)
        conn.commit()

        spec = build_export_topics_batch_spec(
            [("General", 5), ("Questions", 3), ("Crypto", 0)]
        )
        self.assertIsNotNone(spec)
        parsed = parse_topics_batch_spec(spec)
        resolved = resolve_topics_batch(conn, EXAMPLE_CHAT_ID, parsed)
        self.assertEqual({r.topic_id for r in resolved}, {1, 101})
        conn.close()


class ExportTopicQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.export_root = Path(self.tmp.name) / "exports"
        conn = connect(self.db_path)
        init_schema(conn)
        _seed_catalog(conn)
        records = [
            MessageRecord(
                chat_id=EXAMPLE_CHAT_ID,
                message_id=100,
                topic_id=100,
                sender_id=1,
                sender_display_name="Admin",
                message_date_utc="2026-01-01T10:00:00+00:00",
                text="First announcement",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            ),
            MessageRecord(
                chat_id=EXAMPLE_CHAT_ID,
                message_id=200,
                topic_id=100,
                sender_id=2,
                sender_display_name="Member",
                message_date_utc="2026-01-02T10:00:00+00:00",
                text="Line one\nLine two\n```code```",
                reply_to_message_id=100,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            ),
            MessageRecord(
                chat_id=EXAMPLE_CHAT_ID,
                message_id=300,
                topic_id=100,
                sender_id=3,
                sender_display_name="Bot",
                message_date_utc="2026-01-03T10:00:00+00:00",
                text=None,
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="media-only",
            ),
            MessageRecord(
                chat_id=EXAMPLE_CHAT_ID,
                message_id=999,
                topic_id=99,
                sender_id=1,
                sender_display_name="X",
                message_date_utc="2026-02-01T10:00:00+00:00",
                text="wrong topic",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind="text",
            ),
        ]
        for record in records:
            upsert_message(conn, record)
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_latest_n_by_message_id_chronological_in_markdown(self) -> None:
        conn = connect(self.db_path)
        messages = fetch_latest_topic_messages(conn, EXAMPLE_CHAT_ID, 100, limit=2)
        conn.close()
        self.assertEqual([m.message_id for m in messages], [200, 300])

        md = render_topic_export_markdown(
            chat_id=EXAMPLE_CHAT_ID,
            topic_id=100,
            chat_title="Example Forum",
            topic_title="Announcements",
            export_mode=EXPORT_MODE_COUNT,
            requested_limit=2,
            after_message_id=None,
            messages=messages,
            exported_at_utc="2026-10-18T12:00:00+00:00",
        )
        pos_200 = md.index("### Message 200")
        pos_300 = md.index("### Message 300")
        self.assertLess(pos_200, pos_300)
        self.assertIn("Line one", md)
        self.assertIn("```code```", md)
        self.assertIn("media-only", md)

    def test_metadata_and_links(self) -> None:
        conn = connect(self.db_path)
        messages = fetch_latest_topic_messages(conn, EXAMPLE_CHAT_ID, 100, limit=2)
        conn.close()
        msg = next(m for m in messages if m.message_id == 200)
        expected = telegram_message_link(EXAMPLE_CHAT_ID, msg.message_id)
        self.assertEqual(msg.message_link, expected)

        md = render_topic_export_markdown(
            chat_id=EXAMPLE_CHAT_ID,
            topic_id=100,
            chat_title="G",
            topic_title="Announcements",
            export_mode=EXPORT_MODE_COUNT,
            requested_limit=2,
            after_message_id=None,
            messages=messages,
            exported_at_utc="2026-10-18T12:00:00+00:00",
        )
        self.assertIn("**Reply to message ID:** 100", md)
        self.assertIn(expected or "", md)
        self.assertIn("Member", md)

    def test_fewer_messages_than_limit(self) -> None:
        result = export_topic_messages(
            EXAMPLE_CHAT_ID,
            100,
            limit=60,
            db_path=self.db_path,
            export_root=self.export_root,
        )
        self.assertEqual(result.exported_count, 3)
        self.assertEqual(result.requested_limit, 60)
        self.assertIn("Exported message count:** 3", result.export_path.read_text(encoding="utf-8"))

    def test_no_overwrite_and_safe_filename(self) -> None:
        when = datetime(2026, 10, 18, 15, 30, tzinfo=timezone.utc)
        first = allocate_topic_export_path(
            100, when, topic_title="Announcements", export_root=self.export_root
        )
        first.write_text("existing", encoding="utf-8")
        second = allocate_topic_export_path(
            100, when, topic_title="Announcements", export_root=self.export_root
        )
        self.assertNotEqual(first, second)
        self.assertIn("announcements-100_20261018T153000Z", str(first))
        self.assertIn("_2.md", str(second))

    def test_export_writes_file_no_db_mutation(self) -> None:
        conn = connect(self.db_path)
        before_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        before_cp = conn.execute("SELECT COUNT(*) FROM sync_checkpoints").fetchone()[0]
        conn.close()

        export_topic_messages(
            EXAMPLE_CHAT_ID,
            100,
            limit=2,
            db_path=self.db_path,
            export_root=self.export_root,
        )

        conn = connect(self.db_path)
        after_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        after_cp = conn.execute("SELECT COUNT(*) FROM sync_checkpoints").fetchone()[0]
        conn.close()
        self.assertEqual(before_msgs, after_msgs)
        self.assertEqual(before_cp, after_cp)

    def test_invalid_limit(self) -> None:
        with self.assertRaises(TopicExportError):
            export_topic_messages(
                EXAMPLE_CHAT_ID,
                100,
                limit=0,
                db_path=self.db_path,
                export_root=self.export_root,
            )

    def test_export_by_topic_name(self) -> None:
        result = export_topic_by_name(
            EXAMPLE_CHAT_ID,
            "Announcements",
            limit=2,
            db_path=self.db_path,
            export_root=self.export_root,
        )
        self.assertEqual(result.topic_id, 100)
        self.assertEqual(result.exported_count, 2)

    def test_batch_export_creates_multiple_files(self) -> None:
        spec = "Announcements:2|Other:1"
        results = export_topics_batch(
            EXAMPLE_CHAT_ID,
            spec,
            db_path=self.db_path,
            export_root=self.export_root,
        )
        self.assertEqual(len(results), 2)
        paths = {r.export_path for r in results}
        self.assertEqual(len(paths), 2)
        self.assertEqual(results[0].exported_count, 2)
        self.assertEqual(results[1].exported_count, 1)

    def test_batch_validation_before_write(self) -> None:
        with self.assertRaises(TopicExportError):
            export_topics_batch(
                EXAMPLE_CHAT_ID,
                "Announcements:2|UnknownTopic:5",
                db_path=self.db_path,
                export_root=self.export_root,
            )
        self.assertEqual(list(self.export_root.glob("*.md")), [])

    def test_malformed_and_duplicate_batch(self) -> None:
        with self.assertRaises(TopicExportError):
            parse_topics_batch_spec("BadEntry")
        with self.assertRaises(TopicExportError):
            parse_topics_batch_spec("Announcements:0")
        conn = connect(self.db_path)
        with self.assertRaises(TopicExportError):
            resolve_topics_batch(
                conn,
                EXAMPLE_CHAT_ID,
                parse_topics_batch_spec("Announcements:2|Announcements:3"),
            )
        conn.close()

    def test_after_boundary_excludes_boundary_message(self) -> None:
        conn = connect(self.db_path)
        messages = fetch_topic_messages_after_id(
            conn, EXAMPLE_CHAT_ID, 100, after_message_id=200
        )
        conn.close()
        self.assertEqual([m.message_id for m in messages], [300])

    def test_mixed_batch_count_and_after(self) -> None:
        spec = "Announcements:1|Other:after-998"
        results = export_topics_batch(
            EXAMPLE_CHAT_ID,
            spec,
            db_path=self.db_path,
            export_root=self.export_root,
        )
        self.assertEqual(len(results), 2)
        by_topic = {r.topic_id: r for r in results}
        self.assertEqual(by_topic[100].exported_count, 1)
        self.assertEqual(by_topic[100].export_mode, EXPORT_MODE_COUNT)
        self.assertEqual(by_topic[99].exported_count, 1)
        self.assertEqual(by_topic[99].export_mode, EXPORT_MODE_AFTER)
        self.assertEqual(by_topic[99].lowest_message_id, 999)

    def test_after_with_no_new_messages_skips_file(self) -> None:
        results = export_topics_batch(
            EXAMPLE_CHAT_ID,
            "Announcements:after-99999",
            db_path=self.db_path,
            export_root=self.export_root,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].skipped)
        self.assertIsNone(results[0].export_path)
        self.assertEqual(list(self.export_root.glob("*.md")), [])

    def test_parse_after_syntax(self) -> None:
        parsed = parse_topics_batch_spec("Announcements:after-12345|Crypto:10")
        self.assertEqual(parsed[0].export_mode, EXPORT_MODE_AFTER)
        self.assertEqual(parsed[0].after_message_id, 12345)
        self.assertEqual(parsed[1].export_mode, EXPORT_MODE_COUNT)
        self.assertEqual(parsed[1].limit, 10)

    def test_after_header_in_markdown(self) -> None:
        conn = connect(self.db_path)
        messages = fetch_topic_messages_after_id(
            conn, EXAMPLE_CHAT_ID, 100, after_message_id=100
        )
        conn.close()
        md = render_topic_export_markdown(
            chat_id=EXAMPLE_CHAT_ID,
            topic_id=100,
            chat_title="G",
            topic_title="Announcements",
            export_mode=EXPORT_MODE_AFTER,
            requested_limit=None,
            after_message_id=100,
            messages=messages,
            exported_at_utc="2026-10-18T12:00:00+00:00",
        )
        self.assertIn("after message ID", md)
        self.assertIn("**>** `100`", md)
        self.assertIn("200 .. 300", md)
        self.assertNotIn("### Message 100", md)


if __name__ == "__main__":
    unittest.main()
