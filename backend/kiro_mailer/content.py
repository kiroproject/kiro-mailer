"""Mailing content and audience: normalisation and validation against Telegram's rules.

A mailing is delivered as a short sequence of parts (see `plan_parts`): optional text/media, then an
optional poll. The recipient row remembers how many parts were already delivered, so a retry after a
partial failure never repeats a message that arrived.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .audience import clean_filter

MAX_TEXT = 4096
MAX_CAPTION = 1024
MAX_MEDIA = 10
MEDIA_KINDS = ("photo", "video", "animation", "document")
BUTTON_KINDS = ("url", "webapp_section")
SECTIONS = ("home", "plans", "install", "trial", "invite", "devices", "support", "settings")
POLL_QUESTION_MAX = 300
POLL_OPTION_MAX = 100
POLL_OPTIONS = (2, 10)
POLL_EXPLANATION_MAX = 200
MAX_CLOSE_HOURS = 24 * 30
_ID = re.compile(r"^[A-Za-z0-9]{1,40}$")

BUILTIN_GROUPS: dict[str, dict[str, Any]] = {
    "all": {"label": "Все пользователи", "filter": {}},
    "active": {"label": "С активной подпиской", "filter": {"sub_state": "active"}},
    "expiring7": {"label": "Подписка кончается за 7 дней", "filter": {"sub_state": "active", "days_left_max": 7}},
    "expired": {"label": "Подписка истекла", "filter": {"sub_state": "expired"}},
    "expired30": {"label": "Истекла 30 дней назад и больше", "filter": {"sub_state": "expired", "expired_days_min": 30}},
    "never": {"label": "Подписки никогда не было", "filter": {"sub_state": "never"}},
    "trial_used": {"label": "Использовали пробный период", "filter": {"trial": "used"}},
    "paid": {"label": "Платили хотя бы раз", "filter": {"paid": "yes"}},
    "unpaid": {"label": "Никогда не платили", "filter": {"paid": "no"}},
}


class ContentError(ValueError):
    pass


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def clean_content(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    media: list[dict[str, str]] = []
    for item in (raw.get("media") or [])[:MAX_MEDIA]:
        if not isinstance(item, dict):
            continue
        mid, kind = str(item.get("id") or ""), str(item.get("kind") or "")
        if not _ID.match(mid) or kind not in MEDIA_KINDS:
            raise ContentError("invalid_media")
        media.append({"id": mid, "kind": kind})
    buttons: list[dict[str, str]] = []
    for item in (raw.get("buttons") or [])[:4]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "url")
        if kind not in BUTTON_KINDS:
            raise ContentError("invalid_button_kind")
        section = str(item.get("section") or "home")
        buttons.append({
            "kind": kind,
            "label": _text(item.get("label"), 64),
            "url": _text(item.get("url"), 512),
            "section": section if section in SECTIONS else "home",
        })
    poll = None
    rp = raw.get("poll")
    if isinstance(rp, dict) and (rp.get("question") or rp.get("options")):
        options = [_text(o, POLL_OPTION_MAX) for o in (rp.get("options") or []) if _text(o, POLL_OPTION_MAX)][: POLL_OPTIONS[1]]
        quiz = bool(rp.get("quiz"))
        try:
            correct = int(rp.get("correct", 0))
        except (TypeError, ValueError):
            correct = 0
        try:
            close_hours = max(0, min(int(rp.get("close_hours") or 0), MAX_CLOSE_HOURS))
        except (TypeError, ValueError):
            close_hours = 0
        poll = {
            "question": _text(rp.get("question"), POLL_QUESTION_MAX),
            "options": options,
            "quiz": quiz,
            "multiple": bool(rp.get("multiple")) and not quiz,
            "correct": correct if 0 <= correct < max(1, len(options)) else 0,
            "explanation": _text(rp.get("explanation"), POLL_EXPLANATION_MAX) if quiz else "",
            "close_hours": close_hours,
        }
    return {
        "text": str(raw.get("text") or "").strip()[:MAX_TEXT],
        "media": media,
        "buttons": buttons,
        "poll": poll,
        "disable_preview": bool(raw.get("disable_preview", True)),
        "silent": bool(raw.get("silent")),
        "protect": bool(raw.get("protect")),
    }


def plan_parts(content: dict[str, Any]) -> list[dict[str, Any]]:
    """The ordered messages one recipient receives."""
    text, media, buttons, poll = content["text"], content["media"], content["buttons"], content["poll"]
    parts: list[dict[str, Any]] = []
    if media:
        if text and len(text) > MAX_CAPTION:
            parts.append({"type": "text", "text": text, "buttons": buttons})
            parts.append({"type": "media", "items": media, "caption": "", "buttons": []})
        else:
            parts.append({"type": "media", "items": media, "caption": text, "buttons": buttons if len(media) == 1 else []})
    elif text:
        parts.append({"type": "text", "text": text, "buttons": buttons})
    if poll:
        parts.append({"type": "poll"})
    return parts


def validate_content(content: dict[str, Any], *, html_error: Callable[[str], str | None] | None = None) -> list[str]:
    errors: list[str] = []
    text, media, buttons, poll = content["text"], content["media"], content["buttons"], content["poll"]
    if not text and not media and not poll:
        errors.append("Добавьте текст, медиа или опрос")
    if html_error and text:
        problem = html_error(text)
        if problem:
            errors.append(f"Ошибка разметки текста: {problem}")
    if len(media) > 1:
        kinds = {m["kind"] for m in media}
        if "animation" in kinds:
            errors.append("GIF-анимацию нельзя отправлять в альбоме: уберите её или оставьте только её")
        elif "document" in kinds and kinds != {"document"}:
            errors.append("Документы можно объединять в альбом только с другими документами")
        if buttons:
            errors.append("К альбому нельзя прикрепить кнопки: оставьте одно медиа или уберите кнопки")
    for b in buttons:
        if b["kind"] == "url":
            if not b["label"]:
                errors.append("У кнопки-ссылки нужна подпись")
            if not b["url"].startswith(("https://", "tg://")):
                errors.append("Ссылка кнопки должна начинаться с https://")
    if poll:
        if not poll["question"]:
            errors.append("Задайте вопрос опроса")
        if len(poll["options"]) < POLL_OPTIONS[0]:
            errors.append(f"В опросе нужно минимум {POLL_OPTIONS[0]} варианта")
        if len(set(poll["options"])) != len(poll["options"]):
            errors.append("Варианты опроса не должны повторяться")
    return errors


def clean_audience(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    ex = raw.get("exclude") if isinstance(raw.get("exclude"), dict) else {}

    def ints(values: Any, limit: int = 200) -> list[int]:
        out = []
        for v in values if isinstance(values, list) else []:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out[:limit]

    def tags(values: Any, pattern: str = r"^[A-Za-z0-9_.:-]{1,60}$", limit: int = 100) -> list[str]:
        return [str(v) for v in (values if isinstance(values, list) else []) if re.match(pattern, str(v))][:limit]

    return {
        "groups": [g for g in tags(raw.get("groups")) if g in BUILTIN_GROUPS],
        "tariffs": tags(raw.get("tariffs")),
        "filter": clean_filter(raw.get("filter")),
        "core_groups": tags(raw.get("core_groups"), r"^[A-Za-z0-9_.:@-]{1,80}$"),
        "lists": ints(raw.get("lists")),
        "manual": str(raw.get("manual") or "")[:200_000],
        "exclude": {
            "manual": str(ex.get("manual") or "")[:200_000],
            "lists": ints(ex.get("lists")),
            "mailings": ints(ex.get("mailings")),
        },
    }
