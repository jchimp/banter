"""Playback leveling (PRD FR-28): bring every clip the box plays to one loudness.

Why: the prompt tones are a synthesized sine at a fixed amplitude (about -9 dBFS
RMS), while speech from the Codec Zero mic or a parent's phone peaks anywhere and
sits 10-20 dB under the tones on average. Leveling is done once, at write time, on
the derived copies only -- the play cache and BTN3's replay copy -- so the queued
upload and the server archive stay exactly what the mic captured.

The gain is decided from the *voiced* windows (same rule as `analysis.py`), not the
whole file, so pauses and room noise do not drag the measurement down and a quiet
room does not get boosted into hiss. The clip's peak and `max_gain_db` cap the boost.
Pure apart from `level_wav`'s file I/O; no settings object, no state.
"""

import logging
import os
import shutil
import wave
from collections.abc import Callable
from functools import partial
from pathlib import Path

import numpy as np

from banter_client.analysis import DBFS_FLOOR, FLOOR_PERCENTILE, load_mono, window_rms_db
from banter_client.config import ClientSettings

log = logging.getLogger("banter.leveling")

#: Peaks are limited to this after gain. Leaves the speaker a little headroom and
#: keeps the int16 clip from ever wrapping; a hair under the tones' -6 dBFS peak.
PEAK_CEILING_DBFS = -1.0


def plan_gain_db(
    x: np.ndarray,
    rate: int,
    *,
    target_dbfs: float,
    silence_dbfs: float,
    voice_margin_db: float,
    max_gain_db: float,
    window_ms: int = 100,
) -> tuple[float, float, float] | None:
    """Gain (dB) that moves the voiced level of `x` to `target_dbfs`.

    Args:
        x: Mono samples in [-1, 1].
        rate: Sample rate in Hz.
        target_dbfs: Desired mean RMS of the voiced windows.
        silence_dbfs: Windows at or below this never count as voiced.
        voice_margin_db: A voiced window must also clear the noise floor by this much.
        max_gain_db: Never boost more than this, whatever the target says.
        window_ms: Analysis window; 100 ms is roughly one syllable.

    Returns:
        `(gain_db, level_dbfs, peak_dbfs)`, or None when there is nothing voiced to
        measure (digital silence, an empty clip) -- the caller leaves such a clip alone.
        Negative gain is normal for a shouted clip.
    """
    if x.size == 0:
        return None
    peak = float(20.0 * np.log10(max(float(np.max(np.abs(x))), 10 ** (DBFS_FLOOR / 20.0))))
    rms_db = window_rms_db(x, rate, window_ms)
    floor = float(np.percentile(rms_db, FLOOR_PERCENTILE))
    threshold = max(silence_dbfs, floor + voice_margin_db)
    voiced = rms_db[rms_db >= threshold]
    if voiced.size == 0:
        return None
    level = float(np.mean(voiced))
    gain = target_dbfs - level
    # The peak ceiling wins over the target: a clip with big transients and a low
    # average gets less boost rather than a clipped consonant.
    gain = min(gain, max_gain_db, PEAK_CEILING_DBFS - peak)
    return gain, level, peak


def level_wav(
    src: Path,
    dest: Path,
    *,
    target_dbfs: float,
    silence_dbfs: float,
    voice_margin_db: float,
    max_gain_db: float,
) -> float | None:
    """Write a leveled copy of `src` at `dest`. Best-effort, never loses the clip.

    Reads `src` as 16-bit PCM, applies `plan_gain_db`, clips to int16 and writes the
    same rate/mono/16-bit format the box plays. When `src` is unreadable or has no
    voiced audio, `dest` becomes a byte-for-byte copy instead and None is returned,
    so callers can treat leveling as a transparent step. Writes go to a `.tmp` sibling
    first and are renamed into place, like every other writer in the client.

    Returns:
        The gain applied in dB, or None when the clip was copied unchanged.

    Raises:
        OSError: only for a failure to read `src` or write `dest` at all.
    """
    loaded = load_mono(src)
    if loaded is None:
        shutil.copyfile(src, dest)
        return None
    x, rate = loaded
    planned = plan_gain_db(
        x,
        rate,
        target_dbfs=target_dbfs,
        silence_dbfs=silence_dbfs,
        voice_margin_db=voice_margin_db,
        max_gain_db=max_gain_db,
    )
    if planned is None:
        shutil.copyfile(src, dest)
        return None
    gain, level, peak = planned
    scaled = np.clip(x * 10 ** (gain / 20.0), -1.0, 32767 / 32768).astype(np.float64)
    pcm = (scaled * 32768.0).astype("<i2").tobytes()
    tmp = dest.with_name(dest.name + ".leveling")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    os.replace(tmp, dest)
    log.info(
        "event=leveled file=%s gain_db=%+.1f level_dbfs=%.1f peak_dbfs=%.1f",
        src.name,
        gain,
        level,
        peak,
    )
    return gain


Leveler = Callable[[Path, Path], float | None]


def leveler_for(settings: ClientSettings) -> Leveler | None:
    """`level_wav` bound to the settings' thresholds, or None when leveling is off.

    The one place the settings object meets this module, so the cache and the
    controller can take a plain `(src, dest)` callable and stay testable with a fake.
    """
    if not settings.play_leveling:
        return None
    return partial(
        level_wav,
        target_dbfs=settings.play_target_dbfs,
        silence_dbfs=settings.silence_dbfs,
        voice_margin_db=settings.voice_margin_db,
        max_gain_db=settings.play_max_gain_db,
    )
