import io
import uuid
import wave
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, api_key="test-key", _env_file=None)


@pytest.fixture
def client(settings) -> TestClient:
    # TestClient as a context manager runs lifespan (and therefore migrations).
    with TestClient(create_app(settings)) as c:
        yield c


def _make_wav_bytes(
    *, seconds: float = 1.0, rate: int = 16000, channels: int = 1, sample_width: int = 2
) -> bytes:
    """Build an in-memory WAV file. Stdlib `wave` only — no numpy, no client backends."""
    nframes = int(seconds * rate)
    frame = b"\x00" * (channels * sample_width)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(frame * nframes)
    return buf.getvalue()


@pytest.fixture
def wav_bytes() -> Callable[..., bytes]:
    """Factory fixture: `wav_bytes(seconds=1.0, rate=16000, channels=1, sample_width=2)`."""
    return _make_wav_bytes


@pytest.fixture
def post_recording() -> Callable[..., object]:
    """Factory fixture: POST a recording with sensible defaults, override what varies.

    `recorded_at` defaults to a fixed timestamp so the expected `YYYYMM` directory
    (`202608`) is deterministic across tests. `id` defaults to a fresh uuid4 hex so
    unrelated tests never collide; pass an explicit `id` when the test cares about it
    (e.g. idempotency).
    """

    def _post(
        client: TestClient,
        *,
        id: str | None = None,
        audio: bytes | None = None,
        filename: str = "rec.wav",
        source: str = "kid",
        device_id: str = "kidbox-01",
        recorded_at: str = "2026-08-12T10:30:00Z",
        duration_ms: int = 1000,
        api_key: str | None = "test-key",
    ):
        if id is None:
            id = uuid.uuid4().hex[:16]
        if audio is None:
            audio = _make_wav_bytes()
        headers = {"X-API-Key": api_key} if api_key is not None else {}
        data = {
            "id": id,
            "source": source,
            "device_id": device_id,
            "recorded_at": recorded_at,
            "duration_ms": str(duration_ms),
        }
        files = {"audio": (filename, audio, "audio/wav")}
        return client.post("/api/recordings", data=data, files=files, headers=headers)

    return _post
