"""Parent-notification loop: pushes new kidbox recordings to Telegram (FR-15/PRD 3.4).

Runs as a background pass driven by the bot's lifespan task, independent of the
`getUpdates` polling loop in `bot.py`/`handlers.py`. Each pass is intentionally
shaped as "read from the DB, then talk to the network, then write to the DB" in
that order and never interleaved — a `sendVoice` round trip can take seconds, and
CLAUDE.md gotcha 8 ("SQLite + concurrent writers") plus `db.py`'s own docstring
("keep these short") both rule out holding a transaction open across it. Holding
the write lock during a network call would block the upload route the whole time.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

from app import db, store
from app.audio import parse_iso8601
from app.telegram.handlers import BotContext, send_recording

log = logging.getLogger("banter.telegram.notify")

# Column each parent role's delivery flag lives in. Mirrors store._NOTIFIED_COLUMN,
# which is private to that module — duplicated here rather than exported since this
# is the only other place that needs it.
_FLAG_COLUMN = {"mom": "notified_mom", "dad": "notified_dad"}

# Small, deterministic backoff between retries of a single send within one pass.
# Shaped like banter_client.uploader.backoff_delay (start, double, cap) but kept
# local: this is a per-attempt in-pass retry, not the client's cross-pass queue
# backoff, and the server has no reason to import from the client package.
_RETRY_BACKOFF_START = 0.5
_RETRY_BACKOFF_CAP = 5.0


def _retry_delay(attempt: int) -> float:
    """Exponential backoff for a 0-indexed retry attempt, capped."""
    return min(_RETRY_BACKOFF_START * (2 ** max(attempt, 0)), _RETRY_BACKOFF_CAP)


def _format_duration(duration_ms: int | None) -> str:
    """`Ns` under a minute, `M:SS` at or above — short enough for a one-line caption."""
    if not duration_ms or duration_ms < 0:
        return "0s"
    total_seconds = round(duration_ms / 1000)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def caption_for(row: sqlite3.Row) -> str:
    """One-line caption for a parent's voice note: readable timestamp + duration (FR-15).

    `created_at` is stored as ISO8601 UTC; formatted by hand (not `%-d`) because that
    strftime extension isn't portable to Windows dev machines, and the server's own
    stack is otherwise platform-agnostic.
    """
    created_at = parse_iso8601(row["created_at"])
    when = f"{created_at:%b} {created_at.day}, {created_at:%H:%M} UTC"
    duration = _format_duration(row["duration_ms"])
    return f"{when} · {duration}"


async def _send_with_retries(
    ctx: BotContext,
    *,
    chat_id: str,
    row: dict,
    role: str,
    caption: str,
    retries: int,
) -> str | None:
    """Attempt one role's delivery, retrying transient failures within this pass.

    `send_recording` already absorbs a `TelegramError`/`TranscodeError`/missing-file
    case internally (it logs and returns `None` rather than raising — see its
    docstring: "callers should not let one bad row abort a whole /joke batch"), so
    `None` is the failure signal this function retries on. A stray `OSError` that
    somehow escapes it (e.g. a permission error reading the WAV, distinct from the
    missing-file case `send_recording` already checks) is caught defensively so it
    can't propagate up and stall the rest of the pass either.

    Returns the Telegram `file_id` on success, or `None` once retries are exhausted
    (already logged) — the caller leaves the role's flag at 0 so the next
    `notify_once` pass retries from scratch.
    """
    attempt = 0
    while True:
        try:
            file_id = await send_recording(chat_id, row, ctx, caption=caption)
        except OSError as exc:
            log.warning(
                "notify | event=send_error | role=%s | rec_id=%s | attempt=%d | error=%s",
                role,
                row["id"],
                attempt,
                exc.__class__.__name__,
            )
            file_id = None

        if file_id is not None:
            return file_id

        log.warning(
            "notify | event=send_failed | role=%s | rec_id=%s | attempt=%d",
            role,
            row["id"],
            attempt,
        )
        attempt += 1
        if attempt > retries:
            return None
        await asyncio.sleep(_retry_delay(attempt - 1))


def _mark_notified(db_path: Path, rec_id: str, role: str, file_id: str) -> None:
    """Persist one role's delivery in its own short transaction — no network call inside."""
    with db.session(db_path) as conn, db.transaction(conn):
        store.mark_notified(conn, rec_id, role, file_id=file_id)


def _finalize(db_path: Path, rec_id: str, roles: list[str]) -> None:
    """Flip `notified` once every configured role's flag is set, in its own short transaction."""
    with db.session(db_path) as conn, db.transaction(conn):
        store.finalize_notified(conn, rec_id, roles)


async def notify_once(ctx: BotContext) -> int:
    """Run one pass over pending kidbox recordings. Returns the count of sends that succeeded.

    Structured deliberately in three phases that never overlap: read pending rows (DB),
    send to Telegram (network, no DB handle open), then persist results (DB). See the
    module docstring for why the network phase must never happen inside a transaction.
    """
    settings = ctx.settings
    roles = settings.configured_parent_roles()
    if not roles:
        return 0

    with db.session(settings.db_path) as conn:
        rows = store.pending_notifications(conn, limit=20)
    # `conn` is closed here — everything below this line is network + short, separate
    # writes. No `db.transaction()` block may wrap a `send_recording` call.

    sent = 0
    for row in rows:
        rec_id = row["id"]
        caption = caption_for(row)
        # Mutable working copy so a file_id returned by the first role's send is
        # visible to the second role's send without re-reading the DB (the row this
        # loop is holding is a stale snapshot from before any write this pass).
        row_data = dict(row)

        for role in roles:
            if row_data[_FLAG_COLUMN[role]]:
                continue  # already delivered to this role on a prior pass

            file_id = await _send_with_retries(
                ctx,
                chat_id=settings.chat_id_for(role),
                row=row_data,
                role=role,
                caption=caption,
                retries=settings.telegram_send_retries,
            )
            if file_id is None:
                continue  # logged already; flag stays 0, next pass retries

            sent += 1
            row_data["telegram_file_id"] = file_id
            _mark_notified(settings.db_path, rec_id, role, file_id)

        _finalize(settings.db_path, rec_id, roles)

    return sent


async def notify_loop(ctx: BotContext) -> None:
    """Run `notify_once` forever on `telegram_notify_interval_s` cadence.

    An unexpected exception from a single pass is logged and swallowed so the loop
    keeps running on the next tick — the M2 lesson (CLAUDE.md) was a crashed worker
    silently stranding the box; this must not repeat here. `asyncio.CancelledError`
    is deliberately not caught: it's how the lifespan shuts this task down cleanly.
    """
    while True:
        try:
            await notify_once(ctx)
        except Exception:
            log.exception("notify | event=pass_failed")
        await asyncio.sleep(ctx.settings.telegram_notify_interval_s)
