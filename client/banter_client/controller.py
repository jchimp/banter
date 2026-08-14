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
from banter_client.player import Clip, Player
from banter_client.queue import RecordingMeta, probe_duration_ms
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
        player: Player | None = None,
    ) -> None:
        self.s = settings
        self.machine = machine or StateMachine()
        self.audio = audio or make_audio(settings)
        self.ring = ring or make_ring(settings)
        self.on_recorded = on_recorded
        self.player = player
        self.current: Path | None = None
        self._watchdog: threading.Timer | None = None
        # start/stop are reachable from a button callback AND the watchdog thread.
        # StateMachine is itself locked, but the check-then-act around self.current
        # is not, so guard the whole transition.
        self._lock = threading.RLock()
        # Bumped every time a play_next() attempt starts or is cancelled. The worker
        # thread compares its captured value against this after the (unlocked, slow)
        # fetch returns; a mismatch means it was cancelled or superseded, so it must
        # drop the result instead of starting playback (Step 8 tap-during-fetch race).
        self._play_generation = 0
        self._play_worker: threading.Thread | None = None
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
        held = self.audio.stop_record()
        path, self.current = self.current, None
        # The wall clock says how long the button was held; the file says what was
        # actually captured. Trust the file. A dead mic or a busy capture device
        # yields a header-only WAV, and a wall-clock check waves that straight
        # through (CLAUDE.md gotcha 3) — the empty clip then travels all the way to
        # a parent's phone as an unplayable voice note. Every backend finishes
        # writing before stop_record() returns, so this probe sees the final file.
        captured = probe_duration_ms(path) / 1000 if path is not None and path.exists() else 0.0
        if captured < self.s.min_seconds:
            if path:
                path.unlink(missing_ok=True)
            self.ring.flash("discarded")
            # Log both: captured well under held means the mic produced nothing, which
            # is a different problem from the kid tapping instead of holding.
            log.info(
                "event=discarded captured=%.2f held=%.2f min=%.2f",
                captured,
                held,
                self.s.min_seconds,
            )
        else:
            meta = self._build_meta(path, captured)
            self.ring.flash("success")
            log.info(
                "event=saved id=%s duration=%.2f bytes=%d", meta.id, captured, path.stat().st_size
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

    # -- play (server-backed, BTN2) ---------------------------------------------
    def play_next(self) -> None:
        """Fetch and play the next clip from the server (PRD FR-8). A tap while
        already playing stops it (FR-9); a tap while still fetching cancels the
        fetch instead (see the generation comment in `_fetch_and_play`)."""
        with self._lock:
            if self.machine.state is State.PLAYING:
                if self.audio.is_playing:
                    self.audio.stop_play()
                else:
                    # Audio hasn't started yet -- we're still waiting on
                    # Player.fetch_next() on the worker thread. There is nothing
                    # for the audio backend to stop, so cancel the fetch by
                    # bumping the generation and settle the box back to IDLE
                    # ourselves; a slow/offline fetch must not keep BTN1 locked
                    # out for up to `http_timeout` seconds after the kid cancels.
                    self._play_generation += 1
                    self.machine.to(State.IDLE)
                    self.ring.show("idle")
                    log.info("event=play_cancelled")
                return
            if self.player is None:
                # No server-backed player wired (demo.py, or a misconfigured build).
                # Refuse BEFORE taking the PLAYING transition: failing after it would
                # strand the box in PLAYING with no worker to ever settle it, locking
                # out both buttons until restart.
                log.info("event=play_ignored reason=no_player")
                return
            if self.machine.is_busy() or not self.machine.to(State.PLAYING):
                log.info("event=play_ignored state=%s", self.machine.state)
                return
            self._play_generation += 1
            generation = self._play_generation
        # Player.fetch_next() does a blocking HTTP call (up to http_timeout); never
        # run it under the button callback or self._lock, or a slow/offline server
        # would wedge the whole box (CLAUDE.md gotcha 5 / this step's spec).
        self._play_worker = threading.Thread(
            target=self._fetch_and_play,
            args=(generation,),
            daemon=True,
            name="banter-player",
        )
        self._play_worker.start()

    def _fetch_and_play(self, generation: int) -> None:
        """Worker body: resolve the next clip off-thread, then hand it to the audio
        backend. Runs with no lock held while `fetch_next()` is in flight."""
        try:
            clip = self.player.fetch_next() if self.player else None
        except Exception:
            # Anything unexpected out of fetch_next() dies on THIS thread, so if we
            # don't settle the machine here the box stays PLAYING forever and both
            # buttons go dead. An unplayable joke is recoverable; a wedged box is not.
            log.exception("event=play_fetch_crashed")
            with self._lock:
                if generation == self._play_generation:
                    self.machine.to(State.IDLE)
                    self.ring.flash("error")
                    self.ring.show("idle")
            return
        with self._lock:
            if generation != self._play_generation:
                # Cancelled (tap-during-fetch) or superseded by a later play_next()
                # while this fetch was in flight. Leave the clip cached -- it was
                # already written to disk by PlayCache and is fine to play next
                # time -- just don't start playback or touch state that a newer
                # generation may already own.
                log.info("event=play_cancelled")
                return
            if clip is None:
                self.ring.flash("error")
                self.machine.to(State.IDLE)
                self.ring.show("idle")
                return
            log.info("event=playing id=%s cached=%s", clip.id, clip.from_cache)
            self.ring.show("playing")
            self.audio.play(clip.path, on_done=lambda: self._play_next_done(clip))

    def _play_next_done(self, clip: Clip) -> None:
        """AudioBackend on_done for play_next(). Always return to IDLE first so the
        best-effort receipt POST can never delay it."""
        with self._lock:
            self.machine.to(State.IDLE)
            self.ring.show("idle")
        if clip.id is not None:
            # Fallback clips (offline cache) have id=None and get no receipt --
            # see Player._offline_fallback. report_played() swallows its own errors.
            self.player.report_played(clip.id)

    def close(self) -> None:
        """Release backends. Safe to call more than once."""
        self._cancel_watchdog()
        # Bump the generation first: a worker still blocked in fetch_next() past the
        # join below would otherwise wake up and call play() on an already-closed
        # backend. The mismatch makes it drop the clip and return instead.
        with self._lock:
            self._play_generation += 1
        # Daemon thread, so a slow/offline fetch can't block process exit; a short
        # join just gives a clean shutdown log/state in the common case where it
        # finishes quickly. If it's still running past the timeout we just move on.
        if self._play_worker and self._play_worker.is_alive():
            self._play_worker.join(timeout=1.0)
        self.audio.close()
        self.ring.close()
