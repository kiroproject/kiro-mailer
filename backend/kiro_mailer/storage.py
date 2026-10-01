"""Tables and settings for the mailer. Every table is plugin-owned (ext_kiro_mailer_*)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from db.migrator.engine import Migration

PLUGIN_ID = "kiro-mailer"

MAILING_STATUSES = ("draft", "scheduled", "sending", "paused", "done", "cancelled")
RECIPIENT_STATUSES = ("pending", "sent", "failed", "blocked", "skipped")

DEFAULT_SETTINGS: dict[str, Any] = {
    # Offset from UTC in hours: how admins enter the schedule time (MSK = 3).
    "tz_offset_hours": 3,
    # Telegram allows about 30 messages per second overall; stay well below.
    "rate_per_second": 20,
    # A failed delivery is retried this many times in total, `retry_minutes` apart.
    "max_attempts": 3,
    "retry_minutes": 5,
}
SETTING_BOUNDS = {
    "tz_offset_hours": (-12, 14),
    "rate_per_second": (1, 28),
    "max_attempts": (1, 10),
    "retry_minutes": (1, 1440),
}


def _upgrade_0001(connection: Connection) -> None:
    for statement in (
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_mailer_settings (
            id INTEGER PRIMARY KEY,
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_mailer_media (
            id VARCHAR(40) PRIMARY KEY,
            kind VARCHAR(12) NOT NULL,
            filename VARCHAR(120) NOT NULL DEFAULT '',
            content_type VARCHAR(80) NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            expected_size INTEGER NOT NULL DEFAULT 0,
            body BYTEA NOT NULL DEFAULT ''::bytea,
            complete BOOLEAN NOT NULL DEFAULT FALSE,
            tg_file_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_mailer_lists (
            id SERIAL PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            user_ids BIGINT[] NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_mailer_mailings (
            id SERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'draft',
            content JSONB NOT NULL DEFAULT '{}'::jsonb,
            audience JSONB NOT NULL DEFAULT '{}'::jsonb,
            scheduled_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            recipient_total INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_mailer_mailings_status ON ext_kiro_mailer_mailings (status, scheduled_at)",
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_mailer_recipients (
            id BIGSERIAL PRIMARY KEY,
            mailing_id INTEGER NOT NULL REFERENCES ext_kiro_mailer_mailings(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT 'pending',
            step SMALLINT NOT NULL DEFAULT 0,
            attempts SMALLINT NOT NULL DEFAULT 0,
            error VARCHAR(240),
            message_id BIGINT,
            poll_id VARCHAR(40),
            next_try_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent_at TIMESTAMPTZ,
            CONSTRAINT uq_ext_kiro_mailer_recipient UNIQUE (mailing_id, user_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_mailer_rcp_due ON ext_kiro_mailer_recipients (mailing_id, status, next_try_at)",
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_mailer_rcp_poll ON ext_kiro_mailer_recipients (poll_id)",
        """
        CREATE TABLE IF NOT EXISTS ext_kiro_mailer_votes (
            poll_id VARCHAR(40) NOT NULL,
            user_id BIGINT NOT NULL,
            mailing_id INTEGER NOT NULL,
            option_ids INTEGER[] NOT NULL DEFAULT '{}',
            voted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (poll_id, user_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_ext_kiro_mailer_votes_mailing ON ext_kiro_mailer_votes (mailing_id)",
    ):
        connection.exec_driver_sql(statement)


MIGRATIONS = [
    Migration(
        id=f"{PLUGIN_ID}.0001_initial",
        description="Mailer: settings, media, lists, mailings, recipients, poll votes",
        upgrade=_upgrade_0001,
    ),
]


def clean_settings(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    data = dict(current)
    for key, (lo, hi) in SETTING_BOUNDS.items():
        if key in body:
            try:
                number = int(float(body[key]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid_{key}") from exc
            if not lo <= number <= hi:
                raise ValueError(f"invalid_{key}")
            data[key] = number
    return data


async def load_settings(session: AsyncSession) -> dict[str, Any]:
    raw = await session.scalar(text("select data from ext_kiro_mailer_settings where id = 1"))
    data = dict(DEFAULT_SETTINGS)
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    return data


async def save_settings(session: AsyncSession, data: dict[str, Any]) -> None:
    await session.execute(
        text(
            "insert into ext_kiro_mailer_settings (id, data, updated_at) values (1, cast(:d as jsonb), now()) "
            "on conflict (id) do update set data = excluded.data, updated_at = now()"
        ),
        {"d": json.dumps(data, ensure_ascii=False)},
    )
