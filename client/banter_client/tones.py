"""Prompt tones: the "go ahead" beep before capture and the double-blip on discard.

Rendered to WAV at startup and played through the ordinary `AudioBackend.play()`, so
there is no second audio path to keep in sync with the hardware seam and every backend
(alsa, sounddevice, synthetic) gets them for free. Same 16 kHz / mono / 16-bit format
the captures use, so `aplay -D <playback>` needs no conversion.
"""

import math
import struct
import wave
from pathlib import Path

# (hz, ms) per note. A discard is two falling notes so it reads as "no" even with the
# ring off (the Pi 4 build today).
TONES: dict[str, list[tuple[float, int]]] = {
    "record": [(880.0, 120)],
    "discard": [(440.0, 80), (330.0, 80)],
}
FADE_MS = 5  # linear fade at both ends of every note; a hard edge clicks on a speaker


def render_notes(
    notes: list[tuple[float, int]], *, rate: int, volume: float, gap_ms: int = 40
) -> bytes:
    """16-bit mono PCM for `notes`, with `gap_ms` of silence between them."""
    amp = max(0.0, min(1.0, volume)) * 32767
    fade = int(rate * FADE_MS / 1000)
    out: list[bytes] = []
    for i, (hz, ms) in enumerate(notes):
        if i:
            out.append(b"\x00\x00" * int(rate * gap_ms / 1000))
        n = int(rate * ms / 1000)
        for k in range(n):
            env = min(1.0, (k + 1) / fade, (n - k) / fade) if fade else 1.0
            out.append(struct.pack("<h", int(amp * env * math.sin(2 * math.pi * hz * k / rate))))
    return b"".join(out)


def write_tone(
    path: Path, notes: list[tuple[float, int]], *, rate: int, volume: float, gap_ms: int = 40
) -> float:
    """Write `notes` to `path` as WAV. Returns the clip length in seconds."""
    pcm = render_notes(notes, rate=rate, volume=volume, gap_ms=gap_ms)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return len(pcm) / 2 / rate


def ensure_tones(tone_dir: Path, *, rate: int, volume: float) -> dict[str, tuple[Path, float]]:
    """(Re)generate every tone in `TONES` under `tone_dir`; returns name -> (path, seconds).

    Always rewrites: it is a few KB, and a changed `tone_volume` must take effect on the
    next start without anyone remembering to clear a cache.
    """
    out: dict[str, tuple[Path, float]] = {}
    for name, notes in TONES.items():
        path = tone_dir / f"{name}.wav"
        out[name] = (path, write_tone(path, notes, rate=rate, volume=volume))
    return out
