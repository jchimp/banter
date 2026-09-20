"""tones.py: generated prompt tones are real, correctly sized, click-free WAVs."""

import wave

import numpy as np
import pytest

from banter_client.tones import TONES, ensure_tones, render_notes, write_tone


def _samples(path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 16000)
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")


def test_ensure_tones_writes_every_tone(tmp_path):
    tones = ensure_tones(tmp_path / "tones", rate=16000, volume=0.5)
    assert set(tones) == set(TONES)
    for name, (path, seconds) in tones.items():
        assert path == tmp_path / "tones" / f"{name}.wav"
        assert len(_samples(path)) == pytest.approx(seconds * 16000, abs=1)


def test_tone_lengths():
    assert len(render_notes(TONES["record"], rate=16000, volume=1.0)) == 2 * 1920
    # 80 ms + 40 ms gap + 80 ms
    assert len(render_notes(TONES["discard"], rate=16000, volume=1.0)) == 2 * 3200


def test_volume_scales_amplitude_and_zero_is_silent(tmp_path):
    write_tone(tmp_path / "half.wav", [(880.0, 120)], rate=16000, volume=0.5)
    write_tone(tmp_path / "off.wav", [(880.0, 120)], rate=16000, volume=0.0)
    half, off = _samples(tmp_path / "half.wav"), _samples(tmp_path / "off.wav")
    assert np.max(np.abs(half)) == pytest.approx(0.5 * 32767, rel=0.02)
    assert not off.any()


def test_notes_fade_in_and_out(tmp_path):
    """A hard edge clicks on a speaker; the first and last sample of a note stay near 0."""
    write_tone(tmp_path / "t.wav", [(880.0, 120)], rate=16000, volume=1.0)
    x = _samples(tmp_path / "t.wav")
    assert abs(int(x[0])) < 2000 and abs(int(x[-1])) < 2000
    assert np.max(np.abs(x)) > 30000


def test_regenerated_on_every_call(tmp_path):
    """A changed tone_volume must take effect on the next start."""
    ensure_tones(tmp_path, rate=16000, volume=1.0)
    ensure_tones(tmp_path, rate=16000, volume=0.1)
    assert np.max(np.abs(_samples(tmp_path / "record.wav"))) < 0.15 * 32767
