"""ffmpeg wrappers for FR-20: OGG/Opus <-> WAV transcoding.

Telegram voice notes arrive as OGG/Opus and must become 16 kHz mono 16-bit WAV before
kidbox ever plays them; outbound kid recordings are WAV and must become OGG/Opus so
Telegram renders them as a proper voice message bubble (not a generic file upload).

Everything here is a blocking `subprocess.run` call piped through stdin/stdout — no
temp files. These calls block, so an async caller (the bot's polling loop, which
shares an event loop with the FastAPI request handlers) MUST run them via
`asyncio.to_thread`, never awaited directly.
"""

import logging
import subprocess
from collections.abc import Callable

log = logging.getLogger("banter.transcode")

_STDERR_TRUNCATE = 500


class TranscodeError(RuntimeError):
    """ffmpeg missing, exited non-zero, or produced no output."""


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _run(args: list[str], *, input: bytes) -> subprocess.CompletedProcess[bytes]:
    """Default runner: `subprocess.run`, capturing stdout/stderr as bytes."""
    return subprocess.run(args, input=input, capture_output=True)


def _invoke(args: list[str], data: bytes, *, runner: Runner) -> bytes:
    """Run an ffmpeg command through `runner`, mapping every failure to `TranscodeError`."""
    try:
        result = runner(args, input=data)
    except FileNotFoundError as exc:
        raise TranscodeError("ffmpeg not found on PATH") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:_STDERR_TRUNCATE]
        raise TranscodeError(f"ffmpeg exited with code {result.returncode}: {stderr}")

    if not result.stdout:
        raise TranscodeError("ffmpeg produced no output")

    return result.stdout


def ogg_to_wav(ogg_bytes: bytes, *, runner: Runner = _run) -> bytes:
    """Transcode OGG/Opus (Telegram voice note) to 16 kHz mono 16-bit WAV.

    16 kHz mono is the kidbox's canonical playback format (see `app.audio`).

    Args:
        ogg_bytes: Raw OGG/Opus bytes, e.g. a downloaded Telegram voice note.
        runner: Injectable subprocess runner; defaults to a real `subprocess.run`.

    Returns:
        WAV bytes: 16 kHz, mono, 16-bit PCM.

    Raises:
        TranscodeError: ffmpeg is missing, exits non-zero, or produces no output.

    Note:
        Blocking. Call via `asyncio.to_thread` from async code.
    """
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-f",
        "wav",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    wav_bytes = _invoke(args, ogg_bytes, runner=runner)
    log.debug(
        "ogg_to_wav | transcode | in_bytes=%d out_bytes=%d",
        len(ogg_bytes),
        len(wav_bytes),
    )
    return wav_bytes


def wav_to_ogg(wav_bytes: bytes, *, runner: Runner = _run) -> bytes:
    """Transcode 16 kHz mono WAV (kid recording) to OGG/Opus for Telegram.

    Opus is encoded at 48 kHz internally regardless of source rate — that's a codec
    constraint, not a quality choice — so the output sample rate (48000) legitimately
    differs from the WAV input rate (16000).

    Args:
        wav_bytes: Raw WAV bytes, typically a kid recording at 16 kHz mono 16-bit.
        runner: Injectable subprocess runner; defaults to a real `subprocess.run`.

    Returns:
        OGG-container Opus bytes suitable for Telegram's `sendVoice`.

    Raises:
        TranscodeError: ffmpeg is missing, exits non-zero, or produces no output.

    Note:
        Blocking. Call via `asyncio.to_thread` from async code.
    """
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-f",
        "ogg",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-ar",
        "48000",
        "-ac",
        "1",
        "pipe:1",
    ]
    ogg_bytes = _invoke(args, wav_bytes, runner=runner)
    log.debug(
        "wav_to_ogg | transcode | in_bytes=%d out_bytes=%d",
        len(wav_bytes),
        len(ogg_bytes),
    )
    return ogg_bytes
