from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DATA_DIR
from src.message_links import telegram_message_link
from src.evidence_retrieval import (
    DEFAULT_MAX_RELATED,
    DEFAULT_MAX_TOTAL,
    ROLE_DIRECT,
    ROLE_RELATED,
    ROLE_REPLY_PARENT,
    collect_unexamined_urls,
    count_topics_in_chat,
    fetch_related_context,
    resolve_reply_parents,
)
from src.thread_context import fetch_message_thread

EVIDENCE_SCHEMA_VERSION = 3
MAX_MESSAGE_TEXT_CHARS = 50_000
EVIDENCE_ROOT = DATA_DIR / "evidence"

DEFAULT_ANALYSIS_INSTRUCTIONS = """You are analyzing archived Telegram forum messages provided as evidence.

Rules:
- Base your answer only on the message text and metadata in this evidence package.
- Cite message IDs, authors, and dates when making claims.
- If the archive may be incomplete (missing parents, six-month window, etc.), say so explicitly.
- Do not invent messages, links, or facts not present in the evidence.
"""

DEFAULT_USER_PROMPT_TEMPLATE = (
    "Question:\n{question}\n\n"
    "Use the Telegram evidence messages below to answer the question."
)


@dataclass(frozen=True)
class EvidenceMessage:
    message_id: int
    topic_id: int | None
    topic_title: str | None
    message_date_utc: str
    sender_display_name: str | None
    text: str
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    message_link: str | None
    role: str
    text_truncated: bool = False
    text_length_chars: int = 0
    relevance_matched_terms: tuple[str, ...] = ()


@dataclass
class EvidencePackage:
    run_id: str
    created_at_utc: str
    question: str
    chat_id: int
    analysis_instructions: str
    retrieval: dict[str, Any]
    archive_coverage: dict[str, Any]
    messages: list[EvidenceMessage]
    prompt: dict[str, str]
    evidence_hash: str = ""

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at_utc": self.created_at_utc,
            "question": self.question,
            "chat_id": self.chat_id,
            "analysis_instructions": self.analysis_instructions,
            "retrieval": self.retrieval,
            "archive_coverage": self.archive_coverage,
            "messages": [asdict(m) for m in self.messages],
            "prompt": self.prompt,
        }


def compute_evidence_hash(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "evidence_hash"}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _archive_coverage(conn: sqlite3.Connection, chat_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT MIN(window_cutoff_utc) AS oldest_cutoff,
               MAX(window_months) AS window_months
        FROM sync_checkpoints
        WHERE chat_id = ?
        """,
        (chat_id,),
    ).fetchone()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()["n"]
    note = (
        "Local SQLite archive only; not live Telegram. "
        "Messages outside the synced time window or missing reply parents "
        "are not included unless explicitly noted as missing."
    )
    return {
        "message_count_in_archive": int(total),
        "window_months": row["window_months"] if row else None,
        "oldest_window_cutoff_utc": row["oldest_cutoff"] if row else None,
        "limitations": note,
    }


def _load_topic_titles(
    conn: sqlite3.Connection, chat_id: int, topic_ids: set[int | None]
) -> dict[int | None, str | None]:
    titles: dict[int | None, str | None] = {None: None}
    for tid in topic_ids:
        if tid is None:
            continue
        row = conn.execute(
            "SELECT title FROM topics WHERE chat_id = ? AND topic_id = ?",
            (chat_id, tid),
        ).fetchone()
        titles[tid] = row["title"] if row else None
    return titles


def _apply_text_limit(body: str) -> tuple[str, bool, int]:
    original_len = len(body)
    if original_len <= MAX_MESSAGE_TEXT_CHARS:
        return body, False, original_len
    truncated = body[: MAX_MESSAGE_TEXT_CHARS - 1] + "…"
    return truncated, True, original_len


def _message_from_row(
    row: sqlite3.Row,
    chat_id: int,
    *,
    topic_titles: dict[int | None, str | None],
    role: str,
    matched_terms: tuple[str, ...] = (),
) -> EvidenceMessage:
    tid = row["topic_id"]
    raw_body = row["text"] or ""
    body, truncated, original_len = _apply_text_limit(raw_body)
    mid = int(row["message_id"])
    return EvidenceMessage(
        message_id=mid,
        topic_id=tid,
        topic_title=topic_titles.get(tid),
        message_date_utc=row["message_date_utc"],
        sender_display_name=row["sender_display_name"],
        text=body,
        reply_to_message_id=row["reply_to_message_id"],
        reply_to_top_message_id=row["reply_to_top_message_id"],
        message_link=telegram_message_link(
            chat_id, mid, stored_link=row["message_link"]
        ),
        role=role,
        text_truncated=truncated,
        text_length_chars=original_len,
        relevance_matched_terms=matched_terms,
    )


def _load_message_row(
    conn: sqlite3.Connection, chat_id: int, message_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT message_id, topic_id, message_date_utc, sender_display_name, text,
               reply_to_message_id, reply_to_top_message_id, message_link
        FROM messages
        WHERE chat_id = ? AND message_id = ?
        """,
        (chat_id, message_id),
    ).fetchone()


def gather_evidence(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    question: str,
    search_limit: int = 10,
    context_mode: str = "thread",
    anchor_message_id: int | None = None,
    topic_id: int | None = None,
    days: int | None = None,
    max_context_messages: int = 50,
    max_related: int = DEFAULT_MAX_RELATED,
    max_total_messages: int = DEFAULT_MAX_TOTAL,
    analysis_instructions: str = DEFAULT_ANALYSIS_INSTRUCTIONS,
) -> EvidencePackage:
    from src.search import search_messages

    anchor_id = anchor_message_id
    if anchor_id is None:
        hits = search_messages(
            conn,
            chat_id=chat_id,
            query=question,
            limit=1,
            topic_id=topic_id,
            days=days,
        )
        if hits:
            anchor_id = hits[0].message_id

    direct_by_id: dict[int, EvidenceMessage] = {}
    topic_ids: set[int | None] = set()
    thread_texts: list[str] = []

    if anchor_id is not None and context_mode == "thread":
        thread_rows = fetch_message_thread(
            conn,
            chat_id=chat_id,
            message_id=anchor_id,
            max_messages=max_context_messages,
        )
        for entry in thread_rows:
            if entry.message_id is None:
                continue
            row = _load_message_row(conn, chat_id, entry.message_id)
            if row is None:
                continue
            topic_ids.add(row["topic_id"])
            if row["text"]:
                thread_texts.append(row["text"])
        titles = _load_topic_titles(conn, chat_id, topic_ids)
        for entry in thread_rows:
            if entry.message_id is None:
                continue
            row = _load_message_row(conn, chat_id, entry.message_id)
            if row is None:
                continue
            direct_by_id[entry.message_id] = _message_from_row(
                row, chat_id, topic_titles=titles, role=ROLE_DIRECT
            )
    elif anchor_id is not None:
        row = _load_message_row(conn, chat_id, anchor_id)
        if row is not None:
            topic_ids.add(row["topic_id"])
            if row["text"]:
                thread_texts.append(row["text"])
            titles = _load_topic_titles(conn, chat_id, topic_ids)
            direct_by_id[anchor_id] = _message_from_row(
                row, chat_id, topic_titles=titles, role=ROLE_DIRECT
            )

    min_date: str | None = None
    if days is not None and days > 0:
        from src.search import _min_date_utc_for_days

        min_date = _min_date_utc_for_days(days)

    related, secondary_meta = fetch_related_context(
        conn,
        chat_id=chat_id,
        question=question,
        thread_texts=thread_texts,
        exclude_message_ids=set(direct_by_id.keys()),
        max_related=max_related,
        days_min_date=min_date,
    )

    related_by_id: dict[int, EvidenceMessage] = {}
    for cand in related:
        topic_ids.add(cand.topic_id)
    titles = _load_topic_titles(conn, chat_id, topic_ids)
    for cand in related:
        body, truncated, original_len = _apply_text_limit(cand.text)
        related_by_id[cand.message_id] = EvidenceMessage(
            message_id=cand.message_id,
            topic_id=cand.topic_id,
            topic_title=titles.get(cand.topic_id),
            message_date_utc=cand.message_date_utc,
            sender_display_name=cand.sender_display_name,
            text=body,
            reply_to_message_id=cand.reply_to_message_id,
            reply_to_top_message_id=cand.reply_to_top_message_id,
            message_link=cand.message_link,
            role=ROLE_RELATED,
            text_truncated=truncated,
            text_length_chars=original_len,
            relevance_matched_terms=cand.matched_terms,
        )

    direct_list = sorted(
        direct_by_id.values(), key=lambda m: (m.message_date_utc, m.message_id)
    )
    related_list = [
        related_by_id[c.message_id]
        for c in related
        if c.message_id in related_by_id
    ]

    excluded_due_to_total: list[int] = []
    slots_for_related = max(0, max_total_messages - len(direct_list))
    if len(related_list) > slots_for_related:
        excluded_due_to_total.extend(
            m.message_id for m in related_list[slots_for_related:]
        )
        related_list = related_list[:slots_for_related]

    existing_ids = {m.message_id for m in direct_list} | {
        m.message_id for m in related_list
    }
    related_for_parents = [
        c for c in related if c.message_id in {m.message_id for m in related_list}
    ]
    parent_rows, unresolved_deps, _dup = resolve_reply_parents(
        conn,
        chat_id=chat_id,
        related=related_for_parents,
        existing_ids=set(existing_ids),
    )

    parent_messages: list[EvidenceMessage] = []
    for parent in parent_rows:
        if parent.message_id in existing_ids:
            continue
        topic_ids.add(parent.topic_id)
        titles = _load_topic_titles(conn, chat_id, topic_ids)
        row = _load_message_row(conn, chat_id, parent.message_id)
        if row is None:
            continue
        parent_messages.append(
            _message_from_row(
                row,
                chat_id,
                topic_titles=titles,
                role=ROLE_REPLY_PARENT,
                matched_terms=(f"child:{parent.child_message_id}",),
            )
        )
        existing_ids.add(parent.message_id)

    parent_messages.sort(key=lambda m: (m.message_date_utc, m.message_id))
    slots_for_parents = max(
        0, max_total_messages - len(direct_list) - len(related_list)
    )
    excluded_parent_ids: list[int] = []
    if len(parent_messages) > slots_for_parents:
        excluded_parent_ids = [
            m.message_id for m in parent_messages[slots_for_parents:]
        ]
        excluded_due_to_total.extend(excluded_parent_ids)
        parent_messages = parent_messages[:slots_for_parents]

    messages = direct_list + related_list + parent_messages
    topics_searched = count_topics_in_chat(conn, chat_id)

    url_sources = [(m.message_id, m.text) for m in messages]
    unexamined_urls = collect_unexamined_urls(url_sources)

    retrieval = {
        "query": question,
        "context_mode": context_mode,
        "anchor_message_id": anchor_id,
        "topic_id_filter": topic_id,
        "days_filter": days,
        "max_context_messages": max_context_messages,
        "max_related_messages": max_related,
        "max_total_messages": max_total_messages,
        "search_limit_legacy": search_limit,
        "direct_thread_ids": [m.message_id for m in direct_list],
        "related_context_ids": [m.message_id for m in related_list],
        "reply_parent_context_ids": [m.message_id for m in parent_messages],
        "unresolved_reply_dependencies": unresolved_deps,
        "excluded_reply_parent_ids": excluded_parent_ids,
        "excluded_due_to_total_limit": excluded_due_to_total,
        "unexamined_external_urls": unexamined_urls,
        "secondary_search": secondary_meta,
        "topics_in_archive": topics_searched,
    }

    evidence_coverage = {
        "primary_source": "reply_thread",
        "secondary_source": "fts5_or_group_search",
        "reply_parent_source": "local_sqlite_reply_to_message_id",
        "topics_searched": topics_searched,
        "direct_thread_count": len(direct_list),
        "related_context_count": len(related_list),
        "reply_parent_context_count": len(parent_messages),
        "unresolved_dependency_count": len(unresolved_deps),
        "total_selected": len(messages),
        "search_terms_used": secondary_meta.get("expanded_terms", []),
        "fts_queries_used": secondary_meta.get("fts_queries", []),
        "candidates_considered": secondary_meta.get("candidate_count", 0),
        "unknowns": (
            "This package only includes messages from the local SQLite archive. "
            "Messages not indexed, outside the synced window, or not matching "
            "relevance thresholds are omitted. Omission does not prove an event "
            "never occurred on Telegram."
        ),
    }

    run_id = uuid.uuid4().hex[:12]
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    archive = _archive_coverage(conn, chat_id)

    user_prompt = DEFAULT_USER_PROMPT_TEMPLATE.format(question=question)
    prompt = {
        "system": analysis_instructions,
        "user": user_prompt,
    }

    package = EvidencePackage(
        run_id=run_id,
        created_at_utc=created,
        question=question,
        chat_id=chat_id,
        analysis_instructions=analysis_instructions,
        retrieval=retrieval,
        archive_coverage={**archive, "evidence_coverage": evidence_coverage},
        messages=messages,
        prompt=prompt,
    )
    canonical = package.canonical_dict()
    package.evidence_hash = compute_evidence_hash(canonical)
    return package


def render_evidence_markdown(package: EvidencePackage) -> str:
    lines: list[str] = [
        "# Telegram Intelligence — Evidence Package",
        "",
        f"**Run ID:** `{package.run_id}`  ",
        f"**Evidence SHA-256:** `{package.evidence_hash}`  ",
        f"**Created (UTC):** {package.created_at_utc}  ",
        f"**Chat ID:** {package.chat_id}",
        "",
        "## Question",
        "",
        package.question.strip(),
        "",
        "## Analysis instructions (use with ChatGPT or any LLM)",
        "",
        "Copy everything from this section through **End of evidence messages**, "
        "paste into ChatGPT (or upload this file), and ask the model to follow "
        "these instructions when answering the question above.",
        "",
        package.analysis_instructions.strip(),
        "",
        "## Archive coverage limitations",
        "",
        f"- Messages in local archive: {package.archive_coverage.get('message_count_in_archive')}",
        f"- Sync window (months): {package.archive_coverage.get('window_months')}",
        f"- Oldest window cutoff (UTC): {package.archive_coverage.get('oldest_window_cutoff_utc')}",
        f"- {package.archive_coverage.get('limitations')}",
        "",
        "## Evidence coverage",
        "",
    ]
    cov = package.archive_coverage.get("evidence_coverage") or {}
    if cov:
        lines.extend(
            [
                f"- **Primary (direct thread):** {cov.get('direct_thread_count', 0)} message(s)",
                f"- **Related context (cross-topic FTS):** {cov.get('related_context_count', 0)} message(s)",
                f"- **Reply parent context:** {cov.get('reply_parent_context_count', 0)} message(s)",
                f"- **Unresolved reply dependencies:** {cov.get('unresolved_dependency_count', 0)}",
                f"- **Topics in archive searched:** {cov.get('topics_searched', '—')}",
                f"- **FTS candidates considered:** {cov.get('candidates_considered', '—')}",
                f"- **Search terms:** {', '.join(cov.get('search_terms_used') or []) or '—'}",
                "",
                str(cov.get("unknowns", "")),
                "",
            ]
        )
    lines.extend(
        [
            "## Retrieval parameters",
            "",
            "```json",
            json.dumps(package.retrieval, indent=2, ensure_ascii=False),
            "```",
            "",
            "## Evidence messages (full text)",
            "",
        ]
    )

    unexamined = package.retrieval.get("unexamined_external_urls") or []
    if unexamined:
        lines.extend(["### Unexamined external URLs", ""])
        for row in unexamined:
            lines.append(
                f"- {row['url']} (from message {row['source_message_id']}; content not fetched)"
            )
        lines.append("")

    unresolved = package.retrieval.get("unresolved_reply_dependencies") or []
    if unresolved:
        lines.extend(["### Unresolved reply dependencies", ""])
        for row in unresolved:
            lines.append(
                f"- Child {row['child_message_id']} → parent {row['parent_message_id']}: "
                f"{row['reason']}"
            )
        lines.append("")

    role_sections = (
        (ROLE_DIRECT, "Direct thread evidence"),
        (ROLE_RELATED, "Related context evidence"),
        (ROLE_REPLY_PARENT, "Reply parent context"),
    )
    by_role = {role: [] for role, _ in role_sections}
    for msg in package.messages:
        by_role.setdefault(msg.role, []).append(msg)

    for role, heading in role_sections:
        section = by_role.get(role) or []
        if not section:
            continue
        lines.extend([f"### {heading}", ""])
        for msg in section:
            topic = msg.topic_title or (
                f"topic_id={msg.topic_id}" if msg.topic_id else "—"
            )
            lines.extend(
                [
                    f"#### Message {msg.message_id} (`{msg.role}`)",
                    "",
                    f"- **Topic:** {topic}",
                    f"- **Date (UTC):** {msg.message_date_utc}",
                    f"- **Author:** {msg.sender_display_name or '—'}",
                    f"- **Reply to message ID:** {msg.reply_to_message_id or '—'}",
                    f"- **Reply to top message ID:** {msg.reply_to_top_message_id or '—'}",
                    f"- **Link:** {msg.message_link or '—'}",
                ]
            )
            if msg.text_truncated:
                lines.append(
                    f"- **Text note:** truncated for export limit "
                    f"({msg.text_length_chars} chars in archive)"
                )
            if msg.relevance_matched_terms:
                lines.append(
                    f"- **Matched terms:** {', '.join(msg.relevance_matched_terms)}"
                )
            lines.extend(
                [
                    "",
                    msg.text if msg.text else "(no text)",
                    "",
                ]
            )

    lines.extend(
        [
            "---",
            "**End of evidence messages**",
            "",
            "## Suggested ChatGPT prompt",
            "",
            "After uploading this file, you can send:",
            "",
            "```",
            "Please answer the Question in this evidence package using only the "
            "Analysis instructions and Evidence messages sections. Cite message IDs.",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def request_json_dict(package: EvidencePackage) -> dict[str, Any]:
    data = package.canonical_dict()
    data["evidence_hash"] = package.evidence_hash
    return data


def _restrictive_permissions(path: Path) -> None:
    try:
        if path.is_dir():
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass(frozen=True)
class EvidenceExportPaths:
    run_id: str
    directory: Path
    evidence_md: Path
    request_json: Path


def write_evidence_package(package: EvidencePackage) -> EvidenceExportPaths:
    root = EVIDENCE_ROOT / package.run_id
    root.mkdir(parents=True, exist_ok=True)
    _restrictive_permissions(root)

    md_path = root / "evidence.md"
    json_path = root / "request.json"

    md_path.write_text(render_evidence_markdown(package), encoding="utf-8")
    json_path.write_text(
        json.dumps(request_json_dict(package), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _restrictive_permissions(md_path)
    _restrictive_permissions(json_path)

    return EvidenceExportPaths(
        run_id=package.run_id,
        directory=root,
        evidence_md=md_path,
        request_json=json_path,
    )


def load_frozen_package(run_id: str) -> EvidencePackage:
    json_path = EVIDENCE_ROOT / run_id / "request.json"
    if not json_path.is_file():
        raise FileNotFoundError(f"No frozen package at {json_path}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    stored_hash = data.get("evidence_hash", "")
    computed = compute_evidence_hash(data)
    if stored_hash != computed:
        raise ValueError(
            f"Evidence hash mismatch for run {run_id}: "
            f"stored={stored_hash} computed={computed}"
        )
    messages = []
    for m in data["messages"]:
        text = m.get("text") or ""
        messages.append(
            EvidenceMessage(
                message_id=int(m["message_id"]),
                topic_id=m.get("topic_id"),
                topic_title=m.get("topic_title"),
                message_date_utc=m["message_date_utc"],
                sender_display_name=m.get("sender_display_name"),
                text=text,
                reply_to_message_id=m.get("reply_to_message_id"),
                reply_to_top_message_id=m.get("reply_to_top_message_id"),
                message_link=m.get("message_link"),
                role=m["role"],
                text_truncated=bool(m.get("text_truncated", False)),
                text_length_chars=int(m.get("text_length_chars") or len(text)),
                relevance_matched_terms=tuple(
                    m.get("relevance_matched_terms") or ()
                ),
            )
        )
    return EvidencePackage(
        run_id=data["run_id"],
        created_at_utc=data["created_at_utc"],
        question=data["question"],
        chat_id=int(data["chat_id"]),
        analysis_instructions=data["analysis_instructions"],
        retrieval=data["retrieval"],
        archive_coverage=data["archive_coverage"],
        messages=messages,
        prompt=data["prompt"],
        evidence_hash=stored_hash,
    )


def build_provider_payload_stub(
    provider: str, package: EvidencePackage
) -> dict[str, Any]:
    """Provider-shaped payload without secrets (for reproducibility only)."""
    return {
        "provider": provider,
        "evidence_hash": package.evidence_hash,
        "run_id": package.run_id,
        "model_placeholder": {
            "haiku": "claude-haiku-4-5",
            "gemini_flash": "gemini-2.0-flash",
            "sonnet": "claude-sonnet-4-5",
        }.get(provider, provider),
        "messages": [
            {"role": "system", "content": package.prompt["system"]},
            {
                "role": "user",
                "content": package.prompt["user"]
                + "\n\n---\n\n"
                + _evidence_block_for_llm(package),
            },
        ],
    }


def _evidence_block_for_llm(package: EvidencePackage) -> str:
    parts: list[str] = []
    for msg in package.messages:
        parts.append(
            f"[{msg.message_id}] {msg.message_date_utc} "
            f"{msg.sender_display_name or 'unknown'} ({msg.role}):\n{msg.text}"
        )
    return "\n\n".join(parts)


def write_provider_payload(
    package: EvidencePackage, provider: str, directory: Path
) -> Path:
    path = directory / f"payload_{provider}.json"
    path.write_text(
        json.dumps(build_provider_payload_stub(provider, package), indent=2)
        + "\n",
        encoding="utf-8",
    )
    _restrictive_permissions(path)
    return path
