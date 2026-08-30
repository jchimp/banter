"""Banter server — app factory.

healthz, config, migrations on startup, recording routes, and the Telegram bot as a
lifespan-scoped background task. The bot long-polls rather than taking a webhook —
there's no public URL — and is a no-op when no token is configured.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import devices, recordings
from app.config import Settings, get_settings
from app.telegram.bot import run_bot
from app.ui import routes as ui_routes

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
    app.include_router(devices.router)
    # The UI router is key-free by design (FR-25, trusted LAN): a browser <audio> tag
    # can't send X-API-Key, so /ui/* serves audio without one. /api/* is unchanged.
    app.include_router(ui_routes.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app


app = create_app()
