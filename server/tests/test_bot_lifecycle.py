"""Bot task lifecycle: the disabled path, clean cancellation, and lifespan wiring.

These are the tests that catch a hung shutdown (CLAUDE.md gotcha 2). Nothing here
talks to Telegram — the poll loop is driven through an injected fake client.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.telegram import bot
from app.telegram.bot import backoff_delay, poll_loop, run_bot
from app.telegram.handlers import BotContext


class SlowFakeClient:
    """Blocks in `get_updates` the way a real long-poll does, and records dispatches."""

    def __init__(self) -> None:
        self.polls = 0
        self.closed = False

    async def get_updates(self, offset, *, timeout: int = 30):
        self.polls += 1
        await asyncio.sleep(timeout)  # never resolves within a test
        return []

    async def aclose(self) -> None:
        self.closed = True


class ScriptedFakeClient:
    """Returns canned update batches, then blocks so the loop parks instead of spinning."""

    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = list(batches)
        self.offsets: list[int | None] = []
        self.closed = False

    async def get_updates(self, offset, *, timeout: int = 30):
        self.offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(timeout)
        return []

    async def aclose(self) -> None:
        self.closed = True


def _settings(tmp_path, **overrides) -> Settings:
    base = {
        "data_dir": tmp_path,
        "telegram_bot_token": "test-token",
        "telegram_chat_mom": "111",
        "telegram_poll_timeout_s": 30,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


# --- backoff ---------------------------------------------------------------
def test_backoff_delay_grows_and_caps():
    assert backoff_delay(0) == 1.0
    assert backoff_delay(1) == 2.0
    assert backoff_delay(2) == 4.0
    assert backoff_delay(99) == 30.0


# --- disabled path ---------------------------------------------------------
async def test_run_bot_returns_immediately_without_a_token(tmp_path):
    settings = _settings(tmp_path, telegram_bot_token="")
    # No timeout guard needed: if this ever starts a real loop the test hangs, which
    # is exactly the failure we want to be loud.
    await asyncio.wait_for(run_bot(settings), timeout=2.0)


# --- cancellation ----------------------------------------------------------
async def test_run_bot_cancels_promptly_and_closes_the_client(tmp_path, monkeypatch):
    fake = SlowFakeClient()
    monkeypatch.setattr(bot, "TelegramClient", lambda token: fake)

    task = asyncio.create_task(run_bot(_settings(tmp_path)))
    await asyncio.sleep(0)  # let the TaskGroup spin up both loops
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert fake.closed, "the HTTP client must be closed even when the task is cancelled"


async def test_poll_loop_propagates_cancellation_mid_poll(tmp_path):
    ctx = BotContext(settings=_settings(tmp_path), client=SlowFakeClient())
    task = asyncio.create_task(poll_loop(ctx))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)


# --- dispatch --------------------------------------------------------------
async def test_poll_loop_advances_offset_past_handled_updates(tmp_path, monkeypatch):
    seen: list[int] = []

    async def fake_handle(update, ctx):
        seen.append(update["update_id"])

    monkeypatch.setattr(bot, "handle_update", fake_handle)
    client = ScriptedFakeClient([[{"update_id": 7}, {"update_id": 8}]])
    ctx = BotContext(settings=_settings(tmp_path), client=client)

    task = asyncio.create_task(poll_loop(ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == [7, 8]
    # First poll has no offset; the second asks for everything after the last handled id.
    assert client.offsets[:2] == [None, 9]


async def test_poll_loop_survives_a_handler_that_raises(tmp_path, monkeypatch):
    seen: list[int] = []

    async def fake_handle(update, ctx):
        if update["update_id"] == 1:
            raise RuntimeError("bad update")
        seen.append(update["update_id"])

    monkeypatch.setattr(bot, "handle_update", fake_handle)
    client = ScriptedFakeClient([[{"update_id": 1}, {"update_id": 2}]])
    ctx = BotContext(settings=_settings(tmp_path), client=client)

    task = asyncio.create_task(poll_loop(ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == [2], "a raising handler must not stop the ones after it"
    assert client.offsets[:2] == [None, 3], "a failed update is still acknowledged"


# --- lifespan wiring -------------------------------------------------------
def test_lifespan_starts_and_stops_the_bot_task(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def fake_run_bot(settings):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.main.run_bot", fake_run_bot)
    settings = Settings(data_dir=tmp_path, api_key="test-key", _env_file=None)

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert started.is_set()
        task = app.state.bot_task

    assert task.cancelled() or task.done(), "the bot task must not outlive the lifespan"
