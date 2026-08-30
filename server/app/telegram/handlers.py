"""Telegram update dispatch and command handlers (FR-16..FR-20).

`handle_update` is the allowlist choke point: every inbound update — voice notes,
`/joke`, `/stats`, anything else — passes through `Settings.source_for_chat` first.
An unknown chat id returns before any reply is sent or any row is written. Keeping
that check as the first real act of `handle_update` (rather than duplicated in each
sub-handler) is what makes "unknown chat produces no reply and no DB write"
structurally true instead of merely tested.

`send_recording` lives here (not in `notify.py`) because both `handle_joke` and the
M3-step-5 notifier need to turn a `recordings` row into an outbound Telegram voice
message, and the notifier is the one importing from handlers, not the reverse.
"""

import asyncio
import hashlib
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app import store, transcode
from app.audio import InvalidAudioError, audio_path, probe_wav
from app.config import Settings
from app.db import session, transaction
from app.telegram.client import TelegramClient, TelegramError

log = logging.getLogger("banter.telegram.handlers")

#: Telegram appends "@botname" to commands sent in groups (`/joke@banterbot`); strip
#: it before matching so the bot works the same in a DM or a group chat.
_COMMAND_RE = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)

#: Matches the upload route's id shape (`app/api/recordings.py`) so a telegram-origin
#: id is accepted anywhere a kidbox-origin id would be.
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,64}$")

_DEFAULT_JOKE_COUNT = 1
_MAX_JOKE_COUNT = 5
_MIN_JOKE_COUNT = 1


@dataclass
class BotContext:
    """Everything a handler needs, bundled so tests can inject fakes.

    `ogg_to_wav`/`wav_to_ogg` default to the real ffmpeg wrappers but are swappable in
    tests for canned-bytes fakes — keeps the handler tests independent of ffmpeg being
    on PATH. `wav_to_ogg` isn't in the prompt's original signature; it's added here
    for the same reason `ogg_to_wav` is: `send_recording`'s outbound path needs it and
    a real subprocess call has no place in a unit test.
    """

    settings: Settings
    client: TelegramClient
    ogg_to_wav: Callable[[bytes], bytes] = field(default=transcode.ogg_to_wav)
    wav_to_ogg: Callable[[bytes], bytes] = field(default=transcode.wav_to_ogg)


async def handle_update(update: dict, ctx: BotContext) -> None:
    """Route one Telegram update, after the allowlist gate.

    Args:
        update: A single element of `TelegramClient.get_updates()`'s result list.
        ctx: Shared settings/client/transcode dependencies.

    The allowlist check is deliberately the first thing this function does after
    extracting `chat_id`, before any other branch — an unknown chat must hit this
    `return` and nothing else, ever (FR-19).
    """
    message = update.get("message") or update.get("edited_message")
    if message is None:
        return

    chat_id = str(message["chat"]["id"])
    source = ctx.settings.source_for_chat(chat_id)
    if source is None:
        log.debug("handle_update | ignored_unknown_chat")
        return

    if "voice" in message:
        await handle_voice(chat_id, source, message["voice"], ctx)
        return

    text = message.get("text")
    if not text:
        return

    match = _COMMAND_RE.match(text.strip())
    if not match:
        return

    command, arg = match.group(1).lower(), match.group(2)
    if command == "joke":
        await handle_joke(chat_id, arg, ctx)
    elif command == "stats":
        await handle_stats(chat_id, ctx)
    # Any other command/text: ignore silently — the bot doesn't chat back at parents.


async def handle_voice(chat_id: str, source: str, voice: dict, ctx: BotContext) -> None:
    """Ingest one Telegram voice note as a `recordings` row (FR-16, FR-20).

    Idempotent on `file_unique_id`: Telegram redelivers updates after a restart (it
    only advances its own offset once `get_updates` is called with a higher one), so
    a redelivered voice note must not create a second row or send a second
    confirmation.
    """
    rec_id = "tg-" + hashlib.sha1(voice["file_unique_id"].encode()).hexdigest()[:16]
    assert _ID_RE.match(rec_id), f"generated id fails upload id shape: {rec_id!r}"

    settings = ctx.settings
    with session(settings.db_path) as conn:
        if store.get_recording(conn, rec_id) is not None:
            log.info("handle_voice | duplicate_redelivery | source=%s id=%s", source, rec_id)
            return

    try:
        file_info = await ctx.client.get_file(voice["file_id"])
        ogg_bytes = await ctx.client.download_file(file_info["file_path"])
        wav_bytes = await asyncio.to_thread(ctx.ogg_to_wav, ogg_bytes)
    except (TelegramError, transcode.TranscodeError):
        log.exception("handle_voice | ingest_failed | source=%s id=%s", source, rec_id)
        await _reply(ctx, chat_id, "Sorry, that voice note didn't come through. Try again?")
        return

    created_at = datetime.now(UTC)
    ogg_path = audio_path(settings.audio_dir, source, created_at, rec_id).with_suffix(".ogg")
    wav_path = audio_path(settings.audio_dir, source, created_at, rec_id)
    ogg_path.parent.mkdir(parents=True, exist_ok=True)
    ogg_path.write_bytes(ogg_bytes)
    wav_path.write_bytes(wav_bytes)

    try:
        info = probe_wav(wav_path)
        duration_ms = info.duration_ms
        wav_size = info.bytes
    except InvalidAudioError:
        # Server-side probe beats any client-supplied number (M1 precedent), but a
        # transcode that produces a bad WAV shouldn't lose the voice note entirely —
        # fall back to Telegram's whole-second duration rather than dropping the row.
        log.warning("handle_voice | probe_failed_fallback | source=%s id=%s", source, rec_id)
        duration_ms = int(voice.get("duration", 0)) * 1000
        wav_size = len(wav_bytes)

    created_at_str = created_at.isoformat().replace("+00:00", "Z")
    rel_ogg = ogg_path.relative_to(settings.audio_dir).as_posix()
    rel_wav = wav_path.relative_to(settings.audio_dir).as_posix()

    with session(settings.db_path) as conn, transaction(conn):
        store.insert_recording(
            conn,
            id=rec_id,
            source=source,
            origin="telegram",
            path=rel_wav,
            duration_ms=duration_ms,
            bytes=wav_size,
            created_at=created_at_str,
            original_path=rel_ogg,
            telegram_file_id=voice["file_id"],
        )

    log.info("handle_voice | created | source=%s id=%s duration_ms=%d", source, rec_id, duration_ms)
    await _reply(ctx, chat_id, "Got it! Thanks for the joke.")


async def handle_joke(chat_id: str, arg: str | None, ctx: BotContext) -> None:
    """`/joke [n]` — send `n` random kid recordings as voice messages (FR-17).

    Never writes a `plays` row and never touches `play_count`: per PRD open question
    1, a Telegram send and a kidbox box-play are different kinds of "heard it", and
    conflating them would corrupt `select_next`'s no-repeat/cooldown bookkeeping,
    which is meant to model plays on the physical box only.
    """
    n = _parse_joke_count(arg)

    with session(ctx.settings.db_path) as conn:
        rows = store.random_kid_recordings(conn, n)

    if not rows:
        await _reply(ctx, chat_id, "No jokes recorded yet!")
        return

    for row in rows:
        # `/joke` doesn't persist a freshly-minted file_id back onto the row: that
        # caching is `mark_notified`'s job for the kidbox->parent notify path
        # (M3 step 5), and reusing it here would mean writing to the DB from a loop
        # that's also making network calls per iteration. A `/joke` resend of a row
        # that already has no cached file_id just re-transcodes each time — a
        # reasonable cost for an on-demand parent command, not the ~10s-budget path
        # the notifier is optimizing.
        await send_recording(chat_id, row, ctx)


async def handle_stats(chat_id: str, ctx: BotContext) -> None:
    """`/stats` — per-source counts, total duration, and last activity (FR-18)."""
    with session(ctx.settings.db_path) as conn:
        rows = store.source_stats(conn)

    if not rows:
        await _reply(ctx, chat_id, "No recordings yet.")
        return

    lines = []
    total_ms = 0
    total_count = 0
    for row in rows:
        total_ms += row["total_ms"]
        total_count += row["count"]
        lines.append(
            f"{row['source']}: {row['count']} ({_format_duration(row['total_ms'])}), "
            f"last {row['last_at']}"
        )
    lines.append(f"total: {total_count} ({_format_duration(total_ms)})")

    await _reply(ctx, chat_id, "\n".join(lines))


async def send_recording(
    chat_id: str, row: sqlite3.Row, ctx: BotContext, caption: str | None = None
) -> str | None:
    """Send `row` to `chat_id` as a Telegram voice message.

    Reuses `row["telegram_file_id"]` when present — no disk read, no transcode, no
    re-upload (Telegram file ids are stable and free to resend). Otherwise reads the
    WAV off disk and transcodes it to OGG/Opus so it renders as a proper voice
    bubble rather than a generic file.

    Never writes to the DB: the caller owns persistence of the returned `file_id` (a
    fresh one from a bytes-upload send, or the caller's own transaction), so this can
    be called from inside or outside a `db.transaction()` block without risk of a
    network call holding the write lock.

    Returns:
        The Telegram `file_id` for this voice message, or None if the send couldn't
        happen (e.g. the file is missing on disk) or Telegram's response lacked one —
        callers should not let one bad row abort a whole `/joke` batch.
    """
    if row["telegram_file_id"]:
        try:
            result = await ctx.client.send_voice(
                chat_id, voice=row["telegram_file_id"], caption=caption
            )
        except TelegramError:
            log.exception("send_recording | send_failed | id=%s", row["id"])
            return None
        return result.get("voice", {}).get("file_id")

    wav_path = ctx.settings.audio_dir / row["path"]
    if not wav_path.is_file():
        log.warning("send_recording | wav_missing | id=%s path=%s", row["id"], row["path"])
        return None

    wav_bytes = wav_path.read_bytes()
    try:
        ogg_bytes = await asyncio.to_thread(ctx.wav_to_ogg, wav_bytes)
        result = await ctx.client.send_voice(chat_id, voice=ogg_bytes, caption=caption)
    except (transcode.TranscodeError, TelegramError):
        log.exception("send_recording | send_failed | id=%s", row["id"])
        return None

    return result.get("voice", {}).get("file_id")


# -- helpers ------------------------------------------------------------------


async def _reply(ctx: BotContext, chat_id: str, text: str) -> None:
    """Best-effort text reply. A failed reply is logged, never raised into the caller."""
    try:
        await ctx.client.send_message(chat_id, text)
    except TelegramError:
        log.exception("_reply | send_failed")


def _parse_joke_count(arg: str | None) -> int:
    """Parse `/joke [n]`'s argument: default 1, clamp to 1..5, garbage falls back to 1."""
    if arg is None or not arg.strip():
        return _DEFAULT_JOKE_COUNT
    try:
        n = int(arg.strip())
    except ValueError:
        return _DEFAULT_JOKE_COUNT
    return max(_MIN_JOKE_COUNT, min(_MAX_JOKE_COUNT, n))


def _format_duration(total_ms: int) -> str:
    """Render milliseconds as `Mm SSs` for a human-readable `/stats` reply."""
    total_seconds = total_ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m {seconds:02d}s"
