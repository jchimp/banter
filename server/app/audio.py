"""WAV helpers: probe, canonical-format check, canonical/staging paths, ISO8601 parsing.

Most of this module is pure and dependency-free (stdlib `wave` only) — no ffmpeg, no
subprocess. Transcoding (e.g. Telegram's OGG/Opus) is out of scope here; this module
only understands WAV. `resolve_playable_audio` is the one exception: it pulls in
sqlite3/FastAPI/`Settings` because it's the single source of truth for the audio
path-escape guard shared by the API and web-UI playback routes (see its docstring),
and that guard has to live somewhere both routes can call. Do not add further
FastAPI-flavored helpers here beyond that one without reconsidering the module split.
"""

import logging
import sqlite3
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from app.config import Settings

log = logging.getLogger("banter.audio")


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


def resolve_playable_audio(conn: sqlite3.Connection, settings: Settings, rec_id: str) -> Path:
    """Resolve `rec_id` to an existing, in-bounds audio file, or raise a 404.

    This is the SINGLE OWNER of the audio path-escape guard: it looks up the row via
    `store.get_playable` (which already excludes soft-deleted rows), resolves the
    stored `path` against `settings.audio_dir`, and refuses anything that resolves
    outside that tree. Both `/api/recordings/{id}/audio` and the web UI's playback
    route call this — neither may re-derive `is_relative_to`/`.resolve()` itself.
    If a change to the guard is ever needed, it changes here and only here.

    Every failure mode — unknown id, soft-deleted row, path escape, file missing on
    disk — raises the identical 404, so a caller (or an attacker probing ids) can't
    use the response to distinguish "doesn't exist" from "exists but blocked".

    Note: imports `app.store` locally rather than at module level. `store.py` already
    imports `parse_iso8601` from this module, so a top-level `from app import store`
    here would be circular (this module partially initialized while store.py is
    mid-import). Deferring the import to call time breaks the cycle; it is not a
    style choice, it's required — see CLAUDE.md on being explicit about this rather
    than papering over it silently.

    Raises:
        HTTPException: 404 for any of the failure modes described above.
    """
    from app import store

    row = store.get_playable(conn, rec_id)
    if row is None:
        log.info("audio_not_playable | audio | id=%s", rec_id)
        raise HTTPException(status_code=404, detail="recording not found")

    audio_root = settings.audio_dir.resolve()
    resolved = (settings.audio_dir / row["path"]).resolve()
    if not resolved.is_relative_to(audio_root):
        log.error("audio_path_escape | audio | id=%s path=%s", rec_id, row["path"])
        raise HTTPException(status_code=404, detail="recording not found")

    if not resolved.is_file():
        log.error("audio_missing_on_disk | audio | id=%s path=%s", rec_id, row["path"])
        raise HTTPException(status_code=404, detail="recording not found")

    return resolved


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
