"""Entry point: buttons -> recorder -> queue -> uploader (M1). Player lands in M2.

Kept deliberately thin so the wiring stays visible: build the queue, recover anything
left from a prior run (PRD FR-5), build the uploader and controller, wire buttons
through the hardware seam (`backends/factory.py` — never `gpiozero` directly), then
idle until SIGINT/SIGTERM.
"""

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from types import FrameType

from banter_client.backends.audio import check_alsa_devices
from banter_client.backends.base import ButtonCallbacks
from banter_client.backends.factory import describe, make_buttons
from banter_client.config import ClientSettings, get_settings
from banter_client.controller import RecordController
from banter_client.heartbeat import Heartbeat
from banter_client.player import Player
from banter_client.queue import RecordingMeta, RecordingQueue
from banter_client.uploader import Uploader

log = logging.getLogger("banter.client")


class App:
    """Owns the wired-together components and their shutdown order."""

    def __init__(self, settings: ClientSettings) -> None:
        self.s = settings
        self.queue = RecordingQueue(settings.queue_dir, device_id=settings.device_id)
        # Player builds its own PlayCache from settings (cache_dir, play_cache_size).
        self.player = Player(settings)
        self.controller = RecordController(
            settings, on_recorded=self._on_recorded, player=self.player
        )
        # Uploader and controller share one ring instance so "recording" / "uploading"
        # / "queued" feedback don't fight each other over the same NeoPixels.
        self.uploader = Uploader(settings, self.queue, ring=self.controller.ring)
        self.heartbeat = Heartbeat(settings, self.queue)
        self.buttons = make_buttons(
            settings,
            ButtonCallbacks(
                on_record_press=self.controller.start_record,
                on_record_release=self.controller.stop_record,
                on_play_press=self.controller.play_next,
                on_quit=self._shutdown_requested,
            ),
        )
        self._done = threading.Event()

    def _on_recorded(self, meta: RecordingMeta) -> None:
        """Hook from `RecordController`: persist to the queue, then nudge the uploader.

        Enqueue-then-wake (not the reverse) so the uploader never wakes to find the
        sidecar still missing.
        """
        self.queue.enqueue(meta)
        self.uploader.wake()

    def _shutdown_requested(self, *_: object) -> None:
        self._done.set()

    def run(self) -> int:
        recovered = self.queue.recover()
        log.info(
            "event=startup device=%s server=%s %s queue_depth=%d recovered=%d",
            self.s.device_id,
            self.s.api_url,
            describe(self.s),
            self.queue.depth(),
            recovered,
        )

        self._check_audio_devices()
        self._check_buttons()

        self.uploader.start()
        self.heartbeat.start()
        self.buttons.start()

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        try:
            self._done.wait()
        finally:
            log.info("event=shutdown")
            self.buttons.close()
            self.controller.close()
            self.uploader.stop()
            self.heartbeat.stop()
        log.info("event=bye")
        return 0

    def _check_audio_devices(self) -> None:
        """Warn loudly if the configured ALSA devices aren't there.

        Logged, not fatal: the unit restarts on failure, and a restart loop over a
        typo'd device name is worse than a box that boots and says what's wrong.
        """
        if self.s.audio_backend != "alsa":
            return
        for problem in check_alsa_devices(self.s.alsa_capture, self.s.alsa_playback):
            log.error("event=alsa_device_missing %s", problem)

    def _check_buttons(self) -> None:
        """Warn if a button is already reading pressed before we start listening.

        Same contract as `_check_audio_devices`: say what's wrong and carry on. A
        genuine finger on the button at boot is indistinguishable from a shorted line,
        so this can't be fatal — but a stuck line is otherwise completely silent, which
        is worse. Must run before `buttons.start()`, while the callbacks are still
        unbound.
        """
        if self.s.button_backend != "gpio":
            return
        try:
            held = self.buttons.held_pins()
        except Exception:
            # Reading the lines is diagnostics; never let it be why the box won't boot.
            log.warning("event=button_check_failed")
            return
        for problem in stuck_button_problems(held):
            log.error("event=button_stuck %s", problem)

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("event=signal signum=%s", signal.Signals(signum).name)
        self._done.set()


def dir_problem(path: Path) -> str | None:
    """Why `path` can't serve as a data dir, or None if it's fine.

    Separate from the queue/cache constructors so the failure is one legible log line
    instead of a mkdir traceback in journalctl. The paths come from .env and routinely
    name another user's home — the templates ship `/home/pi` and a `<user>`
    placeholder — so this is the first thing a fresh install gets wrong.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"path={path} error={exc.strerror or exc}"
    if not os.access(path, os.W_OK):
        return f"path={path} error=exists but is not writable by this user"
    return None


def stuck_button_problems(held: list[tuple[str, int]]) -> list[str]:
    """One message per button that reads pressed at startup.

    Split from `_check_buttons` for the same reason `dir_problem` is split out: the
    formatting is the part worth testing, and it tests without a GPIO.
    """
    return [
        f"name={name} pin={pin} error=reads pressed at startup; gpiozero fires on a "
        f"press EDGE, so this button will never trigger. Check the switch lugs "
        f"(one to GPIO{pin}, one to GND) or a faulty pin — see the CLAUDE.md pin table"
        for name, pin in held
    ]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    s = get_settings()
    problems = [p for p in (dir_problem(s.queue_dir), dir_problem(s.cache_dir)) if p]
    if problems:
        for problem in problems:
            log.error("event=data_dir_unusable %s", problem)
        log.error(
            "event=fatal hint=set BANTER_QUEUE_DIR and BANTER_CACHE_DIR to paths this "
            "user owns (see README, 'Client — on the Pi')"
        )
        return 2
    return App(s).run()


if __name__ == "__main__":
    sys.exit(main())
