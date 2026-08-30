"""Player (network + cache resolution) and RecordController.play_next() (threading +
state machine) for the M2 playback path (PRD FR-8..FR-12).

No real sockets: `FakeSession` stands in for `requests.Session`, same discipline and
shape as `test_uploader.py`'s `FakeSession`. Controller tests use a `FakeAudio` that
never actually plays anything -- `play()` just records the call and holds `on_done`
until the test fires it, so playback completion is driven explicitly instead of by a
race with a real backend.
"""

import io
import os
import threading
import wave
from collections.abc import Callable
from pathlib import Path

import requests

from banter_client.backends.io import NullRing
from banter_client.config import ClientSettings
from banter_client.controller import RecordController
from banter_client.player import Clip, Player
from banter_client.state import State

BASE_MTIME = 1_700_000_000


def _wav_bytes(seconds: float = 0.05, rate: int = 16000) -> bytes:
    """Real mono 16-bit WAV bytes. The cache rejects anything `wave` can't parse, so
    fixtures have to be actual audio rather than a b"data" placeholder."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


# --------------------------------------------------------------------------- fakes
class FakeResponse:
    """Stands in for `requests.Response`. `chunks`/`iter_exc` model a streamed
    download that fails partway through."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: dict | None = None,
        chunks: list[bytes] | None = None,
        iter_exc: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._chunks = chunks or []
        self._iter_exc = iter_exc

    def json(self):
        return self._json_data

    def iter_content(self, chunk_size: int = 8192):
        yield from self._chunks
        if self._iter_exc is not None:
            raise self._iter_exc


class FakeSession:
    """Fake `requests.Session`. `get_responses`/`post_responses` are per-URL queues
    of either a `FakeResponse` or an `Exception` instance to raise."""

    def __init__(self) -> None:
        self.get_responses: dict[str, list] = {}
        self.post_responses: dict[str, list] = {}
        self.calls: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        self.calls.append(
            {"method": "GET", "url": url, "headers": headers, "params": params, "timeout": timeout}
        )
        return self._pop(self.get_responses, url)

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {"method": "POST", "url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return self._pop(self.post_responses, url)

    def _pop(self, table: dict[str, list], url: str):
        queue = table.get(url)
        if not queue:
            raise AssertionError(f"no fake response queued for GET/POST {url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAudio:
    """Records what it was asked to play; `on_done` fires only when the test calls
    `finish()` or `stop_play()`, never on its own -- that's what lets tests assert on
    the mid-playback state without racing a real backend."""

    def __init__(self) -> None:
        self.played: list[Path] = []
        self.is_recording = False
        self.is_playing = False
        self._on_done: Callable[[], None] | None = None

    def start_record(self, path: Path) -> None:
        # The controller probes the finished file for real audio, not just its
        # existence -- a header-only WAV is exactly the bug that guard exists for --
        # so the fake has to leave a genuinely playable one, like a real backend does.
        path.write_bytes(_wav_bytes(seconds=1.0))
        self.is_recording = True

    def stop_record(self) -> float:
        self.is_recording = False
        return 1.0

    def play(self, path: Path, on_done: Callable[[], None] | None = None) -> None:
        self.played.append(path)
        self.is_playing = True
        self._on_done = on_done

    def stop_play(self) -> None:
        self.is_playing = False
        cb, self._on_done = self._on_done, None
        if cb is not None:
            cb()

    def finish(self) -> None:
        """Simulate playback finishing naturally (as opposed to a stop-tap)."""
        self.is_playing = False
        cb, self._on_done = self._on_done, None
        if cb is not None:
            cb()

    def close(self) -> None:
        pass


class FakePlayer:
    """Stub for `Player` in controller tests. `block`, if given, makes `fetch_next()`
    wait on the event so a test can control exactly when the worker thread proceeds
    (Step 8's tap-during-fetch race)."""

    def __init__(
        self,
        clip: Clip | None = None,
        exc: Exception | None = None,
        block: threading.Event | None = None,
    ) -> None:
        self.clip = clip
        self.exc = exc
        self.block = block
        self.fetch_calls = 0
        self.reported: list[str] = []

    def fetch_next(self) -> Clip | None:
        self.fetch_calls += 1
        if self.block is not None:
            self.block.wait()
        if self.exc is not None:
            raise self.exc
        return self.clip

    def report_played(self, rec_id: str, played_at: str | None = None) -> None:
        self.reported.append(rec_id)


def _settings(tmp_path: Path, **overrides) -> ClientSettings:
    defaults = dict(
        audio_backend="synthetic",
        ring_backend="null",
        queue_dir=tmp_path / "q",
        cache_dir=tmp_path / "cache",
        play_cache_size=3,
        api_key="secret-key",
        device_id="kidbox-01",
        _env_file=None,
    )
    defaults.update(overrides)
    return ClientSettings(**defaults)


def _join_worker(controller: RecordController, timeout: float = 2.0) -> None:
    worker = controller._play_worker
    assert worker is not None, "expected play_next() to have spawned a worker thread"
    worker.join(timeout=timeout)
    assert not worker.is_alive(), "worker thread did not finish within the timeout"


# =========================================================================== Player
# ------------------------------------------------------------------- fetch_next: 200
def test_fetch_next_downloads_and_caches_uncached_clip(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(200, json_data={"id": "abc"})]
    audio_url = f"{settings.recordings_url}/abc/audio"
    wav = _wav_bytes()
    session.get_responses[audio_url] = [FakeResponse(200, chunks=[wav[:30], wav[30:]])]
    player = Player(settings, session=session)

    clip = player.fetch_next()

    assert clip == Clip(path=player.cache.path_for("abc"), id="abc", from_cache=False)
    assert clip.path.read_bytes() == wav
    assert player.cache.has("abc")


def test_fetch_next_cache_hit_returns_cached_copy_without_downloading(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(200, json_data={"id": "abc"})]
    player = Player(settings, session=session)
    player.cache.store("abc", _wav_bytes())

    clip = player.fetch_next()

    assert clip.id == "abc"
    assert clip.from_cache is True
    assert [c["url"] for c in session.calls] == [settings.next_url]  # no audio GET


# ------------------------------------------------------------------- fetch_next: 204
def test_fetch_next_204_returns_none_and_does_not_consult_cache(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(204)]
    player = Player(settings, session=session)
    player.cache.store("cached1", _wav_bytes())  # present, but must be ignored

    assert player.fetch_next() is None


# --------------------------------------------------------------- offline fallback
def test_fetch_next_network_exception_falls_back_to_oldest_cached_clip(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [requests.ConnectionError("offline")]
    player = Player(settings, session=session)
    player.cache.store("cached1", _wav_bytes())

    clip = player.fetch_next()

    assert clip.id is None
    assert clip.from_cache is True
    assert clip.path == player.cache.path_for("cached1")


def test_fetch_next_non_2xx_falls_back_to_oldest_cached_clip(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(500)]
    player = Player(settings, session=session)
    player.cache.store("cached1", _wav_bytes())

    clip = player.fetch_next()

    assert clip.id is None
    assert clip.from_cache is True


def test_fetch_next_partial_download_falls_back_and_leaves_no_truncated_file(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(200, json_data={"id": "new"})]
    audio_url = f"{settings.recordings_url}/new/audio"
    session.get_responses[audio_url] = [
        FakeResponse(200, chunks=[b"partial"], iter_exc=requests.ConnectionError("dropped"))
    ]
    player = Player(settings, session=session)
    player.cache.store("cached1", _wav_bytes())

    clip = player.fetch_next()

    assert clip.id is None  # the offline fallback, not the failed "new" download
    assert not player.cache.has("new")
    assert list(player.cache.cache_dir.glob("*.tmp")) == []


def test_offline_fallback_rotates_across_consecutive_calls(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(500), FakeResponse(500)]
    player = Player(settings, session=session)
    player.cache.store("a", _wav_bytes())
    player.cache.store("b", _wav_bytes())
    os.utime(player.cache.path_for("a"), (BASE_MTIME, BASE_MTIME))
    os.utime(player.cache.path_for("b"), (BASE_MTIME + 1, BASE_MTIME + 1))

    first = player.fetch_next()
    second = player.fetch_next()

    assert {first.path.stem, second.path.stem} == {"a", "b"}
    assert first.path != second.path


def test_offline_fallback_with_empty_cache_returns_none(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(500)]
    player = Player(settings, session=session)

    assert player.fetch_next() is None


def test_offline_fallback_clip_has_id_none(tmp_path):
    """id=None is the caller's signal not to POST a play receipt for this clip."""
    settings = _settings(tmp_path)
    session = FakeSession()
    session.get_responses[settings.next_url] = [FakeResponse(500)]
    player = Player(settings, session=session)
    player.cache.store("cached1", _wav_bytes())

    clip = player.fetch_next()

    assert clip.id is None


# --------------------------------------------------------------------- report_played
def test_report_played_posts_device_id_and_played_at(tmp_path):
    settings = _settings(tmp_path, device_id="kidbox-42")
    session = FakeSession()
    post_url = f"{settings.recordings_url}/abc/played"
    session.post_responses[post_url] = [FakeResponse(200)]
    player = Player(settings, session=session)

    player.report_played("abc", played_at="2026-08-12T10:00:00+00:00")

    [call] = session.calls
    assert call["json"]["device_id"] == "kidbox-42"
    assert call["json"]["played_at"] == "2026-08-12T10:00:00+00:00"


def test_report_played_swallows_network_exception(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    post_url = f"{settings.recordings_url}/abc/played"
    session.post_responses[post_url] = [requests.ConnectionError("offline")]
    player = Player(settings, session=session)

    player.report_played("abc")  # must not raise


def test_report_played_swallows_non_2xx(tmp_path):
    settings = _settings(tmp_path)
    session = FakeSession()
    post_url = f"{settings.recordings_url}/abc/played"
    session.post_responses[post_url] = [FakeResponse(500)]
    player = Player(settings, session=session)

    player.report_played("abc")  # must not raise


# =============================================================== RecordController
def test_play_next_happy_path_plays_and_reports_on_completion(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    clip = Clip(path=tmp_path / "cache" / "rec1.wav", id="rec1", from_cache=False)
    player = FakePlayer(clip=clip)
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=player)

    controller.play_next()
    _join_worker(controller)

    assert audio.played == [clip.path]
    assert controller.machine.state is State.PLAYING
    assert player.reported == []  # not yet -- on_done hasn't fired

    audio.finish()

    assert controller.machine.state is State.IDLE
    assert player.reported == ["rec1"]


def test_play_next_fallback_clip_gets_no_receipt(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    clip = Clip(path=tmp_path / "cache" / "fallback.wav", id=None, from_cache=True)
    player = FakePlayer(clip=clip)
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=player)

    controller.play_next()
    _join_worker(controller)
    audio.finish()

    assert controller.machine.state is State.IDLE
    assert player.reported == []


def test_play_next_tap_while_playing_stops_and_does_not_start_a_second_playback(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    clip = Clip(path=tmp_path / "cache" / "rec1.wav", id="rec1", from_cache=False)
    player = FakePlayer(clip=clip)
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=player)

    controller.play_next()
    _join_worker(controller)
    assert audio.is_playing

    controller.play_next()  # second tap: FR-9, stop instead of fetching again

    assert audio.played == [clip.path]  # never played a second time
    assert not audio.is_playing
    assert controller.machine.state is State.IDLE


def test_start_record_ignored_while_playing(tmp_path):
    """FR-10: BTN1 is dead while a server-fetched clip is playing."""
    settings = _settings(tmp_path)
    audio = FakeAudio()
    clip = Clip(path=tmp_path / "cache" / "rec1.wav", id="rec1", from_cache=False)
    player = FakePlayer(clip=clip)
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=player)

    controller.play_next()
    _join_worker(controller)
    assert controller.machine.state is State.PLAYING

    controller.start_record()

    assert controller.machine.state is State.PLAYING
    assert not audio.is_recording


def test_play_next_ignored_while_recording(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    player = FakePlayer(clip=None)
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=player)

    controller.start_record()
    controller.play_next()

    assert controller.machine.state is State.RECORDING
    assert controller._play_worker is None  # never even spawned
    controller.stop_record()


def test_play_next_tap_during_fetch_cancels_and_settles_to_idle(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    block = threading.Event()
    clip = Clip(path=tmp_path / "cache" / "rec1.wav", id="rec1", from_cache=False)
    player = FakePlayer(clip=clip, block=block)
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=player)

    controller.play_next()  # spawns worker; fetch_next() blocks on `block`
    assert controller.machine.state is State.PLAYING
    assert not audio.is_playing  # still waiting on the fetch, nothing to stop

    controller.play_next()  # tap-during-fetch: cancel instead of stop_play()

    assert controller.machine.state is State.IDLE

    block.set()  # let the blocked fetch return
    _join_worker(controller)

    assert audio.played == []  # cancelled generation must never start playback


def test_fetch_next_returning_none_flashes_error_and_returns_to_idle(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    player = FakePlayer(clip=None)
    ring = NullRing()
    controller = RecordController(settings, audio=audio, ring=ring, player=player)

    controller.play_next()
    _join_worker(controller)

    assert controller.machine.state is State.IDLE
    assert "error" in ring.flashes
    assert audio.played == []


def test_fetch_next_unexpected_exception_still_returns_to_idle(tmp_path):
    """A crashed worker must never leave the box wedged with both buttons dead."""
    settings = _settings(tmp_path)
    audio = FakeAudio()
    player = FakePlayer(exc=RuntimeError("boom"))
    ring = NullRing()
    controller = RecordController(settings, audio=audio, ring=ring, player=player)

    controller.play_next()
    _join_worker(controller)

    assert controller.machine.state is State.IDLE
    assert "error" in ring.flashes
    assert audio.played == []


def test_play_next_with_no_player_wired_is_ignored(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=None)

    controller.play_next()

    assert controller.machine.state is State.IDLE
    assert controller._play_worker is None
    assert audio.played == []


def test_play_latest_demo_path_still_works_unchanged(tmp_path):
    settings = _settings(tmp_path)
    audio = FakeAudio()
    player = FakePlayer(clip=None)  # wired, but play_latest() must not touch it
    controller = RecordController(settings, audio=audio, ring=NullRing(), player=player)

    controller.start_record()
    controller.stop_record()
    [clip_path] = settings.queue_dir.glob("*.wav")

    controller.play_latest()

    assert controller.machine.state is State.PLAYING
    assert audio.played == [clip_path]
    assert player.fetch_calls == 0

    audio.finish()

    assert controller.machine.state is State.IDLE


# ------------------------------------------- fast-fail timeouts + offline memo
# A dead server that still ACCEPTS the TCP connection (Docker leaves its port proxy
# bound after the container stops) hangs until the READ timeout. These cover the two
# mitigations: a short timeout on the small JSON calls, and a memo so only the first
# tap of an outage pays it at all.


def _wav(cache_dir: Path, rec_id: str, mtime_offset: int = 0) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{rec_id}.wav"
    path.write_bytes(_wav_bytes())
    os.utime(path, (BASE_MTIME + mtime_offset, BASE_MTIME + mtime_offset))
    return path


def test_next_uses_short_timeout_and_audio_download_uses_the_long_one(tmp_path):
    s = _settings(tmp_path, next_timeout=3.0, http_timeout=30.0)
    session = FakeSession()
    session.get_responses[s.next_url] = [FakeResponse(200, json_data={"id": "abc"})]
    session.get_responses[f"{s.recordings_url}/abc/audio"] = [FakeResponse(200, chunks=[b"data"])]
    Player(s, session=session).fetch_next()

    timeouts = {c["url"]: c["timeout"] for c in session.calls}
    assert timeouts[s.next_url] == 3.0
    assert timeouts[f"{s.recordings_url}/abc/audio"] == 30.0


def test_offline_memo_skips_the_network_on_the_next_tap(tmp_path):
    s = _settings(tmp_path, offline_memo_seconds=30.0)
    _wav(s.cache_dir, "cached1", 0)
    _wav(s.cache_dir, "cached2", 1)
    session = FakeSession()
    session.get_responses[s.next_url] = [requests.ConnectionError("boom")]
    player = Player(s, session=session)

    first = player.fetch_next()
    second = player.fetch_next()  # no queued response: a network call would AssertionError

    assert first is not None and first.id is None
    assert second is not None and second.id is None
    assert len(session.calls) == 1  # only the first tap touched the network


def test_memo_expiry_lets_the_network_be_retried(tmp_path):
    # A zero-length memo is expired the instant it's set, so the second tap must go
    # back out to the network rather than short-circuiting forever.
    s = _settings(tmp_path, offline_memo_seconds=0.0)
    _wav(s.cache_dir, "cached1", 0)
    session = FakeSession()
    session.get_responses[s.next_url] = [requests.ConnectionError("boom"), FakeResponse(204)]
    player = Player(s, session=session)

    player.fetch_next()
    assert player.fetch_next() is None  # 204 reached, so the second call hit the network
    assert len(session.calls) == 2


def test_204_clears_the_offline_memo(tmp_path):
    s = _settings(tmp_path, offline_memo_seconds=30.0)
    _wav(s.cache_dir, "cached1", 0)
    session = FakeSession()
    session.get_responses[s.next_url] = [
        requests.ConnectionError("boom"),
        FakeResponse(204),
        FakeResponse(204),
    ]
    player = Player(s, session=session)
    player.fetch_next()
    player._offline_until = 0.0  # simulate the memo lapsing so the 204 can land

    assert player.fetch_next() is None
    # The 204 marked us online, so this third tap must still reach the network.
    assert player.fetch_next() is None
    assert len(session.calls) == 3


def test_successful_fetch_clears_the_offline_memo(tmp_path):
    s = _settings(tmp_path, offline_memo_seconds=30.0)
    session = FakeSession()
    session.get_responses[s.next_url] = [FakeResponse(200, json_data={"id": "abc"})]
    session.get_responses[f"{s.recordings_url}/abc/audio"] = [
        FakeResponse(200, chunks=[_wav_bytes()])
    ]
    player = Player(s, session=session)
    player._offline_until = 0.0
    player.fetch_next()
    assert not player._is_offline()


def test_report_played_is_skipped_while_the_memo_is_active(tmp_path):
    # report_played runs on the playback-completion thread; paying a timeout there
    # would stall the box's return to idle for a receipt we know will fail.
    s = _settings(tmp_path, offline_memo_seconds=30.0)
    _wav(s.cache_dir, "cached1", 0)
    session = FakeSession()
    session.get_responses[s.next_url] = [requests.ConnectionError("boom")]
    player = Player(s, session=session)
    player.fetch_next()

    player.report_played("abc")  # no queued POST: a network call would AssertionError

    assert not [c for c in session.calls if c["method"] == "POST"]


def test_report_played_uses_the_short_timeout(tmp_path):
    s = _settings(tmp_path, next_timeout=3.0, http_timeout=30.0)
    session = FakeSession()
    session.post_responses[f"{s.recordings_url}/abc/played"] = [FakeResponse(200)]
    Player(s, session=session).report_played("abc")
    assert session.calls[0]["timeout"] == 3.0


def test_offline_fallback_cycles_through_every_cached_clip(tmp_path):
    # Not just "the next one is different": oldest() alone ping-pongs between the two
    # oldest entries forever, so this asserts a full cycle across three clips.
    s = _settings(tmp_path, offline_memo_seconds=0.0, play_cache_size=5)
    for i, rec_id in enumerate(["one", "two", "three"]):
        _wav(s.cache_dir, rec_id, i)
    session = FakeSession()
    session.get_responses[s.next_url] = [requests.ConnectionError("boom")] * 3
    player = Player(s, session=session)

    served = [player.fetch_next().path.stem for _ in range(3)]

    assert sorted(served) == ["one", "three", "two"]


def test_download_of_a_non_wav_body_falls_back_and_caches_nothing(tmp_path):
    # The captive-portal case: 200 OK, but the body is a sign-in page, not audio.
    # Serving that would hand the kid silence, so it must never enter the cache.
    s = _settings(tmp_path, offline_memo_seconds=30.0)
    _wav(s.cache_dir, "cached1", 0)
    session = FakeSession()
    session.get_responses[s.next_url] = [FakeResponse(200, json_data={"id": "abc"})]
    session.get_responses[f"{s.recordings_url}/abc/audio"] = [
        FakeResponse(200, chunks=[b"<html>Sign in to WiFi</html>"])
    ]
    player = Player(s, session=session)

    clip = player.fetch_next()

    assert clip is not None
    assert clip.id is None  # fell back to cache, so no receipt is owed
    assert clip.path.stem == "cached1"
    assert not player.cache.has("abc")


def test_a_lying_network_is_memoed_so_junk_is_not_re_downloaded(tmp_path):
    # A portal that intercepts every request would otherwise be re-fetched on each tap.
    s = _settings(tmp_path, offline_memo_seconds=30.0)
    _wav(s.cache_dir, "cached1", 0)
    session = FakeSession()
    session.get_responses[s.next_url] = [FakeResponse(200, json_data={"id": "abc"})]
    session.get_responses[f"{s.recordings_url}/abc/audio"] = [
        FakeResponse(200, chunks=[b"<html>Sign in to WiFi</html>"])
    ]
    player = Player(s, session=session)

    player.fetch_next()
    player.fetch_next()  # no queued responses left: a network call would AssertionError

    assert len([c for c in session.calls if c["url"].endswith("/audio")]) == 1
