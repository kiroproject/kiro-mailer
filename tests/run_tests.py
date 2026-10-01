"""Mailer tests on a throwaway PostgreSQL (pgserver). Core is replaced by a minimal schema and a fake host.

Run:  python tests/run_tests.py     (needs: pgserver sqlalchemy[asyncio] asyncpg)
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pgserver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1] / "backend" / "kiro_mailer"

migr = types.ModuleType("db.migrator.engine")


@dataclass
class Migration:
    id: str
    description: str
    upgrade: object


migr.Migration = Migration
for name in ("db", "db.migrator"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["db.migrator.engine"] = migr
pkg = types.ModuleType("kiro_mailer")
pkg.__path__ = [str(ROOT)]
sys.modules["kiro_mailer"] = pkg

from kiro_mailer import content as ct, engine as eng, recipients as rc, reports, storage  # noqa: E402

CORE_DDL = """
CREATE TABLE users (user_id BIGINT PRIMARY KEY, username TEXT, telegram_id BIGINT UNIQUE, first_name TEXT,
  language_code TEXT DEFAULT 'ru', registration_date TIMESTAMPTZ DEFAULT now(), is_banned BOOLEAN DEFAULT FALSE,
  referred_by_id BIGINT, telegram_notifications_status VARCHAR(32) NOT NULL DEFAULT 'unknown');
CREATE TABLE subscriptions (subscription_id SERIAL PRIMARY KEY, user_id BIGINT, start_date TIMESTAMPTZ,
  end_date TIMESTAMPTZ NOT NULL, duration_days INT, is_active BOOLEAN DEFAULT TRUE, last_connected_at TIMESTAMPTZ,
  provider TEXT, tariff_key TEXT);
CREATE TABLE payments (payment_id SERIAL PRIMARY KEY, user_id BIGINT, provider TEXT, funding_source VARCHAR(48) DEFAULT 'external',
  amount FLOAT, currency TEXT, status TEXT, tariff_key TEXT, created_at TIMESTAMPTZ DEFAULT now());
"""

PASSED = 0


def ok(cond: bool, label: str) -> None:
    global PASSED
    if not cond:
        raise AssertionError(label)
    PASSED += 1
    print(f"  ok  {label}")


class FakeHost:
    def __init__(self) -> None:
        self.log: list[tuple[int, str]] = []
        self.polls: dict[int, str] = {}
        self.script: dict[tuple[int, str], list[str]] = {}  # (user, part type) -> queue of statuses
        self.core: dict[str, list[int]] = {"core:vip": [1, 2]}

    async def send_part(self, session, *, chat_id, user_id, part, content):
        queue = self.script.get((user_id, part["type"]))
        status = queue.pop(0) if queue else "sent"
        if status == "retry_after":
            return eng.SendResult("retry_after", retry_after=1)
        if status in ("blocked", "failed"):
            return eng.SendResult(status, error=f"{status}-error")
        self.log.append((user_id, part["type"]))
        poll_id = None
        if part["type"] == "poll":
            poll_id = f"poll{user_id}"
        return eng.SendResult("sent", message_id=len(self.log), poll_id=poll_id)

    async def core_ids(self, target):
        return self.core.get(target, [])


def msg(**over):
    base = {"text": "Привет", "media": [], "buttons": [], "poll": None}
    base.update(over)
    return ct.clean_content(base)


async def main() -> None:
    server = pgserver.get_server(tempfile.mkdtemp())
    url = server.get_uri().replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        for stmt in CORE_DDL.strip().split(";"):
            if stmt.strip():
                await conn.exec_driver_sql(stmt)
        await conn.run_sync(lambda c: storage._upgrade_0001(c))

    async def sql(q: str, **a):
        async with factory() as s:
            r = await s.execute(text(q), a)
            await s.commit()
            return r

    async def scalar(q: str, **a):
        async with factory() as s:
            v = await s.scalar(text(q), a)
            await s.commit()
            return v

    # ------------------------------------------------------------------ content rules
    print("content")
    c = msg()
    ok(ct.validate_content(c) == [], "plain text is valid")
    ok(ct.validate_content(msg(text="")) != [], "empty mailing is rejected")
    album = msg(media=[{"id": "a1", "kind": "photo"}, {"id": "a2", "kind": "video"}], buttons=[{"kind": "url", "label": "x", "url": "https://x.io"}])
    ok(any("альбом" in e for e in ct.validate_content(album)), "buttons on an album are rejected")
    ok(any("GIF" in e for e in ct.validate_content(msg(media=[{"id": "a1", "kind": "photo"}, {"id": "a2", "kind": "animation"}]))), "animation cannot be in an album")
    ok(any("Документы" in e for e in ct.validate_content(msg(media=[{"id": "a1", "kind": "photo"}, {"id": "a2", "kind": "document"}]))), "documents only with documents")
    ok(ct.validate_content(msg(media=[{"id": "a1", "kind": "photo"}, {"id": "a2", "kind": "photo"}])) == [], "two photos make a valid album")
    ok(any("https" in e for e in ct.validate_content(msg(buttons=[{"kind": "url", "label": "x", "url": "http://x.io"}]))), "button needs https")
    long = msg(text="я" * 1500, media=[{"id": "a1", "kind": "photo"}], buttons=[{"kind": "webapp_section", "section": "plans", "label": ""}])
    parts = ct.plan_parts(long)
    ok([p["type"] for p in parts] == ["text", "media"] and parts[0]["buttons"] and not parts[1]["caption"], "long text is sent before the media with the buttons")
    short = ct.plan_parts(msg(text="коротко", media=[{"id": "a1", "kind": "video"}], buttons=[{"kind": "webapp_section", "section": "home", "label": ""}]))
    ok(len(short) == 1 and short[0]["caption"] == "коротко" and short[0]["buttons"], "short text becomes the caption, buttons stay on the media")
    poll = msg(text="", poll={"question": "Как вам?", "options": ["Хорошо", "Плохо", "Хорошо"], "quiz": True, "correct": 5, "explanation": "x" * 500, "multiple": True})
    ok(poll["poll"]["correct"] == 0 and not poll["poll"]["multiple"] and len(poll["poll"]["explanation"]) == 200, "quiz forces single answer, clamps correct option and explanation")
    ok(any("повторяться" in e for e in ct.validate_content(poll)), "duplicate poll options are rejected")
    ok(any("минимум" in e for e in ct.validate_content(msg(text="", poll={"question": "?", "options": ["один"]}))), "poll needs two options")
    ok([p["type"] for p in ct.plan_parts(msg(poll={"question": "?", "options": ["а", "б"]}))] == ["text", "poll"], "poll follows the text")
    aud = ct.clean_audience({"groups": ["active", "bogus"], "lists": ["3", "x"], "manual": "1 2", "exclude": {"mailings": [5]}})
    ok(aud["groups"] == ["active"] and aud["lists"] == [3] and aud["exclude"]["mailings"] == [5], "audience cleaning drops junk")

    # ------------------------------------------------------------------ recipients
    print("recipients")

    async def user(uid, *, tg=True, banned=False, status="unknown", name=None, lang="ru"):
        await sql(
            "insert into users (user_id, telegram_id, username, first_name, language_code, is_banned, telegram_notifications_status) "
            "values (:u, :t, :n, :f, :l, :b, :s)",
            u=uid, t=uid + 1000 if tg else None, n=name or f"user{uid}", f=f"Имя{uid}", l=lang, b=banned, s=status,
        )

    async def sub(uid, ends_in, tariff="start", provider="panel"):
        await sql(
            "insert into subscriptions (user_id, start_date, end_date, is_active, tariff_key, provider) values "
            "(:u, now() - interval '60 days', now() + cast(:e as double precision) * interval '1 day', true, :t, :p)",
            u=uid, e=ends_in, t=tariff, p=provider,
        )

    await user(1); await sub(1, 3, "start")            # active, ends soon
    await user(2); await sub(2, 40, "premium")         # active, long
    await user(3); await sub(3, -45)                   # expired 45 days ago
    await user(4)                                       # never had a subscription
    await user(5, banned=True); await sub(5, 10)       # banned
    await user(6, tg=False); await sub(6, 10)          # no telegram
    await user(7, status="blocked"); await sub(7, 10)  # blocked the bot
    await user(8, name="Vasya"); await sub(8, 2, provider="trial")
    await sql("insert into payments (user_id, provider, funding_source, amount, currency, status) values (2, 'y', 'external', 100, 'RUB', 'succeeded')")
    host = FakeHost()
    en = eng.Engine(factory, host, sleep=lambda s: asyncio.sleep(0))

    async def resolve(**aud):
        async with factory() as s:
            return await rc.resolve(s, ct.clean_audience(aud), core_ids=host.core_ids)

    r = await resolve(groups=["active"])
    ok(sorted(u for u, _ in r["deliver"]) == [1, 2, 8], "group 'active': only deliverable users")
    ok(r["selected"] == 6 and r["ineligible"] == {"banned": 1, "no_telegram": 1, "blocked": 1, "missing": 0}, "breakdown explains who is dropped")
    ok(r["final"] == 3 and r["components"]["group:active"] == 6, "final count and component size")
    r = await resolve(groups=["active", "expired"])
    ok(sorted(u for u, _ in r["deliver"]) == [1, 2, 3, 8], "groups are united without duplicates")
    r = await resolve(tariffs=["premium"])
    ok([u for u, _ in r["deliver"]] == [2], "tariff group")
    r = await resolve(groups=["never"])
    ok([u for u, _ in r["deliver"]] == [4], "never had a subscription")
    r = await resolve(groups=["trial_used"])
    ok([u for u, _ in r["deliver"]] == [8], "trial used")
    r = await resolve(groups=["paid"])
    ok([u for u, _ in r["deliver"]] == [2], "paid at least once")
    r = await resolve(filter={"days_left_max": 5})
    ok(sorted(u for u, _ in r["deliver"]) == [1, 8], "custom filter")
    r = await resolve(manual="1, 1002 @Vasya 999999 @nobody\n4")
    ok(sorted(u for u, _ in r["deliver"]) == [1, 2, 4, 8], "manual: ids, telegram ids and @usernames (case-insensitive)")
    ok(sorted(r["unresolved"]) == ["999999", "@nobody"], "unknown entries are reported")
    r = await resolve(core_groups=["core:vip"])
    ok(sorted(u for u, _ in r["deliver"]) == [1, 2], "groups of Core / other plugins")
    await sql("insert into ext_kiro_mailer_lists (name, user_ids) values ('vip', '{3,4}')")
    lid = await scalar("select id from ext_kiro_mailer_lists limit 1")
    r = await resolve(lists=[lid])
    ok(sorted(u for u, _ in r["deliver"]) == [3, 4], "saved list")
    r = await resolve(groups=["active"], exclude={"manual": "2"})
    ok(sorted(u for u, _ in r["deliver"]) == [1, 8] and r["excluded"] == 1, "manual exclusion")
    r = await resolve(groups=["active", "expired"], exclude={"lists": [lid]})
    ok(sorted(u for u, _ in r["deliver"]) == [1, 2, 8], "list exclusion")
    r = await resolve()
    ok(r["final"] == 0, "empty audience selects nobody")

    # ------------------------------------------------------------------ sending
    print("sending")

    async def new_mailing(name, content, audience):
        return await scalar(
            "insert into ext_kiro_mailer_mailings (name, status, content, audience) values (:n, 'draft', cast(:c as jsonb), cast(:a as jsonb)) returning id",
            n=name, c=json.dumps(content), a=json.dumps(ct.clean_audience(audience)),
        )

    mid = await new_mailing("m1", msg(text="Акция"), {"groups": ["active"]})
    async with factory() as s:
        total = await en.start_now(s, mid)
        await s.commit()
    ok(total == 3, "recipients are frozen at the start")
    await user(9); await sub(9, 5)  # appears after the start: must NOT receive it
    stats = await en.tick()
    ok(stats["sent"] == 3 and sorted(u for u, _ in host.log) == [1, 2, 8], "all frozen recipients got the message")
    ok(await scalar("select status from ext_kiro_mailer_mailings where id = :i", i=mid) == "done", "mailing completes")
    ok(stats["done"] == 1, "tick reports one completed mailing")
    await en.tick()
    ok(len(host.log) == 3, "nothing is sent twice")

    # blocked / failed / retries
    host.log.clear()
    mid2 = await new_mailing("m2", msg(text="Сообщение"), {"groups": ["active"]})
    host.script[(1, "text")] = ["blocked"]
    host.script[(2, "text")] = ["failed", "failed", "failed", "failed"]
    host.script[(8, "text")] = ["failed"]  # one transient failure, then fine
    async with factory() as s:
        await storage.save_settings(s, {**storage.DEFAULT_SETTINGS, "retry_minutes": 1, "max_attempts": 3})
        await en.start_now(s, mid2)
        await s.commit()
    await en.tick()
    rows = {r["user_id"]: r for r in (await sql("select user_id, status, attempts, error from ext_kiro_mailer_recipients where mailing_id = :m", m=mid2)).mappings()}
    ok(rows[1]["status"] == "blocked", "blocked bot is final")
    ok(rows[2]["status"] == "pending" and rows[2]["attempts"] == 1, "first failure schedules a retry")
    ok(rows[8]["status"] == "pending" and rows[8]["attempts"] == 1, "transient failure schedules a retry")
    ok(await scalar("select status from ext_kiro_mailer_mailings where id = :i", i=mid2) == "sending", "mailing waits for the retries")
    for _ in range(3):
        await sql("update ext_kiro_mailer_recipients set next_try_at = now() - interval '1 second' where mailing_id = :m and status = 'pending'", m=mid2)
        await en.tick()
    rows = {r["user_id"]: r["status"] for r in (await sql("select user_id, status from ext_kiro_mailer_recipients where mailing_id = :m", m=mid2)).mappings()}
    ok(rows == {1: "blocked", 2: "failed", 8: "sent", 9: "sent"}, "gave up after max attempts; the transient one was delivered; later joiner included")
    ok(await scalar("select status from ext_kiro_mailer_mailings where id = :i", i=mid2) == "done", "mailing completes after retries")
    async with factory() as s:
        n = await en.retry_failed(s, mid2)
        await s.commit()
    ok(n == 1 and await scalar("select status from ext_kiro_mailer_mailings where id = :i", i=mid2) == "sending", "retry_failed reopens only failed deliveries")
    host.script[(2, "text")] = []
    await en.tick()
    ok(await scalar("select status from ext_kiro_mailer_recipients where mailing_id = :m and user_id = 2", m=mid2) == "sent", "retried delivery succeeds")

    # partial delivery: text arrives, poll fails once -> the text is not sent again
    host.log.clear()
    pm = msg(text="Вопрос ниже", poll={"question": "Нравится?", "options": ["Да", "Нет"]})
    mid3 = await new_mailing("m3", pm, {"manual": "1"})
    host.script[(1, "poll")] = ["failed"]
    async with factory() as s:
        await en.start_now(s, mid3)
        await s.commit()
    await en.tick()
    ok(host.log == [(1, "text")], "text delivered, poll failed once")
    await sql("update ext_kiro_mailer_recipients set next_try_at = now() - interval '1 second' where mailing_id = :m", m=mid3)
    await en.tick()
    ok(host.log == [(1, "text"), (1, "poll")], "retry sends only the missing poll")
    ok(await scalar("select poll_id from ext_kiro_mailer_recipients where mailing_id = :m", m=mid3) == "poll1", "poll id is remembered for votes")

    # retry_after
    host.log.clear()
    mid4 = await new_mailing("m4", msg(text="Ещё"), {"manual": "2"})
    host.script[(2, "text")] = ["retry_after"]
    async with factory() as s:
        await en.start_now(s, mid4)
        await s.commit()
    await en.tick()
    row = (await sql("select status, attempts, next_try_at > now() as later from ext_kiro_mailer_recipients where mailing_id = :m", m=mid4)).mappings().one()
    ok(row["status"] == "pending" and row["attempts"] == 0, "flood wait does not count as a failed attempt")

    # pause / resume / cancel
    host.log.clear()
    mid5 = await new_mailing("m5", msg(text="Пауза"), {"groups": ["active"]})
    async with factory() as s:
        await en.start_now(s, mid5)
        await en.pause(s, mid5)
        await s.commit()
    await en.tick()
    ok(host.log == [], "paused mailing sends nothing")
    async with factory() as s:
        await en.resume(s, mid5)
        await s.commit()
    await en.tick()
    ok(len(host.log) == 4, "resumed mailing continues")
    mid6 = await new_mailing("m6", msg(text="Отмена"), {"groups": ["active"]})
    host.log.clear()
    async with factory() as s:
        await en.start_now(s, mid6)
        skipped = await en.cancel(s, mid6)
        await s.commit()
    await en.tick()
    ok(skipped == 4 and host.log == [], "cancelled mailing skips everyone and sends nothing")
    try:
        async with factory() as s:
            await en.pause(s, mid6)
        ok(False, "bad state refused")
    except eng.MailerError as exc:
        ok(exc.code == "bad_state", "actions in a wrong state are refused")

    # scheduling
    host.log.clear()
    mid7 = await new_mailing("m7", msg(text="По расписанию"), {"manual": "1"})
    try:
        async with factory() as s:
            await en.schedule(s, mid7, datetime.now(UTC) - timedelta(minutes=1))
        ok(False, "past schedule refused")
    except eng.MailerError as exc:
        ok(exc.code == "schedule_in_past", "scheduling into the past is refused")
    async with factory() as s:
        await en.schedule(s, mid7, datetime.now(UTC) + timedelta(hours=1))
        await s.commit()
    await en.tick()
    ok(host.log == [], "scheduled mailing waits")
    await sql("update ext_kiro_mailer_mailings set scheduled_at = now() - interval '1 second' where id = :i", i=mid7)
    stats = await en.tick()
    ok(stats["started"] == 1 and host.log == [(1, "text")], "scheduled mailing starts when due")
    mid8 = await new_mailing("m8", msg(text="x"), {"manual": "1"})
    async with factory() as s:
        await en.schedule(s, mid8, datetime.now(UTC) + timedelta(hours=1))
        await en.unschedule(s, mid8)
        await s.commit()
    ok(await scalar("select status from ext_kiro_mailer_mailings where id = :i", i=mid8) == "draft", "schedule can be removed")

    # ------------------------------------------------------------------ stats and polls
    print("stats")
    async with factory() as s:
        st = await reports.mailing_stats(s, mid2)
    ok(st["counts"] == {"pending": 0, "sent": 3, "failed": 0, "blocked": 1, "skipped": 0, "total": 4}, "delivery counters")
    ok(any(e["status"] == "blocked" for e in st["errors"]), "errors are grouped by reason")
    ok(st["delivered_pct"] == 75.0, "delivery percentage")
    async with factory() as s:
        ok(await reports.record_vote(s, "poll1", 1001, [1]), "vote for our poll is recorded")
        ok(not await reports.record_vote(s, "foreign", 1001, [0]), "vote for a foreign poll is ignored")
        await s.commit()
    mid9 = await new_mailing("m9", msg(text="", poll={"question": "Q", "options": ["a", "b", "c"], "quiz": True, "correct": 2, "explanation": "e"}), {"manual": "1 2 8"})
    async with factory() as s:
        await en.start_now(s, mid9)
        await s.commit()
    await en.tick()
    async with factory() as s:
        await reports.record_vote(s, "poll1", 1001, [2])   # re-vote: replaces the earlier answer in mid3's poll
        await reports.record_vote(s, "poll2", 1002, [2])
        await reports.record_vote(s, "poll8", 1008, [0])
        await s.commit()
        st = await reports.mailing_stats(s, mid9)
    pr = st["poll"]
    ok(pr["voters"] == 2 and pr["polls_sent"] == 3, "poll participation counted")
    ok([o["votes"] for o in pr["options"]] == [1, 0, 1] and pr["options"][2]["correct"], "votes per option, correct option marked")
    ok(pr["correct_pct"] == 50.0 and pr["response_pct"] == 66.7, "quiz and response percentages")
    async with factory() as s:
        await reports.record_vote(s, "poll8", 1008, [])
        await s.commit()
        pr2 = (await reports.mailing_stats(s, mid9))["poll"]
    ok(pr2["voters"] == 1, "retracted vote is not counted")
    async with factory() as s:
        page = await reports.recipient_page(s, mid2, status="blocked")
        q = await reports.recipient_page(s, mid2, query="@vasya")
        ov = await reports.overview(s)
    ok(page["total"] == 1 and page["rows"][0]["user_id"] == 1, "recipient log filters by status")
    ok(q["total"] == 1 and q["rows"][0]["user_id"] == 8, "recipient log search by username")
    ok(ov[mid2]["sent"] == 3 and ov[mid9]["voted"] == 1, "overview counters")

    ok(storage.clean_settings({"rate_per_second": "10"}, dict(storage.DEFAULT_SETTINGS))["rate_per_second"] == 10, "settings accept valid values")
    try:
        storage.clean_settings({"rate_per_second": 99}, dict(storage.DEFAULT_SETTINGS))
        ok(False, "rate bounded")
    except ValueError:
        ok(True, "rate bounded")

    await engine.dispose()
    server.cleanup()
    print(f"\n{PASSED} checks passed")


asyncio.run(main())
