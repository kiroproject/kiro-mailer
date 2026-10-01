"""Who receives a mailing: union of groups, tariffs, a custom filter, Core groups, saved lists and a manual
list, minus exclusions, minus people who cannot be messaged (banned, no Telegram, blocked the bot).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .audience import BASE_FROM, filter_where
from .content import BUILTIN_GROUPS

CoreIds = Callable[[str], Awaitable[list[int]]]
_SPLIT = re.compile(r"[\s,;]+")
MAX_MANUAL = 100_000


def tokens(raw: str) -> list[str]:
    return [t for t in _SPLIT.split(raw or "") if t][:MAX_MANUAL]


async def parse_manual(session: AsyncSession, raw: str) -> dict[str, Any]:
    """Resolve pasted ids / Telegram ids / @usernames to users. Unknown entries are reported, not guessed."""
    toks = tokens(raw)
    numbers = sorted({int(t) for t in toks if t.lstrip("-").isdigit()})
    names = sorted({t.lstrip("@").lower() for t in toks if not t.lstrip("-").isdigit()})
    found: dict[int, dict[str, Any]] = {}
    seen_tokens: set[str] = set()
    if numbers:
        rows = (
            await session.execute(
                text("select user_id, telegram_id, username from users where user_id = any(:n) or telegram_id = any(:n)"),
                {"n": numbers},
            )
        ).mappings()
        for r in rows:
            found[int(r["user_id"])] = dict(r)
            seen_tokens.update({str(r["user_id"]), str(r["telegram_id"])})
    if names:
        rows = (
            await session.execute(
                text("select user_id, telegram_id, username from users where lower(username) = any(:n)"), {"n": names}
            )
        ).mappings()
        for r in rows:
            found[int(r["user_id"])] = dict(r)
            seen_tokens.add(str(r["username"]).lower())
    unresolved = [
        t for t in dict.fromkeys(toks)
        if (t.lstrip("-") if t.lstrip("-").isdigit() else t.lstrip("@").lower()) not in seen_tokens and t not in seen_tokens
    ]
    return {"ids": set(found), "unresolved": unresolved[:200], "total_tokens": len(toks)}


async def _filter_ids(session: AsyncSession, spec: dict[str, Any], prefix: str) -> set[int]:
    cond, args = filter_where(spec, prefix)
    where = " AND ".join(cond) if cond else "TRUE"
    rows = (await session.execute(text(f"SELECT u.user_id {BASE_FROM} WHERE {where}"), args)).scalars().all()
    return {int(r) for r in rows}


async def _list_ids(session: AsyncSession, list_ids: list[int]) -> set[int]:
    if not list_ids:
        return set()
    rows = (await session.execute(text("select user_ids from ext_kiro_mailer_lists where id = any(:i)"), {"i": list_ids})).scalars().all()
    return {int(u) for ids in rows for u in ids}


async def _sent_ids(session: AsyncSession, mailing_ids: list[int]) -> set[int]:
    if not mailing_ids:
        return set()
    rows = (
        await session.execute(
            text("select user_id from ext_kiro_mailer_recipients where mailing_id = any(:m) and status = 'sent'"), {"m": mailing_ids}
        )
    ).scalars().all()
    return {int(u) for u in rows}


async def resolve(
    session: AsyncSession, audience: dict[str, Any], *, core_ids: CoreIds | None = None, sample: int = 8
) -> dict[str, Any]:
    """Full resolution. `deliver` is the list of (user_id, chat_id) to message; the rest is the explanation."""
    selected: set[int] = set()
    components: dict[str, int] = {}

    async def add(name: str, ids: set[int]) -> None:
        components[name] = len(ids)
        selected.update(ids)

    for i, group in enumerate(audience["groups"]):
        await add(f"group:{group}", await _filter_ids(session, BUILTIN_GROUPS[group]["filter"], f"g{i}"))
    if audience["tariffs"]:
        await add("tariffs", await _filter_ids(session, {"tariffs": audience["tariffs"]}, "t"))
    if audience["filter"]:
        await add("filter", await _filter_ids(session, audience["filter"], "c"))
    for target in audience["core_groups"]:
        ids = set(await core_ids(target)) if core_ids else set()
        await add(f"core:{target}", {int(i) for i in ids})
    if audience["lists"]:
        await add("lists", await _list_ids(session, audience["lists"]))
    manual = await parse_manual(session, audience["manual"]) if audience["manual"].strip() else {"ids": set(), "unresolved": [], "total_tokens": 0}
    if audience["manual"].strip():
        await add("manual", manual["ids"])

    ex = audience["exclude"]
    excluded: set[int] = set()
    ex_manual = await parse_manual(session, ex["manual"]) if ex["manual"].strip() else {"ids": set(), "unresolved": []}
    excluded |= ex_manual["ids"]
    excluded |= await _list_ids(session, ex["lists"])
    excluded |= await _sent_ids(session, ex["mailings"])
    removed = selected & excluded
    candidates = selected - excluded

    deliver: list[tuple[int, int]] = []
    ineligible = {"banned": 0, "no_telegram": 0, "blocked": 0, "missing": 0}
    sample_rows: list[dict[str, Any]] = []
    if candidates:
        ids = sorted(candidates)
        rows = (
            await session.execute(
                text(
                    "select user_id, telegram_id, is_banned, telegram_notifications_status as st, username, first_name "
                    "from users where user_id = any(:ids)"
                ),
                {"ids": ids},
            )
        ).mappings().all()
        by_id = {int(r["user_id"]): r for r in rows}
        for uid in ids:
            r = by_id.get(uid)
            if r is None:
                ineligible["missing"] += 1
            elif r["is_banned"]:
                ineligible["banned"] += 1
            elif not r["telegram_id"]:
                ineligible["no_telegram"] += 1
            elif r["st"] == "blocked":
                ineligible["blocked"] += 1
            else:
                deliver.append((uid, int(r["telegram_id"])))
                if len(sample_rows) < sample:
                    sample_rows.append({"user_id": uid, "username": r["username"], "first_name": r["first_name"]})
    return {
        "deliver": deliver,
        "selected": len(selected),
        "excluded": len(removed),
        "ineligible": ineligible,
        "final": len(deliver),
        "components": components,
        "unresolved": manual["unresolved"],
        "unresolved_excluded": ex_manual["unresolved"],
        "sample": sample_rows,
    }
