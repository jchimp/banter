"""Button and ring backends. See backends/base.py for the contract."""

import logging
import sys
import threading

from banter_client.backends.base import ButtonCallbacks

log = logging.getLogger("banter.io")


# --------------------------------------------------------------------- buttons
class GpioButtons:
    """Pi. Arcade buttons wired switch -> GPIO, other lug -> GND (internal pull-up)."""

    def __init__(
        self,
        callbacks: ButtonCallbacks,
        pin_record: int,
        pin_play: int,
        bounce_seconds: float = 0.05,
        mode: str = "hold",
    ) -> None:
        from gpiozero import Button  # lazy: not installed off-hardware

        self.cb, self.mode = callbacks, mode
        self._rec_btn = Button(pin_record, pull_up=True, bounce_time=bounce_seconds)
        self._play_btn = Button(pin_play, pull_up=True, bounce_time=bounce_seconds)
        self._toggled = False

    def start(self) -> None:
        if self.mode == "hold":
            self._rec_btn.when_pressed = self.cb.on_record_press
            self._rec_btn.when_released = self.cb.on_record_release
        else:
            self._rec_btn.when_pressed = self._toggle
        self._play_btn.when_pressed = self.cb.on_play_press

    def _toggle(self) -> None:
        self._toggled = not self._toggled
        (self.cb.on_record_press if self._toggled else self.cb.on_record_release)()

    def close(self) -> None:
        self._rec_btn.close()
        self._play_btn.close()


class KeyboardButtons:
    """Dev box. Line-based stdin so it works in any terminal on any OS.

      r + Enter   toggle recording (start, then stop)
      p + Enter   play
      q + Enter   quit

    Hold-to-record can't be simulated over line input, so the sim is always toggle
    mode. That difference is confined to this class.
    """

    def __init__(self, callbacks: ButtonCallbacks) -> None:
        self.cb = callbacks
        self._recording = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        print("\n  [r] record toggle   [p] play   [q] quit\n", flush=True)
        while not self._stop.is_set():
            try:
                key = sys.stdin.readline().strip().lower()
            except (EOFError, ValueError):
                break
            if not key:
                continue
            if key == "r":
                self._recording = not self._recording
                (self.cb.on_record_press if self._recording else self.cb.on_record_release)()
            elif key == "p":
                self.cb.on_play_press()
            elif key == "q":
                self.cb.on_quit()
                break

    def close(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------------ ring
#: state/flash -> (label, ANSI colour). Mirrors the PRD ring table.
RING_COLORS: dict[str, tuple[str, str]] = {
    "idle": ("idle", "\033[90m"),
    "recording": ("RECORDING", "\033[91m"),
    "uploading": ("uploading", "\033[93m"),
    "queued": ("queued (offline)", "\033[33m"),
    "playing": ("PLAYING", "\033[92m"),
    "error": ("error", "\033[91m"),
    "success": ("ok", "\033[92m"),
    "discarded": ("too short", "\033[93m"),
}


class TerminalRing:
    """Dev box. Prints what the NeoPixel ring would be doing."""

    def _emit(self, key: str) -> None:
        label, color = RING_COLORS.get(key, (key, "\033[0m"))
        print(f"  {color}\u25cf\u25cf\u25cf\033[0m {label}", flush=True)

    def show(self, state: str) -> None:
        self._emit(state)

    def flash(self, kind: str) -> None:
        self._emit(kind)

    def close(self) -> None:
        pass


class NullRing:
    """CI. Records calls so tests can assert on state transitions."""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.flashes: list[str] = []

    def show(self, state: str) -> None:
        self.states.append(state)

    def flash(self, kind: str) -> None:
        self.flashes.append(kind)

    def close(self) -> None:
        pass


class NeoPixelRing:
    """Pi. WS2812 over SPI (GPIO10). Animations land in M5; M0 is solid colour."""

    def __init__(self, pixels: int, max_brightness: float) -> None:
        import board  # lazy: hardware only
        import neopixel_spi

        self._px = neopixel_spi.NeoPixel_SPI(
            board.SPI(), pixels, brightness=max_brightness, auto_write=False
        )

    _RGB = {
        "idle": (0, 0, 0),
        "recording": (255, 40, 0),
        "uploading": (255, 160, 0),
        "queued": (200, 120, 0),
        "playing": (0, 255, 80),
        "error": (255, 0, 0),
        "success": (0, 255, 80),
        "discarded": (255, 160, 0),
    }

    def show(self, state: str) -> None:
        self._px.fill(self._RGB.get(state, (0, 0, 0)))
        self._px.show()

    def flash(self, kind: str) -> None:
        import time

        self.show(kind)
        time.sleep(0.4)
        self.show("idle")

    def close(self) -> None:
        self.show("idle")
