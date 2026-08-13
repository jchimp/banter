"""Uploader: backoff math, response handling, the offline drill, and prompt shutdown.

No real sockets. `FakeSession` stands in for `requests.Session` (Uploader accepts an
injected `session`, per Step 6 of CLAUDE.md's plan). Most tests drive `_upload_one` /
`_upload_pass` directly instead of starting the daemon thread, so the suite stays fast
and deterministic; the two tests that are inherently about thread behavior (prompt
`stop()`, a real drain observed through the ring) start the thread and bound their
waits.
"""

import time
import wave
from pathlib import Path

import pytest
import requests

from banter_client.backends.io import NullRing
from banter_client.config import ClientSettings
from banter_client.queue import RecordingMeta, RecordingQueue
from banter_client.uploader import Uploader, backoff_delay


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeSession:
    """Records every call; either returns `status_code` or raises `raise_exc`."""

    def __init__(self, status_code: int = 201, raise_exc: Exception | None = None) -> None:
        self.status_code = status_code
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def post(self, url, headers=None, data=None, files=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "data": data, "files": files, "timeout": timeout}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResponse(self.status_code)


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))


def _enqueued(queue: RecordingQueue, id_: str, recorded_at: str) -> RecordingMeta:
    meta = RecordingMeta(
        id=id_,
        path=queue.queue_dir / f"{id_}.wav",
        source="kid",
        device_id="kidbox-01",
        recorded_at=recorded_at,
        duration_ms=1000,
    )
    _write_wav(meta.path)
    queue.enqueue(meta)
    return meta


def _settings(tmp_path: Path, **overrides) -> ClientSettings:
    defaults = dict(
        audio_backend="synthetic",
        ring_backend="null",
        queue_dir=tmp_path / "q",
        api_key="secret-key",
        backoff_start_seconds=2.0,
        backoff_max_seconds=60.0,
        _env_file=None,
    )
    defaults.update(overrides)
    return ClientSettings(**defaults)


def _wait_until(pred, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# --------------------------------------------------------------------- backoff_delay
def test_backoff_delay_start_value():
    assert backoff_delay(0, 2.0, 60) == 2.0


def test_backoff_delay_doubles_per_attempt():
    assert backoff_delay(1, 2.0, 60) == 4.0
    assert backoff_delay(2, 2.0, 60) == 8.0
    assert backoff_delay(3, 2.0, 60) == 16.0


def test_backoff_delay_clamps_at_cap():
    assert backoff_delay(10, 2.0, 60) == 60.0


def test_backoff_delay_negative_attempt_not_below_start():
    assert backoff_delay(-5, 2.0, 60) == 2.0


# ------------------------------------------------------------------------- success
def test_201_deletes_wav_and_sidecar(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    uploader = Uploader(settings, queue, session=FakeSession(status_code=201))

    outcome = uploader._upload_one(meta)

    assert outcome == "success"
    assert not meta.path.exists()
    assert not (queue.queue_dir / "a.json").exists()
    assert queue.pending() == []


def test_200_duplicate_response_is_also_success(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    uploader = Uploader(settings, queue, session=FakeSession(status_code=200))

    outcome = uploader._upload_one(meta)

    assert outcome == "success"
    assert not meta.path.exists()


# ------------------------------------------------------------------------ transient
def test_500_leaves_item_pending_and_increments_attempts(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    uploader = Uploader(settings, queue, session=FakeSession(status_code=500))

    outcome = uploader._upload_one(meta)

    assert outcome == "transient"
    assert meta.path.exists()
    [got] = queue.pending()
    assert got.attempts == 1


def test_request_exception_is_transient(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    fake = FakeSession(raise_exc=requests.ConnectionError("offline"))
    uploader = Uploader(settings, queue, session=fake)

    outcome = uploader._upload_one(meta)

    assert outcome == "transient"
    assert meta.path.exists()
    [got] = queue.pending()
    assert got.attempts == 1


@pytest.mark.parametrize("status", [429, 408, 500, 502, 503])
def test_transient_statuses_not_quarantined(tmp_path, status):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    uploader = Uploader(settings, queue, session=FakeSession(status_code=status))

    outcome = uploader._upload_one(meta)

    assert outcome == "transient"
    assert meta.path.exists()
    assert [m.id for m in queue.pending()] == ["a"]
    assert list(queue.rejected_dir.glob("*.wav")) == []


# ------------------------------------------------------------------------ permanent
@pytest.mark.parametrize("status", [400, 401])
def test_permanent_statuses_are_quarantined(tmp_path, status):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    uploader = Uploader(settings, queue, session=FakeSession(status_code=status))

    outcome = uploader._upload_one(meta)

    assert outcome == "permanent"
    assert not meta.path.exists()  # gone from its original location
    assert (queue.rejected_dir / "a.wav").exists()  # but not deleted, quarantined
    assert queue.pending() == []


# --------------------------------------------------------------------- offline drill
def test_offline_drill_recovers_after_restart(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    _enqueued(queue, "b", "2026-08-12T10:01:00+00:00")
    _enqueued(queue, "c", "2026-08-12T10:02:00+00:00")
    settings = _settings(tmp_path)
    fake = FakeSession(status_code=500)
    uploader = Uploader(settings, queue, session=fake)

    uploader._upload_pass(queue.pending())
    assert queue.depth() == 3
    for id_ in ("a", "b", "c"):
        assert (queue.queue_dir / f"{id_}.wav").exists()

    fake.status_code = 201
    uploader._upload_pass(queue.pending())
    assert queue.depth() == 0
    for id_ in ("a", "b", "c"):
        assert not (queue.queue_dir / f"{id_}.wav").exists()
        assert not (queue.queue_dir / f"{id_}.json").exists()


# ------------------------------------------------------------------------- shape
def test_post_shape_and_api_key_header(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path, api_key="the-secret")
    fake = FakeSession(status_code=201)
    uploader = Uploader(settings, queue, session=fake)

    uploader._upload_one(meta)

    [call] = fake.calls
    assert call["headers"]["X-API-Key"] == "the-secret"
    data = call["data"]
    for key in ("id", "source", "device_id", "recorded_at", "duration_ms"):
        assert key in data
    assert "audio" in call["files"]


def test_file_handle_closed_after_upload_wav_unlinkable(tmp_path):
    # On Windows a leaked handle would block the unlink inside queue.done(); asserting
    # the wav is gone after a 201 is therefore a real leak check, not just a formality.
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    uploader = Uploader(settings, queue, session=FakeSession(status_code=201))

    uploader._upload_one(meta)

    assert not meta.path.exists()


# ------------------------------------------------------------------------- shutdown
def test_stop_returns_promptly_during_backoff_wait(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path, backoff_start_seconds=30.0, backoff_max_seconds=60.0)
    uploader = Uploader(settings, queue, session=FakeSession(status_code=500))

    uploader.start()
    # Let the thread run its first (failing) pass and enter the long backoff wait.
    assert _wait_until(lambda: uploader._pass_failures >= 1, timeout=2.0)

    start = time.monotonic()
    uploader.stop()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert uploader._thread is not None
    assert not uploader._thread.is_alive()


# ------------------------------------------------------------------------------ ring
def test_ring_is_optional_and_never_raises(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    uploader = Uploader(settings, queue, ring=None, session=FakeSession(status_code=201))

    uploader._upload_one(meta)  # must not raise despite ring=None


def test_successful_drain_ends_with_idle_on_ring(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    settings = _settings(tmp_path)
    ring = NullRing()
    uploader = Uploader(settings, queue, ring=ring, session=FakeSession(status_code=201))

    uploader.start()
    assert _wait_until(lambda: queue.depth() == 0, timeout=2.0)
    assert _wait_until(lambda: "idle" in ring.states, timeout=2.0)
    uploader.stop()
