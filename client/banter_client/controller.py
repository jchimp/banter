"""Record/play orchestration.

Extracted from `demo.py` so both the demo and the (Step 4) uploader can drive the same
loop. No server or queue calls happen here directly — `on_recorded` is the seam:
`demo.py` passes `None` (server-less by design), the uploader will pass
`RecordingQueue.enqueue`.
"""

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from banter_client.backends.base import AudioBackend, RingBackend
from banter_client.backends.factory import make_audio, make_ring
from banter_client.config import ClientSettings
from banter_client.queue import RecordingMeta
from banter_client.state import State, StateMachine

log = logging.getLogger("banter.controller")


class RecordController:
    """Drives one record/play cycle. Backends and state machine are injectable for tests."""

    def __init__(
        self,
        settings: ClientSettings,
        audio: AudioBackend | None = None,
        ring: RingBackend | None = None,
        machine: StateMachine | None = None,
        on_recorded: Callable[[RecordingMeta], None] | None = None,
    ) -> None:
        self.s = settings
        self.machine = machine or StateMachine()
        self.audio = audio or make_audio(settings)
        self.ring = ring or make_ring(settings)
        self.on_recorded = on_recorded
        self.current: Path | None = None
        self._watchdog: threading.Timer | None = None
        # start/stop are reachable from a button callback AND the watchdog thread.
        # StateMachine is itself locked, but the check-then-act around self.current
        # is not, so guard the whole transition.
        self._lock = threading.RLock()
        settings.queue_dir.mkdir(parents=True, exist_ok=True)

    # -- record --------------------------------------------------------------
    def start_record(self) -> None:
        """Begin capture. No-op while busy (FR-10: record/play are mutually exclusive)."""
        with self._lock:
            self._start_record()

    def _start_record(self) -> None:
        if self.machine.is_busy() or not self.machine.to(State.RECORDING):
            log.info("event=record_ignored state=%s", self.machine.state)
            return
        # Client owns the id (PRD S4, CLAUDE.md "idempotent uploads": server upserts
        # on it), so it must exist before the file is even written.
        self.current = self.s.queue_dir / f"{uuid.uuid4().hex[:12]}.wav"
        self.audio.start_record(self.current)
        self.ring.show("recording")
        # ALSA self-limits via `arecord -d`; sounddevice/synthetic don't, so arm a
        # watchdog here too. PRD FR-2: auto-stop and KEEP the clip at the cap.
        self._watchdog = threading.Timer(self.s.max_seconds, self._on_max_seconds)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _on_max_seconds(self) -> None:
        log.info("event=max_seconds_hit seconds=%s", self.s.max_seconds)
        self.stop_record()

    def stop_record(self) -> None:
        """Stop capture. Discards clips under `min_seconds` (FR-3); else keeps + notifies."""
        with self._lock:
            self._stop_record()

    def _stop_record(self) -> None:
        if self.machine.state is not State.RECORDING:
            return
        self._cancel_watchdog()
        duration = self.audio.stop_record()
        path, self.current = self.current, None
        if duration < self.s.min_seconds or not (path and path.exists()):
            if path:
                path.unlink(missing_ok=True)
            self.ring.flash("discarded")
            log.info("event=discarded duration=%.2f min=%.2f", duration, self.s.min_seconds)
        else:
            meta = self._build_meta(path, duration)
            self.ring.flash("success")
            log.info(
                "event=saved id=%s duration=%.2f bytes=%d", meta.id, duration, path.stat().st_size
            )
            if self.on_recorded:
                self.on_recorded(meta)
        self.machine.to(State.IDLE)
        self.ring.show("idle")

    def _build_meta(self, path: Path, duration: float) -> RecordingMeta:
        return RecordingMeta(
            id=path.stem,
            path=path,
            source="kid",
            device_id=self.s.device_id,
            recorded_at=datetime.now(UTC).isoformat(),
            duration_ms=int(duration * 1000),
        )

    def _cancel_watchdog(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None

    # -- play ------------------------------------------------------------------
    def play_latest(self) -> None:
        """Play the newest queued clip; a tap while playing stops it (FR-9)."""
        with self._lock:
            self._play_latest()

    def _play_latest(self) -> None:
        if self.machine.state is State.PLAYING:
            self.audio.stop_play()
            return
        if self.machine.is_busy() or not self.machine.to(State.PLAYING):
            log.info("event=play_ignored state=%s", self.machine.state)
            return
        clips = sorted(self.s.queue_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        if not clips:
            log.info("event=play_empty_queue")
            self.machine.to(State.IDLE)
            self.ring.flash("error")
            return
        clip = clips[-1]
        log.info("event=playing file=%s", clip.name)
        self.ring.show("playing")
        self.audio.play(clip, on_done=self._played)

    def _played(self) -> None:
        self.machine.to(State.IDLE)
        self.ring.show("idle")

    def close(self) -> None:
        """Release backends. Safe to call more than once."""
        self._cancel_watchdog()
        self.audio.close()
        self.ring.close()
