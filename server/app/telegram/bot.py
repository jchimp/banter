"""The bot task: long-poll `getUpdates` and run the parent notifier alongside it.

Attached to the FastAPI lifespan (`app/main.py`), not a separate process — there is no
public URL for a webhook, so polling is the only option (PRD 3.4). Both loops live under
one `asyncio.TaskGroup` so the lifespan has a single handle to cancel; cancelling the
outer task tears down both and closes the HTTP client.

Cancellation matters more than it looks: `getUpdates` holds a connection open for up to
`telegram_poll_timeout_s`, and Docker only allows ~10s between SIGTERM and SIGKILL. Every
`except` in this module catches `Exception`, never `BaseException` and never bare, so
`asyncio.CancelledError` propagates immediately instead of being swallowed into the next
retry sleep (CLAUDE.md gotcha 2 — a swallowed cancel is how shutdowns hang).
"""

import asyncio
import logging

from app.config import Settings
from app.telegram.client import TelegramClient
from app.telegram.handlers import BotContext, handle_update
from app.telegram.notify import notify_loop

log = logging.getLogger("banter.telegram.bot")

_BACKOFF_START = 1.0
_BACKOFF_CAP = 30.0


def backoff_delay(attempt: int, start: float = _BACKOFF_START, cap: float = _BACKOFF_CAP) -> float:
    """Exponential backoff for a 0-indexed attempt, capped.

    Pure — no sleeping, no jitter — so the growth curve is asserted directly in tests,
    matching the client-side uploader's helper.
    """
    return min(start * (2 ** max(attempt, 0)), cap)


async def poll_loop(ctx: BotContext) -> None:
    """Long-poll for updates and dispatch each one.

    The offset is held in memory only. Telegram redelivers anything not acknowledged, so
    a restart can replay the last update — that's fine, and deliberately not solved with
    a persisted offset: `handle_voice` derives its recording id from the voice note's
    `file_unique_id` and returns early if that row already exists, so a replay is a no-op
    with no duplicate row and no duplicate reply. Idempotency, not bookkeeping.
    """
    offset: int | None = None
    attempt = 0

    while True:
        try:
            updates = await ctx.client.get_updates(
                offset, timeout=ctx.settings.telegram_poll_timeout_s
            )
        except Exception:
            log.exception("bot | event=poll_failed | attempt=%d", attempt)
            await asyncio.sleep(backoff_delay(attempt))
            attempt += 1
            continue

        attempt = 0
        for update in updates:
            try:
                await handle_update(update, ctx)
            except Exception:
                # One malformed update must not kill the loop and strand the bot —
                # the M2 play-worker crash taught this the hard way. Acknowledge it
                # anyway (below) so it isn't redelivered forever.
                log.exception("bot | event=handler_failed | update_id=%s", update.get("update_id"))
            # Advance only after handling, so a crash mid-update replays it rather
            # than silently dropping it.
            offset = update["update_id"] + 1


async def run_bot(settings: Settings) -> None:
    """Run the Telegram bot until cancelled. No-op when no token is configured.

    A blank `TELEGRAM_BOT_TOKEN` disables the bot entirely — the same convention as a
    blank `API_KEY` disabling auth, and what `.env.example` documents. The rest of the
    server runs normally.
    """
    if not settings.telegram_bot_token:
        log.info("bot | event=disabled | reason=no_token")
        return

    client = TelegramClient(settings.telegram_bot_token)
    ctx = BotContext(settings=settings, client=client)
    log.info(
        "bot | event=started | parents=%s",
        ",".join(settings.configured_parent_roles()) or "none",
    )
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(poll_loop(ctx))
            tg.create_task(notify_loop(ctx))
    finally:
        await client.aclose()
        log.info("bot | event=stopped")
