"""KIRO mailer: one-off Telegram mailings with flexible recipients, scheduling, media, buttons and polls.

Admin API: /api/admin/kiro-mailer/*   (Core admin middleware resolves the role)
A periodic job (once a minute) starts due mailings and delivers them in rate-limited batches.
Poll answers arrive through an aiogram router registered with the bot.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Router
from aiogram.types import PollAnswer
from aiohttp import web
from sqlalchemy import text

from bot.app.web.context import get_session_factory
from bot.plugins.extensions.contracts import ExtensionContributions, JobHandler, OperationContext
from bot.plugins.spec import WEB_SCOPE_WEBAPP, Plugin, PluginContext

from . import content as ct
from . import recipients as rc
from . import reports
from .audience import FILTER_FIELDS
from .core import CoreHost, html_error
from .engine import Engine, MailerError, jload
from .storage import MIGRATIONS, PLUGIN_ID, clean_settings, load_settings, save_settings

logger = logging.getLogger(__name__)

ADMIN = f"/api/admin/{PLUGIN_ID}"
RUNTIME: dict[str, PluginContext] = {}
MEDIA_LIMITS = {"photo": 10 * 1024 * 1024, "video": 50 * 1024 * 1024, "animation": 50 * 1024 * 1024, "document": 50 * 1024 * 1024}
MAX_CHUNK = 4 * 1024 * 1024
EDITABLE = ("draft", "scheduled")


def _ok(payload: dict[str, Any] | None = None) -> web.Response:
    return web.json_response({"ok": True, **(payload or {})}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))


def _err(code: str, status: int = 400, **extra: Any) -> web.Response:
    return web.json_response({"ok": False, "error": code, **extra}, status=status)


def _guard(handler):
    async def wrapped(request: web.Request) -> web.Response:
        try:
            if not request.get("admin_authorized", False):
                raise MailerError("forbidden", 403)
            return await handler(request)
        except MailerError as exc:
            return _err(exc.code, exc.status)
        except ct.ContentError as exc:
            return _err(str(exc), 400)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("kiro-mailer: %s %s failed", request.method, request.path)
            return _err("internal_error", 500)

    return wrapped


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise MailerError("invalid_json") from None
    if not isinstance(body, dict):
        raise MailerError("invalid_json")
    return body


def _ctx() -> PluginContext:
    ctx = RUNTIME.get("ctx")
    if ctx is None:
        raise MailerError("not_ready", 503)
    return ctx


def _factory(request: web.Request):
    return get_session_factory(request)


def _engine(factory: Any = None) -> Engine:
    ctx = _ctx()
    return Engine(factory or ctx.require_session_factory(), CoreHost(ctx))


def _mid(request: web.Request) -> int:
    try:
        return int(request.match_info["mid"])
    except (KeyError, ValueError):
        raise MailerError("not_found", 404) from None


async def _mailing(session: Any, mailing_id: int) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                "select id, name, status, content, audience, scheduled_at, started_at, finished_at, recipient_total, last_error, "
                "created_at, updated_at from ext_kiro_mailer_mailings where id = :i"
            ),
            {"i": mailing_id},
        )
    ).mappings().first()
    if row is None:
        raise MailerError("not_found", 404)
    data = dict(row)
    data["content"] = ct.clean_content(jload(data["content"]))
    data["audience"] = ct.clean_audience(jload(data["audience"]))
    return data


def _local(dt: datetime | None, offset: int) -> str | None:
    return (dt + timedelta(hours=offset)).strftime("%Y-%m-%dT%H:%M") if dt else None


async def _media_ids_exist(session: Any, content: dict[str, Any]) -> list[str]:
    ids = [m["id"] for m in content["media"]]
    if not ids:
        return []
    found = {r for r in (await session.execute(text("select id from ext_kiro_mailer_media where id = any(:i) and complete"), {"i": ids})).scalars()}
    return [i for i in ids if i not in found]


async def _issues(session: Any, mailing: dict[str, Any]) -> list[str]:
    issues = ct.validate_content(mailing["content"], html_error=html_error)
    missing = await _media_ids_exist(session, mailing["content"])
    if missing:
        issues.append("Один из файлов не загружен до конца: загрузите его заново")
    a = mailing["audience"]
    if not (a["groups"] or a["tariffs"] or a["filter"] or a["core_groups"] or a["lists"] or a["manual"].strip()):
        issues.append("Выберите получателей")
    return issues


# ------------------------------------------------------------------------- meta / settings


async def meta(request: web.Request) -> web.Response:
    async with _factory(request)() as session:
        settings = await load_settings(session)
        lists = (await session.execute(text("select id, name, cardinality(user_ids) as n from ext_kiro_mailer_lists order by name"))).mappings().all()
        tariffs = (await session.execute(text("select distinct tariff_key from subscriptions where tariff_key is not null order by 1"))).scalars().all()
        recent = (
            await session.execute(
                text("select id, name from ext_kiro_mailer_mailings where status in ('done','sending','paused') order by id desc limit 30")
            )
        ).mappings().all()
    host = CoreHost(_ctx())
    return _ok({
        "settings": settings,
        "groups": [{"id": k, "label": v["label"]} for k, v in ct.BUILTIN_GROUPS.items()],
        "core_groups": host.core_groups(),
        "tariffs": list(tariffs),
        "lists": [dict(r) for r in lists],
        "recent_mailings": [dict(r) for r in recent],
        "filter_fields": FILTER_FIELDS,
        "limits": {"text": ct.MAX_TEXT, "caption": ct.MAX_CAPTION, "media": ct.MAX_MEDIA, "poll_options": list(ct.POLL_OPTIONS),
                   "question": ct.POLL_QUESTION_MAX, "option": ct.POLL_OPTION_MAX, "explanation": ct.POLL_EXPLANATION_MAX,
                   "media_bytes": MEDIA_LIMITS, "chunk": MAX_CHUNK},
        "sections": list(ct.SECTIONS),
    })


async def get_settings(request: web.Request) -> web.Response:
    async with _factory(request)() as session:
        return _ok({"settings": await load_settings(session)})


async def put_settings(request: web.Request) -> web.Response:
    body = await _body(request)
    async with _factory(request)() as session:
        try:
            data = clean_settings(body, await load_settings(session))
        except ValueError as exc:
            raise MailerError(str(exc)) from None
        await save_settings(session, data)
        await session.commit()
    return _ok({"settings": data})


# ------------------------------------------------------------------------- mailings


async def list_mailings(request: web.Request) -> web.Response:
    async with _factory(request)() as session:
        settings = await load_settings(session)
        rows = (
            await session.execute(
                text(
                    "select id, name, status, scheduled_at, started_at, finished_at, recipient_total, last_error, created_at, "
                    "content->>'text' as preview, jsonb_array_length(coalesce(content->'media', '[]'::jsonb)) as media, "
                    "(content->'poll') is not null and content->>'poll' <> 'null' as has_poll "
                    "from ext_kiro_mailer_mailings order by id desc limit 200"
                )
            )
        ).mappings().all()
        stats = await reports.overview(session)
    offset = int(settings["tz_offset_hours"])
    items = []
    for r in rows:
        d = dict(r)
        d["scheduled_local"] = _local(d["scheduled_at"], offset)
        d["stats"] = stats.get(d["id"], {})
        items.append(d)
    return _ok({"mailings": items})


async def create_mailing(request: web.Request) -> web.Response:
    body = await _body(request)
    name = str(body.get("name") or "").strip()[:120]
    async with _factory(request)() as session:
        source = None
        if body.get("copy_of"):
            source = await _mailing(session, int(body["copy_of"]))
        content = source["content"] if source else ct.clean_content({})
        audience = source["audience"] if source else ct.clean_audience({})
        if source:
            audience["exclude"]["mailings"] = [source["id"]] if body.get("exclude_received") else audience["exclude"]["mailings"]
        new_id = await session.scalar(
            text(
                "insert into ext_kiro_mailer_mailings (name, status, content, audience) "
                "values (:n, 'draft', cast(:c as jsonb), cast(:a as jsonb)) returning id"
            ),
            {"n": name or (f"{source['name']} (копия)"[:120] if source else "Новая рассылка"), "c": json.dumps(content, ensure_ascii=False), "a": json.dumps(audience, ensure_ascii=False)},
        )
        await session.commit()
    return _ok({"id": new_id})


async def get_mailing(request: web.Request) -> web.Response:
    async with _factory(request)() as session:
        mailing = await _mailing(session, _mid(request))
        settings = await load_settings(session)
        issues = await _issues(session, mailing)
    mailing["issues"] = issues
    mailing["scheduled_local"] = _local(mailing["scheduled_at"], int(settings["tz_offset_hours"]))
    mailing["tz_offset_hours"] = int(settings["tz_offset_hours"])
    return _ok({"mailing": mailing})


async def save_mailing(request: web.Request) -> web.Response:
    body = await _body(request)
    mid = _mid(request)
    async with _factory(request)() as session:
        current = await _mailing(session, mid)
        if current["status"] not in EDITABLE:
            raise MailerError("bad_state", 409)
        name = str(body.get("name") or current["name"]).strip()[:120] or current["name"]
        content = ct.clean_content(body["content"]) if "content" in body else current["content"]
        audience = ct.clean_audience(body["audience"]) if "audience" in body else current["audience"]
        await session.execute(
            text(
                "update ext_kiro_mailer_mailings set name = :n, content = cast(:c as jsonb), audience = cast(:a as jsonb), updated_at = now() where id = :i"
            ),
            {"n": name, "c": json.dumps(content, ensure_ascii=False), "a": json.dumps(audience, ensure_ascii=False), "i": mid},
        )
        await session.commit()
        issues = await _issues(session, {**current, "content": content, "audience": audience})
    return _ok({"issues": issues})


async def delete_mailing(request: web.Request) -> web.Response:
    mid = _mid(request)
    async with _factory(request)() as session:
        mailing = await _mailing(session, mid)
        if mailing["status"] in ("sending", "paused", "scheduled"):
            raise MailerError("bad_state", 409)
        await session.execute(text("delete from ext_kiro_mailer_votes where mailing_id = :i"), {"i": mid})
        await session.execute(text("delete from ext_kiro_mailer_mailings where id = :i"), {"i": mid})
        await session.commit()
    return _ok()


async def audience_preview(request: web.Request) -> web.Response:
    body = await _body(request)
    async with _factory(request)() as session:
        audience = ct.clean_audience(body["audience"]) if "audience" in body else (await _mailing(session, _mid(request)))["audience"]
        host = CoreHost(_ctx())
        result = await rc.resolve(session, audience, core_ids=host.core_ids)
        await session.rollback()
    result.pop("deliver")
    return _ok({"preview": result})


async def parse_manual(request: web.Request) -> web.Response:
    body = await _body(request)
    async with _factory(request)() as session:
        result = await rc.parse_manual(session, str(body.get("text") or ""))
    return _ok({"found": len(result["ids"]), "unresolved": result["unresolved"], "total": result["total_tokens"]})


async def send_test(request: web.Request) -> web.Response:
    mid = _mid(request)
    admin_tg = request.get("admin_telegram_id")
    if not admin_tg:
        raise MailerError("admin_telegram_unavailable", 403)
    async with _factory(request)() as session:
        mailing = await _mailing(session, mid)
        issues = ct.validate_content(mailing["content"], html_error=html_error)
        if issues:
            return _err("content_invalid", 400, issues=issues)
        uid = await session.scalar(text("select user_id from users where telegram_id = :t"), {"t": int(admin_tg)})
        result = await CoreHost(_ctx()).send_test(
            session, chat_id=int(admin_tg), user_id=int(uid or admin_tg), parts=ct.plan_parts(mailing["content"]), content=mailing["content"]
        )
        await session.commit()
    if result.status != "sent":
        return _err("test_failed", 502, detail=result.error)
    return _ok()


async def send_now(request: web.Request) -> web.Response:
    mid = _mid(request)
    async with _factory(request)() as session:
        mailing = await _mailing(session, mid)
        issues = await _issues(session, mailing)
        if issues:
            return _err("content_invalid", 400, issues=issues)
        total = await _engine(lambda: session).start_now(session, mid)
        await session.commit()
    return _ok({"recipients": total})


async def schedule(request: web.Request) -> web.Response:
    body = await _body(request)
    mid = _mid(request)
    async with _factory(request)() as session:
        mailing = await _mailing(session, mid)
        issues = await _issues(session, mailing)
        if issues:
            return _err("content_invalid", 400, issues=issues)
        settings = await load_settings(session)
        try:
            local = datetime.fromisoformat(str(body.get("at") or ""))
        except ValueError:
            raise MailerError("invalid_time") from None
        at = local.replace(tzinfo=None) - timedelta(hours=int(settings["tz_offset_hours"]))
        await _engine(lambda: session).schedule(session, mid, at.replace(tzinfo=UTC))
        await session.commit()
    return _ok()


async def control(request: web.Request) -> web.Response:
    action, mid = request.match_info["action"], _mid(request)
    async with _factory(request)() as session:
        engine = _engine(lambda: session)
        result: Any = None
        if action == "unschedule":
            await engine.unschedule(session, mid)
        elif action == "pause":
            await engine.pause(session, mid)
        elif action == "resume":
            await engine.resume(session, mid)
        elif action == "cancel":
            result = await engine.cancel(session, mid)
        elif action == "retry-failed":
            result = await engine.retry_failed(session, mid)
        else:
            raise MailerError("not_found", 404)
        await session.commit()
    return _ok({"count": result})


async def stats(request: web.Request) -> web.Response:
    mid = _mid(request)
    async with _factory(request)() as session:
        settings = await load_settings(session)
        await _mailing(session, mid)
        data = await reports.mailing_stats(session, mid, rate=int(settings["rate_per_second"]))
    return _ok({"stats": data})


async def recipients_log(request: web.Request) -> web.Response:
    mid = _mid(request)
    limit = max(1, min(int(request.query.get("limit") or 50), 200))
    offset = max(0, int(request.query.get("offset") or 0))
    async with _factory(request)() as session:
        await _mailing(session, mid)
        page = await reports.recipient_page(session, mid, status=request.query.get("status") or "", query=request.query.get("q") or "", limit=limit, offset=offset)
    return _ok(page)


async def export(request: web.Request) -> web.Response:
    mid = _mid(request)
    async with _factory(request)() as session:
        await _mailing(session, mid)
        rows = await reports.export_rows(session, mid)
    return _ok({"rows": rows})


# ------------------------------------------------------------------------- saved lists


async def _list_ids_from(session: Any, body: dict[str, Any]) -> list[int]:
    result = await rc.parse_manual(session, str(body.get("text") or ""))
    return sorted(result["ids"])


async def create_list(request: web.Request) -> web.Response:
    body = await _body(request)
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise MailerError("name_required")
    async with _factory(request)() as session:
        ids = await _list_ids_from(session, body)
        new_id = await session.scalar(
            text("insert into ext_kiro_mailer_lists (name, user_ids) values (:n, cast(:u as bigint[])) returning id"), {"n": name, "u": ids}
        )
        await session.commit()
    return _ok({"id": new_id, "count": len(ids)})


async def update_list(request: web.Request) -> web.Response:
    body = await _body(request)
    lid = int(request.match_info["lid"])
    async with _factory(request)() as session:
        ids = await _list_ids_from(session, body)
        result = await session.execute(
            text("update ext_kiro_mailer_lists set name = coalesce(nullif(:n, ''), name), user_ids = cast(:u as bigint[]), updated_at = now() where id = :i"),
            {"n": str(body.get("name") or "").strip()[:80], "u": ids, "i": lid},
        )
        if not result.rowcount:
            raise MailerError("not_found", 404)
        await session.commit()
    return _ok({"count": len(ids)})


async def get_list(request: web.Request) -> web.Response:
    lid = int(request.match_info["lid"])
    async with _factory(request)() as session:
        row = (await session.execute(text("select id, name, user_ids from ext_kiro_mailer_lists where id = :i"), {"i": lid})).mappings().first()
    if row is None:
        raise MailerError("not_found", 404)
    return _ok({"list": {"id": row["id"], "name": row["name"], "text": "\n".join(str(u) for u in row["user_ids"])}})


async def delete_list(request: web.Request) -> web.Response:
    lid = int(request.match_info["lid"])
    async with _factory(request)() as session:
        await session.execute(text("delete from ext_kiro_mailer_lists where id = :i"), {"i": lid})
        await session.commit()
    return _ok()


# ------------------------------------------------------------------------- media (uploaded in chunks)


def _magic_ok(kind: str, head: bytes) -> bool:
    if kind == "photo":
        return head.startswith((b"\xff\xd8\xff", b"\x89PNG")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    if kind in ("video", "animation"):
        return head[4:8] == b"ftyp" or head[:4] == b"\x1aE\xdf\xa3" or head[:3] == b"GIF"
    return True


async def media_init(request: web.Request) -> web.Response:
    body = await _body(request)
    kind = str(body.get("kind") or "")
    if kind not in ct.MEDIA_KINDS:
        raise MailerError("invalid_media")
    try:
        size = int(body.get("size") or 0)
    except (TypeError, ValueError):
        raise MailerError("invalid_media") from None
    if size <= 0:
        raise MailerError("invalid_media")
    if size > MEDIA_LIMITS[kind]:
        raise MailerError("media_too_large")
    media_id = secrets.token_hex(16)
    async with _factory(request)() as session:
        await session.execute(
            text("insert into ext_kiro_mailer_media (id, kind, filename, content_type, expected_size) values (:i, :k, :f, :c, :s)"),
            {"i": media_id, "k": kind, "f": str(body.get("filename") or "")[:120], "c": str(body.get("content_type") or "")[:80], "s": size},
        )
        await session.commit()
    return _ok({"id": media_id, "chunk": MAX_CHUNK})


async def media_chunk(request: web.Request) -> web.Response:
    media_id = request.match_info["mediaid"]
    try:
        offset = int(request.headers.get("X-Offset", "-1"))
    except ValueError:
        raise MailerError("invalid_chunk") from None
    data = await request.read()
    if not data or len(data) > MAX_CHUNK:
        raise MailerError("invalid_chunk")
    async with _factory(request)() as session:
        result = await session.execute(
            text(
                "update ext_kiro_mailer_media set body = body || :d, size = size + :n "
                "where id = :i and not complete and size = :o and size + :n <= expected_size"
            ),
            {"d": data, "n": len(data), "i": media_id, "o": offset},
        )
        if not result.rowcount:
            raise MailerError("chunk_out_of_order", 409)
        await session.commit()
    return _ok({"received": offset + len(data)})


async def media_finish(request: web.Request) -> web.Response:
    media_id = request.match_info["mediaid"]
    async with _factory(request)() as session:
        row = (await session.execute(text("select kind, size, expected_size, substring(body from 1 for 16) as head from ext_kiro_mailer_media where id = :i and not complete"), {"i": media_id})).mappings().first()
        if row is None:
            raise MailerError("not_found", 404)
        if row["size"] != row["expected_size"]:
            raise MailerError("incomplete_upload")
        if not _magic_ok(row["kind"], bytes(row["head"])):
            await session.execute(text("delete from ext_kiro_mailer_media where id = :i"), {"i": media_id})
            await session.commit()
            raise MailerError("unsupported_file")
        await session.execute(text("update ext_kiro_mailer_media set complete = true where id = :i"), {"i": media_id})
        await session.commit()
    return _ok({"id": media_id, "kind": row["kind"]})


async def media_get(request: web.Request) -> web.Response:
    media_id = request.match_info["mediaid"]
    if not media_id.isalnum():
        raise MailerError("not_found", 404)
    async with _factory(request)() as session:
        row = (await session.execute(text("select content_type, body, kind from ext_kiro_mailer_media where id = :i and complete"), {"i": media_id})).first()
    if row is None:
        raise MailerError("not_found", 404)
    return web.Response(body=bytes(row[1]), content_type=row[0] or "application/octet-stream", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------------- job and bot


async def _tick(op: OperationContext, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = op.runtime
    RUNTIME["ctx"] = ctx
    op.assert_current()
    engine = Engine(ctx.require_session_factory(), CoreHost(ctx))
    result = await engine.tick(budget_seconds=30)
    async with ctx.require_session_factory()() as session:  # abandoned uploads
        await session.execute(text("delete from ext_kiro_mailer_media where not complete and created_at < now() - interval '1 day'"))
        await session.commit()
    return {"at": datetime.now(UTC).isoformat(), **result}


def _poll_router(ctx: PluginContext) -> Router:
    router = Router(name="kiro_mailer")

    @router.poll_answer()
    async def on_poll_answer(answer: PollAnswer) -> None:
        if answer.user is None:
            return
        try:
            async with ctx.require_session_factory()() as session:
                await reports.record_vote(session, answer.poll_id, int(answer.user.id), [int(i) for i in answer.option_ids])
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("kiro-mailer: could not store a poll answer")

    return router


class KiroMailerPlugin(Plugin):
    name = PLUGIN_ID
    version = "0.1.1"
    plugin_api_min_version = 1
    plugin_api_max_version = 1

    def setup(self, ctx: PluginContext) -> None:
        RUNTIME["ctx"] = ctx

    def migrations(self):
        return MIGRATIONS

    def extensions(self, ctx: PluginContext) -> ExtensionContributions:
        return ExtensionContributions(
            jobs=(JobHandler(id="tick", run=_tick, timeout_seconds=55, max_attempts=1, interval_seconds=60),),
        )

    def setup_bot(self, ctx: PluginContext, *, user_root: Router, admin_root: Router) -> None:
        user_root.include_router(_poll_router(ctx))

    def setup_web(self, ctx: PluginContext, app: web.Application, *, scope: str) -> None:
        if scope != WEB_SCOPE_WEBAPP:
            return
        RUNTIME["ctx"] = ctx
        r = app.router
        r.add_get(f"{ADMIN}/meta", _guard(meta))
        r.add_get(f"{ADMIN}/settings", _guard(get_settings))
        r.add_put(f"{ADMIN}/settings", _guard(put_settings))
        r.add_get(f"{ADMIN}/mailings", _guard(list_mailings))
        r.add_post(f"{ADMIN}/mailings", _guard(create_mailing))
        r.add_get(ADMIN + "/mailings/{mid:\\d+}", _guard(get_mailing))
        r.add_put(ADMIN + "/mailings/{mid:\\d+}", _guard(save_mailing))
        r.add_delete(ADMIN + "/mailings/{mid:\\d+}", _guard(delete_mailing))
        r.add_post(ADMIN + "/mailings/{mid:\\d+}/audience", _guard(audience_preview))
        r.add_post(ADMIN + "/mailings/{mid:\\d+}/test", _guard(send_test))
        r.add_post(ADMIN + "/mailings/{mid:\\d+}/send", _guard(send_now))
        r.add_post(ADMIN + "/mailings/{mid:\\d+}/schedule", _guard(schedule))
        r.add_post(ADMIN + "/mailings/{mid:\\d+}/{action:unschedule|pause|resume|cancel|retry-failed}", _guard(control))
        r.add_get(ADMIN + "/mailings/{mid:\\d+}/stats", _guard(stats))
        r.add_get(ADMIN + "/mailings/{mid:\\d+}/recipients", _guard(recipients_log))
        r.add_get(ADMIN + "/mailings/{mid:\\d+}/export", _guard(export))
        r.add_post(f"{ADMIN}/manual", _guard(parse_manual))
        r.add_post(f"{ADMIN}/lists", _guard(create_list))
        r.add_get(ADMIN + "/lists/{lid:\\d+}", _guard(get_list))
        r.add_put(ADMIN + "/lists/{lid:\\d+}", _guard(update_list))
        r.add_delete(ADMIN + "/lists/{lid:\\d+}", _guard(delete_list))
        r.add_post(f"{ADMIN}/media", _guard(media_init))
        r.add_post(ADMIN + "/media/{mediaid}/chunk", _guard(media_chunk))
        r.add_post(ADMIN + "/media/{mediaid}/finish", _guard(media_finish))
        r.add_get(ADMIN + "/media/{mediaid}", _guard(media_get))


plugin = KiroMailerPlugin()
