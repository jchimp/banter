"""Banter server — app factory.

healthz, config, migrations on startup, recording routes, and the Telegram bot as a
lifespan-scoped background task. The bot long-polls rather than taking a webhook —
there's no public URL — and is a no-op when no token is configured.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.api import recordings
from app.config import Settings, get_settings
from app.telegram.bot import run_bot

log = logging.getLogger("banter")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(levelname)s | %(name)s | %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    applied = db.migrate(settings.db_path)
    if applied:
        log.info("migrations applied: %s", applied)
    else:
        log.info("schema up to date")
    bot_task = asyncio.create_task(run_bot(settings))
    app.state.bot_task = bot_task
    try:
        yield
    finally:
        # Cancel and await: `getUpdates` holds a connection open for ~30s, and Docker
        # SIGKILLs ~10s after SIGTERM. Awaiting the cancellation is what stops the
        # shutdown from dangling the task (CLAUDE.md gotcha 2).
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Banter", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(recordings.router)
    # M4: app.include_router(ui.router)
    return app


app = create_app()
