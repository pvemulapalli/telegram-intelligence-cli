from __future__ import annotations

import argparse
import asyncio
import sys

from hydrogram.errors import RPCError

from src.config import ConfigError, load_telegram_config
from src.errors import exit_with_message, format_rpc_error
from src.groups import collect_groups, print_groups
from src.ingest import IngestError, ingest_topic_messages, print_ingest_result
from src.inspect_db import print_inspect
from src.ask import AskError, build_and_export_package, print_package_paths, run_compare_on_frozen
from src.export_topic import (
    TopicExportError,
    export_topic_by_name,
    export_topic_messages,
    export_topics_batch,
    print_topic_export_result,
    print_topic_export_results,
)
from src.evidence_storage import (
    DEFAULT_RETENTION_DAYS,
    execute_evidence_cleanup,
    plan_evidence_cleanup,
    print_cleanup_plan,
    print_storage_usage,
    storage_usage_report,
)
from src.search import SearchQueryError, print_message_context, print_search_results, run_search
from src.sync import SyncError, sync_topic_messages, print_sync_result
from src.diagnose_general import (
    fetch_general_replies_sample,
    fetch_history_sample_for_comparison,
    print_general_diagnostic,
)
from src.sync_group import print_group_sync_report, sync_group_forum_topics
from src.sync_status import print_sync_status
from src.sync_window import DEFAULT_SYNC_GROUP_MONTHS
from src.telegram_app import build_client
from src.topic_messages import TopicMessageError
from src.topics import fetch_forum_topics, forum_error_hint, print_topics, sync_access_hint


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Telegram Intelligence — read-only CLI (groups, topics, ingest)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("groups", help="List accessible basic groups and supergroups.")

    topics = sub.add_parser(
        "topics", help="List forum topics for a forum-enabled supergroup."
    )
    topics.add_argument(
        "--chat-id",
        type=int,
        required=True,
        help="Supergroup chat ID (from the groups command).",
    )
    topics.add_argument(
        "--debug",
        action="store_true",
        help="Show forum topic pagination progress (no message or credential data).",
    )

    ingest = sub.add_parser(
        "ingest", help="Download recent forum-topic messages into SQLite."
    )
    ingest.add_argument("--chat-id", type=int, required=True)
    ingest.add_argument("--topic-id", type=int, required=True)
    ingest.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum messages to fetch for this topic (default: 100).",
    )

    inspect_cmd = sub.add_parser(
        "inspect", help="Preview ingested messages from SQLite (no Telegram API)."
    )
    inspect_cmd.add_argument("--chat-id", type=int, required=True)
    inspect_cmd.add_argument("--topic-id", type=int, required=True)
    inspect_cmd.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of newest messages to show (default: 10).",
    )

    sync_cmd = sub.add_parser(
        "sync", help="Incrementally synchronize forum-topic messages into SQLite."
    )
    sync_cmd.add_argument("--chat-id", type=int, required=True)
    sync_cmd.add_argument("--topic-id", type=int, required=True)
    sync_cmd.add_argument(
        "--overlap",
        type=int,
        default=10,
        help="Message ID overlap for incremental sync (default: 10).",
    )

    sub.add_parser(
        "sync-status",
        help="Show synchronization checkpoints (no Telegram API).",
    )

    sync_group = sub.add_parser(
        "sync-group",
        help=(
            "Synchronize a supergroup: all forum topics, or main chat if not a forum."
        ),
    )
    sync_group.add_argument("--chat-id", type=int, required=True)
    sync_group.add_argument(
        "--months",
        type=int,
        default=None,
        help=(
            f"Calendar months of history to backfill "
            f"(default: {DEFAULT_SYNC_GROUP_MONTHS} when --days is omitted)."
        ),
    )
    sync_group.add_argument(
        "--days",
        type=int,
        default=None,
        help="Calendar days of history to backfill (alternative to --months).",
    )
    sync_group.add_argument(
        "--overlap",
        type=int,
        default=10,
        help="Message ID overlap for incremental sync (default: 10).",
    )
    sync_group.add_argument(
        "--topic-delay",
        type=float,
        default=1.0,
        help="Seconds to wait between topics (default: 1.0).",
    )

    diagnose = sub.add_parser(
        "diagnose-general",
        help="Inspect General topic (ID 1) classification metadata (no message bodies).",
    )
    diagnose.add_argument("--chat-id", type=int, required=True)
    diagnose.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Messages to sample from GetReplies(1) and GetHistory each (default: 30).",
    )

    search_cmd = sub.add_parser(
        "search",
        help="Full-text search archived messages (SQLite FTS5, offline).",
    )
    search_cmd.add_argument("--chat-id", type=int, required=True)
    search_cmd.add_argument("--query", type=str, required=True)
    search_cmd.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum results (default: 20).",
    )
    search_cmd.add_argument(
        "--topic-id",
        type=int,
        default=None,
        help="Restrict to one forum topic.",
    )
    search_cmd.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only messages from the last N days (UTC).",
    )

    context_cmd = sub.add_parser(
        "message-context",
        help="Show nearby messages and reply parent for one archived message (offline).",
    )
    context_cmd.add_argument("--chat-id", type=int, required=True)
    context_cmd.add_argument("--message-id", type=int, required=True)
    context_cmd.add_argument(
        "--before",
        type=int,
        default=3,
        help="Older messages in the same topic (default: 3).",
    )
    context_cmd.add_argument(
        "--after",
        type=int,
        default=3,
        help="Newer messages in the same topic (default: 3).",
    )
    context_cmd.add_argument(
        "--mode",
        choices=("chrono", "thread"),
        default="chrono",
        help="chrono: nearby messages by time; thread: reply graph (default: chrono).",
    )
    context_cmd.add_argument(
        "--max-messages",
        type=int,
        default=50,
        help="Max messages in thread mode (default: 50).",
    )

    ask_cmd = sub.add_parser(
        "ask",
        help="Build LLM evidence package from local archive (export-first; offline).",
    )
    ask_cmd.add_argument("--chat-id", type=int, required=True)
    ask_cmd.add_argument("--query", type=str, required=True, help="Question to answer.")
    ask_cmd.add_argument(
        "--message-id",
        type=int,
        default=None,
        help="Anchor message for thread context (optional).",
    )
    ask_cmd.add_argument("--search-limit", type=int, default=10)
    ask_cmd.add_argument(
        "--context-mode",
        choices=("thread", "single"),
        default="thread",
    )
    ask_cmd.add_argument("--topic-id", type=int, default=None)
    ask_cmd.add_argument("--days", type=int, default=None)
    ask_cmd.add_argument("--max-context-messages", type=int, default=50)
    ask_cmd.add_argument(
        "--max-related",
        type=int,
        default=8,
        help="Max cross-topic related messages (default: 8).",
    )
    ask_cmd.add_argument(
        "--max-total",
        type=int,
        default=30,
        help="Max total evidence messages (default: 30).",
    )
    ask_cmd.add_argument(
        "--package-only",
        action="store_true",
        help="Export evidence only (default unless --invoke-llm is added later).",
    )
    ask_cmd.add_argument(
        "--compare",
        action="store_true",
        help="Run compare providers on frozen evidence (offline stubs without API keys).",
    )
    ask_cmd.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Reuse existing evidence run (with --compare) instead of building new.",
    )
    ask_cmd.add_argument(
        "--invoke-llm",
        action="store_true",
        help="Call external LLM APIs after packaging (requires keys; not default).",
    )

    sub.add_parser(
        "storage-usage",
        help="Report local SQLite and evidence export disk usage (offline).",
    )

    cleanup = sub.add_parser(
        "cleanup-evidence",
        help="Remove old evidence export directories (dry-run by default).",
    )
    cleanup.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Delete exports older than N days (default: {DEFAULT_RETENTION_DAYS}).",
    )
    cleanup.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete eligible directories (default is preview only).",
    )

    export_topic = sub.add_parser(
        "export-topic",
        help="Export latest stored topic messages to Markdown (SQLite only).",
    )
    export_topic.add_argument("--chat-id", type=int, required=True)
    export_topic.add_argument(
        "--topic-id",
        type=int,
        default=None,
        help="Forum topic ID (use with --limit).",
    )
    export_topic.add_argument(
        "--topic",
        type=str,
        default=None,
        help="Forum topic name resolved from SQLite metadata (use with --limit).",
    )
    export_topic.add_argument(
        "--topics",
        type=str,
        default=None,
        help='Batch: "Announcements:56|Questions:after-12345" (count or after-MessageId per topic).',
    )
    export_topic.add_argument(
        "--limit",
        type=int,
        default=60,
        help="Maximum messages for --topic-id or --topic (default: 60).",
    )

    return parser


async def _run_groups() -> None:
    config = load_telegram_config()
    client = build_client(config)
    async with client:
        rows = await collect_groups(client)
    print_groups(rows)


async def _run_topics(chat_id: int, *, debug: bool = False) -> None:
    config = load_telegram_config()
    client = build_client(config)
    async with client:
        rows = await fetch_forum_topics(client, chat_id, debug=debug)
    print_topics(rows, chat_id)


async def _run_diagnose_general(chat_id: int, limit: int) -> None:
    config = load_telegram_config()
    client = build_client(config)
    async with client:
        replies = await fetch_general_replies_sample(
            client, chat_id, limit=limit
        )
        history = await fetch_history_sample_for_comparison(
            client, chat_id, limit=limit
        )
    print_general_diagnostic(
        chat_id=chat_id, get_replies=replies, get_history=history
    )


async def _run_sync_group(
    chat_id: int,
    months: int | None,
    days: int | None,
    overlap: int,
    topic_delay: float,
) -> None:
    config = load_telegram_config()
    client = build_client(config)
    async with client:
        report = await sync_group_forum_topics(
            client,
            chat_id,
            months=months,
            days=days,
            overlap_message_ids=overlap,
            topic_delay_seconds=topic_delay,
        )
    print_group_sync_report(report)


async def _run_sync(chat_id: int, topic_id: int, overlap: int) -> None:
    config = load_telegram_config()
    client = build_client(config)
    async with client:
        result = await sync_topic_messages(
            client, chat_id, topic_id, overlap_message_ids=overlap
        )
    print_sync_result(result)


async def _run_ingest(chat_id: int, topic_id: int, limit: int) -> None:
    config = load_telegram_config()
    client = build_client(config)
    async with client:
        result = await ingest_topic_messages(
            client, chat_id, topic_id, limit=limit
        )
    print_ingest_result(result)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "groups":
            asyncio.run(_run_groups())
        elif args.command == "topics":
            asyncio.run(_run_topics(args.chat_id, debug=args.debug))
        elif args.command == "ingest":
            if args.limit <= 0:
                parser.error("--limit must be a positive integer.")
            asyncio.run(_run_ingest(args.chat_id, args.topic_id, args.limit))
        elif args.command == "inspect":
            if args.limit <= 0:
                parser.error("--limit must be a positive integer.")
            print_inspect(args.chat_id, args.topic_id, limit=args.limit)
        elif args.command == "search":
            if args.limit <= 0:
                parser.error("--limit must be a positive integer.")
            if args.days is not None and args.days <= 0:
                parser.error("--days must be a positive integer.")
            hits = run_search(
                args.chat_id,
                args.query,
                limit=args.limit,
                topic_id=args.topic_id,
                days=args.days,
            )
            print_search_results(hits, query=args.query)
        elif args.command == "ask":
            if not args.query.strip():
                parser.error("--query must not be empty.")
            if args.search_limit <= 0:
                parser.error("--search-limit must be positive.")
            if args.max_related <= 0 or args.max_total <= 0:
                parser.error("--max-related and --max-total must be positive.")
            if args.invoke_llm:
                exit_with_message(
                    "External LLM invocation is not enabled yet. "
                    "Use --package-only (default) to export evidence locally."
                )
            if args.compare and args.run_id:
                log = run_compare_on_frozen(args.run_id)
                print(f"Compare log (evidence frozen): {log}")
            elif args.compare:
                result = build_and_export_package(
                    chat_id=args.chat_id,
                    question=args.query,
                    search_limit=args.search_limit,
                    context_mode=args.context_mode,
                    anchor_message_id=args.message_id,
                    topic_id=args.topic_id,
                    days=args.days,
                    max_context_messages=args.max_context_messages,
                    max_related=args.max_related,
                    max_total_messages=args.max_total,
                )
                print_package_paths(result)
                log = run_compare_on_frozen(result.paths.run_id)
                print(f"Compare log: {log}")
            elif args.run_id:
                parser.error("--run-id requires --compare.")
            else:
                result = build_and_export_package(
                    chat_id=args.chat_id,
                    question=args.query,
                    search_limit=args.search_limit,
                    context_mode=args.context_mode,
                    anchor_message_id=args.message_id,
                    topic_id=args.topic_id,
                    days=args.days,
                    max_context_messages=args.max_context_messages,
                    max_related=args.max_related,
                    max_total_messages=args.max_total,
                )
                print_package_paths(result)
        elif args.command == "storage-usage":
            print_storage_usage(storage_usage_report())
        elif args.command == "cleanup-evidence":
            if args.days <= 0:
                parser.error("--days must be a positive integer.")
            plan = plan_evidence_cleanup(retention_days=args.days)
            print_cleanup_plan(plan, dry_run=not args.confirm)
            if args.confirm:
                deleted = execute_evidence_cleanup(plan, confirm=True)
                print(f"Deleted {len(deleted)} evidence director{'y' if len(deleted) == 1 else 'ies'}.")
        elif args.command == "export-topic":
            modes = sum(
                1
                for flag in (args.topics, args.topic_id is not None, args.topic)
                if flag
            )
            if modes == 0:
                parser.error(
                    "Specify one of --topics, --topic-id, or --topic for export-topic."
                )
            if modes > 1:
                parser.error(
                    "Use only one of --topics, --topic-id, or --topic for export-topic."
                )
            if args.topics:
                results = export_topics_batch(args.chat_id, args.topics)
                print_topic_export_results(results)
            elif args.topic_id is not None:
                if args.limit <= 0:
                    parser.error("--limit must be a positive integer.")
                result = export_topic_messages(
                    args.chat_id,
                    args.topic_id,
                    limit=args.limit,
                )
                print_topic_export_results([result])
            else:
                if args.limit <= 0:
                    parser.error("--limit must be a positive integer.")
                result = export_topic_by_name(
                    args.chat_id,
                    args.topic,
                    limit=args.limit,
                )
                print_topic_export_results([result])
        elif args.command == "message-context":
            if args.before < 0 or args.after < 0:
                parser.error("--before and --after must be zero or positive.")
            if args.max_messages <= 0:
                parser.error("--max-messages must be a positive integer.")
            print_message_context(
                args.chat_id,
                args.message_id,
                mode=args.mode,
                before=args.before,
                after=args.after,
                max_messages=args.max_messages,
            )
        elif args.command == "sync":
            if args.overlap < 0:
                parser.error("--overlap must be zero or a positive integer.")
            asyncio.run(_run_sync(args.chat_id, args.topic_id, args.overlap))
        elif args.command == "sync-status":
            print_sync_status()
        elif args.command == "diagnose-general":
            if args.limit <= 0:
                parser.error("--limit must be a positive integer.")
            asyncio.run(_run_diagnose_general(args.chat_id, args.limit))
        elif args.command == "sync-group":
            if args.months is not None and args.days is not None:
                parser.error("Cannot use both --months and --days.")
            if args.months is not None and args.months <= 0:
                parser.error("--months must be a positive integer.")
            if args.days is not None and args.days <= 0:
                parser.error("--days must be a positive integer.")
            if args.overlap < 0:
                parser.error("--overlap must be zero or a positive integer.")
            if args.topic_delay < 0:
                parser.error("--topic-delay must be zero or a positive number.")
            asyncio.run(
                _run_sync_group(
                    args.chat_id,
                    args.months,
                    args.days,
                    args.overlap,
                    args.topic_delay,
                )
            )
        else:
            parser.error(f"Unknown command: {args.command}")
    except ConfigError as exc:
        exit_with_message(str(exc))
    except (
        IngestError,
        SyncError,
        TopicMessageError,
        SearchQueryError,
        AskError,
        TopicExportError,
    ) as exc:
        exit_with_message(str(exc))
    except RPCError as exc:
        if args.command == "topics":
            hint = forum_error_hint(exc)
        elif args.command in ("sync-group", "sync"):
            hint = sync_access_hint(exc)
        else:
            hint = None
        message = format_rpc_error(exc)
        if hint:
            message = f"{hint}\n{message}"
        exit_with_message(message)
    except KeyboardInterrupt:
        exit_with_message("Interrupted.", code=130)


if __name__ == "__main__":
    main(sys.argv[1:])
