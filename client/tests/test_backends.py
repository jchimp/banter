"""Backends + a full record/play loop with no hardware and no server.

If these pass on your laptop, the loop works; only the backend swaps on the Pi.
"""

import io
import math
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from banter_client.backends.audio import (
    SyntheticAudio,
    alsa_device_ok,
    amixer_cmd,
    aplay_cmd,
    arecord_cmd,
    card_from_device,
    parse_cards,
    parse_pcm_names,
    set_mixer_level,
)
from banter_client.backends.base import AudioBackend, ButtonBackend, ButtonCallbacks, RingBackend
from banter_client.backends.factory import make_audio, make_buttons, make_ring
from banter_client.backends.io import KeyboardButtons, NullRing
from banter_client.config import ClientSettings
from banter_client.controller import RecordController
from banter_client.demo import Demo
from banter_client.queue import RecordingMeta
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
        # Loop tests press and release back to back; the arm guard and tones have their
        # own tests further down.
        record_arm_seconds=0.0,
        tones=False,
        # ...and SyntheticAudio's back-to-back capture is ~50 ms of tone: too short
        # to have the dynamics the content gate wants, so that gate is off here.
        min_voiced_seconds=0.0,
        # Auto-replay after record (FR-27) is covered in test_player.py; here it would
        # start a playback in the middle of a loop assertion.
        auto_replay_seconds=0.0,
        _env_file=None,
    )


def _pcm(seconds: float, *, tone_hz: float | None = 440.0, amplitude: int = 12000) -> bytes:
    """16 kHz mono 16-bit frames: a pulsed tone (300 ms on / 100 ms off, like
    SyntheticAudio) or, with `tone_hz=None`, digital silence."""
    rate, n = 16000, int(16000 * seconds)
    if tone_hz is None:
        return b"\x00\x00" * n
    return b"".join(
        struct.pack(
            "<h",
            int(amplitude * math.sin(2 * math.pi * tone_hz * i / rate)) if i % 6400 < 4800 else 0,
        )
        for i in range(n)
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


# ------------------------------------------------------- alsa device preflight
# Trimmed but verbatim-shaped output from a Pi 4 with a USB webcam + USB speaker.
ARECORD_L = """null
    Discard all samples (playback) or generate zero samples (capture)
plughw:CARD=Webcam,DEV=0
    USB Webcam, USB Audio
    Hardware device with all software conversions
sysdefault:CARD=Webcam
    USB Webcam, USB Audio
"""

ARECORD_l = """**** List of CAPTURE Hardware Devices ****
card 1: Webcam [USB Webcam], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: Speaker [USB Speaker], device 0: USB Audio [USB Audio]
"""


def test_parse_pcm_names_takes_unindented_lines():
    assert parse_pcm_names(ARECORD_L) == [
        "null",
        "plughw:CARD=Webcam,DEV=0",
        "sysdefault:CARD=Webcam",
    ]


def test_parse_cards_maps_index_to_name():
    assert parse_cards(ARECORD_l) == {1: "Webcam", 2: "Speaker"}


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        ("plughw:CARD=Webcam,DEV=0", True),  # exact PCM name from -L
        ("sysdefault:CARD=Webcam", True),
        ("plughw:1,0", True),  # by card index
        ("hw:2,0", True),
        ("plughw:CARD=Speaker,DEV=0", True),  # by card name, not listed in -L
        ("plughw:7,0", False),  # no such card
        ("plughw:CARD=CodecZero,DEV=0", False),  # HAT profile's device on a USB box
        ("gibberish", False),
    ],
)
def test_alsa_device_ok(device, expected):
    pcms, cards = parse_pcm_names(ARECORD_L), parse_cards(ARECORD_l)
    assert alsa_device_ok(device, pcms, cards) is expected


def test_alsa_device_ok_with_empty_listings_rejects():
    # check_alsa_devices skips the check entirely when both listings are empty; this
    # just pins the pure function's own behaviour.
    assert alsa_device_ok("plughw:1,0", [], {}) is False


# --------------------------------------------------------------------- mixer
@pytest.mark.parametrize(
    "device, expected",
    [
        ("plughw:0,0", "0"),
        ("hw:1", "1"),
        ("plughw:CARD=Speaker,DEV=0", "Speaker"),
        ("sysdefault:CARD=Webcam", "Webcam"),
        ("default", None),
        ("", None),
    ],
)
def test_card_from_device(device, expected):
    assert card_from_device(device) == expected


def test_amixer_cmd_shape():
    assert amixer_cmd("0", "Master", 80) == ["amixer", "-q", "-c", "0", "sset", "Master", "80%"]


def test_set_mixer_level_runs_amixer(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert set_mixer_level("plughw:CARD=Speaker,DEV=0", "PCM", 75) is None
    assert calls == [["amixer", "-q", "-c", "Speaker", "sset", "PCM", "75%"]]


def test_set_mixer_level_reports_a_bad_control(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="amixer: Unable to find simple control 'Nope',0"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    problem = set_mixer_level("plughw:0,0", "Nope", 80)
    assert problem is not None
    assert "Unable to find simple control" in problem


def test_set_mixer_level_reports_a_missing_amixer(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError("amixer")

    monkeypatch.setattr(subprocess, "run", fake_run)
    problem = set_mixer_level("plughw:0,0", "Master", 80)
    assert problem is not None and "amixer" in problem


def test_set_mixer_level_needs_a_card():
    # `default` names no card, so there is nothing for -c; say so instead of guessing 0.
    problem = set_mixer_level("default", "Master", 80)
    assert problem is not None and "names no card" in problem


# ------------------------------------------------------------------- factory
def test_factory_selects_simulated_backends(sim_settings):
    audio = make_audio(sim_settings)
    ring = make_ring(sim_settings)
    buttons = make_buttons(sim_settings, ButtonCallbacks(on_record_press=lambda: None))
    assert isinstance(audio, AudioBackend)
    assert isinstance(ring, RingBackend)
    assert isinstance(buttons, ButtonBackend)


def test_keyboard_buttons_dispatch_replay_key(monkeypatch):
    """`l` is BTN3 on the laptop. First direct coverage of the stdin key map."""
    fired: list[str] = []
    cb = ButtonCallbacks(
        on_record_press=lambda: fired.append("record"),
        on_play_press=lambda: fired.append("play"),
        on_replay_press=lambda: fired.append("replay"),
        on_quit=lambda: fired.append("quit"),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("l\np\nl\nq\n"))
    buttons = KeyboardButtons(cb)
    buttons.start()
    buttons._thread.join(timeout=2.0)
    assert fired == ["replay", "play", "replay", "quit"]
    assert buttons.held_pins() == []


def test_button_callbacks_replay_defaults_to_noop(sim_settings):
    cb = ButtonCallbacks(on_record_press=lambda: None)
    cb.on_replay_press()  # must not raise: old callers never pass it
    assert isinstance(make_buttons(sim_settings, cb), ButtonBackend)


def test_neopixel_ring_falls_back_when_hardware_is_absent(sim_settings, caplog):
    """SPI off / no Blinka must degrade to NullRing, not kill the process.

    Needs no mocking: off-Pi the `import board` inside NeoPixelRing already fails, which
    is exactly the failure this guards. Unguarded it propagates out of App.__init__ and
    systemd's start limit turns it into a permanently dead unit.
    """
    s = sim_settings.model_copy(update={"ring_backend": "neopixel"})
    with caplog.at_level("ERROR"):
        ring = make_ring(s)
    assert isinstance(ring, NullRing)
    assert isinstance(ring, RingBackend)
    assert "event=ring_unavailable" in caplog.text


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
# `RecordController` is the real code path both `demo.py` and `__main__.py` share
# (Step 5), so the loop coverage below drives it directly. One `Demo`-based test is
# kept so `banter-demo` itself stays exercised end to end.
def test_record_then_play_loop(sim_settings):
    controller = RecordController(sim_settings, ring=NullRing())

    controller.start_record()
    assert controller.machine.state is State.RECORDING
    controller.stop_record()
    assert controller.machine.state is State.IDLE

    clips = list(sim_settings.queue_dir.glob("*.wav"))
    assert len(clips) == 1

    controller.play_latest()
    assert controller.machine.state is State.PLAYING
    for _ in range(100):
        if controller.machine.state is State.IDLE:
            break
        __import__("time").sleep(0.05)
    assert controller.machine.state is State.IDLE
    assert "playing" in controller.ring.states


def test_demo_record_then_play_loop(sim_settings):
    """`banter-demo`'s own wiring, kept covered end to end (not just via the controller)."""
    demo = Demo(sim_settings)
    demo.ring = NullRing()

    demo.start_record()
    assert demo.machine.state is State.RECORDING
    demo.stop_record()
    assert demo.machine.state is State.IDLE
    assert list(sim_settings.queue_dir.glob("*.wav"))


def test_too_short_recording_is_discarded(tmp_path):
    settings = ClientSettings(
        audio_backend="synthetic",
        ring_backend="null",
        queue_dir=tmp_path / "q",
        cache_dir=tmp_path / "cache",
        min_seconds=99.0,  # nothing can clear this
        record_arm_seconds=0.0,
        tones=False,
        _env_file=None,
    )
    controller = RecordController(settings, ring=NullRing())
    controller.start_record()
    controller.stop_record()
    assert list(settings.queue_dir.glob("*.wav")) == []
    assert "discarded" in controller.ring.flashes


class SilentAudio:
    """A mic that captures nothing: long hold, header-only WAV.

    This is the real failure seen on the box — a busy or dead capture device leaves
    `arecord` writing a 44-byte WAV while the wall clock happily reports a full hold.
    """

    def __init__(self) -> None:
        self.is_recording = False
        self.is_playing = False

    def start_record(self, path: Path) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"")  # zero frames — a valid WAV containing no audio
        self.is_recording = True

    def stop_record(self) -> float:
        self.is_recording = False
        return 5.0  # the button really was held for five seconds

    def play(self, path: Path, on_done=None) -> None:
        self.is_playing = True

    def stop_play(self) -> None:
        self.is_playing = False


def test_recording_with_no_captured_audio_is_discarded(tmp_path):
    """A held button that captured nothing must never reach the queue.

    Guarding on wall-clock duration alone let a 44-byte WAV upload, get transcoded,
    and land on a parent's phone as an unplayable voice note.
    """
    settings = ClientSettings(
        audio_backend="synthetic",
        ring_backend="null",
        queue_dir=tmp_path / "q",
        cache_dir=tmp_path / "cache",
        min_seconds=0.8,
        record_arm_seconds=0.0,
        tones=False,
        _env_file=None,
    )
    saved: list[object] = []
    controller = RecordController(
        settings, audio=SilentAudio(), ring=NullRing(), on_recorded=saved.append
    )

    controller.start_record()
    controller.stop_record()

    assert list(settings.queue_dir.glob("*.wav")) == [], "empty capture must be deleted"
    assert saved == [], "empty capture must not be handed to the uploader"
    assert "discarded" in controller.ring.flashes


class ScriptedAudio:
    """Fake mic + speaker for the arm-guard / gate / tone tests.

    `start_record` writes `pcm` as a real WAV; every `play()` is appended to `events`
    (as `play:<stem>`) and completes immediately, so the order of beep vs capture is
    observable and no test waits on a real backend.
    """

    def __init__(self, pcm: bytes, held: float = 3.0) -> None:
        self.pcm, self.held = pcm, held
        self.events: list[str] = []
        self.is_recording = False
        self.is_playing = False

    def start_record(self, path: Path) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(self.pcm)
        self.events.append("start")
        self.is_recording = True

    def stop_record(self) -> float:
        self.is_recording = False
        return self.held

    def play(self, path: Path, on_done=None) -> None:
        self.events.append(f"play:{path.stem}")
        if on_done:
            on_done()

    def stop_play(self) -> None:
        self.is_playing = False

    def close(self) -> None: ...


def _gate_settings(tmp_path, **overrides) -> ClientSettings:
    defaults = dict(
        audio_backend="synthetic",
        ring_backend="null",
        queue_dir=tmp_path / "q",
        cache_dir=tmp_path / "cache",
        record_arm_seconds=0.0,
        tones=True,
        auto_replay_seconds=0.0,
        _env_file=None,
    )
    defaults.update(overrides)
    return ClientSettings(**defaults)


# -------------------------------------------------------------- arm guard
def test_tap_shorter_than_arm_is_ignored_entirely(tmp_path, caplog):
    """A bump on BTN1 must not beep, must not spawn a capture, must not blip."""
    audio = ScriptedAudio(_pcm(3.0))
    settings = _gate_settings(tmp_path, record_arm_seconds=0.5)
    saved: list[object] = []
    controller = RecordController(settings, audio=audio, ring=NullRing(), on_recorded=saved.append)

    with caplog.at_level("INFO"):
        controller.start_record()
        assert controller.machine.state is State.RECORDING  # BTN2 is locked out at once
        controller.stop_record()

    assert controller.machine.state is State.IDLE
    assert audio.events == []
    assert list(settings.queue_dir.glob("*.wav")) == []
    assert saved == []
    assert "discarded" not in controller.ring.flashes
    assert "event=record_ignored reason=tap" in caplog.text


def test_hold_past_arm_beeps_then_captures(tmp_path):
    audio = ScriptedAudio(_pcm(3.0))
    settings = _gate_settings(tmp_path, record_arm_seconds=0.05)
    saved: list[object] = []
    controller = RecordController(settings, audio=audio, ring=NullRing(), on_recorded=saved.append)

    controller.start_record()
    deadline = time.monotonic() + 2.0
    while "start" not in audio.events and time.monotonic() < deadline:
        time.sleep(0.01)
    controller.stop_record()

    assert audio.events[:2] == ["play:record", "start"], "beep must finish before capture"
    assert len(saved) == 1
    assert controller.machine.state is State.IDLE


def test_arm_disabled_captures_synchronously(tmp_path):
    audio = ScriptedAudio(_pcm(3.0))
    controller = RecordController(_gate_settings(tmp_path), audio=audio, ring=NullRing())
    controller.start_record()
    assert audio.events == ["play:record", "start"]
    controller.stop_record()
    assert "success" in controller.ring.flashes


# ---------------------------------------------------------- content gate
def test_silent_capture_is_discarded_with_blip(tmp_path, caplog):
    """Three seconds of digital silence: long enough for FR-3, still not a joke."""
    audio = ScriptedAudio(_pcm(3.0, tone_hz=None))
    settings = _gate_settings(tmp_path)
    saved: list[object] = []
    controller = RecordController(settings, audio=audio, ring=NullRing(), on_recorded=saved.append)

    with caplog.at_level("INFO"):
        controller.start_record()
        controller.stop_record()

    assert list(settings.queue_dir.glob("*.wav")) == []
    assert saved == []
    assert "discarded" in controller.ring.flashes
    assert audio.events == ["play:record", "start", "play:discard"]
    assert "event=discarded reason=silent" in caplog.text
    assert "peak_dbfs=-100.0" in caplog.text


def test_low_content_capture_is_discarded(tmp_path, caplog):
    """One short noise in a long quiet hold is not worth a parent's phone buzzing."""
    audio = ScriptedAudio(_pcm(0.2, tone_hz=440.0) + _pcm(4.0, tone_hz=None))
    settings = _gate_settings(tmp_path, tones=False)
    controller = RecordController(settings, audio=audio, ring=NullRing())

    with caplog.at_level("INFO"):
        controller.start_record()
        controller.stop_record()

    assert list(settings.queue_dir.glob("*.wav")) == []
    assert "event=discarded reason=low_content" in caplog.text


def test_content_gate_can_be_disabled(tmp_path):
    audio = ScriptedAudio(_pcm(0.2, tone_hz=440.0) + _pcm(4.0, tone_hz=None))
    settings = _gate_settings(tmp_path, tones=False, min_voiced_seconds=0.0)
    controller = RecordController(settings, audio=audio, ring=NullRing())
    controller.start_record()
    controller.stop_record()
    assert len(list(settings.queue_dir.glob("*.wav"))) == 1


def test_saved_clip_logs_tuning_stats(tmp_path, caplog):
    audio = ScriptedAudio(_pcm(2.0))
    settings = _gate_settings(tmp_path, tones=False)
    controller = RecordController(settings, audio=audio, ring=NullRing())
    with caplog.at_level("INFO"):
        controller.start_record()
        controller.stop_record()
    assert "event=saved" in caplog.text
    assert "peak_dbfs=" in caplog.text and "voiced=" in caplog.text


def test_tones_off_never_calls_play(tmp_path):
    audio = ScriptedAudio(_pcm(3.0, tone_hz=None))
    settings = _gate_settings(tmp_path, tones=False)
    controller = RecordController(settings, audio=audio, ring=NullRing())
    controller.start_record()
    controller.stop_record()  # discarded as silent -> would blip if tones were on
    assert audio.events == ["start"]
    assert not (controller.s.cache_dir / "tones").exists()


def test_play_ignored_while_recording(sim_settings):
    controller = RecordController(sim_settings, ring=NullRing())
    controller.start_record()
    controller.play_latest()  # FR-10
    assert controller.machine.state is State.RECORDING
    controller.stop_record()


def test_play_with_empty_queue_recovers(sim_settings):
    controller = RecordController(sim_settings, ring=NullRing())
    controller.play_latest()
    assert controller.machine.state is State.IDLE
    assert "error" in controller.ring.flashes


def test_on_recorded_fires_once_with_matching_id(sim_settings):
    calls: list[RecordingMeta] = []
    controller = RecordController(sim_settings, ring=NullRing(), on_recorded=calls.append)

    controller.start_record()
    controller.stop_record()

    assert len(calls) == 1
    meta = calls[0]
    [wav] = sim_settings.queue_dir.glob("*.wav")
    assert meta.id == wav.stem
