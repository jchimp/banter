"""M3 step 5 acceptance: `app.telegram.notify` — the pending-parent-notification loop.

No mocking libraries (none are in this repo): a hand-rolled `FakeTelegramClient`
records every `send_voice` call and can fail selectively per chat id, mirroring
`FakeSession` in `client/tests/test_uploader.py` and the fake in
`test_telegram_handlers.py`.
"""

import asyncio

import pytest

from app import db, store
from app.config import Settings
from app.telegram import notify
from app.telegram.client import TelegramError
from app.telegram.handlers import BotContext

from .conftest import _make_wav_bytes

MOM_CHAT = "111"
DAD_CHAT = "222"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        api_key="test-key",
        telegram_chat_mom=MOM_CHAT,
        telegram_chat_dad=DAD_CHAT,
        telegram_send_retries=1,
        _env_file=None,
    )


@pytest.fixture(autouse=True)
def _migrated(settings):
    db.migrate(settings.db_path)


class FakeTelegramClient:
    """Records every `send_voice` call; can be told to fail for a given chat id.

    `fail_chat_ids` raises `TelegramError` for every call to that chat id until the
    id is removed — tests use this to simulate "dad's send keeps failing" without
    ever hitting a real socket.
    """

    def __init__(self) -> None:
        self.sent_voices: list[dict] = []
        self.fail_chat_ids: set[str] = set()
        self.next_file_id_counter = 0

    async def send_message(self, chat_id: str, text: str) -> dict:
        return {"message_id": 1}

    async def send_voice(self, chat_id, *, voice, caption=None, filename="joke.ogg") -> dict:
        if chat_id in self.fail_chat_ids:
            raise TelegramError("boom")
        self.sent_voices.append({"chat_id": chat_id, "voice": voice, "caption": caption})
        self.next_file_id_counter += 1
        return {"voice": {"file_id": f"file-{self.next_file_id_counter}"}}


@pytest.fixture
def fake_client() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest.fixture
def transcode_calls() -> list[bytes]:
    """Records every WAV handed to the injected `wav_to_ogg` fake below."""
    return []


@pytest.fixture
def ctx(settings, fake_client, transcode_calls) -> BotContext:
    # `send_recording`'s outbound WAV->OGG transcode (for a row with no cached
    # telegram_file_id yet) goes through `BotContext.wav_to_ogg`, which defaults to
    # real ffmpeg. ffmpeg isn't installed in this dev environment (matches the "2
    # ffmpeg tests skip by design" convention in `test_transcode.py`, which this
    # suite doesn't want to add more skips to), so a canned-bytes fake is injected.
    def _fake_wav_to_ogg(wav_bytes: bytes) -> bytes:
        transcode_calls.append(wav_bytes)
        return b"fake-ogg-bytes"

    return BotContext(settings=settings, client=fake_client, wav_to_ogg=_fake_wav_to_ogg)


def _insert_kid(settings, rec_id, *, wav_on_disk=True, created_at="2026-08-13T10:30:00Z"):
    wav_bytes = _make_wav_bytes(seconds=1.0)
    rel_path = f"kid/202608/{rec_id}.wav"
    if wav_on_disk:
        full = settings.audio_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(wav_bytes)
    with db.session(settings.db_path) as conn, db.transaction(conn):
        store.insert_recording(
            conn,
            id=rec_id,
            source="kid",
            origin="kidbox",
            path=rel_path,
            duration_ms=1000,
            bytes=len(wav_bytes),
            created_at=created_at,
        )


def _insert_telegram_origin(settings, rec_id):
    with db.session(settings.db_path) as conn, db.transaction(conn):
        store.insert_recording(
            conn,
            id=rec_id,
            source="mom",
            origin="telegram",
            path=f"mom/202608/{rec_id}.wav",
            duration_ms=1000,
            bytes=100,
            created_at="2026-08-13T10:30:00Z",
        )


def _delete(settings, rec_id):
    with db.session(settings.db_path) as conn, db.transaction(conn):
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = ?", (rec_id,))


def _row(settings, rec_id):
    with db.session(settings.db_path) as conn:
        return store.get_recording(conn, rec_id)


# --- caption_for --------------------------------------------------------------


def test_caption_for_contains_timestamp_and_duration(settings):
    _insert_kid(settings, "k1", created_at="2026-08-13T10:05:00Z")
    row = _row(settings, "k1")
    caption = notify.caption_for(row)
    assert "Aug" in caption
    assert "13" in caption
    assert "10:05" in caption
    assert "1s" in caption


def test_caption_for_minutes_seconds_format(settings):
    with db.session(settings.db_path) as conn, db.transaction(conn):
        store.insert_recording(
            conn,
            id="long1",
            source="kid",
            origin="kidbox",
            path="kid/202608/long1.wav",
            duration_ms=65_000,
            bytes=100,
            created_at="2026-08-13T10:05:00Z",
        )
    caption = notify.caption_for(_row(settings, "long1"))
    assert "1:05" in caption


# --- notify_once: both parents succeed -----------------------------------------


async def test_both_parents_succeed(settings, fake_client, ctx):
    _insert_kid(settings, "k1")

    sent = await notify.notify_once(ctx)

    assert sent == 2
    assert len(fake_client.sent_voices) == 2
    chat_ids = {call["chat_id"] for call in fake_client.sent_voices}
    assert chat_ids == {MOM_CHAT, DAD_CHAT}

    row = _row(settings, "k1")
    assert row["notified_mom"] == 1
    assert row["notified_dad"] == 1
    assert row["notified"] == 1
    assert row["telegram_file_id"] is not None


async def test_second_parent_reuses_file_id_no_second_transcode(settings, fake_client, ctx):
    _insert_kid(settings, "k1")

    await notify.notify_once(ctx)

    assert len(fake_client.sent_voices) == 2
    first_voice = fake_client.sent_voices[0]["voice"]
    second_voice = fake_client.sent_voices[1]["voice"]
    # First send uploads raw bytes; second send reuses the returned file_id as a
    # plain string -- no re-upload, no second ffmpeg invocation.
    assert isinstance(first_voice, bytes)
    assert isinstance(second_voice, str)
    assert second_voice == "file-1"  # the file_id returned by the first send


# --- notify_once: partial failure ----------------------------------------------


async def test_partial_failure_dad_fails_mom_succeeds(settings, fake_client, ctx):
    _insert_kid(settings, "k1")
    fake_client.fail_chat_ids.add(DAD_CHAT)

    sent = await notify.notify_once(ctx)

    assert sent == 1
    row = _row(settings, "k1")
    assert row["notified_mom"] == 1
    assert row["notified_dad"] == 0
    assert row["notified"] == 0
    # Only mom was actually messaged this pass (plus dad's failed attempts, which
    # never land in sent_voices since send_voice raised before recording the call).
    assert all(call["chat_id"] == MOM_CHAT for call in fake_client.sent_voices)


async def test_second_pass_resends_only_to_dad(settings, fake_client, ctx):
    _insert_kid(settings, "k1")
    fake_client.fail_chat_ids.add(DAD_CHAT)
    await notify.notify_once(ctx)

    mom_sends_after_first_pass = sum(
        1 for call in fake_client.sent_voices if call["chat_id"] == MOM_CHAT
    )
    assert mom_sends_after_first_pass == 1

    fake_client.fail_chat_ids.discard(DAD_CHAT)
    sent = await notify.notify_once(ctx)

    assert sent == 1
    mom_sends_total = sum(1 for call in fake_client.sent_voices if call["chat_id"] == MOM_CHAT)
    dad_sends_total = sum(1 for call in fake_client.sent_voices if call["chat_id"] == DAD_CHAT)
    assert mom_sends_total == 1  # mom was not messaged a second time
    assert dad_sends_total == 1

    row = _row(settings, "k1")
    assert row["notified_mom"] == 1
    assert row["notified_dad"] == 1
    assert row["notified"] == 1


# --- notify_once: single-parent deployment -------------------------------------


async def test_only_dad_configured(tmp_path, fake_client, transcode_calls):
    settings = Settings(
        data_dir=tmp_path,
        api_key="test-key",
        telegram_chat_mom="",
        telegram_chat_dad=DAD_CHAT,
        telegram_send_retries=1,
        _env_file=None,
    )
    db.migrate(settings.db_path)
    _insert_kid(settings, "k1")

    def _fake_wav_to_ogg(wav_bytes: bytes) -> bytes:
        transcode_calls.append(wav_bytes)
        return b"fake-ogg-bytes"

    ctx = BotContext(settings=settings, client=fake_client, wav_to_ogg=_fake_wav_to_ogg)

    sent = await notify.notify_once(ctx)

    assert sent == 1
    assert len(fake_client.sent_voices) == 1
    assert fake_client.sent_voices[0]["chat_id"] == DAD_CHAT

    row = _row(settings, "k1")
    assert row["notified_dad"] == 1
    assert row["notified"] == 1


# --- notify_once: pending_notifications scoping ---------------------------------


async def test_telegram_origin_and_deleted_rows_never_notified(settings, fake_client, ctx):
    _insert_kid(settings, "k1")
    _insert_telegram_origin(settings, "t1")
    _insert_kid(settings, "k2")
    _delete(settings, "k2")

    sent = await notify.notify_once(ctx)

    # Exactly one recording (k1) drove all sends -- t1 (telegram-origin) and k2
    # (deleted) are never candidates.
    assert sent == 2
    assert len(fake_client.sent_voices) == 2
    assert _row(settings, "t1")["notified"] == 0
    assert _row(settings, "k2")["notified"] == 0


# --- notify_once: missing wav doesn't stall the queue ---------------------------


async def test_missing_wav_does_not_stall_remaining_rows(settings, fake_client, ctx):
    _insert_kid(settings, "k1", wav_on_disk=False, created_at="2026-08-13T10:00:00Z")
    _insert_kid(settings, "k2", wav_on_disk=True, created_at="2026-08-13T10:01:00Z")

    sent = await notify.notify_once(ctx)

    # k1 can't be sent to either parent (file missing); k2 sends to both.
    assert sent == 2
    row1 = _row(settings, "k1")
    row2 = _row(settings, "k2")
    assert row1["notified"] == 0
    assert row2["notified"] == 1


# --- notify_loop ------------------------------------------------------------------


async def test_notify_loop_survives_failing_pass_and_cancels_promptly(monkeypatch, settings, ctx):
    settings.telegram_notify_interval_s = 0.01
    calls = {"n": 0}

    async def _boom(_ctx):
        calls["n"] += 1
        raise RuntimeError("pass exploded")

    monkeypatch.setattr(notify, "notify_once", _boom)

    task = asyncio.create_task(notify.notify_loop(ctx))
    await asyncio.sleep(0.05)
    assert calls["n"] > 0  # the loop kept calling notify_once despite the raise

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
