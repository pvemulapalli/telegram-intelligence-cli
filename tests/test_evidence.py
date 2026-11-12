from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from src.content_kind import CONTENT_TEXT
from src.db import MessageRecord, connect, init_schema, upsert_group, upsert_message, upsert_topic
from src.evidence import (
    compute_evidence_hash,
    gather_evidence,
    load_frozen_package,
    render_evidence_markdown,
    request_json_dict,
    write_evidence_package,
)
from src.evidence_retrieval import ROLE_DIRECT
from src.search_index import sync_message_fts


class EvidenceExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        init_schema(self.conn)
        upsert_group(self.conn, -100, "G")
        upsert_topic(self.conn, -100, 1, "General")
        upsert_message(
            self.conn,
            MessageRecord(
                chat_id=-100,
                message_id=40443,
                topic_id=1,
                sender_id=1,
                sender_display_name="Alice",
                message_date_utc="2026-10-12T11:35:06+00:00",
                text="Are legacy devices still supported?",
                reply_to_message_id=None,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind=CONTENT_TEXT,
            ),
        )
        upsert_message(
            self.conn,
            MessageRecord(
                chat_id=-100,
                message_id=40446,
                topic_id=1,
                sender_id=3,
                sender_display_name="Bob",
                message_date_utc="2026-10-12T14:55:08+00:00",
                text="Yeah that firmware rollout feels risky",
                reply_to_message_id=40444,
                reply_to_top_message_id=40443,
                message_link=None,
                content_kind=CONTENT_TEXT,
            ),
        )
        upsert_message(
            self.conn,
            MessageRecord(
                chat_id=-100,
                message_id=40444,
                topic_id=1,
                sender_id=2,
                sender_display_name="Carol",
                message_date_utc="2026-10-12T12:03:07+00:00",
                text="The issue is not whether firmware is old or new. " + ("x" * 200),
                reply_to_message_id=40443,
                reply_to_top_message_id=None,
                message_link=None,
                content_kind=CONTENT_TEXT,
            ),
        )
        sync_message_fts(
            self.conn,
            chat_id=-100,
            message_id=40443,
            text="Are legacy devices still supported?",
            content_kind=CONTENT_TEXT,
        )
        sync_message_fts(
            self.conn,
            chat_id=-100,
            message_id=40444,
            text="The issue is not whether firmware is old or new. " + ("x" * 200),
            content_kind=CONTENT_TEXT,
        )
        sync_message_fts(
            self.conn,
            chat_id=-100,
            message_id=40446,
            text="Yeah that firmware rollout feels risky",
            content_kind=CONTENT_TEXT,
        )
        self.conn.commit()

    def test_direct_thread_role(self) -> None:
        package = gather_evidence(
            self.conn,
            chat_id=-100,
            question="firmware safe",
            anchor_message_id=40444,
        )
        direct = [m for m in package.messages if m.role == ROLE_DIRECT]
        self.assertGreaterEqual(len(direct), 2)
        self.assertIn(40443, {m.message_id for m in direct})
        self.assertIn(40444, {m.message_id for m in direct})

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_full_text_in_export_not_truncated(self) -> None:
        package = gather_evidence(
            self.conn,
            chat_id=-100,
            question="firmware safe",
            anchor_message_id=40444,
            search_limit=5,
        )
        long = [m for m in package.messages if m.message_id == 40444][0]
        self.assertIn("x" * 200, long.text)
        md = render_evidence_markdown(package)
        self.assertIn("x" * 200, md)

    def test_hash_stable_and_in_request(self) -> None:
        package = gather_evidence(
            self.conn,
            chat_id=-100,
            question="firmware",
            anchor_message_id=40444,
        )
        data = request_json_dict(package)
        self.assertEqual(data["evidence_hash"], package.evidence_hash)
        again = compute_evidence_hash(data)
        self.assertEqual(again, package.evidence_hash)

    def test_no_credentials_in_exports(self) -> None:
        package = gather_evidence(
            self.conn,
            chat_id=-100,
            question="firmware",
            anchor_message_id=40444,
        )
        blob = json.dumps(request_json_dict(package)) + render_evidence_markdown(package)
        for pattern in (
            r"api[_-]?key",
            r"TELEGRAM_API",
            r"Authorization",
            r"Bearer ",
            r"session",
        ):
            self.assertIsNone(
                re.search(pattern, blob, re.I),
                msg=f"unexpected credential pattern {pattern}",
            )

    def test_write_and_reload_frozen(self) -> None:
        import src.evidence as ev

        with tempfile.TemporaryDirectory() as out:
            original = ev.EVIDENCE_ROOT
            ev.EVIDENCE_ROOT = Path(out)
            try:
                package = gather_evidence(
                    self.conn,
                    chat_id=-100,
                    question="firmware",
                    anchor_message_id=40444,
                )
                paths = write_evidence_package(package)
                loaded = load_frozen_package(paths.run_id)
                self.assertEqual(loaded.evidence_hash, package.evidence_hash)
                self.assertEqual(len(loaded.messages), len(package.messages))
            finally:
                ev.EVIDENCE_ROOT = original

    def test_compare_reuses_identical_hash(self) -> None:
        import src.ask as ask_mod
        import src.evidence as ev

        with tempfile.TemporaryDirectory() as out:
            original = ev.EVIDENCE_ROOT
            ev.EVIDENCE_ROOT = Path(out)
            try:
                package = gather_evidence(
                    self.conn,
                    chat_id=-100,
                    question="firmware",
                    anchor_message_id=40444,
                )
                paths = write_evidence_package(package)
                log = ask_mod.run_compare_on_frozen(paths.run_id)
                log_text = log.read_text()
                self.assertGreaterEqual(log_text.count(package.evidence_hash), 3)
            finally:
                ev.EVIDENCE_ROOT = original


if __name__ == "__main__":
    unittest.main()
