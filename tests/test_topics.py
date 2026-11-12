from __future__ import annotations

import unittest

from hydrogram.raw.types import ForumTopic, ForumTopicDeleted

from src.topics import parse_topic


class ParseTopicTests(unittest.TestCase):
    def test_deleted_topic(self) -> None:
        row = parse_topic(ForumTopicDeleted(id=42))
        self.assertEqual(row.topic_id, 42)
        self.assertEqual(row.status, "deleted")
        self.assertEqual(row.closed, "—")

    def test_general_and_flags(self) -> None:
        topic = ForumTopic(
            id=1,
            date=1_700_000_000,
            title="General",
            icon_color=0x6FB9F0,
            top_message=100,
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            unread_mentions_count=0,
            unread_reactions_count=0,
            from_id=None,
            notify_settings=None,
            closed=True,
            pinned=True,
            hidden=True,
        )
        row = parse_topic(topic)
        self.assertEqual(row.title, "General")
        self.assertEqual(row.closed, "yes")
        self.assertEqual(row.pinned, "yes")
        self.assertIn("hidden", row.status)


if __name__ == "__main__":
    unittest.main()
