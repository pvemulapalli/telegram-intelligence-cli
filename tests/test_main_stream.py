from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from hydrogram import enums

from src.chat_mode import resolve_supergroup_sync_mode
from src.export_topic import build_export_topics_batch_spec
from src.message_map import message_to_record
from src.sync import SyncError
from src.topic_attribution import MAIN_STREAM_TOPIC_ID, MAIN_STREAM_TOPIC_TITLE
from src.topic_messages import iter_main_chat_message_pages


class _Msg:
    def __init__(
        self,
        message_id: int,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_to_top_message_id: int | None = None,
        text: str | None = "hi",
        service=None,
    ) -> None:
        self.id = message_id
        self.message_thread_id = message_thread_id
        self.reply_to_message_id = reply_to_message_id
        self.reply_to_top_message_id = reply_to_top_message_id
        self.text = text
        self.caption = None
        self.service = service
        self.date = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.from_user = None
        self.sender_chat = None
        self.reply_to_message = None

    @property
    def link(self) -> str:
        return f"https://t.me/c/1/{self.id}"


class PinTopicTests(unittest.TestCase):
    def test_main_stream_pins_topic_id_zero(self) -> None:
        msg = _Msg(42, message_thread_id=99)
        record = message_to_record(
            msg,
            chat_id=-100,
            topic_id=MAIN_STREAM_TOPIC_ID,
            pin_topic_id=MAIN_STREAM_TOPIC_ID,
        )
        self.assertEqual(record.topic_id, 0)
        self.assertEqual(record.message_id, 42)
        self.assertEqual(record.reply_to_message_id, None)

    def test_forum_uses_thread_metadata(self) -> None:
        msg = _Msg(42, message_thread_id=101)
        record = message_to_record(msg, chat_id=-100, topic_id=101)
        self.assertEqual(record.topic_id, 101)


class ExportHintTests(unittest.TestCase):
    def test_export_spec_for_main(self) -> None:
        spec = build_export_topics_batch_spec([(MAIN_STREAM_TOPIC_TITLE, 25)])
        self.assertEqual(spec, "Main:25")


class ChatModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_forum_supergroup(self) -> None:
        client = AsyncMock()
        chat = MagicMock()
        chat.type = enums.ChatType.SUPERGROUP
        chat.is_forum = True
        client.get_chat.return_value = chat
        self.assertTrue(await resolve_supergroup_sync_mode(client, -100))

    async def test_ordinary_supergroup(self) -> None:
        client = AsyncMock()
        chat = MagicMock()
        chat.type = enums.ChatType.SUPERGROUP
        chat.is_forum = False
        client.get_chat.return_value = chat
        self.assertFalse(await resolve_supergroup_sync_mode(client, -100))

    async def test_rejects_channel(self) -> None:
        client = AsyncMock()
        chat = MagicMock()
        chat.type = enums.ChatType.CHANNEL
        client.get_chat.return_value = chat
        with self.assertRaises(SyncError):
            await resolve_supergroup_sync_mode(client, -100)

    async def test_rejects_basic_group(self) -> None:
        client = AsyncMock()
        chat = MagicMock()
        chat.type = enums.ChatType.GROUP
        client.get_chat.return_value = chat
        with self.assertRaises(SyncError):
            await resolve_supergroup_sync_mode(client, -100)


class MainChatPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_paginates_with_offset_and_min_id(self) -> None:
        client = AsyncMock()
        client.resolve_peer.return_value = MagicMock()

        pages = [[_Msg(300), _Msg(299)], [_Msg(298)]]
        call_idx = {"n": 0}

        async def fake_history(
            _client,
            _peer,
            *,
            offset_id: int,
            min_id: int,
            limit: int,
        ) -> list[_Msg]:
            self.assertEqual(min_id, 50)
            if call_idx["n"] == 0:
                self.assertEqual(offset_id, 0)
                call_idx["n"] += 1
                return pages[0]
            if call_idx["n"] == 1:
                self.assertEqual(offset_id, 299)
                call_idx["n"] += 1
                return pages[1]
            return []

        collected: list[list[_Msg]] = []
        with (
            patch("src.topic_messages.MAX_PAGE_SIZE", 2),
            patch(
                "src.topic_messages._invoke_history_page", side_effect=fake_history
            ),
        ):
            async for page in iter_main_chat_message_pages(
                client, -100, min_id=50, start_offset_id=0
            ):
                collected.append(page)

        self.assertEqual(len(collected), 2)
        self.assertEqual([m.id for m in collected[0]], [300, 299])
        self.assertEqual([m.id for m in collected[1]], [298])

    async def test_stops_on_empty_page(self) -> None:
        client = AsyncMock()
        client.resolve_peer.return_value = MagicMock()

        async def empty(*_args, **_kwargs):
            return []

        collected: list[list[_Msg]] = []
        with patch("src.topic_messages._invoke_history_page", side_effect=empty):
            async for page in iter_main_chat_message_pages(client, -100):
                collected.append(page)

        self.assertEqual(collected, [])

    async def test_stops_on_repeated_offset(self) -> None:
        client = AsyncMock()
        client.resolve_peer.return_value = MagicMock()
        stuck = [_Msg(5)]

        async def stuck_page(*_args, **_kwargs):
            return stuck

        collected: list[list[_Msg]] = []
        with patch(
            "src.topic_messages._invoke_history_page", side_effect=stuck_page
        ):
            async for page in iter_main_chat_message_pages(client, -100):
                collected.append(page)

        self.assertEqual(len(collected), 1)

    async def test_includes_service_messages(self) -> None:
        client = AsyncMock()
        client.resolve_peer.return_value = MagicMock()
        svc = _Msg(1, text=None, service=MagicMock())

        async def one_page(*_args, **_kwargs):
            return [svc]

        with patch("src.topic_messages._invoke_history_page", side_effect=one_page):
            async for page in iter_main_chat_message_pages(client, -100):
                record = message_to_record(
                    page[0],
                    chat_id=-100,
                    topic_id=MAIN_STREAM_TOPIC_ID,
                    pin_topic_id=MAIN_STREAM_TOPIC_ID,
                )
                self.assertEqual(record.topic_id, 0)
                self.assertIsNotNone(record.content_kind)


class SyncGroupRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_forum_single_sync(self) -> None:
        from src.sync_group import sync_group_forum_topics

        client = AsyncMock()
        fake_result = MagicMock(inserted=3, fetched=3)

        with (
            patch(
                "src.sync_group.resolve_supergroup_sync_mode",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "src.sync_group.sync_topic_messages",
                new=AsyncMock(return_value=fake_result),
            ) as sync_mock,
            patch("src.sync_group.fetch_forum_topics", new=AsyncMock()) as topics_mock,
        ):
            report = await sync_group_forum_topics(client, -100, months=6)

        topics_mock.assert_not_called()
        sync_mock.assert_awaited_once()
        kwargs = sync_mock.await_args.kwargs
        self.assertEqual(sync_mock.await_args.args[2], MAIN_STREAM_TOPIC_ID)
        self.assertEqual(kwargs.get("stream"), "main")
        self.assertEqual(len(report.topics), 1)
        self.assertEqual(report.topics[0].title, MAIN_STREAM_TOPIC_TITLE)

    async def test_forum_uses_topic_list(self) -> None:
        from src.sync_group import sync_group_forum_topics
        from src.topics import TopicRow

        client = AsyncMock()
        rows = [
            TopicRow(
                topic_id=1,
                title="General",
                closed="no",
                pinned="no",
                status="active",
            ),
            TopicRow(
                topic_id=101,
                title="News",
                closed="no",
                pinned="no",
                status="active",
            ),
        ]
        fake_result = MagicMock(inserted=1, fetched=1)

        with (
            patch(
                "src.sync_group.resolve_supergroup_sync_mode",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.sync_group.fetch_forum_topics", new=AsyncMock(return_value=rows)
            ),
            patch(
                "src.sync_group.sync_topic_messages",
                new=AsyncMock(return_value=fake_result),
            ) as sync_mock,
        ):
            report = await sync_group_forum_topics(
                client, -100, months=6, topic_delay_seconds=0
            )

        self.assertEqual(sync_mock.await_count, 2)
        self.assertEqual(len(report.topics), 2)
        for call in sync_mock.await_args_list:
            self.assertNotIn("stream", call.kwargs)
            self.assertNotEqual(call.args[2], MAIN_STREAM_TOPIC_ID)


class MainStreamSyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_stream_sync_stores_topic_zero(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock

        from src.sync import sync_topic_messages
        from src.topic_attribution import MAIN_STREAM_TOPIC_ID

        client = AsyncMock()
        client.get_chat.return_value = MagicMock(title="Example Chat")

        messages = [_Msg(10, text="a"), _Msg(9, text="b")]

        async def pages(*_args, **_kwargs):
            yield messages

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            with patch(
                "src.sync.iter_main_chat_message_pages", return_value=pages()
            ):
                result = await sync_topic_messages(
                    client,
                    -100,
                    MAIN_STREAM_TOPIC_ID,
                    window_months=12,
                    stream="main",
                    db_path=db_path,
                )

            from src.db import connect

            conn = connect(db_path)
            rows = conn.execute(
                "SELECT message_id, topic_id FROM messages ORDER BY message_id"
            ).fetchall()
            cp = conn.execute(
                "SELECT topic_id, window_months FROM sync_checkpoints"
            ).fetchone()
            conn.close()

        self.assertEqual(result.inserted, 2)
        self.assertEqual([r["topic_id"] for r in rows], [0, 0])
        self.assertEqual(cp["topic_id"], 0)


class SyncAccessHintTests(unittest.TestCase):
    def test_user_not_participant_hint(self) -> None:
        from hydrogram.errors import RPCError

        from src.topics import sync_access_hint

        class Fake(RPCError):
            CODE = 400
            NAME = "USER_NOT_PARTICIPANT"

            def __init__(self) -> None:
                super().__init__(self.CODE, self.NAME, "n/a")

        hint = sync_access_hint(Fake())
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertIn("not a member", hint.lower())


if __name__ == "__main__":
    unittest.main()
