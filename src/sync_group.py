from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from hydrogram import Client

from src.config import DATABASE_PATH
from src.export_topic import build_export_topics_batch_spec
from src.sync import SyncError, SyncResult, sync_topic_messages
from src.sync_window import DEFAULT_SYNC_GROUP_MONTHS, cutoff_utc_from_months, cutoff_utc_iso
from src.topic_attribution import GENERAL_TOPIC_ID
from src.topics import fetch_forum_topics


@dataclass(frozen=True)
class GroupSyncTopicOutcome:
    topic_id: int
    title: str | None
    skipped: bool
    skip_reason: str | None
    result: SyncResult | None


@dataclass(frozen=True)
class GroupSyncReport:
    chat_id: int
    window_months: int
    window_cutoff_utc: str
    topics: tuple[GroupSyncTopicOutcome, ...]

    @property
    def total_inserted(self) -> int:
        return sum(
            o.result.inserted for o in self.topics if o.result is not None
        )

    @property
    def total_fetched(self) -> int:
        return sum(o.result.fetched for o in self.topics if o.result is not None)


async def sync_group_forum_topics(
    client: Client,
    chat_id: int,
    *,
    months: int = DEFAULT_SYNC_GROUP_MONTHS,
    overlap_message_ids: int = 10,
    topic_delay_seconds: float = 1.0,
    db_path: Path = DATABASE_PATH,
) -> GroupSyncReport:
    if months <= 0:
        raise SyncError("--months must be a positive integer.")

    cutoff = cutoff_utc_from_months(months)
    cutoff_iso = cutoff_utc_iso(cutoff)

    topic_rows = await fetch_forum_topics(client, chat_id, debug=False)
    other_topic_ids = frozenset(
        row.topic_id for row in topic_rows if row.topic_id != GENERAL_TOPIC_ID
    )
    outcomes: list[GroupSyncTopicOutcome] = []

    synced_count = 0
    for row in topic_rows:
        topic_id = row.topic_id
        title = row.title

        if synced_count > 0 and topic_delay_seconds > 0:
            await asyncio.sleep(topic_delay_seconds)
        synced_count += 1

        result = await sync_topic_messages(
            client,
            chat_id,
            topic_id,
            overlap_message_ids=overlap_message_ids,
            window_months=months,
            window_cutoff=cutoff,
            other_topic_ids=other_topic_ids
            if topic_id == GENERAL_TOPIC_ID
            else None,
            db_path=db_path,
        )
        outcomes.append(
            GroupSyncTopicOutcome(
                topic_id=topic_id,
                title=title,
                skipped=False,
                skip_reason=None,
                result=result,
            )
        )

    return GroupSyncReport(
        chat_id=chat_id,
        window_months=months,
        window_cutoff_utc=cutoff_iso,
        topics=tuple(outcomes),
    )


def print_group_sync_report(report: GroupSyncReport) -> None:
    print(f"Chat ID:       {report.chat_id}")
    print(f"Window:        last {report.window_months} calendar month(s)")
    print(f"Cutoff UTC:    {report.window_cutoff_utc}")
    print()
    print(
        f"{'Topic':>6}  {'Title':<28}  {'Mode':<14}  {'Fetch':>5}  "
        f"{'Ins':>4}  {'Upd':>4}  {'Skip':>4}  Window"
    )
    print("-" * 88)
    for outcome in report.topics:
        title = outcome.title or "—"
        if len(title) > 28:
            title = title[:27] + "…"
        if outcome.skipped:
            print(
                f"{outcome.topic_id:>6}  {title:<28}  {'—':<14}  "
                f"{'—':>5}  {'—':>4}  {'—':>4}  {'—':>4}  skipped ({outcome.skip_reason})"
            )
            continue
        assert outcome.result is not None
        r = outcome.result
        window = "yes" if r.window_complete else "no"
        print(
            f"{outcome.topic_id:>6}  {title:<28}  {r.mode:<14}  {r.fetched:>5}  "
            f"{r.inserted:>4}  {r.updated:>4}  {r.skipped:>4}  {window}"
        )
    print("-" * 88)
    print(
        f"Totals: fetched={report.total_fetched}  inserted={report.total_inserted}"
    )

    inserts: list[tuple[str, int]] = []
    for outcome in report.topics:
        if outcome.skipped or outcome.result is None or not outcome.title:
            continue
        if outcome.result.inserted > 0:
            inserts.append((outcome.title, outcome.result.inserted))

    topics_spec = build_export_topics_batch_spec(inserts)
    print()
    if topics_spec is None:
        print("Next export: no new inserts — skip export-topic batch.")
        return

    print("Next export (copy/paste):")
    print("python -m src.main export-topic \\")
    print(f"    --chat-id {report.chat_id} \\")
    print(f'    --topics "{topics_spec}"')
