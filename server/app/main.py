"""Banter server — app factory.

M0 scope: healthz, config, migrations on startup. Routers land in M1+.
The Telegram bot task attaches to this lifespan in M3.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.api import recordings
from app.config import Settings, get_settings

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
    # M3: app.state.bot_task = asyncio.create_task(run_bot(settings))
    yield
    # M3: cancel bot task here


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
