"""Backends + a full record/play loop with no hardware and no server.

If these pass on your laptop, the loop works; only the backend swaps on the Pi.
"""

import wave

import pytest

from banter_client.backends.audio import SyntheticAudio, aplay_cmd, arecord_cmd
from banter_client.backends.base import AudioBackend, ButtonBackend, ButtonCallbacks, RingBackend
from banter_client.backends.factory import make_audio, make_buttons, make_ring
from banter_client.backends.io import NullRing
from banter_client.config import ClientSettings
from banter_client.demo import Demo
from banter_client.state import State


@pytest.fixture
def sim_settings(tmp_path) -> ClientSettings:
    return ClientSettings(
        audio_backend="synthetic",
        button_backend="keyboard",
        ring_backend="null",
        queue_dir=tmp_path / "queue",
        cache_dir=tmp_path / "cache",
        min_seconds=0.0,
        _env_file=None,
    )


# ------------------------------------------------------------------ commands
def test_arecord_cmd_shape():
    cmd = arecord_cmd("plughw:1,0", 16000, 60, "/tmp/a.wav")
    assert cmd[0] == "arecord"
    assert "-D" in cmd and "plughw:1,0" in cmd
    assert "16000" in cmd and cmd[-1] == "/tmp/a.wav"
    assert "S16_LE" in cmd  # mono 16-bit is what the server expects


def test_aplay_cmd_shape():
    assert aplay_cmd("plughw:1,0", "/tmp/a.wav") == ["aplay", "-D", "plughw:1,0", "/tmp/a.wav"]


# ------------------------------------------------------------------- factory
def test_factory_selects_simulated_backends(sim_settings):
    audio = make_audio(sim_settings)
    ring = make_ring(sim_settings)
    buttons = make_buttons(sim_settings, ButtonCallbacks(on_record_press=lambda: None))
    assert isinstance(audio, AudioBackend)
    assert isinstance(ring, RingBackend)
    assert isinstance(buttons, ButtonBackend)


@pytest.mark.parametrize(
    ("field", "value"),
    [("audio_backend", "bogus"), ("button_backend", "bogus"), ("ring_backend", "bogus")],
)
def test_unknown_backend_rejected_at_config(field, value):
    with pytest.raises(ValueError):
        ClientSettings(**{field: value}, _env_file=None)


# ---------------------------------------------------------------- synthetic
def test_synthetic_audio_writes_a_real_wav(tmp_path):
    audio = SyntheticAudio(rate=16000)
    path = tmp_path / "t.wav"
    audio.start_record(path)
    assert audio.is_recording
    duration = audio.stop_record()
    assert duration > 0
    assert path.exists()
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() > 0


# --------------------------------------------------------------- full loop
def test_record_then_play_loop(sim_settings):
    demo = Demo(sim_settings)
    demo.ring = NullRing()

    demo.start_record()
    assert demo.machine.state is State.RECORDING
    demo.stop_record()
    assert demo.machine.state is State.IDLE

    clips = list(sim_settings.queue_dir.glob("*.wav"))
    assert len(clips) == 1

    demo.play_latest()
    assert demo.machine.state is State.PLAYING
    for _ in range(100):
        if demo.machine.state is State.IDLE:
            break
        __import__("time").sleep(0.05)
    assert demo.machine.state is State.IDLE
    assert "playing" in demo.ring.states


def test_too_short_recording_is_discarded(tmp_path):
    settings = ClientSettings(
        audio_backend="synthetic",
        ring_backend="null",
        queue_dir=tmp_path / "q",
        min_seconds=99.0,  # nothing can clear this
        _env_file=None,
    )
    demo = Demo(settings)
    demo.ring = NullRing()
    demo.start_record()
    demo.stop_record()
    assert list(settings.queue_dir.glob("*.wav")) == []
    assert "discarded" in demo.ring.flashes


def test_play_ignored_while_recording(sim_settings):
    demo = Demo(sim_settings)
    demo.ring = NullRing()
    demo.start_record()
    demo.play_latest()  # FR-10
    assert demo.machine.state is State.RECORDING
    demo.stop_record()


def test_play_with_empty_queue_recovers(sim_settings):
    demo = Demo(sim_settings)
    demo.ring = NullRing()
    demo.play_latest()
    assert demo.machine.state is State.IDLE
    assert "error" in demo.ring.flashes
