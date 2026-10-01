"""Delivery statistics, recipient log and poll results for the mailer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .engine import jload

STATUSES = ("pending", "sent", "failed", "blocked", "skipped")


def _counts(rows: list[Any]) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    out["total"] = sum(out[s] for s in STATUSES)
    return out


async def overview(session: AsyncSession) -> dict[int, dict[str, Any]]:
    """Counters for every mailing on the list screen."""
    out: dict[int, dict[str, Any]] = {}
    rows = (
        await session.execute(
            text("select mailing_id, status, count(*) as n from ext_kiro_mailer_recipients group by mailing_id, status")
        )
    ).mappings().all()
    for r in rows:
        info = out.setdefault(int(r["mailing_id"]), {s: 0 for s in STATUSES})
        info[str(r["status"])] = int(r["n"])
    for info in out.values():
        info["total"] = sum(info[s] for s in STATUSES)
    votes = (
        await session.execute(text("select mailing_id, count(*) as n from ext_kiro_mailer_votes where cardinality(option_ids) > 0 group by mailing_id"))
    ).mappings().all()
    for r in votes:
        out.setdefault(int(r["mailing_id"]), {s: 0 for s in STATUSES} | {"total": 0})["voted"] = int(r["n"])
    return out


async def mailing_stats(session: AsyncSession, mailing_id: int, *, rate: int = 20) -> dict[str, Any]:
    args = {"m": mailing_id}
    counts = _counts(
        (await session.execute(text("select status, count(*) as n from ext_kiro_mailer_recipients where mailing_id = :m group by status"), args)).mappings().all()
    )
    errors = [
        {"error": r["error"] or "—", "status": r["status"], "count": int(r["n"])}
        for r in (
            await session.execute(
                text(
                    "select status, error, count(*) as n from ext_kiro_mailer_recipients where mailing_id = :m "
                    "and status in ('failed','blocked','skipped') group by status, error order by n desc limit 12"
                ),
                args,
            )
        ).mappings()
    ]
    mailing = (
        await session.execute(
            text("select status, content, started_at, finished_at, scheduled_at, recipient_total from ext_kiro_mailer_mailings where id = :m"), args
        )
    ).mappings().one()
    started, finished = mailing["started_at"], mailing["finished_at"]
    speed = None
    if started:
        elapsed = ((finished or datetime.now(UTC)) - started).total_seconds()
        if elapsed >= 5 and counts["sent"]:
            speed = round(counts["sent"] / elapsed, 2)
    eta = None
    if mailing["status"] == "sending" and counts["pending"]:
        eta = int(counts["pending"] / max(1.0, min(float(rate), speed or float(rate))))
    content = jload(mailing["content"]) or {}
    result: dict[str, Any] = {
        "status": mailing["status"],
        "counts": counts,
        "errors": errors,
        "started_at": started,
        "finished_at": finished,
        "scheduled_at": mailing["scheduled_at"],
        "speed_per_sec": speed,
        "eta_seconds": eta,
        "delivered_pct": round(100.0 * counts["sent"] / counts["total"], 1) if counts["total"] else 0.0,
    }
    poll = content.get("poll")
    if poll:
        result["poll"] = await poll_results(session, mailing_id, poll, counts["sent"])
    return result


async def poll_results(session: AsyncSession, mailing_id: int, poll: dict[str, Any], delivered: int) -> dict[str, Any]:
    args = {"m": mailing_id}
    opts = poll["options"]
    per = {int(r["o"]): int(r["n"]) for r in (
        await session.execute(
            text("select o, count(*) as n from ext_kiro_mailer_votes v, unnest(v.option_ids) as o where v.mailing_id = :m group by o"), args
        )
    ).mappings()}
    voters = int(await session.scalar(text("select count(*) from ext_kiro_mailer_votes where mailing_id = :m and cardinality(option_ids) > 0"), args) or 0)
    polls_sent = int(await session.scalar(text("select count(*) from ext_kiro_mailer_recipients where mailing_id = :m and poll_id is not null"), args) or 0)
    options = [
        {"index": i, "text": t, "votes": per.get(i, 0), "pct": round(100.0 * per.get(i, 0) / voters, 1) if voters else 0.0,
         "correct": bool(poll["quiz"] and i == poll["correct"])}
        for i, t in enumerate(opts)
    ]
    out: dict[str, Any] = {"question": poll["question"], "quiz": poll["quiz"], "options": options, "voters": voters,
                           "polls_sent": polls_sent, "response_pct": round(100.0 * voters / polls_sent, 1) if polls_sent else 0.0}
    if poll["quiz"] and voters:
        out["correct_pct"] = round(100.0 * per.get(poll["correct"], 0) / voters, 1)
    return out


async def recipient_page(
    session: AsyncSession, mailing_id: int, *, status: str = "", query: str = "", limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    where, args = ["r.mailing_id = :m"], {"m": mailing_id, "l": limit, "o": offset}
    if status in STATUSES:
        where.append("r.status = :s")
        args["s"] = status
    q = query.strip().lstrip("@")
    if q:
        if q.lstrip("-").isdigit():
            where.append("(r.user_id = :uid or r.chat_id = :uid)")
            args["uid"] = int(q)
        else:
            where.append("lower(u.username) like lower(:name)")
            args["name"] = f"%{q}%"
    clause = " and ".join(where)
    total = int(await session.scalar(
        text(f"select count(*) from ext_kiro_mailer_recipients r left join users u on u.user_id = r.user_id where {clause}"),
        {k: v for k, v in args.items() if k not in ("l", "o")},
    ) or 0)
    rows = (
        await session.execute(
            text(
                "select r.user_id, r.chat_id, r.status, r.error, r.attempts, r.sent_at, u.username, u.first_name, v.option_ids "
                "from ext_kiro_mailer_recipients r left join users u on u.user_id = r.user_id "
                "left join ext_kiro_mailer_votes v on v.poll_id = r.poll_id and v.user_id = r.user_id "
                f"where {clause} order by r.id limit :l offset :o"
            ),
            args,
        )
    ).mappings().all()
    return {"total": total, "rows": [dict(r) for r in rows]}


async def export_rows(session: AsyncSession, mailing_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "select r.user_id, u.username, r.status, r.error, r.sent_at, v.option_ids from ext_kiro_mailer_recipients r "
                "left join users u on u.user_id = r.user_id left join ext_kiro_mailer_votes v on v.poll_id = r.poll_id and v.user_id = r.user_id "
                "where r.mailing_id = :m order by r.id"
            ),
            {"m": mailing_id},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def record_vote(session: AsyncSession, poll_id: str, telegram_id: int, option_ids: list[int]) -> bool:
    """Store the latest answer of a person to a poll we sent. Votes for foreign polls are ignored.

    Telegram identifies the voter by their Telegram id, which is the recipient's `chat_id` in a private chat.
    """
    row = (
        await session.execute(
            text("select mailing_id, user_id from ext_kiro_mailer_recipients where poll_id = :p and chat_id = :c"),
            {"p": poll_id, "c": telegram_id},
        )
    ).first()
    if row is None:
        return False
    await session.execute(
        text(
            "insert into ext_kiro_mailer_votes (poll_id, user_id, mailing_id, option_ids, voted_at) "
            "values (:p, :u, :m, cast(:o as integer[]), now()) "
            "on conflict (poll_id, user_id) do update set option_ids = excluded.option_ids, voted_at = now()"
        ),
        {"p": poll_id, "u": int(row[1]), "m": int(row[0]), "o": option_ids},
    )
    return True
