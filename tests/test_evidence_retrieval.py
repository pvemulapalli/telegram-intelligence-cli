from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.content_kind import CONTENT_TEXT
from src.db import MessageRecord, connect, init_schema, upsert_group, upsert_message, upsert_topic
from src.evidence import gather_evidence
from src.evidence_retrieval import (
    ROLE_DIRECT,
    ROLE_RELATED,
    ROLE_REPLY_PARENT,
    expand_search_terms,
    fetch_related_context,
    resolve_reply_parents,
    _score_related,
)
from src.search_index import sync_message_fts


class EvidenceRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        init_schema(self.conn)
        upsert_group(self.conn, -100, "G")
        upsert_topic(self.conn, -100, 1, "General")
        upsert_topic(self.conn, -100, 99, "Support")

        def add(mid: int, tid: int, text: str, reply: int | None = None) -> None:
            upsert_message(
                self.conn,
                MessageRecord(
                    chat_id=-100,
                    message_id=mid,
                    topic_id=tid,
                    sender_id=1,
                    sender_display_name="U",
                    message_date_utc="2026-10-01T00:00:00+00:00",
                    text=text,
                    reply_to_message_id=reply,
                    reply_to_top_message_id=None,
                    message_link=None,
                    content_kind=CONTENT_TEXT,
                ),
            )
            sync_message_fts(
                self.conn,
                chat_id=-100,
                message_id=mid,
                text=text,
                content_kind=CONTENT_TEXT,
            )

        add(1, 1, "Are legacy devices still supported?")
        add(2, 1, "The vendor shipped a tampered firmware image.", reply=1)
        add(3, 99, "Supply chain warning for refurbished hardware")
        add(4, 99, "Unrelated note about quarterly planning")
        add(5, 1, "Can we rotate credentials safely?", reply=None)
        add(6, 1, "Child reply about firmware tamper and vendor", reply=5)
        for mid, text in (
            (3, "Supply chain warning for refurbished hardware"),
            (4, "Unrelated note about quarterly planning"),
            (5, "Can we rotate credentials safely?"),
            (6, "Child reply about firmware tamper and vendor"),
        ):
            sync_message_fts(
                self.conn,
                chat_id=-100,
                message_id=mid,
                text=text,
                content_kind=CONTENT_TEXT,
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_term_expansion_includes_firmware(self) -> None:
        exp = expand_search_terms(
            "Are legacy devices still supported?",
            ["The vendor shipped a tampered firmware image."],
        )
        self.assertIn("firmware", exp["terms"])

    def test_related_excludes_direct_thread_ids(self) -> None:
        selected, meta = fetch_related_context(
            self.conn,
            chat_id=-100,
            question="Are legacy devices still supported?",
            thread_texts=[
                "Are legacy devices still supported?",
                "firmware tamper vendor",
            ],
            exclude_message_ids={1, 2},
            max_related=5,
        )
        ids = {c.message_id for c in selected}
        self.assertNotIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertIn(6, ids)
        self.assertNotIn(4, ids)

    def test_gather_labels_and_dedupes(self) -> None:
        package = gather_evidence(
            self.conn,
            chat_id=-100,
            question="Are legacy devices still supported?",
            anchor_message_id=2,
            max_related=5,
            max_total_messages=10,
        )
        roles = {m.message_id: m.role for m in package.messages}
        self.assertEqual(roles[1], ROLE_DIRECT)
        self.assertEqual(roles[2], ROLE_DIRECT)
        self.assertEqual(len(package.messages), len(set(roles)))
        related = [m for m in package.messages if m.role == ROLE_RELATED]
        self.assertGreaterEqual(len(related), 1)
        self.assertIn(6, {m.message_id for m in related})

    def test_total_cap_excludes_related_first(self) -> None:
        package = gather_evidence(
            self.conn,
            chat_id=-100,
            question="firmware",
            anchor_message_id=2,
            max_related=10,
            max_total_messages=2,
        )
        self.assertEqual(len(package.messages), 2)
        self.assertTrue(all(m.role == ROLE_DIRECT for m in package.messages))
        self.assertEqual(package.retrieval["related_context_ids"], [])

    def test_reply_parent_included_for_related_child(self) -> None:
        selected, _ = fetch_related_context(
            self.conn,
            chat_id=-100,
            question="firmware tamper vendor",
            thread_texts=["firmware tamper vendor"],
            exclude_message_ids={1, 2},
            max_related=5,
        )
        child = next((c for c in selected if c.message_id == 6), None)
        assert child is not None
        parents, unresolved, _ = resolve_reply_parents(
            self.conn,
            chat_id=-100,
            related=[child],
            existing_ids={1, 2, 6},
        )
        self.assertEqual([p.message_id for p in parents], [5])
        self.assertEqual(unresolved, [])

    def test_pair_scoring_ranks_incident_above_generic_security(self) -> None:
        incident = "Vendor firmware tampering and supply chain risk"
        generic = "General passkey and phone security discussion"
        terms = expand_search_terms(
            "Are legacy devices still supported?",
            ["Vendor shipped tampered firmware image"],
        )["terms"]
        subject = terms
        anchors = ["firmware", "vendor"]
        s_incident, _ = _score_related(
            incident,
            terms=terms,
            phrases=[],
            subject_terms=subject,
            anchor_terms=anchors,
        )
        s_generic, _ = _score_related(
            generic,
            terms=terms,
            phrases=[],
            subject_terms=subject,
            anchor_terms=anchors,
        )
        self.assertGreater(s_incident, s_generic)

    def test_gather_includes_reply_parent_role(self) -> None:
        package = gather_evidence(
            self.conn,
            chat_id=-100,
            question="firmware tamper",
            anchor_message_id=2,
            max_related=10,
        )
        roles = {m.message_id: m.role for m in package.messages}
        if 6 in roles:
            self.assertIn(5, roles)
            self.assertEqual(roles[5], ROLE_REPLY_PARENT)

    def test_deterministic_message_selection(self) -> None:
        a = gather_evidence(
            self.conn,
            chat_id=-100,
            question="Are legacy devices still supported?",
            anchor_message_id=2,
        )
        b = gather_evidence(
            self.conn,
            chat_id=-100,
            question="Are legacy devices still supported?",
            anchor_message_id=2,
        )
        self.assertEqual(
            [m.message_id for m in a.messages],
            [m.message_id for m in b.messages],
        )


if __name__ == "__main__":
    unittest.main()
