"""Mailing engine: freezes recipients at the start, then delivers in batches with a rate limit.

Telegram delivery itself is behind the `Host` adapter, so everything here runs on a plain database.

A recipient row records the number of parts already delivered (`step`). A retry resumes from there, so a
person who already received the text is never sent it twice because the poll after it failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .content import plan_parts
from .recipients import resolve
from .storage import load_settings

logger = logging.getLogger(__name__)

BATCH = 50
MAX_RETRY_AFTER = 60


@dataclass
class SendResult:
    status: str  # sent | blocked | failed | retry_after
    error: str = ""
    message_id: int | None = None
    poll_id: str | None = None
    retry_after: int = 0


class Host(Protocol):
    async def send_part(
        self, session: AsyncSession, *, chat_id: int, user_id: int, part: dict[str, Any], content: dict[str, Any]
    ) -> SendResult: ...

    async def core_ids(self, target: str) -> list[int]: ...


def jload(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value
    return json.loads(value)


class MailerError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class Engine:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        host: Host,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session_factory = session_factory
        self.host = host
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep

    # ------------------------------------------------------------ starting

    async def freeze_recipients(self, session: AsyncSession, mailing_id: int) -> int:
        """Resolve the audience now and store one pending row per person. Safe to call once per mailing."""
        row = (
            await session.execute(
                text("select audience from ext_kiro_mailer_mailings where id = :i"), {"i": mailing_id}
            )
        ).mappings().one()
        result = await resolve(session, jload(row["audience"]), core_ids=self.host.core_ids)
        deliver = result["deliver"]
        for start in range(0, len(deliver), 2000):
            chunk = deliver[start : start + 2000]
            await session.execute(
                text(
                    "insert into ext_kiro_mailer_recipients (mailing_id, user_id, chat_id) "
                    "select :m, u, c from unnest(cast(:u as bigint[]), cast(:c as bigint[])) as t(u, c) "
                    "on conflict (mailing_id, user_id) do nothing"
                ),
                {"m": mailing_id, "u": [u for u, _ in chunk], "c": [c for _, c in chunk]},
            )
        total = int(await session.scalar(text("select count(*) from ext_kiro_mailer_recipients where mailing_id = :m"), {"m": mailing_id}) or 0)
        await session.execute(
            text(
                "update ext_kiro_mailer_mailings set status = 'sending', started_at = coalesce(started_at, now()), "
                "recipient_total = :t, last_error = null, updated_at = now() where id = :i"
            ),
            {"t": total, "i": mailing_id},
        )
        return total

    async def begin_due(self, session: AsyncSession) -> list[int]:
        rows = (
            await session.execute(
                text(
                    "select id from ext_kiro_mailer_mailings where status = 'scheduled' and scheduled_at <= now() "
                    "order by scheduled_at for update skip locked"
                )
            )
        ).scalars().all()
        started = []
        for mailing_id in rows:
            try:
                async with session.begin_nested():
                    await self.freeze_recipients(session, int(mailing_id))
                started.append(int(mailing_id))
            except Exception as exc:  # noqa: BLE001
                logger.exception("kiro-mailer: cannot start mailing %s", mailing_id)
                await session.execute(
                    text("update ext_kiro_mailer_mailings set status = 'paused', last_error = :e, updated_at = now() where id = :i"),
                    {"e": f"start_failed: {exc}"[:240], "i": mailing_id},
                )
        return started

    # ---------------------------------------------------------------- tick

    async def tick(self, budget_seconds: float = 30.0) -> dict[str, int]:
        started_at = asyncio.get_event_loop().time()
        stats = {"started": 0, "sent": 0, "failed": 0, "blocked": 0, "done": 0}
        async with self.session_factory() as session:
            if not await session.scalar(text("select pg_try_advisory_xact_lock(hashtext('kiro-mailer-tick'))")):
                return stats
            stats["started"] = len(await self.begin_due(session))
            await session.commit()
        settings = None
        while asyncio.get_event_loop().time() - started_at < budget_seconds:
            async with self.session_factory() as session:
                settings = settings or await load_settings(session)
                handled = await self._process_batch(session, settings, stats)
                await session.commit()
            if handled == 0:
                break
        async with self.session_factory() as session:
            stats["done"] = await self._complete(session)
            await session.commit()
        return stats

    async def _process_batch(self, session: AsyncSession, settings: dict[str, Any], stats: dict[str, int]) -> int:
        mailing = (
            await session.execute(
                text(
                    "select m.id, m.content from ext_kiro_mailer_mailings m where m.status = 'sending' and exists ("
                    "  select 1 from ext_kiro_mailer_recipients r where r.mailing_id = m.id and r.status = 'pending' and r.next_try_at <= now()) "
                    "order by m.id limit 1"
                )
            )
        ).mappings().first()
        if mailing is None:
            return 0
        content = jload(mailing["content"])
        parts = plan_parts(content)
        rows = (
            await session.execute(
                text(
                    "select id, user_id, chat_id, step, attempts from ext_kiro_mailer_recipients "
                    "where mailing_id = :m and status = 'pending' and next_try_at <= now() "
                    "order by id limit :n for update skip locked"
                ),
                {"m": mailing["id"], "n": BATCH},
            )
        ).mappings().all()
        pause = 1.0 / max(1, int(settings["rate_per_second"]))
        for row in rows:
            await self._deliver(session, int(mailing["id"]), dict(row), parts, content, settings, stats)
            await self.sleep(pause)
        return len(rows)

    async def _deliver(
        self, session: AsyncSession, mailing_id: int, row: dict[str, Any], parts: list[dict[str, Any]],
        content: dict[str, Any], settings: dict[str, Any], stats: dict[str, int],
    ) -> None:
        step, message_id, poll_id = int(row["step"]), None, None
        for index in range(step, len(parts)):
            result = await self.host.send_part(
                session, chat_id=int(row["chat_id"]), user_id=int(row["user_id"]), part=parts[index], content=content
            )
            if result.status == "sent":
                step = index + 1
                message_id = result.message_id or message_id
                poll_id = result.poll_id or poll_id
                continue
            if result.status == "blocked":
                await self._finish_row(session, row["id"], "blocked", step, result.error, message_id, poll_id)
                stats["blocked"] += 1
                return
            if result.status == "retry_after":
                wait = max(1, min(int(result.retry_after or 1), MAX_RETRY_AFTER))
                await session.execute(
                    text("update ext_kiro_mailer_recipients set step = :s, next_try_at = now() + (cast(:w as integer) * interval '1 second') where id = :i"),
                    {"s": step, "w": wait, "i": row["id"]},
                )
                await self.sleep(wait)
                return
            attempts = int(row["attempts"]) + 1
            if attempts >= int(settings["max_attempts"]):
                await self._finish_row(session, row["id"], "failed", step, result.error, message_id, poll_id, attempts)
                stats["failed"] += 1
            else:
                await session.execute(
                    text(
                        "update ext_kiro_mailer_recipients set step = :s, attempts = :a, error = :e, "
                        "next_try_at = now() + (cast(:r as integer) * interval '1 minute') where id = :i"
                    ),
                    {"s": step, "a": attempts, "e": result.error[:240], "r": int(settings["retry_minutes"]), "i": row["id"]},
                )
            return
        await self._finish_row(session, row["id"], "sent", step, "", message_id, poll_id)
        stats["sent"] += 1

    async def _finish_row(
        self, session: AsyncSession, row_id: int, status: str, step: int, error: str,
        message_id: int | None, poll_id: str | None, attempts: int | None = None,
    ) -> None:
        await session.execute(
            text(
                "update ext_kiro_mailer_recipients set status = cast(:st as varchar), step = :s, error = :e, "
                "message_id = coalesce(:m, message_id), poll_id = coalesce(:p, poll_id), "
                "attempts = coalesce(:a, attempts), sent_at = case when cast(:st as varchar) = 'sent' then now() else sent_at end where id = :i"
            ),
            {"st": status, "s": step, "e": error[:240] or None, "m": message_id, "p": poll_id, "a": attempts, "i": row_id},
        )

    async def _complete(self, session: AsyncSession) -> int:
        result = await session.execute(
            text(
                "update ext_kiro_mailer_mailings m set status = 'done', finished_at = now(), updated_at = now() "
                "where m.status = 'sending' and not exists (select 1 from ext_kiro_mailer_recipients r "
                "where r.mailing_id = m.id and r.status = 'pending')"
            )
        )
        return result.rowcount or 0

    # ------------------------------------------------------------ controls

    async def _status(self, session: AsyncSession, mailing_id: int) -> str:
        status = await session.scalar(text("select status from ext_kiro_mailer_mailings where id = :i for update"), {"i": mailing_id})
        if status is None:
            raise MailerError("not_found", 404)
        return str(status)

    async def start_now(self, session: AsyncSession, mailing_id: int) -> int:
        if await self._status(session, mailing_id) not in ("draft", "scheduled"):
            raise MailerError("bad_state", 409)
        return await self.freeze_recipients(session, mailing_id)

    async def schedule(self, session: AsyncSession, mailing_id: int, at: datetime) -> None:
        if await self._status(session, mailing_id) not in ("draft", "scheduled"):
            raise MailerError("bad_state", 409)
        if at <= self.clock():
            raise MailerError("schedule_in_past")
        await session.execute(
            text("update ext_kiro_mailer_mailings set status = 'scheduled', scheduled_at = :a, updated_at = now() where id = :i"),
            {"a": at, "i": mailing_id},
        )

    async def unschedule(self, session: AsyncSession, mailing_id: int) -> None:
        if await self._status(session, mailing_id) != "scheduled":
            raise MailerError("bad_state", 409)
        await session.execute(
            text("update ext_kiro_mailer_mailings set status = 'draft', scheduled_at = null, updated_at = now() where id = :i"), {"i": mailing_id}
        )

    async def pause(self, session: AsyncSession, mailing_id: int) -> None:
        if await self._status(session, mailing_id) != "sending":
            raise MailerError("bad_state", 409)
        await session.execute(text("update ext_kiro_mailer_mailings set status = 'paused', updated_at = now() where id = :i"), {"i": mailing_id})

    async def resume(self, session: AsyncSession, mailing_id: int) -> None:
        if await self._status(session, mailing_id) != "paused":
            raise MailerError("bad_state", 409)
        await session.execute(text("update ext_kiro_mailer_mailings set status = 'sending', updated_at = now() where id = :i"), {"i": mailing_id})

    async def cancel(self, session: AsyncSession, mailing_id: int) -> int:
        if await self._status(session, mailing_id) not in ("scheduled", "sending", "paused"):
            raise MailerError("bad_state", 409)
        skipped = await session.execute(
            text(
                "update ext_kiro_mailer_recipients set status = 'skipped', error = 'cancelled' "
                "where mailing_id = :i and status = 'pending'"
            ),
            {"i": mailing_id},
        )
        await session.execute(
            text("update ext_kiro_mailer_mailings set status = 'cancelled', finished_at = now(), updated_at = now() where id = :i"), {"i": mailing_id}
        )
        return skipped.rowcount or 0

    async def retry_failed(self, session: AsyncSession, mailing_id: int) -> int:
        """Give failed deliveries (not blocked bots) another round; the mailing goes back to sending."""
        if await self._status(session, mailing_id) not in ("done", "paused", "sending"):
            raise MailerError("bad_state", 409)
        result = await session.execute(
            text(
                "update ext_kiro_mailer_recipients set status = 'pending', attempts = 0, next_try_at = now() "
                "where mailing_id = :i and status = 'failed'"
            ),
            {"i": mailing_id},
        )
        if result.rowcount:
            await session.execute(
                text("update ext_kiro_mailer_mailings set status = 'sending', finished_at = null, updated_at = now() where id = :i"), {"i": mailing_id}
            )
        return result.rowcount or 0
