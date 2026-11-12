from __future__ import annotations

import unittest

from hydrogram.raw.types import ForumTopic, ForumTopicDeleted

from src.topics import (
    TopicPage,
    collect_forum_topics_from_pages,
    next_page_offsets,
    offset_date_for_topic,
    register_topics,
)


def _topic(
    topic_id: int,
    *,
    title: str,
    top_message: int,
    date: int = 1_700_000_000,
    short: bool = False,
) -> ForumTopic:
    return ForumTopic(
        id=topic_id,
        date=date,
        title=title,
        icon_color=0x6FB9F0,
        top_message=top_message,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        from_id=None,
        notify_settings=None,
        short=short,
    )


class PaginationHelperTests(unittest.TestCase):
    def test_offset_date_uses_top_message_when_not_order_by_create_date(self) -> None:
        topic = _topic(10, title="Alpha", top_message=500, date=100)
        self.assertEqual(
            offset_date_for_topic(
                topic,
                order_by_create_date=False,
                message_dates={500: 9_999},
            ),
            9_999,
        )

    def test_next_offsets_for_deleted_topic(self) -> None:
        deleted = ForumTopicDeleted(id=77)
        self.assertEqual(
            next_page_offsets(
                deleted,
                order_by_create_date=False,
                message_dates={},
            ),
            (77, 0, 0),
        )

    def test_register_topics_dedupes_and_upgrades_deleted(self) -> None:
        registry: dict = {}
        register_topics(registry, [ForumTopicDeleted(id=5)])
        register_topics(
            registry,
            [_topic(5, title="Restored", top_message=50)],
        )
        self.assertIsInstance(registry[5], ForumTopic)
        self.assertEqual(registry[5].title, "Restored")

    def test_register_topics_prefers_full_over_short(self) -> None:
        registry: dict = {}
        register_topics(
            registry,
            [_topic(3, title="Short", top_message=30, short=True)],
        )
        register_topics(
            registry,
            [_topic(3, title="Full", top_message=30, short=False)],
        )
        self.assertFalse(registry[3].short)
        self.assertEqual(registry[3].title, "Full")


class MultiPageCollectionTests(unittest.TestCase):
    def test_multiple_pages_merge_all_topics(self) -> None:
        page_one = TopicPage(
            count=3,
            topics=(
                _topic(1, title="General", top_message=100),
                _topic(2, title="Alpha", top_message=200),
            ),
            message_dates={100: 1_000, 200: 2_000},
            order_by_create_date=False,
            offsets=(0, 0, 0),
        )
        page_two = TopicPage(
            count=3,
            topics=(_topic(3, title="Beta", top_message=300),),
            message_dates={300: 3_000},
            order_by_create_date=False,
            offsets=(2, 200, 2_000),
        )
        rows = collect_forum_topics_from_pages([page_one, page_two])
        titles = {row.title for row in rows}
        self.assertEqual(titles, {"Alpha", "Beta", "General"})

    def test_overlapping_pages_do_not_duplicate(self) -> None:
        alpha = _topic(2, title="Alpha", top_message=200)
        page_one = TopicPage(
            count=2,
            topics=(alpha,),
            message_dates={200: 2_000},
            order_by_create_date=False,
            offsets=(0, 0, 0),
        )
        page_two = TopicPage(
            count=2,
            topics=(alpha, _topic(3, title="Beta", top_message=300)),
            message_dates={200: 2_000, 300: 3_000},
            order_by_create_date=False,
            offsets=(2, 200, 2_000),
        )
        rows = collect_forum_topics_from_pages([page_one, page_two])
        self.assertEqual(len(rows), 2)

    def test_deleted_topic_objects_are_listed(self) -> None:
        page = TopicPage(
            count=2,
            topics=(
                _topic(1, title="General", top_message=10),
                ForumTopicDeleted(id=99),
            ),
            message_dates={10: 500},
            order_by_create_date=False,
            offsets=(0, 0, 0),
        )
        rows = collect_forum_topics_from_pages([page])
        statuses = {row.topic_id: row.status for row in rows}
        self.assertEqual(statuses[99], "deleted")

    def test_stops_when_count_less_than_returned_but_second_page_empty(self) -> None:
        """Simulates API count=1 while page one returns two topics; page two adds none."""
        page_one = TopicPage(
            count=1,
            topics=(
                _topic(1, title="General", top_message=100),
                _topic(2, title="Extra", top_message=200),
            ),
            message_dates={100: 1_000, 200: 2_000},
            order_by_create_date=False,
            offsets=(0, 0, 0),
        )
        page_two = TopicPage(
            count=1,
            topics=(),
            message_dates={},
            order_by_create_date=False,
            offsets=(2, 200, 2_000),
        )
        rows = collect_forum_topics_from_pages([page_one, page_two])
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
