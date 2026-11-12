from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hydrogram import Client

from src.config import DATABASE_PATH
from src.db import (
    DEFAULT_OVERLAP_IDS,
    SyncCheckpoint,
    TopicMessageStats,
    UpsertStats,
    compute_incremental_min_id,
    connect,
    get_sync_checkpoint,
    get_topic_message_stats,
    init_schema,
    save_sync_checkpoint,
    upsert_group,
    upsert_messages,
    upsert_topic,
    utc_now_iso,
)
from src.message_map import message_to_record
from src.sync_window import (
    cutoff_utc_from_months,
    cutoff_utc_iso,
    split_page_at_cutoff,
)
from src.topic_messages import iter_forum_topic_message_pages


@dataclass(frozen=True)
class SyncPlan:
    mode: str
    start_offset_id: int
    min_id: int


@dataclass(frozen=True)
class SyncResult:
    chat_id: int
    topic_id: int
    mode: str
    fetched: int
    inserted: int
    updated: int
    skipped: int
    date_min_utc: str | None
    date_max_utc: str | None
    history_complete: bool
    window_cutoff_utc: str | None
    window_months: int | None
    window_complete: bool
    full_history_complete: bool
    checkpoint_resume_offset_id: int | None
    last_sync_utc: str | None


def window_policy_matches(
    checkpoint: SyncCheckpoint,
    *,
    window_months: int,
    window_cutoff_utc: str | None = None,
) -> bool:
    """
    True when the checkpoint was created for the same calendar-month window length.

    The rolling cutoff timestamp advances daily; completed windows match on ``window_months``
    only so daily ``sync-group`` stays incremental. A different ``window_months`` value
    (for example expanding 6 → 12) does not match and triggers backfill.
    """
    _ = window_cutoff_utc
    return checkpoint.window_months == window_months


def plan_sync(
    stats: TopicMessageStats,
    checkpoint: SyncCheckpoint | None,
    *,
    overlap_message_ids: int,
    window_months: int | None = None,
    window_cutoff_utc: str | None = None,
) -> SyncPlan:
    if window_months is not None and window_cutoff_utc is not None:
        if checkpoint is not None and window_policy_matches(
            checkpoint,
            window_months=window_months,
            window_cutoff_utc=window_cutoff_utc,
        ):
            if checkpoint.window_complete:
                latest = (
                    stats.latest_message_id
                    or checkpoint.latest_stored_message_id
                    or 0
                )
                overlap = checkpoint.overlap_message_ids or overlap_message_ids
                return SyncPlan(
                    mode="incremental",
                    start_offset_id=0,
                    min_id=compute_incremental_min_id(latest, overlap),
                )
            return SyncPlan(
                mode="resume_window",
                start_offset_id=checkpoint.resume_offset_id or 0,
                min_id=0,
            )
        return SyncPlan(mode="window_backfill", start_offset_id=0, min_id=0)

    if checkpoint is not None and not checkpoint.history_complete:
        return SyncPlan(
            mode="resume_initial",
            start_offset_id=checkpoint.resume_offset_id or 0,
            min_id=0,
        )
    if checkpoint is not None and checkpoint.history_complete:
        latest = stats.latest_message_id or checkpoint.latest_stored_message_id or 0
        overlap = checkpoint.overlap_message_ids or overlap_message_ids
        return SyncPlan(
            mode="incremental",
            start_offset_id=0,
            min_id=compute_incremental_min_id(latest, overlap),
        )
    return SyncPlan("initial", 0, 0)


async def _resolve_titles(
    client: Client, chat_id: int, topic_id: int
) -> tuple[str | None, str | None]:
    chat_title: str | None = None
    try:
        chat_title = (await client.get_chat(chat_id)).title
    except Exception:
        pass

    topic_title: str | None = None
    from hydrogram.raw.functions.channels import GetForumTopicsByID
    from hydrogram.raw.types import ForumTopic

    try:
        channel = await client.resolve_peer(chat_id)
        by_id = await client.invoke(
            GetForumTopicsByID(channel=channel, topics=[topic_id])
        )
        for raw_topic in by_id.topics:
            if isinstance(raw_topic, ForumTopic):
                topic_title = raw_topic.title
                break
    except Exception:
        pass
    return chat_title, topic_title


def _checkpoint_from_stats(
    chat_id: int,
    topic_id: int,
    stats: TopicMessageStats,
    *,
    overlap_message_ids: int,
    window_months: int | None = None,
    window_cutoff_utc: str | None = None,
) -> SyncCheckpoint:
    return SyncCheckpoint(
        chat_id=chat_id,
        topic_id=topic_id,
        history_complete=False,
        last_sync_utc=None,
        last_sync_status=None,
        resume_offset_id=0,
        overlap_message_ids=overlap_message_ids,
        latest_stored_message_id=stats.latest_message_id,
        latest_stored_message_date_utc=stats.latest_message_date_utc,
        window_cutoff_utc=window_cutoff_utc,
        window_months=window_months,
        window_complete=False,
        full_history_complete=False,
    )


def _merge_checkpoint_fields(
    base: SyncCheckpoint,
    *,
    history_complete: bool,
    last_sync_utc: str | None,
    last_sync_status: str | None,
    resume_offset_id: int | None,
    overlap_message_ids: int,
    latest_stored_message_id: int | None,
    latest_stored_message_date_utc: str | None,
    window_cutoff_utc: str | None,
    window_months: int | None,
    window_complete: bool,
    full_history_complete: bool,
) -> SyncCheckpoint:
    return SyncCheckpoint(
        chat_id=base.chat_id,
        topic_id=base.topic_id,
        history_complete=history_complete,
        last_sync_utc=last_sync_utc,
        last_sync_status=last_sync_status,
        resume_offset_id=resume_offset_id,
        overlap_message_ids=overlap_message_ids,
        latest_stored_message_id=latest_stored_message_id,
        latest_stored_message_date_utc=latest_stored_message_date_utc,
        window_cutoff_utc=window_cutoff_utc,
        window_months=window_months,
        window_complete=window_complete,
        full_history_complete=full_history_complete,
    )


async def sync_topic_messages(
    client: Client,
    chat_id: int,
    topic_id: int,
    *,
    overlap_message_ids: int = DEFAULT_OVERLAP_IDS,
    window_months: int | None = None,
    window_cutoff: datetime | None = None,
    other_topic_ids: frozenset[int] | None = None,
    db_path: Path = DATABASE_PATH,
) -> SyncResult:
    window_cutoff_utc_str: str | None = None
    cutoff_dt: datetime | None = None
    if window_months is not None:
        cutoff_dt = window_cutoff or cutoff_utc_from_months(window_months)
        window_cutoff_utc_str = cutoff_utc_iso(cutoff_dt)

    conn = connect(db_path)
    try:
        init_schema(conn)
        stats = get_topic_message_stats(conn, chat_id, topic_id)
        checkpoint = get_sync_checkpoint(conn, chat_id, topic_id)
        if checkpoint is None:
            checkpoint = _checkpoint_from_stats(
                chat_id,
                topic_id,
                stats,
                overlap_message_ids=overlap_message_ids,
                window_months=window_months,
                window_cutoff_utc=window_cutoff_utc_str,
            )

        plan = plan_sync(
            stats,
            checkpoint,
            overlap_message_ids=overlap_message_ids,
            window_months=window_months,
            window_cutoff_utc=window_cutoff_utc_str,
        )
        chat_title, topic_title = await _resolve_titles(client, chat_id, topic_id)
        upsert_group(conn, chat_id, chat_title)
        upsert_topic(conn, chat_id, topic_id, topic_title)
        conn.commit()

        windowed = window_months is not None and cutoff_dt is not None
        in_progress = _merge_checkpoint_fields(
            checkpoint,
            history_complete=checkpoint.history_complete,
            last_sync_utc=utc_now_iso(),
            last_sync_status="in_progress",
            resume_offset_id=plan.start_offset_id,
            overlap_message_ids=overlap_message_ids,
            latest_stored_message_id=stats.latest_message_id,
            latest_stored_message_date_utc=stats.latest_message_date_utc,
            window_cutoff_utc=window_cutoff_utc_str or checkpoint.window_cutoff_utc,
            window_months=window_months
            if windowed
            else checkpoint.window_months,
            window_complete=checkpoint.window_complete,
            full_history_complete=checkpoint.full_history_complete,
        )
        save_sync_checkpoint(conn, in_progress)
        conn.commit()

        total_stats = UpsertStats()
        fetched = 0
        dates: list[str] = []
        pagination_finished = plan.mode == "incremental"
        window_finished = plan.mode == "incremental" and windowed
        last_resume_offset: int | None = plan.start_offset_id
        stop_pagination = False

        async for page in iter_forum_topic_message_pages(
            client,
            chat_id,
            topic_id,
            min_id=plan.min_id,
            start_offset_id=plan.start_offset_id,
            other_topic_ids=other_topic_ids,
        ):
            if stop_pagination:
                break

            page_to_store = page
            reached_cutoff = False
            if plan.mode in ("window_backfill", "resume_window") and cutoff_dt:
                page_to_store, reached_cutoff = split_page_at_cutoff(page, cutoff_dt)

            records = [
                message_to_record(message, chat_id=chat_id, topic_id=topic_id)
                for message in page_to_store
            ]
            fetched += len(records)
            dates.extend(record.message_date_utc for record in records)

            page_stats = upsert_messages(conn, records)
            total_stats += page_stats

            stats = get_topic_message_stats(conn, chat_id, topic_id)
            page_is_last = len(page) < 100

            if plan.mode in ("window_backfill", "resume_window"):
                window_finished = reached_cutoff or page_is_last
                pagination_finished = window_finished
                resume_offset_id = None if window_finished else page[-1].id
                last_resume_offset = resume_offset_id
                page_window_complete = window_finished
                page_history_complete = window_finished
                page_full_history = False
                if reached_cutoff:
                    stop_pagination = True
            elif plan.mode in ("initial", "resume_initial"):
                pagination_finished = page_is_last
                resume_offset_id = None if page_is_last else page[-1].id
                last_resume_offset = resume_offset_id
                page_window_complete = page_is_last
                page_history_complete = page_is_last
                page_full_history = page_is_last
            else:
                pagination_finished = True
                resume_offset_id = None
                page_window_complete = checkpoint.window_complete
                page_history_complete = True
                page_full_history = checkpoint.full_history_complete

            page_checkpoint = _merge_checkpoint_fields(
                checkpoint,
                history_complete=page_history_complete,
                last_sync_utc=utc_now_iso(),
                last_sync_status="in_progress",
                resume_offset_id=resume_offset_id,
                overlap_message_ids=overlap_message_ids,
                latest_stored_message_id=stats.latest_message_id,
                latest_stored_message_date_utc=stats.latest_message_date_utc,
                window_cutoff_utc=window_cutoff_utc_str or checkpoint.window_cutoff_utc,
                window_months=window_months
                if windowed
                else checkpoint.window_months,
                window_complete=page_window_complete,
                full_history_complete=page_full_history,
            )
            save_sync_checkpoint(conn, page_checkpoint)
            conn.commit()

        final_stats = get_topic_message_stats(conn, chat_id, topic_id)
        if plan.mode in ("window_backfill", "resume_window"):
            if not window_finished and fetched == 0:
                window_finished = True
            history_complete = window_finished
            window_complete = window_finished
            full_history_complete = False
        elif plan.mode in ("initial", "resume_initial"):
            history_complete = pagination_finished
            window_complete = pagination_finished
            full_history_complete = pagination_finished
        else:
            history_complete = checkpoint.history_complete or checkpoint.window_complete
            window_complete = checkpoint.window_complete
            full_history_complete = checkpoint.full_history_complete

        final_checkpoint = _merge_checkpoint_fields(
            checkpoint,
            history_complete=history_complete,
            last_sync_utc=utc_now_iso(),
            last_sync_status="success" if pagination_finished else "in_progress",
            resume_offset_id=None if pagination_finished else last_resume_offset,
            overlap_message_ids=overlap_message_ids,
            latest_stored_message_id=final_stats.latest_message_id,
            latest_stored_message_date_utc=final_stats.latest_message_date_utc,
            window_cutoff_utc=window_cutoff_utc_str or checkpoint.window_cutoff_utc,
            window_months=window_months if windowed else checkpoint.window_months,
            window_complete=window_complete,
            full_history_complete=full_history_complete,
        )
        save_sync_checkpoint(conn, final_checkpoint)
        conn.commit()

        dates.sort()
        return SyncResult(
            chat_id=chat_id,
            topic_id=topic_id,
            mode=plan.mode,
            fetched=fetched,
            inserted=total_stats.inserted,
            updated=total_stats.updated,
            skipped=total_stats.skipped,
            date_min_utc=dates[0] if dates else None,
            date_max_utc=dates[-1] if dates else None,
            history_complete=history_complete,
            window_cutoff_utc=final_checkpoint.window_cutoff_utc,
            window_months=final_checkpoint.window_months,
            window_complete=window_complete,
            full_history_complete=full_history_complete,
            checkpoint_resume_offset_id=final_checkpoint.resume_offset_id,
            last_sync_utc=final_checkpoint.last_sync_utc,
        )
    finally:
        conn.close()


def print_sync_result(result: SyncResult) -> None:
    print(f"Chat ID:       {result.chat_id}")
    print(f"Topic ID:      {result.topic_id}")
    print(f"Mode:          {result.mode}")
    if result.window_months is not None:
        print(f"Window:        last {result.window_months} calendar month(s)")
        print(f"Cutoff UTC:    {result.window_cutoff_utc or '—'}")
    print(f"Fetched:       {result.fetched}")
    print(f"Inserted:      {result.inserted}")
    print(f"Updated:       {result.updated}")
    print(f"Skipped:       {result.skipped}")
    if result.date_min_utc and result.date_max_utc:
        print(f"Date range:    {result.date_min_utc} .. {result.date_max_utc} (UTC)")
    else:
        print("Date range:    (no messages fetched this run)")
    if result.window_months is not None:
        print(f"Window done:   {'yes' if result.window_complete else 'no'}")
        print(f"Full history:  no (windowed sync only)")
    else:
        print(f"History done:  {'yes' if result.history_complete else 'no'}")
        if result.full_history_complete:
            print("Full history:  yes")
    print(f"Checkpoint:    resume_offset_id={result.checkpoint_resume_offset_id}")
    print(f"Last sync UTC: {result.last_sync_utc or '—'}")


class SyncError(Exception):
    pass
