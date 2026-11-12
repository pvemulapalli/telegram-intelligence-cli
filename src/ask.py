from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.config import DATABASE_PATH
from src.db import connect, init_schema
from src.evidence import (
    EvidenceExportPaths,
    EvidencePackage,
    gather_evidence,
    load_frozen_package,
    write_evidence_package,
)
from src.search_index import ensure_fts_backfill_once

COMPARE_PROVIDERS = ("haiku", "gemini_flash", "sonnet")


class AskError(Exception):
    pass


@dataclass(frozen=True)
class AskResult:
    package: EvidencePackage
    paths: EvidenceExportPaths
    compare_log_path: Path | None


def build_and_export_package(
    *,
    chat_id: int,
    question: str,
    search_limit: int = 10,
    context_mode: str = "thread",
    anchor_message_id: int | None = None,
    topic_id: int | None = None,
    days: int | None = None,
    max_context_messages: int = 50,
    max_related: int = 8,
    max_total_messages: int = 30,
    db_path: Path = DATABASE_PATH,
) -> AskResult:
    conn = connect(db_path)
    try:
        init_schema(conn)
        ensure_fts_backfill_once(conn)
        conn.commit()
        package = gather_evidence(
            conn,
            chat_id=chat_id,
            question=question,
            search_limit=search_limit,
            context_mode=context_mode,
            anchor_message_id=anchor_message_id,
            topic_id=topic_id,
            days=days,
            max_context_messages=max_context_messages,
            max_related=max_related,
            max_total_messages=max_total_messages,
        )
    finally:
        conn.close()

    paths = write_evidence_package(package)
    return AskResult(package=package, paths=paths, compare_log_path=None)


def run_compare_on_frozen(
    run_id: str,
    *,
    write_stubs: bool = True,
) -> Path:
    """
    Reuse frozen request.json for all compare providers (no network; logs hash + stub).
    """
    package = load_frozen_package(run_id)
    from src.evidence import EVIDENCE_ROOT

    directory = EVIDENCE_ROOT / run_id
    log_lines: list[str] = [
        f"evidence_hash={package.evidence_hash}",
        f"run_id={run_id}",
        "",
    ]
    for provider in COMPARE_PROVIDERS:
        log_lines.append(f"provider={provider} evidence_hash={package.evidence_hash}")
        if write_stubs:
            log_lines.append("  status=not_invoked (manual ChatGPT workflow; no API)")
        log_lines.append("")

    log_path = directory / "compare.log"
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return log_path


def print_package_paths(result: AskResult) -> None:
    p = result.paths
    cov = (result.package.archive_coverage or {}).get("evidence_coverage") or {}
    print(f"Evidence package saved (run_id={p.run_id})")
    print(f"  evidence_hash: {result.package.evidence_hash}")
    ret = result.package.retrieval
    print(
        f"  direct_thread: {cov.get('direct_thread_count', 0)}  "
        f"related_context: {cov.get('related_context_count', 0)}  "
        f"reply_parent: {cov.get('reply_parent_context_count', 0)}  "
        f"unresolved_deps: {cov.get('unresolved_dependency_count', 0)}  "
        f"total: {cov.get('total_selected', len(result.package.messages))}  "
        f"topics_searched: {cov.get('topics_searched', '—')}"
    )
    excluded = ret.get("excluded_due_to_total_limit") or []
    if excluded:
        print(f"  excluded_by_cap: {len(excluded)} message id(s)")
    print(f"  {p.evidence_md}")
    print(f"  {p.request_json}")
    print()
    print("Manual analysis: upload evidence.md to your preferred AI assistant.")
    print("Verify hash (optional): python -m src.main ask --compare --run-id", p.run_id)
