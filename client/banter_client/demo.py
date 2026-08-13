"""Local record/playback loop — no server, no Pi required.

    uv run banter-demo

Proves the audio path and the state machine on whatever machine you're sitting at.
Recordings go to the queue dir; 'p' plays the most recent one. This is the same
StateMachine and the same backend protocols M1/M2 will use, so when the hardware
lands you change three env vars and nothing else.
"""

import logging
import signal
import sys
import threading
import time
from pathlib import Path

from banter_client.backends.base import ButtonCallbacks
from banter_client.backends.factory import describe, make_audio, make_buttons, make_ring
from banter_client.config import ClientSettings, get_settings
from banter_client.state import State, StateMachine

log = logging.getLogger("banter.demo")


class Demo:
    def __init__(self, settings: ClientSettings) -> None:
        self.s = settings
        self.machine = StateMachine()
        self.audio = make_audio(settings)
        self.ring = make_ring(settings)
        self.current: Path | None = None
        self.done = threading.Event()
        settings.queue_dir.mkdir(parents=True, exist_ok=True)

    # -- record ------------------------------------------------------------
    def start_record(self) -> None:
        if self.machine.is_busy() or not self.machine.to(State.RECORDING):
            log.info("busy (%s) - ignoring record", self.machine.state)
            return
        self.current = self.s.queue_dir / f"kid_{int(time.time())}.wav"
        self.audio.start_record(self.current)
        self.ring.show("recording")

    def stop_record(self) -> None:
        if self.machine.state is not State.RECORDING:
            return
        duration = self.audio.stop_record()
        path, self.current = self.current, None
        if duration < self.s.min_seconds or not (path and path.exists()):
            if path:
                path.unlink(missing_ok=True)
            self.ring.flash("discarded")
            log.info("discarded (%.2fs < %.2fs min)", duration, self.s.min_seconds)
        else:
            self.ring.flash("success")
            log.info("saved %s (%.2fs, %d bytes)", path.name, duration, path.stat().st_size)
        self.machine.to(State.IDLE)
        self.ring.show("idle")

    # -- play --------------------------------------------------------------
    def play_latest(self) -> None:
        if self.machine.state is State.PLAYING:
            self.audio.stop_play()
            return
        if self.machine.is_busy() or not self.machine.to(State.PLAYING):
            log.info("busy (%s) - ignoring play", self.machine.state)
            return
        clips = sorted(self.s.queue_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        if not clips:
            log.info("nothing recorded yet")
            self.machine.to(State.IDLE)
            self.ring.flash("error")
            return
        clip = clips[-1]
        log.info("playing %s", clip.name)
        self.ring.show("playing")
        self.audio.play(clip, on_done=self._played)

    def _played(self) -> None:
        self.machine.to(State.IDLE)
        self.ring.show("idle")

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
            self.audio.close()
            self.ring.close()
        log.info("bye")
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    return Demo(get_settings()).run()


if __name__ == "__main__":
    sys.exit(main())
