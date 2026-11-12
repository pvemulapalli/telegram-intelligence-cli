from __future__ import annotations

import itertools
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from src.message_links import telegram_message_link
from src.search_index import prepare_fts_query

ROLE_DIRECT = "direct_thread"
ROLE_RELATED = "related_context"
ROLE_REPLY_PARENT = "reply_parent_context"

DEFAULT_REPLY_PARENT_DEPTH = 1

_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", re.IGNORECASE)

_GENERIC_SECURITY_TERMS = frozenset(
    {
        "security",
        "passkey",
        "passkeys",
        "phone",
        "device",
        "devices",
        "password",
        "authentication",
        "blackberry",
        "apple",
        "military",
        "smartphone",
        "trust",
        "audit",
    }
)

DEFAULT_MAX_RELATED = 8
DEFAULT_MAX_TOTAL = 30
DEFAULT_SECONDARY_CANDIDATE_LIMIT = 50

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "about",
        "what",
        "how",
        "when",
        "where",
        "who",
        "why",
        "which",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "if",
        "or",
        "and",
        "but",
        "not",
        "no",
        "yes",
        "any",
        "all",
        "some",
        "there",
        "their",
        "they",
        "them",
        "you",
        "your",
        "we",
        "our",
        "i",
        "me",
        "my",
        "old",
        "new",
        "safe",
        "get",
        "got",
        "use",
        "using",
    }
)

_GENERIC_TERMS = frozenset({"safe", "issue", "question", "help", "thanks"})

_DOMAIN_KEYWORDS = frozenset(
    {
        "firmware",
        "vendor",
        "vendors",
        "reseller",
        "resellers",
        "tamper",
        "tampered",
        "tampering",
        "hardware",
        "provider",
        "providers",
        "supply",
        "chain",
        "compromised",
        "rogue",
        "device",
        "devices",
        "security",
        "audit",
        "credentials",
    }
)

_MAX_EXPANDED_TERMS = 14


@dataclass(frozen=True)
class RelatedCandidate:
    message_id: int
    topic_id: int | None
    message_date_utc: str
    sender_display_name: str | None
    text: str
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    message_link: str | None
    bm25_rank: float
    relevance_score: float
    matched_terms: tuple[str, ...]


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9']+", text.lower())
    out: list[str] = []
    for word in words:
        cleaned = word.strip("'")
        if len(cleaned) < 3 or cleaned in _STOPWORDS:
            continue
        out.append(cleaned)
    return out


def extract_phrases(question: str) -> list[str]:
    lowered = question.lower()
    words = re.findall(r"[a-z0-9']+", lowered)
    phrases: list[str] = []
    for size in (2, 3):
        for i in range(len(words) - size + 1):
            chunk = words[i : i + size]
            if any(w in _STOPWORDS for w in chunk):
                continue
            phrase = " ".join(chunk)
            if len(phrase) >= 5:
                phrases.append(phrase)
    return sorted(set(phrases))


def expand_search_terms(question: str, thread_texts: list[str]) -> dict[str, list[str]]:
    q_terms = tokenize(question)
    thread_terms = tokenize(" ".join(thread_texts))

    terms: set[str] = set(q_terms)
    for t in thread_terms:
        if t in _DOMAIN_KEYWORDS:
            terms.add(t)

    lowered_q = question.lower()
    combined_thread = " ".join(thread_texts).lower()
    for keyword in _DOMAIN_KEYWORDS:
        if keyword in lowered_q or keyword in combined_thread:
            terms.add(keyword)

    terms -= _GENERIC_TERMS
    fts_terms = sorted(terms)[:_MAX_EXPANDED_TERMS]

    phrases = extract_phrases(question)
    for body in thread_texts:
        for phrase in extract_phrases(body):
            if any(k in phrase for k in _DOMAIN_KEYWORDS):
                phrases.append(phrase)
    phrases = sorted(set(phrases))[:6]

    return {
        "terms": fts_terms,
        "phrases": phrases,
    }


def prepare_fts_or_query(terms: list[str]) -> str | None:
    if not terms:
        return None
    parts = []
    for term in terms:
        escaped = term.replace('"', '""')
        parts.append(f'"{escaped}"')
    return " OR ".join(parts)


def prepare_phrase_fts_query(phrases: list[str]) -> str | None:
    if not phrases:
        return None
    parts = []
    for phrase in phrases[:6]:
        escaped = phrase.replace('"', '""')
        parts.append(f'"{escaped}"')
    return " OR ".join(parts)


def subject_terms_for_scoring(terms: list[str], question: str) -> list[str]:
    from_question = [
        t
        for t in tokenize(question)
        if t not in _GENERIC_TERMS and t not in _GENERIC_SECURITY_TERMS
    ]
    merged = list(dict.fromkeys(from_question + terms))
    return merged[:10]


def anchor_terms_from_question(question: str) -> list[str]:
    anchors = [
        t
        for t in tokenize(question)
        if t not in _GENERIC_TERMS and t not in _STOPWORDS and len(t) >= 4
    ]
    lowered = question.lower()
    for keyword in _DOMAIN_KEYWORDS:
        if keyword in lowered:
            anchors.append(keyword)
    return list(dict.fromkeys(anchors))[:6]


def _score_related(
    text: str,
    *,
    terms: list[str],
    phrases: list[str],
    subject_terms: list[str],
    anchor_terms: list[str] | None = None,
) -> tuple[float, tuple[str, ...]]:
    lower = text.lower()
    matched: list[str] = []
    score = 0.0
    for phrase in phrases:
        if phrase in lower:
            score += 8.0
            matched.append(f"phrase:{phrase}")
    term_hits = 0
    for term in terms:
        if term in lower:
            score += 3.0
            term_hits += 1
            matched.append(term)

    pair_terms = [t for t in subject_terms if t not in _GENERIC_SECURITY_TERMS][:8]
    for a, b in itertools.combinations(pair_terms, 2):
        if a in lower and b in lower:
            score += 12.0
            matched.append(f"pair:{a}+{b}")

    subject_hits = sum(1 for t in subject_terms if t in lower)
    if subject_hits >= 2:
        score += 5.0
    domain_hits = sum(1 for t in terms if t in _DOMAIN_KEYWORDS and t in lower)
    if domain_hits >= 1 and subject_hits >= 1:
        score += 3.0

    generic_only = (
        subject_hits == 0
        and any(g in lower for g in _GENERIC_SECURITY_TERMS)
        and domain_hits == 0
    )
    if generic_only:
        score = min(score, 2.5)

    anchors = anchor_terms or []
    has_pair = any(m.startswith("pair:") for m in matched)
    if anchors and not has_pair:
        if not any(a in lower for a in anchors):
            score = min(score, 3.5)

    if term_hits == 0 and not matched:
        score = 0.0
    if term_hits == 1 and subject_hits == 0 and not any(
        m.startswith("pair:") for m in matched
    ):
        score = min(score, 3.0)
    return score, tuple(sorted(set(matched)))


def extract_urls_from_text(text: str) -> list[str]:
    if not text:
        return []
    found = _URL_PATTERN.findall(text)
    return sorted(set(found))


def collect_unexamined_urls(
    messages: list[tuple[int, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message_id, text in messages:
        for url in extract_urls_from_text(text):
            if url in seen:
                continue
            seen.add(url)
            rows.append(
                {
                    "url": url,
                    "source_message_id": message_id,
                    "examined": False,
                    "note": "URL content was not fetched; treat as unverified reference only.",
                }
            )
    return rows


def fts_search_candidates(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    fts_match: str,
    limit: int,
    days_min_date: str | None = None,
) -> list[sqlite3.Row]:
    params: list[object] = [fts_match, chat_id]
    date_clause = ""
    if days_min_date:
        date_clause = "AND m.message_date_utc >= ?"
        params.append(days_min_date)
    params.append(limit)
    sql = f"""
        SELECT
            m.message_id,
            m.topic_id,
            m.message_date_utc,
            m.sender_display_name,
            m.text,
            m.reply_to_message_id,
            m.reply_to_top_message_id,
            m.message_link,
            bm25(messages_fts) AS rank
        FROM messages_fts
        JOIN messages m
          ON m.chat_id = messages_fts.chat_id
         AND m.message_id = messages_fts.message_id
        WHERE messages_fts MATCH ?
          AND m.chat_id = ?
          {date_clause}
        ORDER BY rank ASC
        LIMIT ?
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def fetch_related_context(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    question: str,
    thread_texts: list[str],
    exclude_message_ids: set[int],
    max_related: int,
    candidate_limit: int = DEFAULT_SECONDARY_CANDIDATE_LIMIT,
    days_min_date: str | None = None,
    min_relevance_score: float = 4.0,
) -> tuple[list[RelatedCandidate], dict]:
    expansion = expand_search_terms(question, thread_texts)
    terms = expansion["terms"]
    phrases = expansion["phrases"]

    or_query = prepare_fts_or_query(terms)
    phrase_query = prepare_phrase_fts_query(phrases)

    queries_used: list[str] = []
    rows_by_id: dict[int, sqlite3.Row] = {}

    if or_query:
        queries_used.append(or_query)
        for row in fts_search_candidates(
            conn,
            chat_id=chat_id,
            fts_match=or_query,
            limit=candidate_limit,
            days_min_date=days_min_date,
        ):
            rows_by_id[int(row["message_id"])] = row

    if phrase_query and phrase_query != or_query:
        queries_used.append(phrase_query)
        for row in fts_search_candidates(
            conn,
            chat_id=chat_id,
            fts_match=phrase_query,
            limit=candidate_limit,
            days_min_date=days_min_date,
        ):
            mid = int(row["message_id"])
            if mid not in rows_by_id:
                rows_by_id[mid] = row

    if not rows_by_id and question.strip():
        fallback = prepare_fts_query(question)
        if fallback:
            queries_used.append(fallback)
            for row in fts_search_candidates(
                conn,
                chat_id=chat_id,
                fts_match=fallback,
                limit=candidate_limit,
                days_min_date=days_min_date,
            ):
                rows_by_id[int(row["message_id"])] = row

    subject_terms = subject_terms_for_scoring(terms, question)
    anchor_terms = anchor_terms_from_question(question)

    scored: list[RelatedCandidate] = []
    for mid, row in rows_by_id.items():
        if mid in exclude_message_ids:
            continue
        body = row["text"] or ""
        rel, matched = _score_related(
            body,
            terms=terms,
            phrases=phrases,
            subject_terms=subject_terms,
            anchor_terms=anchor_terms,
        )
        if rel < min_relevance_score:
            continue
        scored.append(
            RelatedCandidate(
                message_id=mid,
                topic_id=row["topic_id"],
                message_date_utc=row["message_date_utc"],
                sender_display_name=row["sender_display_name"],
                text=body,
                reply_to_message_id=row["reply_to_message_id"],
                reply_to_top_message_id=row["reply_to_top_message_id"],
                message_link=telegram_message_link(
                    chat_id, mid, stored_link=row["message_link"]
                ),
                bm25_rank=float(row["rank"]),
                relevance_score=rel,
                matched_terms=matched,
            )
        )

    scored.sort(
        key=lambda c: (-c.relevance_score, c.bm25_rank, -c.message_id),
    )

    selected = scored[:max_related]
    excluded = scored[max_related:]

    meta = {
        "expanded_terms": terms,
        "expanded_phrases": phrases,
        "fts_queries": queries_used,
        "candidate_count": len(rows_by_id),
        "scored_above_threshold": len(scored),
        "selected_related_ids": [c.message_id for c in selected],
        "excluded_related_ids": [c.message_id for c in excluded],
        "excluded_due_to_related_limit": [c.message_id for c in excluded],
        "min_relevance_score": min_relevance_score,
        "subject_terms_for_scoring": subject_terms,
        "anchor_terms_from_question": anchor_terms,
    }
    return selected, meta


@dataclass(frozen=True)
class ReplyParentResult:
    message_id: int
    topic_id: int | None
    message_date_utc: str
    sender_display_name: str | None
    text: str
    reply_to_message_id: int | None
    reply_to_top_message_id: int | None
    message_link: str | None
    child_message_id: int


def resolve_reply_parents(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    related: list[RelatedCandidate],
    existing_ids: set[int],
    max_depth: int = DEFAULT_REPLY_PARENT_DEPTH,
) -> tuple[list[ReplyParentResult], list[dict[str, Any]], list[int]]:
    """
    One-level (default) parent lookup for related-context messages.
    Returns parents to include, unresolved dependencies, parent IDs excluded by dedupe.
    """
    parents: list[ReplyParentResult] = []
    unresolved: list[dict[str, Any]] = []
    skipped_duplicate: list[int] = []

    for cand in related:
        parent_id = cand.reply_to_message_id
        if parent_id is None:
            continue
        if parent_id in existing_ids:
            continue
        if any(p.message_id == parent_id for p in parents):
            skipped_duplicate.append(parent_id)
            continue

        row = conn.execute(
            """
            SELECT message_id, topic_id, message_date_utc, sender_display_name, text,
                   reply_to_message_id, reply_to_top_message_id, message_link
            FROM messages
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, parent_id),
        ).fetchone()

        if row is None:
            unresolved.append(
                {
                    "child_message_id": cand.message_id,
                    "parent_message_id": parent_id,
                    "reason": "missing_from_archive",
                }
            )
            continue

        if row["topic_id"] != cand.topic_id:
            unresolved.append(
                {
                    "child_message_id": cand.message_id,
                    "parent_message_id": parent_id,
                    "reason": "topic_mismatch",
                }
            )
            continue

        mid = int(row["message_id"])
        parents.append(
            ReplyParentResult(
                message_id=mid,
                topic_id=row["topic_id"],
                message_date_utc=row["message_date_utc"],
                sender_display_name=row["sender_display_name"],
                text=row["text"] or "",
                reply_to_message_id=row["reply_to_message_id"],
                reply_to_top_message_id=row["reply_to_top_message_id"],
                message_link=telegram_message_link(
                    chat_id, mid, stored_link=row["message_link"]
                ),
                child_message_id=cand.message_id,
            )
        )
        existing_ids.add(mid)

        if max_depth > 1 and row["reply_to_message_id"]:
            pass

    return parents, unresolved, skipped_duplicate


def count_topics_in_chat(conn: sqlite3.Connection, chat_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT topic_id) AS n FROM messages WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return int(row["n"]) if row else 0
