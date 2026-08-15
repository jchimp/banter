"""Heartbeat: URL/header/body shape, no-raise on failure, fixed-interval firing, and
prompt shutdown.

No real sockets. `FakeSession` stands in for `requests.Session`, same pattern as
test_uploader.py.
"""

import time
from pathlib import Path

import requests

from banter_client.config import ClientSettings
from banter_client.heartbeat import Heartbeat
from banter_client.queue import RecordingMeta, RecordingQueue


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeSession:
    """Records every call; either returns `status_code` or raises `raise_exc`."""

    def __init__(self, status_code: int = 200, raise_exc: Exception | None = None) -> None:
        self.status_code = status_code
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResponse(self.status_code)


def _enqueued(queue: RecordingQueue, id_: str) -> RecordingMeta:
    meta = RecordingMeta(
        id=id_,
        path=queue.queue_dir / f"{id_}.wav",
        source="kid",
        device_id="kidbox-01",
        recorded_at="2026-08-12T10:00:00+00:00",
        duration_ms=1000,
    )
    (queue.queue_dir / f"{id_}.wav").write_bytes(b"")
    queue.enqueue(meta)
    return meta


def _settings(tmp_path: Path, **overrides) -> ClientSettings:
    defaults = dict(
        audio_backend="synthetic",
        ring_backend="null",
        queue_dir=tmp_path / "q",
        api_key="secret-key",
        heartbeat_interval_seconds=60.0,
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


# --------------------------------------------------------------------------- shape
def test_posts_to_heartbeat_url_with_api_key_header(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path, api_key="the-secret")
    fake = FakeSession(status_code=200)
    hb = Heartbeat(settings, queue, session=fake)

    hb._send()

    [call] = fake.calls
    assert call["url"] == settings.heartbeat_url()
    assert call["headers"]["X-API-Key"] == "the-secret"


def test_body_carries_real_queue_depth(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    for i in range(3):
        _enqueued(queue, f"item{i}")
    settings = _settings(tmp_path)
    fake = FakeSession(status_code=200)
    hb = Heartbeat(settings, queue, session=fake)

    hb._send()

    [call] = fake.calls
    assert call["json"] == {"queue_depth": 3}


def test_uses_next_timeout_not_http_timeout(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path, next_timeout=3.0, http_timeout=30.0)
    fake = FakeSession(status_code=200)
    hb = Heartbeat(settings, queue, session=fake)

    hb._send()

    [call] = fake.calls
    assert call["timeout"] == 3.0


# ---------------------------------------------------------------------- failure
def test_network_error_does_not_raise(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path)
    fake = FakeSession(raise_exc=requests.ConnectionError("offline"))
    hb = Heartbeat(settings, queue, session=fake)

    hb._send()  # must not raise


def test_non_2xx_response_does_not_raise(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path)
    fake = FakeSession(status_code=500)
    hb = Heartbeat(settings, queue, session=fake)

    hb._send()  # must not raise


# ------------------------------------------------------------------------- timing
def test_fires_repeatedly_on_interval(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path, heartbeat_interval_seconds=0.05)
    fake = FakeSession(status_code=200)
    hb = Heartbeat(settings, queue, session=fake)

    hb.start()
    assert _wait_until(lambda: len(fake.calls) >= 3, timeout=2.0)
    hb.stop()


def test_sends_immediately_on_start(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path, heartbeat_interval_seconds=60.0)
    fake = FakeSession(status_code=200)
    hb = Heartbeat(settings, queue, session=fake)

    hb.start()
    assert _wait_until(lambda: len(fake.calls) >= 1, timeout=2.0)
    hb.stop()


# ---------------------------------------------------------------------- shutdown
def test_stop_returns_promptly_and_thread_dies(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path, heartbeat_interval_seconds=60.0)
    fake = FakeSession(status_code=200)
    hb = Heartbeat(settings, queue, session=fake)

    hb.start()
    assert _wait_until(lambda: len(fake.calls) >= 1, timeout=2.0)

    start = time.monotonic()
    hb.stop()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert hb._thread is not None
    assert not hb._thread.is_alive()


def test_start_is_idempotent(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    settings = _settings(tmp_path, heartbeat_interval_seconds=60.0)
    fake = FakeSession(status_code=200)
    hb = Heartbeat(settings, queue, session=fake)

    hb.start()
    first_thread = hb._thread
    hb.start()

    assert hb._thread is first_thread
    hb.stop()
