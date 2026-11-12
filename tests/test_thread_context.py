from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.content_kind import CONTENT_TEXT
from src.db import MessageRecord, connect, init_schema, upsert_group, upsert_message, upsert_topic
from src.thread_context import fetch_message_thread


def _msg(
    message_id: int,
    topic_id: int,
    *,
    text: str,
    reply_to: int | None = None,
    reply_top: int | None = None,
    date: str = "2026-10-01T00:00:00+00:00",
    author: str = "User",
) -> MessageRecord:
    return MessageRecord(
        chat_id=-100,
        message_id=message_id,
        topic_id=topic_id,
        sender_id=1,
        sender_display_name=author,
        message_date_utc=date,
        text=text,
        reply_to_message_id=reply_to,
        reply_to_top_message_id=reply_top,
        message_link=None,
        content_kind=CONTENT_TEXT,
    )


class ThreadContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        init_schema(self.conn)
        upsert_group(self.conn, -100, "G")
        upsert_topic(self.conn, -100, 1, "General")
        upsert_topic(self.conn, -100, 101, "Questions")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_direct_reply_pair(self) -> None:
        upsert_message(self.conn, _msg(40443, 1, text="Mark question", author="Mark"))
        upsert_message(
            self.conn,
            _msg(40444, 1, text="Paul answer", reply_to=40443, author="Paul"),
        )
        self.conn.commit()
        entries = fetch_message_thread(
            self.conn, chat_id=-100, message_id=40444, max_messages=20
        )
        ids = [e.message_id for e in entries if e.message_id is not None]
        self.assertEqual(ids, [40443, 40444])
        self.assertEqual(entries[-1].role, "target")

    def test_multi_level_ancestors(self) -> None:
        upsert_message(self.conn, _msg(1, 1, text="root"))
        upsert_message(self.conn, _msg(2, 1, text="mid", reply_to=1))
        upsert_message(self.conn, _msg(3, 1, text="leaf", reply_to=2))
        self.conn.commit()
        entries = fetch_message_thread(self.conn, chat_id=-100, message_id=3)
        ids = [e.message_id for e in entries if e.message_id]
        self.assertEqual(ids, [1, 2, 3])

    def test_branching_descendants(self) -> None:
        upsert_message(self.conn, _msg(10, 1, text="parent"))
        upsert_message(self.conn, _msg(11, 1, text="child a", reply_to=10))
        upsert_message(self.conn, _msg(12, 1, text="child b", reply_to=10))
        self.conn.commit()
        entries = fetch_message_thread(self.conn, chat_id=-100, message_id=10)
        roles = [e.role for e in entries]
        self.assertEqual(roles.count("descendant"), 2)

    def test_missing_parent(self) -> None:
        upsert_message(
            self.conn,
            _msg(40444, 1, text="orphan reply", reply_to=40443),
        )
        self.conn.commit()
        entries = fetch_message_thread(self.conn, chat_id=-100, message_id=40444)
        missing = [e for e in entries if e.role == "missing_parent"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].missing_reference_id, 40443)

    def test_cycle_on_ancestor_chain(self) -> None:
        upsert_message(self.conn, _msg(1, 1, text="a", reply_to=2))
        upsert_message(self.conn, _msg(2, 1, text="b", reply_to=1))
        self.conn.commit()
        entries = fetch_message_thread(self.conn, chat_id=-100, message_id=1)
        ids = [e.message_id for e in entries if e.message_id]
        self.assertLessEqual(len(ids), 2)

    def test_topic_isolation(self) -> None:
        upsert_message(self.conn, _msg(100, 101, text="questions parent"))
        upsert_message(self.conn, _msg(200, 1, text="general", reply_to=100))
        self.conn.commit()
        entries = fetch_message_thread(self.conn, chat_id=-100, message_id=200)
        missing = [e for e in entries if e.role == "missing_parent"]
        self.assertEqual(missing[0].missing_reference_id, 100)
        self.assertFalse(any(e.message_id == 100 for e in entries))

    def test_max_messages_cap(self) -> None:
        upsert_message(self.conn, _msg(1, 1, text="root"))
        for mid in range(2, 30):
            upsert_message(
                self.conn, _msg(mid, 1, text=f"m{mid}", reply_to=mid - 1)
            )
        self.conn.commit()
        entries = fetch_message_thread(
            self.conn, chat_id=-100, message_id=29, max_messages=5
        )
        self.assertLessEqual(len(entries), 5)


if __name__ == "__main__":
    unittest.main()
