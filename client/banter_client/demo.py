"""Local record/playback loop — no server, no Pi required.

    uv run banter-demo

Proves the audio path and the state machine on whatever machine you're sitting at. A
thin wiring shell over `RecordController`: no queue, no uploader, no `on_recorded`
hook — the demo is deliberately server-less. Recordings go to the queue dir; 'p' plays
the most recent one. Same StateMachine and backend protocols M1/M2 use, so when the
hardware lands you change three env vars and nothing else.
"""

import logging
import signal
import sys
import threading
import time

from banter_client.backends.base import ButtonCallbacks, RingBackend
from banter_client.backends.factory import describe, make_buttons
from banter_client.config import ClientSettings, get_settings
from banter_client.controller import RecordController
from banter_client.state import StateMachine

log = logging.getLogger("banter.demo")


class Demo:
    """Wires buttons to a `RecordController` and runs the terminal loop.

    `machine`/`ring` are exposed as properties delegating to the controller so the
    existing test suite (which reads `demo.machine` and reassigns `demo.ring`) keeps
    working unmodified.
    """

    def __init__(self, settings: ClientSettings) -> None:
        self.s = settings
        self.controller = RecordController(settings, on_recorded=None)
        self.done = threading.Event()

    @property
    def machine(self) -> StateMachine:
        return self.controller.machine

    @property
    def ring(self) -> RingBackend:
        return self.controller.ring

    @ring.setter
    def ring(self, value: RingBackend) -> None:
        self.controller.ring = value

    # -- delegate the record/play surface to the controller --------------------
    def start_record(self) -> None:
        self.controller.start_record()

    def stop_record(self) -> None:
        self.controller.stop_record()

    def play_latest(self) -> None:
        self.controller.play_latest()

    def quit(self) -> None:
        self.done.set()

    def run(self) -> int:
        callbacks = ButtonCallbacks(
            on_record_press=self.start_record,
            on_record_release=self.stop_record,
            on_play_press=self.play_latest,
            on_quit=self.quit,
        )
        buttons = make_buttons(self.s, callbacks)
        buttons.start()
        self.ring.show("idle")
        log.info("demo ready | %s | queue=%s", describe(self.s), self.s.queue_dir)

        signal.signal(signal.SIGINT, lambda *_: self.quit())
        try:
            while not self.done.is_set():
                time.sleep(0.2)
        finally:
            buttons.close()
            self.controller.close()
        log.info("bye")
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    return Demo(get_settings()).run()


if __name__ == "__main__":
    sys.exit(main())
