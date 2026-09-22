"""Silence / low-content detection for a just-finished capture.

Pure: takes a WAV path and numbers, returns numbers. No settings object, no logging, no
side effects — same discipline as the server's `select_next()`, and for the same
reason: the thresholds are tunable and the rules have to be testable in isolation.

Two measurements drive the decision (`reject_reason`):

* `peak_dbfs` — loudest single sample. Below `silence_dbfs` means the mic produced
  nothing usable at all (dead mic, wrong ALSA device, nobody there): reason `silent`.
* `voiced_seconds` — how much of the clip is loud enough to be *something*. A 100 ms
  window counts as voiced when its RMS is above `silence_dbfs` AND above the clip's own
  noise floor by `voice_margin_db`. The second condition is what separates one cough
  in a room with a fan from a joke: a hum sits at a constant level, speech doesn't.
  Fewer voiced seconds than `min_voiced_seconds`: reason `low_content`.

The noise floor is the 10th-percentile window RMS, so a clip that is loud from the
first sample to the last has no headroom above its floor. Real captures always have
quiet edges (the beep-to-talk gap, the trailing time before release), so this only
bites signals with no dynamics at all — which is what it is meant to bite.
"""

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# dBFS reported for digital silence; keeps log lines finite.
DBFS_FLOOR = -100.0
FLOOR_PERCENTILE = 10


@dataclass(frozen=True)
class ClipStats:
    duration_s: float
    peak_dbfs: float
    noise_floor_dbfs: float
    voiced_seconds: float


def _dbfs(values: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(values, 10 ** (DBFS_FLOOR / 20.0)))


def load_mono(path: Path) -> tuple[np.ndarray, int] | None:
    """Samples in [-1, 1] (float64) and the sample rate, or None if `path` is not a
    readable 16-bit PCM WAV. Multi-channel input is averaged to mono. Shared with
    `leveling.py` so both measure exactly the same signal."""
    try:
        with wave.open(str(path), "rb") as wf:
            rate, channels, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, OSError, EOFError):
        return None
    if rate <= 0 or width != 2 or channels < 1:
        return None

    samples = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        usable = len(samples) - len(samples) % channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float64) / 32768.0, rate


def window_rms_db(x: np.ndarray, rate: int, window_ms: int) -> np.ndarray:
    """RMS in dBFS of each consecutive `window_ms` slice of `x`. A clip shorter than
    one window is a single window. `x` must be non-empty."""
    win = max(1, int(rate * window_ms / 1000))
    n = x.size // win
    windows = x[: n * win].reshape(n, win) if n else x.reshape(1, -1)
    return _dbfs(np.sqrt(np.mean(windows**2, axis=1)))


def analyze_wav(
    path: Path,
    *,
    silence_dbfs: float,
    voice_margin_db: float,
    window_ms: int = 100,
) -> ClipStats | None:
    """Measure `path`. Returns None if it is not a readable 16-bit PCM WAV.

    Args:
        path: WAV file, 16-bit PCM. Multi-channel input is averaged to mono.
        silence_dbfs: window RMS at or below this never counts as voiced.
        voice_margin_db: a voiced window must also clear the noise floor by this much.
        window_ms: analysis window; 100 ms is roughly one syllable.
    """
    loaded = load_mono(path)
    if loaded is None:
        return None
    x, rate = loaded
    if x.size == 0:
        return ClipStats(0.0, DBFS_FLOOR, DBFS_FLOOR, 0.0)

    duration = x.size / rate
    peak = float(_dbfs(np.array([np.max(np.abs(x))]))[0])

    rms_db = window_rms_db(x, rate, window_ms)
    floor = float(np.percentile(rms_db, FLOOR_PERCENTILE))
    threshold = max(silence_dbfs, floor + voice_margin_db)
    # >= so that voice_margin_db=0 counts a window sitting exactly on the floor.
    window_seconds = min(max(1, int(rate * window_ms / 1000)), x.size) / rate
    voiced = int(np.count_nonzero(rms_db >= threshold)) * window_seconds

    return ClipStats(duration, peak, floor, float(voiced))


def reject_reason(
    stats: ClipStats, *, silence_dbfs: float, min_voiced_seconds: float
) -> str | None:
    """Why this clip should be discarded, or None to keep it.

    `min_voiced_seconds=0` disables the content gate; the silence gate always applies.
    """
    if stats.peak_dbfs < silence_dbfs:
        return "silent"
    if min_voiced_seconds > 0 and stats.voiced_seconds < min_voiced_seconds:
        return "low_content"
    return None
