"""Tests for `app.transcode`: argv construction, error mapping, and (skipped-off-box)
real ffmpeg round trips.

Primary strategy is an injected fake runner — mirrors `FakeSession` in
`client/tests/test_uploader.py` — so argv construction and error handling are covered
without ffmpeg installed. No mocking libraries; none are in this repo.
"""

import io
import shutil
import subprocess
import wave

import pytest

from app.audio import probe_wav
from app.transcode import TranscodeError, ogg_to_wav, wav_to_ogg


class FakeRunner:
    """Records the args/input it was called with; returns a canned CompletedProcess."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        raise_exc: Exception | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def __call__(self, args, *, input: bytes) -> subprocess.CompletedProcess:
        self.calls.append({"args": args, "input": input})
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(
            args=args, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def _make_wav_bytes(*, seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


# ------------------------------------------------------------------------- argv shape
def test_ogg_to_wav_builds_expected_argv():
    fake = FakeRunner(returncode=0, stdout=b"wav-data")
    ogg_to_wav(b"ogg-input", runner=fake)

    [call] = fake.calls
    args = call["args"]
    assert args[0] == "ffmpeg"
    assert "pipe:0" in args
    assert "pipe:1" in args
    assert "-ar" in args and args[args.index("-ar") + 1] == "16000"
    assert "-ac" in args and args[args.index("-ac") + 1] == "1"
    assert "-acodec" in args and args[args.index("-acodec") + 1] == "pcm_s16le"
    assert call["input"] == b"ogg-input"


def test_wav_to_ogg_builds_expected_argv():
    fake = FakeRunner(returncode=0, stdout=b"ogg-data")
    wav_to_ogg(b"wav-input", runner=fake)

    [call] = fake.calls
    args = call["args"]
    assert args[0] == "ffmpeg"
    assert "pipe:0" in args
    assert "pipe:1" in args
    assert "-c:a" in args and args[args.index("-c:a") + 1] == "libopus"
    assert "-f" in args and args[args.index("-f") + 1] == "ogg"
    assert call["input"] == b"wav-input"


# ---------------------------------------------------------------------------- success
def test_ogg_to_wav_returns_stdout_verbatim():
    fake = FakeRunner(returncode=0, stdout=b"the-wav-bytes")
    result = ogg_to_wav(b"anything", runner=fake)
    assert result == b"the-wav-bytes"


def test_wav_to_ogg_returns_stdout_verbatim():
    fake = FakeRunner(returncode=0, stdout=b"the-ogg-bytes")
    result = wav_to_ogg(b"anything", runner=fake)
    assert result == b"the-ogg-bytes"


# --------------------------------------------------------------------------- failures
def test_ffmpeg_not_found_raises_transcode_error():
    fake = FakeRunner(raise_exc=FileNotFoundError())
    with pytest.raises(TranscodeError, match="ffmpeg not found"):
        ogg_to_wav(b"anything", runner=fake)


def test_nonzero_exit_includes_code_and_stderr():
    fake = FakeRunner(returncode=1, stderr=b"boom")
    with pytest.raises(TranscodeError) as exc_info:
        ogg_to_wav(b"anything", runner=fake)
    assert "1" in str(exc_info.value)
    assert "boom" in str(exc_info.value)


def test_long_stderr_is_truncated():
    long_stderr = b"x" * 2000
    fake = FakeRunner(returncode=1, stderr=long_stderr)
    with pytest.raises(TranscodeError) as exc_info:
        wav_to_ogg(b"anything", runner=fake)
    assert len(str(exc_info.value)) < 1000


def test_empty_stdout_raises_no_output_error():
    fake = FakeRunner(returncode=0, stdout=b"")
    with pytest.raises(TranscodeError, match="no output"):
        ogg_to_wav(b"anything", runner=fake)


# --------------------------------------------------------------------- real ffmpeg
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_round_trip_wav_to_ogg_to_wav(tmp_path):
    wav_bytes = _make_wav_bytes(seconds=1.0, rate=16000)

    ogg_bytes = wav_to_ogg(wav_bytes)
    assert ogg_bytes

    round_tripped = ogg_to_wav(ogg_bytes)
    assert round_tripped

    out_path = tmp_path / "roundtrip.wav"
    out_path.write_bytes(round_tripped)
    info = probe_wav(out_path)
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.sample_width == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ogg_to_wav_produces_canonical_wav(tmp_path):
    wav_bytes = _make_wav_bytes(seconds=0.5, rate=16000)
    ogg_bytes = wav_to_ogg(wav_bytes)

    wav_result = ogg_to_wav(ogg_bytes)

    out_path = tmp_path / "from_ogg.wav"
    out_path.write_bytes(wav_result)
    info = probe_wav(out_path)
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.sample_width == 2
