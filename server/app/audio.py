"""WAV helpers: probe, canonical-format check, canonical/staging paths, ISO8601 parsing.

Pure and dependency-free (stdlib `wave` only) — no ffmpeg, no subprocess. Transcoding
(e.g. Telegram's OGG/Opus) is out of scope here; this module only understands WAV.
"""

import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class InvalidAudioError(ValueError):
    """Raised when a file can't be read as a valid WAV."""


@dataclass(frozen=True)
class WavInfo:
    """Metadata probed from a WAV file."""

    duration_ms: int
    sample_rate: int
    channels: int
    sample_width: int
    bytes: int


def probe_wav(path: Path) -> WavInfo:
    """Read WAV header/frame info without decoding samples.

    Raises:
        InvalidAudioError: file isn't a readable WAV, or has a zero framerate.
    """
    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
    except (wave.Error, OSError) as exc:
        raise InvalidAudioError(f"unreadable wav: {path}") from exc

    if framerate <= 0:
        raise InvalidAudioError(f"zero or negative framerate: {path}")

    duration_ms = round(nframes / framerate * 1000)
    return WavInfo(
        duration_ms=duration_ms,
        sample_rate=framerate,
        channels=channels,
        sample_width=sample_width,
        bytes=path.stat().st_size,
    )


def is_canonical(info: WavInfo) -> bool:
    """True when info matches the PRD's canonical format: 16 kHz mono 16-bit."""
    return info.sample_rate == 16000 and info.channels == 1 and info.sample_width == 2


def audio_path(audio_dir: Path, source: str, recorded_at: datetime, rec_id: str) -> Path:
    """Canonical on-disk location: `{audio_dir}/{source}/{YYYYMM}/{id}.wav` (PRD 4).

    Does not create directories — the caller owns that (see main.py's lifespan).
    """
    return audio_dir / source / recorded_at.strftime("%Y%m") / f"{rec_id}.wav"


def incoming_path(audio_dir: Path, rec_id: str) -> Path:
    """Staging location for an in-progress upload, before an atomic `os.replace`."""
    return audio_dir / ".incoming" / f"{rec_id}.wav"


def parse_iso8601(value: str) -> datetime:
    """Tolerant ISO8601 parse of a client-supplied timestamp.

    Accepts a trailing `Z`. Naive input is assumed UTC; aware input is normalised
    to UTC.

    Raises:
        ValueError: `value` isn't a parseable ISO8601 timestamp.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
