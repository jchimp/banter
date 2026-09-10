"""Hardware seam.

Everything above these protocols is identical on a laptop and on the Pi. Only the
implementation swaps, chosen by config:

    audio   alsa       -> arecord/aplay          (Pi, Codec Zero)
            sounddevice-> PortAudio              (laptop mic/speakers, any OS)
            synthetic  -> generated tone, no I/O  (CI, headless tests)

    buttons gpio       -> gpiozero, pins from config (Pi)
            keyboard   -> stdin: r / p / q       (laptop)

    ring    neopixel   -> WS2812 over SPI        (Pi)
            terminal   -> ANSI colour blocks     (laptop)
            null       -> nothing                (CI)

Rule for M1/M2: code against these protocols, never against arecord or gpiozero
directly. That is what keeps the loop testable before the parts arrive.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioBackend(Protocol):
    def start_record(self, path: Path) -> None:
        """Begin capturing to `path` (16-bit mono WAV). Non-blocking."""

    def stop_record(self) -> float:
        """Stop capture. Returns duration in seconds (0.0 if nothing was recording)."""

    def play(self, path: Path, on_done: Callable[[], None] | None = None) -> None:
        """Start playback. Non-blocking; calls `on_done` when finished or stopped."""

    def stop_play(self) -> None:
        """Stop playback early. Safe to call when idle."""

    @property
    def is_recording(self) -> bool: ...

    @property
    def is_playing(self) -> bool: ...

    def close(self) -> None: ...


@runtime_checkable
class ButtonBackend(Protocol):
    def start(self) -> None:
        """Begin delivering callbacks. Non-blocking."""

    def close(self) -> None: ...


@runtime_checkable
class RingBackend(Protocol):
    def show(self, state: str) -> None:
        """Render a device state ('idle', 'recording', 'playing', ...)."""

    def flash(self, kind: str) -> None:
        """One-shot feedback ('success', 'error', 'discarded')."""

    def close(self) -> None: ...


#: Callback bundle handed to a ButtonBackend. Release is unused in toggle mode.
class ButtonCallbacks:
    def __init__(
        self,
        on_record_press: Callable[[], None],
        on_record_release: Callable[[], None] | None = None,
        on_play_press: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self.on_record_press = on_record_press
        self.on_record_release = on_record_release or (lambda: None)
        self.on_play_press = on_play_press or (lambda: None)
        self.on_quit = on_quit or (lambda: None)
