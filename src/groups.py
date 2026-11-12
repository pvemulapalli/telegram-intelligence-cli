from __future__ import annotations

from dataclasses import dataclass

from hydrogram import Client, enums


@dataclass(frozen=True)
class GroupRow:
    title: str
    chat_id: int
    chat_type: str
    forum: str


def _forum_label(chat_type: enums.ChatType, is_forum: bool | None) -> str:
    if chat_type == enums.ChatType.GROUP:
        return "n/a"
    if chat_type == enums.ChatType.SUPERGROUP:
        if is_forum is True:
            return "yes"
        return "no"
    return "n/a"


def _type_label(chat_type: enums.ChatType) -> str:
    if chat_type == enums.ChatType.GROUP:
        return "group"
    if chat_type == enums.ChatType.SUPERGROUP:
        return "supergroup"
    return chat_type.value.lower()


async def collect_groups(client: Client) -> list[GroupRow]:
    rows: list[GroupRow] = []
    async for dialog in client.get_dialogs():
        chat = dialog.chat
        if chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            continue
        title = chat.title or "(untitled)"
        rows.append(
            GroupRow(
                title=title,
                chat_id=chat.id,
                chat_type=_type_label(chat.type),
                forum=_forum_label(chat.type, chat.is_forum),
            )
        )
    rows.sort(key=lambda row: row.title.casefold())
    return rows


def print_groups(rows: list[GroupRow]) -> None:
    if not rows:
        print("No accessible groups or supergroups found.")
        return

    name_w = max(len("Name"), max(len(r.title) for r in rows))
    name_w = min(name_w, 48)

    header = f"{'Name':<{name_w}}  {'Chat ID':>14}  {'Type':<12}  Forum"
    print(header)
    print("-" * len(header))
    for row in rows:
        title = row.title if len(row.title) <= name_w else row.title[: name_w - 1] + "…"
        print(
            f"{title:<{name_w}}  {row.chat_id:>14}  {row.chat_type:<12}  {row.forum}"
        )
    print(f"\n{len(rows)} group(s) listed.")
