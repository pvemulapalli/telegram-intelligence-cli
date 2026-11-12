from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.topic_attribution import (
    GENERAL_TOPIC_ID,
    classify_for_general,
    message_belongs_to_general,
    message_belongs_to_topic,
)
from src.topic_messages import TopicMessageError, _validate_page

OTHER = frozenset({101, 102, 103})


class _Msg:
    def __init__(
        self,
        message_id: int,
        *,
        message_thread_id=None,
        reply_to_top_message_id=None,
        reply_to_message_id=None,
        service=None,
        text=None,
        caption=None,
    ) -> None:
        self.id = message_id
        self.message_thread_id = message_thread_id
        self.reply_to_top_message_id = reply_to_top_message_id
        self.reply_to_message_id = reply_to_message_id
        self.service = service
        self.text = text
        self.caption = caption
        self.date = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.from_user = None
        self.sender_chat = None
        self.chat = None


class GeneralAttributionTests(unittest.TestCase):
    def test_general_root_message(self) -> None:
        msg = _Msg(GENERAL_TOPIC_ID)
        self.assertTrue(message_belongs_to_general(msg, other_topic_ids=OTHER))

    def test_general_reply_without_thread_id(self) -> None:
        msg = _Msg(
            40810,
            reply_to_top_message_id=40749,
            reply_to_message_id=40767,
        )
        self.assertTrue(message_belongs_to_general(msg, other_topic_ids=OTHER))

    def test_rejects_other_topic_thread_id(self) -> None:
        msg = _Msg(40821, message_thread_id=102)
        self.assertFalse(message_belongs_to_general(msg, other_topic_ids=OTHER))

    def test_non_general_topic_unchanged(self) -> None:
        msg = _Msg(100, message_thread_id=101)
        self.assertTrue(message_belongs_to_topic(msg, 101))
        self.assertFalse(message_belongs_to_topic(msg, 999))

    def test_classify_get_replies_general_candidate(self) -> None:
        msg = _Msg(99, reply_to_top_message_id=50)
        row = classify_for_general(msg, other_topic_ids=OTHER, source="get_replies")
        self.assertEqual(row.decision, "general")

    def test_classify_get_history_ambiguous_without_thread(self) -> None:
        msg = _Msg(99)
        row = classify_for_general(msg, other_topic_ids=OTHER, source="get_history")
        self.assertEqual(row.decision, "ambiguous")

    def test_classify_get_history_other_topic(self) -> None:
        msg = _Msg(99, message_thread_id=40647)
        row = classify_for_general(msg, other_topic_ids=OTHER, source="get_history")
        self.assertEqual(row.decision, "non_general")

    def test_validate_page_rejects_leaked_other_topic(self) -> None:
        page = [_Msg(1), _Msg(2, message_thread_id=40647)]
        with self.assertRaises(TopicMessageError):
            _validate_page(page, GENERAL_TOPIC_ID, other_topic_ids=OTHER)

    def test_validate_page_accepts_general_replies(self) -> None:
        page = [
            _Msg(10, reply_to_top_message_id=40749, reply_to_message_id=40767),
            _Msg(11),
        ]
        out = _validate_page(page, GENERAL_TOPIC_ID, other_topic_ids=OTHER)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
