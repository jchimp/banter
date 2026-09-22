"""leveling.py: gain planning and the leveled-copy writer (PRD FR-28).

Inputs are synthesized so the expected gain is arithmetic, not a golden file: a sine
at amplitude A has RMS A/sqrt(2), so its dBFS is known to the decimal. Every clip
gets quiet edges (leading/trailing silence) because `analyze_wav`'s noise-floor rule
needs them, exactly as a real capture has them.
"""

import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from banter_client.analysis import load_mono
from banter_client.config import ClientSettings
from banter_client.leveling import PEAK_CEILING_DBFS, level_wav, leveler_for, plan_gain_db

RATE = 16000
SILENCE = -45.0
MARGIN = 6.0


def _tone_with_edges(
    amplitude: float, *, seconds: float = 1.0, edge_s: float = 0.2, hz: float = 440.0
) -> np.ndarray:
    """A sine at `amplitude` with `edge_s` of digital silence either side."""
    n = int(RATE * seconds)
    body = amplitude * np.sin(2 * math.pi * hz * np.arange(n) / RATE)
    edge = np.zeros(int(RATE * edge_s))
    return np.concatenate([edge, body, edge])


def _write(path: Path, x: np.ndarray) -> Path:
    pcm = b"".join(struct.pack("<h", int(v * 32767)) for v in np.clip(x, -1, 1))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm)
    return path


def _rms_dbfs(x: np.ndarray) -> float:
    return 20 * math.log10(math.sqrt(float(np.mean(x**2))))


def _plan(x: np.ndarray, **kw):
    defaults = dict(
        target_dbfs=-16.0, silence_dbfs=SILENCE, voice_margin_db=MARGIN, max_gain_db=20.0
    )
    defaults.update(kw)
    return plan_gain_db(x, RATE, **defaults)


# ---------------------------------------------------------------- plan_gain_db
def test_quiet_clip_is_boosted_to_target():
    x = _tone_with_edges(0.05)  # RMS ~ -29 dBFS
    gain, level, _peak = _plan(x)
    assert level == pytest.approx(_rms_dbfs(x[3200:-3200]), abs=0.2)
    assert gain == pytest.approx(-16.0 - level, abs=0.2)
    assert gain > 0


def test_loud_clip_is_pulled_down():
    x = _tone_with_edges(0.9)  # RMS ~ -4 dBFS: louder than target, must attenuate
    gain, _level, _peak = _plan(x)
    assert gain < 0
    assert gain == pytest.approx(-16.0 - _rms_dbfs(x[3200:-3200]), abs=0.2)


def test_boost_is_capped_by_max_gain():
    x = _tone_with_edges(0.01)  # RMS ~ -43 dBFS: just voiced, would want +27 dB
    gain, _level, _peak = _plan(x, max_gain_db=12.0)
    assert gain == pytest.approx(12.0)


def test_boost_is_capped_by_the_peak_ceiling():
    """A clip with one big transient and a low average gets only as much gain as
    keeps the transient under the ceiling -- never a clipped consonant."""
    x = _tone_with_edges(0.05)
    x[len(x) // 2] = 0.5  # a single -6 dBFS spike
    gain, _level, peak = _plan(x)
    assert peak == pytest.approx(20 * math.log10(0.5), abs=0.01)
    assert gain == pytest.approx(PEAK_CEILING_DBFS - peak, abs=0.01)
    assert gain < 10  # far less than the ~13 dB the average alone would ask for


def test_target_is_a_parameter():
    x = _tone_with_edges(0.05)
    quiet, _, _ = _plan(x, target_dbfs=-20.0)
    loud, _, _ = _plan(x, target_dbfs=-12.0)
    assert loud - quiet == pytest.approx(8.0, abs=0.01)


def test_silence_has_no_plan():
    assert _plan(np.zeros(RATE)) is None
    assert _plan(np.zeros(0)) is None


def test_below_silence_threshold_has_no_plan():
    # Peak -60 dBFS: every window is under `silence_dbfs`, so nothing counts as
    # voiced and the clip is left alone rather than boosted into hiss.
    assert _plan(_tone_with_edges(0.001)) is None


# ------------------------------------------------------------------- level_wav
def _kw(**over):
    d = dict(target_dbfs=-16.0, silence_dbfs=SILENCE, voice_margin_db=MARGIN, max_gain_db=20.0)
    d.update(over)
    return d


def test_level_wav_writes_a_clip_at_target(tmp_path):
    src = _write(tmp_path / "quiet.wav", _tone_with_edges(0.05))
    dest = tmp_path / "leveled.wav"

    gain = level_wav(src, dest, **_kw())

    assert gain is not None and gain > 0
    y, rate = load_mono(dest)
    assert rate == RATE
    assert y.size == load_mono(src)[0].size
    assert _rms_dbfs(y[3200:-3200]) == pytest.approx(-16.0, abs=0.2)
    assert not list(tmp_path.glob("*.leveling"))  # tmp renamed away
    assert dest.stat().st_size > 44


def test_level_wav_never_exceeds_int16(tmp_path):
    x = _tone_with_edges(0.05)
    x[len(x) // 2] = 0.5
    src = _write(tmp_path / "spiky.wav", x)
    dest = tmp_path / "out.wav"

    level_wav(src, dest, **_kw())

    y, _ = load_mono(dest)
    assert float(np.max(np.abs(y))) <= 10 ** (PEAK_CEILING_DBFS / 20) + 1e-3


def test_level_wav_copies_silence_unchanged(tmp_path):
    src = _write(tmp_path / "silent.wav", np.zeros(RATE))
    dest = tmp_path / "out.wav"

    assert level_wav(src, dest, **_kw()) is None
    assert dest.read_bytes() == src.read_bytes()


def test_level_wav_copies_a_non_wav_unchanged(tmp_path):
    src = tmp_path / "junk.wav"
    src.write_bytes(b"<html>captive portal</html>")
    dest = tmp_path / "out.wav"

    assert level_wav(src, dest, **_kw()) is None
    assert dest.read_bytes() == src.read_bytes()


def test_level_wav_missing_source_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        level_wav(tmp_path / "nope.wav", tmp_path / "out.wav", **_kw())


def test_level_wav_logs_the_gain(tmp_path, caplog):
    src = _write(tmp_path / "quiet.wav", _tone_with_edges(0.05))
    with caplog.at_level("INFO"):
        level_wav(src, tmp_path / "out.wav", **_kw())
    assert "event=leveled file=quiet.wav gain_db=+" in caplog.text


# ----------------------------------------------------------------- leveler_for
def _settings(**over) -> ClientSettings:
    d = dict(audio_backend="synthetic", ring_backend="null", _env_file=None)
    d.update(over)
    return ClientSettings(**d)


def test_leveler_for_is_none_when_disabled():
    assert leveler_for(_settings(play_leveling=False)) is None


def test_leveler_for_binds_the_settings(tmp_path):
    settings = _settings(play_target_dbfs=-12.0, play_max_gain_db=3.0)
    leveler = leveler_for(settings)
    assert leveler is not None
    src = _write(tmp_path / "quiet.wav", _tone_with_edges(0.05))

    gain = leveler(src, tmp_path / "out.wav")

    assert gain == pytest.approx(3.0)  # the max_gain cap from settings, not the default
