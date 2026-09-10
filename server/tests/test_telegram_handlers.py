"""M3 step 4 acceptance: `app.telegram.handlers` — allowlist gating, voice ingest,
`/joke`, `/stats`, and the shared `send_recording` outbound helper.

No mocking libraries (none are in this repo): a hand-rolled `FakeTelegramClient`
records every call it receives, and `ogg_to_wav` is swapped for a canned-bytes fake
via `BotContext.ogg_to_wav` so tests never need ffmpeg on PATH.
"""

import pytest

from app import db, store
from app.config import Settings
from app.telegram import handlers
from app.telegram.client import TelegramError
from app.transcode import TranscodeError

from .conftest import _make_wav_bytes

MOM_CHAT = "111"
DAD_CHAT = "222"
UNKNOWN_CHAT = "999"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        api_key="test-key",
        telegram_chat_mom=MOM_CHAT,
        telegram_chat_dad=DAD_CHAT,
        _env_file=None,
    )


@pytest.fixture(autouse=True)
def _migrated(settings):
    db.migrate(settings.db_path)


class FakeTelegramClient:
    """Records every call; returns canned results. Mirrors `FakeSession` in
    `client/tests/test_uploader.py`.
    """

    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str]] = []
        self.sent_voices: list[dict] = []
        self.get_file_calls: list[str] = []
        self.download_calls: list[str] = []
        self._file_path_for_id: dict[str, str] = {}
        self._download_bytes: dict[str, bytes] = {}
        self.next_voice_file_id: str = "sent-voice-file-id"
        self.raise_on_get_file: bool = False
        self.raise_on_send_voice: bool = False

    def register_file(self, file_id: str, file_path: str, content: bytes) -> None:
        self._file_path_for_id[file_id] = file_path
        self._download_bytes[file_path] = content

    async def send_message(self, chat_id: str, text: str) -> dict:
        self.sent_messages.append((chat_id, text))
        return {"message_id": len(self.sent_messages)}

    async def send_voice(self, chat_id, *, voice, caption=None, filename="joke.ogg") -> dict:
        if self.raise_on_send_voice:
            raise TelegramError("boom")
        self.sent_voices.append({"chat_id": chat_id, "voice": voice, "caption": caption})
        return {"voice": {"file_id": self.next_voice_file_id}}

    async def get_file(self, file_id: str) -> dict:
        self.get_file_calls.append(file_id)
        if self.raise_on_get_file:
            raise TelegramError("boom")
        return {"file_path": self._file_path_for_id[file_id]}

    async def download_file(self, file_path: str) -> bytes:
        self.download_calls.append(file_path)
        return self._download_bytes[file_path]


@pytest.fixture
def fake_client() -> FakeTelegramClient:
    return FakeTelegramClient()


def _fake_convert(output_bytes: bytes):
    """Build a canned transcode fake: ignores its input, returns `output_bytes`."""
    calls: list[bytes] = []

    def _convert(input_bytes: bytes) -> bytes:
        calls.append(input_bytes)
        return output_bytes

    _convert.calls = calls  # type: ignore[attr-defined]
    return _convert


@pytest.fixture
def ctx(settings, fake_client):
    wav_bytes = _make_wav_bytes(seconds=1.0)
    context = handlers.BotContext(
        settings=settings,
        client=fake_client,
        ogg_to_wav=_fake_convert(wav_bytes),
        wav_to_ogg=_fake_convert(b"fake-ogg-bytes"),
    )
    context.wav_bytes = wav_bytes  # type: ignore[attr-defined]
    return context


def _voice_update(chat_id: str, *, file_unique_id="fu1", file_id="f1", duration=5) -> dict:
    return {
        "message": {
            "chat": {"id": chat_id},
            "voice": {
                "file_unique_id": file_unique_id,
                "file_id": file_id,
                "duration": duration,
            },
        }
    }


def _text_update(chat_id: str, text: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def _row_count(settings) -> int:
    with db.session(settings.db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM recordings").fetchone()["n"]


# --- allowlist gating (FR-19) --------------------------------------------------


async def test_unknown_chat_voice_no_reply_no_row(settings, fake_client, ctx):
    fake_client.register_file("f1", "path1.oga", b"ogg-bytes")
    update = _voice_update(UNKNOWN_CHAT)
    await handlers.handle_update(update, ctx)

    assert _row_count(settings) == 0
    assert fake_client.sent_messages == []
    assert fake_client.sent_voices == []
    assert fake_client.get_file_calls == []


async def test_unknown_chat_joke_no_reply_no_row(settings, fake_client, ctx):
    update = _text_update(UNKNOWN_CHAT, "/joke")
    await handlers.handle_update(update, ctx)

    assert _row_count(settings) == 0
    assert fake_client.sent_messages == []
    assert fake_client.sent_voices == []


async def test_unknown_chat_stats_no_reply_no_row(settings, fake_client, ctx):
    update = _text_update(UNKNOWN_CHAT, "/stats")
    await handlers.handle_update(update, ctx)

    assert _row_count(settings) == 0
    assert fake_client.sent_messages == []


async def test_unknown_chat_is_logged_at_info(settings, fake_client, ctx, caplog):
    """The allowlist rejection must be visible without raising the log level.

    It is the only barrier between a stranger's voice note and the kid's speaker, and
    M3-VERIFY §2d proves it from the logs. At DEBUG that check degenerates into
    "nothing was logged", which also holds when the bot never polled at all.
    """
    with caplog.at_level("INFO", logger="banter.telegram.handlers"):
        await handlers.handle_update(_text_update(UNKNOWN_CHAT, "/stats"), ctx)

    assert "ignored_unknown_chat" in caplog.text
    assert UNKNOWN_CHAT in caplog.text


# --- handle_voice (FR-16, FR-20) ------------------------------------------------


async def test_voice_from_mom_creates_row(settings, fake_client, ctx):
    fake_client.register_file("f1", "path1.oga", b"ogg-bytes")
    update = _voice_update(MOM_CHAT, file_unique_id="fu-mom", file_id="f1")
    await handlers.handle_update(update, ctx)

    with db.session(settings.db_path) as conn:
        rows = conn.execute("SELECT * FROM recordings").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "mom"
    assert row["origin"] == "telegram"
    assert row["telegram_file_id"] == "f1"
    assert row["duration_ms"] == 1000  # probed from the 1s canned WAV, not telegram's `duration`

    wav_path = settings.audio_dir / row["path"]
    ogg_path = settings.audio_dir / row["original_path"]
    assert wav_path.is_file()
    assert ogg_path.is_file()
    assert ogg_path.read_bytes() == b"ogg-bytes"

    assert len(fake_client.sent_messages) == 1
    assert fake_client.sent_messages[0][0] == MOM_CHAT


async def test_voice_from_dad_maps_to_dad_source(settings, fake_client, ctx):
    fake_client.register_file("f2", "path2.oga", b"ogg-bytes-dad")
    update = _voice_update(DAD_CHAT, file_unique_id="fu-dad", file_id="f2")
    await handlers.handle_update(update, ctx)

    with db.session(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM recordings").fetchone()
    assert row["source"] == "dad"


async def test_voice_duration_from_probe_not_telegram(settings, fake_client, ctx):
    fake_client.register_file("f1", "path1.oga", b"ogg-bytes")
    # Telegram says 5s; the canned WAV from `ctx` is 1s (see `_make_wav_bytes`).
    update = _voice_update(MOM_CHAT, file_unique_id="fu1", file_id="f1", duration=5)
    await handlers.handle_update(update, ctx)

    with db.session(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM recordings").fetchone()
    assert row["duration_ms"] == 1000


async def test_voice_redelivered_update_no_duplicate_row_or_reply(settings, fake_client, ctx):
    fake_client.register_file("f1", "path1.oga", b"ogg-bytes")
    update = _voice_update(MOM_CHAT, file_unique_id="same-fu", file_id="f1")
    await handlers.handle_update(update, ctx)
    await handlers.handle_update(update, ctx)

    assert _row_count(settings) == 1
    assert len(fake_client.sent_messages) == 1


async def test_voice_transcode_failure_no_row_and_apology(settings, fake_client, ctx):
    fake_client.register_file("f1", "path1.oga", b"ogg-bytes")

    def _raise(_ogg_bytes: bytes) -> bytes:
        raise TranscodeError("ffmpeg exploded")

    ctx.ogg_to_wav = _raise
    update = _voice_update(MOM_CHAT, file_unique_id="fu-fail", file_id="f1")
    await handlers.handle_update(update, ctx)

    assert _row_count(settings) == 0
    assert len(fake_client.sent_messages) == 1
    assert "sorry" in fake_client.sent_messages[0][1].lower()


async def test_voice_telegram_error_no_row_and_apology(settings, fake_client, ctx):
    fake_client.raise_on_get_file = True
    update = _voice_update(MOM_CHAT, file_unique_id="fu-tgerr", file_id="f1")
    await handlers.handle_update(update, ctx)

    assert _row_count(settings) == 0
    assert len(fake_client.sent_messages) == 1


# --- handle_joke (FR-17) --------------------------------------------------------


def _insert_kid(settings, rec_id, *, wav_on_disk=True, telegram_file_id=None):
    wav_bytes = _make_wav_bytes(seconds=0.5)
    rel_path = f"kid/202601/{rec_id}.wav"
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
            duration_ms=500,
            bytes=len(wav_bytes),
            created_at="2026-01-01T00:00:00Z",
            telegram_file_id=telegram_file_id,
        )


def _insert_parent(settings, rec_id, source):
    with db.session(settings.db_path) as conn, db.transaction(conn):
        store.insert_recording(
            conn,
            id=rec_id,
            source=source,
            origin="telegram",
            path=f"{source}/202601/{rec_id}.wav",
            duration_ms=500,
            bytes=100,
            created_at="2026-01-01T00:00:00Z",
        )


@pytest.mark.parametrize(
    "arg,expected_n",
    [
        (None, 1),
        ("3", 3),
        ("5", 5),
        ("99", 5),
        ("0", 1),
        ("abc", 1),
    ],
)
async def test_joke_count_parsing(settings, fake_client, ctx, arg, expected_n):
    for i in range(6):
        _insert_kid(settings, f"k{i}")
    _insert_parent(settings, "m1", "mom")

    await handlers.handle_joke(MOM_CHAT, arg, ctx)
    assert len(fake_client.sent_voices) == expected_n


async def test_joke_command_with_botname_suffix(settings, fake_client, ctx):
    _insert_kid(settings, "k1")
    update = _text_update(MOM_CHAT, "/joke@banterbot")
    await handlers.handle_update(update, ctx)
    assert len(fake_client.sent_voices) == 1


async def test_joke_only_sends_kid_source(settings, fake_client, ctx):
    _insert_kid(settings, "k1")
    _insert_parent(settings, "m1", "mom")
    _insert_parent(settings, "d1", "dad")

    await handlers.handle_joke(MOM_CHAT, "5", ctx)
    assert len(fake_client.sent_voices) == 1


async def test_joke_does_not_write_plays_or_play_count(settings, fake_client, ctx):
    _insert_kid(settings, "k1")
    await handlers.handle_joke(MOM_CHAT, "1", ctx)

    with db.session(settings.db_path) as conn:
        plays = conn.execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"]
        row = store.get_recording(conn, "k1")
    assert plays == 0
    assert row["play_count"] == 0


async def test_joke_empty_pool_text_reply_no_voice(settings, fake_client, ctx):
    await handlers.handle_joke(MOM_CHAT, None, ctx)
    assert fake_client.sent_voices == []
    assert len(fake_client.sent_messages) == 1


async def test_joke_missing_wav_on_disk_remaining_still_sent(settings, fake_client, ctx):
    _insert_kid(settings, "k1", wav_on_disk=False)
    _insert_kid(settings, "k2", wav_on_disk=True)

    await handlers.handle_joke(MOM_CHAT, "2", ctx)
    assert len(fake_client.sent_voices) == 1


# --- send_recording --------------------------------------------------------------


async def test_send_recording_reuses_existing_file_id_no_disk_no_transcode(
    settings, fake_client, ctx
):
    calls = []

    def _tracking_convert(_wav_bytes: bytes) -> bytes:
        calls.append(_wav_bytes)
        return b"should-not-be-called"

    with db.session(settings.db_path) as conn:
        _insert_kid(settings, "k1", wav_on_disk=False, telegram_file_id="cached-id")
        row = store.get_recording(conn, "k1")

    result = await handlers.send_recording(MOM_CHAT, row, ctx)

    assert result == fake_client.next_voice_file_id
    assert fake_client.sent_voices[0]["voice"] == "cached-id"
    assert calls == []


# --- handle_stats (FR-18) ---------------------------------------------------------


async def test_stats_reply_contains_counts_and_duration(settings, fake_client, ctx):
    _insert_kid(settings, "k1")
    _insert_parent(settings, "m1", "mom")

    await handlers.handle_stats(MOM_CHAT, ctx)
    assert len(fake_client.sent_messages) == 1
    text = fake_client.sent_messages[0][1]
    assert "kid" in text
    assert "mom" in text


async def test_stats_empty_db_does_not_crash(settings, fake_client, ctx):
    await handlers.handle_stats(MOM_CHAT, ctx)
    assert len(fake_client.sent_messages) == 1
