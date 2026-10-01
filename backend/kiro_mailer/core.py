"""Bridge between the engine and Minishop Core: Telegram delivery and Core's audience groups.

Buttons and the HTML check are Core's own broadcast helpers, so a mailing validates and renders its
buttons exactly like the built-in broadcast.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaDocument, InputMediaPhoto, InputMediaVideo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.plugins.spec import PluginContext
from bot.services.broadcast_personalization import telegram_html_error
from bot.services.message_composition import (
    MessageButtonInput,
    MessageValidationError,
    resolve_message_buttons,
    telegram_markup_for_buttons,
)
from bot.services.telegram_notifications import (
    mark_telegram_notifications_status,
    telegram_notification_status_from_error,
)

from .engine import SendResult

logger = logging.getLogger(__name__)

__all__ = ["CoreHost", "html_error"]


def html_error(body: str) -> str | None:
    return telegram_html_error(body)


class CoreHost:
    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._username: str | None = None

    # ------------------------------------------------------------------ Core audiences

    async def core_ids(self, target: str) -> list[int]:
        service = self.ctx.audience_segmentation_service
        if service is None:
            return []
        return [int(i) for i in await service.resolve_user_ids(target)]

    def core_groups(self) -> list[dict[str, str]]:
        """Audiences offered by Core and other plugins, with readable labels."""
        service = self.ctx.audience_segmentation_service
        if service is None:
            return []
        out = []
        for a in service.audiences():
            if not a.available or a.target in ("all",) or a.target.startswith("user:"):
                continue
            label = a.fallback_label
            if self.ctx.i18n is not None:
                try:
                    translated = self.ctx.i18n.gettext("ru", a.label_key)
                    if translated and translated != a.label_key:
                        label = translated
                except Exception:  # noqa: BLE001
                    pass
            out.append({"target": a.target, "label": label, "group": a.group_fallback_label or ""})
        return out

    # ------------------------------------------------------------------ helpers

    async def _bot_username(self) -> str:
        if self._username is None:
            try:
                self._username = (await self.ctx.require_bot().get_me()).username or ""
            except Exception:  # noqa: BLE001
                logger.warning("kiro-mailer: could not read the bot username")
                self._username = ""
        return self._username

    async def _markup(self, session: AsyncSession, user_id: int, buttons: list[dict[str, str]]):
        if not buttons:
            return None
        settings = self.ctx.settings
        language = await session.scalar(text("select language_code from users where user_id = :u"), {"u": user_id}) or ""
        specs = [
            MessageButtonInput(
                kind=b["kind"], label=b["label"], url=b["url"], promo_code="",
                section=b["section"] if b["kind"] == "webapp_section" else "",
            )
            for b in buttons
        ]
        resolved = resolve_message_buttons(
            specs,
            mini_app_url=settings.SUBSCRIPTION_MINI_APP_URL,
            bot_username=await self._bot_username(),
            language=str(language).lower() or None,
            translate=(lambda code, key: self.ctx.i18n.gettext(code, key)) if self.ctx.i18n else None,
            default_language=settings.DEFAULT_LANGUAGE,
        )
        return telegram_markup_for_buttons(resolved)

    async def _file(self, session: AsyncSession, media_id: str) -> tuple[Any, str] | None:
        row = (
            await session.execute(
                text("select kind, filename, body, tg_file_id from ext_kiro_mailer_media where id = :i and complete"), {"i": media_id}
            )
        ).first()
        if row is None:
            return None
        if row[3]:
            return row[3], row[0]
        return BufferedInputFile(bytes(row[2]), filename=row[1] or f"{media_id}"), row[0]

    async def _remember(self, session: AsyncSession, media_id: str, file_id: str | None) -> None:
        if file_id:
            await session.execute(
                text("update ext_kiro_mailer_media set tg_file_id = :f where id = :i and tg_file_id is null"), {"f": file_id, "i": media_id}
            )

    @staticmethod
    def _file_id(message: Any, kind: str) -> str | None:
        try:
            if kind == "photo":
                return message.photo[-1].file_id
            return getattr(message, kind).file_id
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ delivery

    async def send_part(
        self, session: AsyncSession, *, chat_id: int, user_id: int, part: dict[str, Any], content: dict[str, Any]
    ) -> SendResult:
        bot = self.ctx.require_bot()
        common = {"disable_notification": bool(content["silent"]), "protect_content": bool(content["protect"])}
        try:
            if part["type"] == "text":
                sent = await bot.send_message(
                    chat_id, part["text"], parse_mode="HTML", disable_web_page_preview=bool(content["disable_preview"]),
                    reply_markup=await self._markup(session, user_id, part["buttons"]), **common,
                )
                return SendResult("sent", message_id=sent.message_id)
            if part["type"] == "media":
                return await self._send_media(session, chat_id, user_id, part, common)
            poll = content["poll"]
            close = datetime.now(UTC) + timedelta(hours=poll["close_hours"]) if poll["close_hours"] else None
            sent = await bot.send_poll(
                chat_id,
                question=poll["question"],
                options=poll["options"],
                is_anonymous=False,
                type="quiz" if poll["quiz"] else "regular",
                allows_multiple_answers=poll["multiple"],
                correct_option_id=poll["correct"] if poll["quiz"] else None,
                explanation=poll["explanation"] or None,
                close_date=close,
                **common,
            )
            return SendResult("sent", message_id=sent.message_id, poll_id=sent.poll.id if sent.poll else None)
        except MessageValidationError as exc:
            return SendResult("failed", f"content:{exc.code}")
        except TelegramRetryAfter as exc:
            return SendResult("retry_after", f"retry_after:{exc.retry_after}", retry_after=int(exc.retry_after))
        except TelegramNetworkError as exc:
            return SendResult("failed", f"network:{str(exc)[:160]}")
        except Exception as exc:  # noqa: BLE001
            status = telegram_notification_status_from_error(exc)
            if status:
                await mark_telegram_notifications_status(session, user_id, status)
                return SendResult("blocked", str(exc)[:200])
            logger.warning("kiro-mailer: send to %s failed: %s", user_id, exc)
            return SendResult("failed", str(exc)[:200])

    async def _send_media(self, session: AsyncSession, chat_id: int, user_id: int, part: dict[str, Any], common: dict[str, Any]) -> SendResult:
        bot = self.ctx.require_bot()
        items = part["items"]
        files = []
        for item in items:
            found = await self._file(session, item["id"])
            if found is None:
                return SendResult("failed", "media_missing")
            files.append(found)
        caption = part["caption"] or None
        if len(items) == 1:
            file, kind = files[0]
            markup = await self._markup(session, user_id, part["buttons"])
            kwargs = {"caption": caption, "parse_mode": "HTML" if caption else None, "reply_markup": markup, **common}
            sender = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation, "document": bot.send_document}[kind]
            sent = await sender(chat_id, file, **kwargs)
            await self._remember(session, items[0]["id"], self._file_id(sent, kind))
            return SendResult("sent", message_id=sent.message_id)
        group = []
        for index, (file, kind) in enumerate(files):
            extra = {"caption": caption, "parse_mode": "HTML"} if index == 0 and caption else {}
            cls = {"photo": InputMediaPhoto, "video": InputMediaVideo, "document": InputMediaDocument}[kind]
            group.append(cls(media=file, **extra))
        messages = await bot.send_media_group(chat_id, media=group, **common)
        for item, message, (_, kind) in zip(items, messages, files, strict=False):
            await self._remember(session, item["id"], self._file_id(message, kind))
        return SendResult("sent", message_id=messages[0].message_id if messages else None)

    async def send_test(self, session: AsyncSession, *, chat_id: int, user_id: int, parts: list[dict[str, Any]], content: dict[str, Any]) -> SendResult:
        """Deliver every part of a mailing to one chat (the admin) without touching recipients."""
        last = SendResult("sent")
        for part in parts:
            last = await self.send_part(session, chat_id=chat_id, user_id=user_id, part=part, content=content)
            if last.status != "sent":
                return last
        return last
