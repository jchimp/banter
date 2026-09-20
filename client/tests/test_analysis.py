"""analysis.py: the silence / low-content gate as a pure function over generated WAVs."""

import wave
from pathlib import Path

import numpy as np
import pytest

from banter_client.analysis import DBFS_FLOOR, ClipStats, analyze_wav, reject_reason

RATE = 16000
DEFAULTS = dict(silence_dbfs=-45.0, voice_margin_db=6.0)


def _write(path: Path, x: np.ndarray, channels: int = 1) -> Path:
    """`x` is float in [-1, 1]; written as 16-bit PCM."""
    pcm = np.clip(x * 32767, -32768, 32767).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm.tobytes())
    return path


def _sine(seconds: float, dbfs: float, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return 10 ** (dbfs / 20) * np.sin(2 * np.pi * hz * t)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds))


def _reason(stats: ClipStats, **over) -> str | None:
    kw = dict(silence_dbfs=-45.0, min_voiced_seconds=0.5)
    kw.update(over)
    return reject_reason(stats, **kw)


def test_digital_silence_is_silent(tmp_path):
    stats = analyze_wav(_write(tmp_path / "z.wav", _silence(5.0)), **DEFAULTS)
    assert stats is not None
    assert stats.duration_s == pytest.approx(5.0)
    assert stats.peak_dbfs == DBFS_FLOOR
    assert stats.voiced_seconds == 0.0
    assert _reason(stats) == "silent"


def test_quiet_room_noise_is_silent(tmp_path):
    noise = np.random.default_rng(0).normal(0.0, 10 ** (-65 / 20), RATE * 5)
    stats = analyze_wav(_write(tmp_path / "n.wav", noise), **DEFAULTS)
    assert stats is not None
    assert stats.peak_dbfs < -45.0
    assert _reason(stats) == "silent"


def test_constant_hum_above_threshold_is_low_content(tmp_path):
    """Loud enough to clear the absolute floor, but no dynamics at all."""
    stats = analyze_wav(_write(tmp_path / "h.wav", _sine(5.0, -35.0, hz=60.0)), **DEFAULTS)
    assert stats is not None
    assert stats.peak_dbfs == pytest.approx(-35.0, abs=0.5)
    assert stats.voiced_seconds == 0.0
    assert _reason(stats) == "low_content"


def test_short_burst_in_long_silence_is_low_content(tmp_path):
    x = np.concatenate([_silence(2.0), _sine(0.3, -10.0), _silence(2.7)])
    stats = analyze_wav(_write(tmp_path / "b.wav", x), **DEFAULTS)
    assert stats is not None
    assert stats.voiced_seconds == pytest.approx(0.3, abs=0.1)
    assert _reason(stats) == "low_content"


def test_one_second_burst_is_kept(tmp_path):
    x = np.concatenate([_silence(2.0), _sine(1.0, -10.0), _silence(2.0)])
    stats = analyze_wav(_write(tmp_path / "k.wav", x), **DEFAULTS)
    assert stats is not None
    assert stats.voiced_seconds == pytest.approx(1.0, abs=0.1)
    assert stats.noise_floor_dbfs == DBFS_FLOOR
    assert _reason(stats) is None


def test_margin_zero_accepts_a_continuous_tone(tmp_path):
    """With the relative rule off, only the absolute threshold decides."""
    path = _write(tmp_path / "c.wav", _sine(2.0, -10.0))
    with_margin = analyze_wav(path, **DEFAULTS)
    no_margin = analyze_wav(path, silence_dbfs=-45.0, voice_margin_db=0.0)
    assert with_margin is not None and no_margin is not None
    assert with_margin.voiced_seconds == 0.0
    assert no_margin.voiced_seconds == pytest.approx(2.0, abs=0.1)
    assert _reason(no_margin) is None


def test_min_voiced_zero_disables_content_gate_only(tmp_path):
    burst = analyze_wav(
        _write(tmp_path / "b.wav", np.concatenate([_sine(0.2, -10.0), _silence(4.0)])),
        **DEFAULTS,
    )
    silent = analyze_wav(_write(tmp_path / "z.wav", _silence(4.0)), **DEFAULTS)
    assert burst is not None and silent is not None
    assert _reason(burst, min_voiced_seconds=0.0) is None
    assert _reason(silent, min_voiced_seconds=0.0) == "silent"


def test_stereo_is_averaged_to_mono(tmp_path):
    x = np.concatenate([_silence(1.0), _sine(1.0, -10.0), _silence(1.0)])
    stats = analyze_wav(_write(tmp_path / "s.wav", x, channels=2), **DEFAULTS)
    assert stats is not None
    assert stats.duration_s == pytest.approx(3.0)
    assert stats.voiced_seconds == pytest.approx(1.0, abs=0.1)


def test_empty_wav_is_silent_not_none(tmp_path):
    stats = analyze_wav(_write(tmp_path / "e.wav", _silence(0.0)), **DEFAULTS)
    assert stats == ClipStats(0.0, DBFS_FLOOR, DBFS_FLOOR, 0.0)


def test_unreadable_file_returns_none(tmp_path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav at all")
    assert analyze_wav(junk, **DEFAULTS) is None
    assert analyze_wav(tmp_path / "missing.wav", **DEFAULTS) is None
