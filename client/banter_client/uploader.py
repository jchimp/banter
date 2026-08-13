"""Background upload thread: drains the on-disk queue to the server.

PRD FR-6: "Upload retries with exponential backoff (2s -> 60s cap), indefinitely. A
dead server or dropped WiFi must never lose a joke." CLAUDE.md gotcha 7: the queue is
the source of truth for unsent audio — `queue.done()` (called only after a 2xx) is the
only thing here that may delete a local file.

This thread never touches the `StateMachine` in `state.py` — that machine exists to
keep recording and playback mutually exclusive (FR-10), and uploads must run
concurrently with an idle box without blocking or delaying the next recording. The
ring is nudged directly instead.
"""

import logging
import random
import threading
from typing import Literal

import requests

from banter_client.backends.base import RingBackend
from banter_client.config import ClientSettings
from banter_client.queue import RecordingMeta, RecordingQueue

log = logging.getLogger("banter.uploader")

#: 408 (timeout) and 429 (rate limited) are worth retrying, like any 5xx.
_TRANSIENT_STATUSES = {408, 429}

UploadOutcome = Literal["success", "transient", "permanent"]


def backoff_delay(attempt: int, start: float, cap: float) -> float:
    """Exponential backoff for a 0-indexed attempt count, capped at `cap`.

    Pure (no jitter, no sleeping) so tests can assert growth without waiting on a
    clock. Jitter belongs in the caller, per the task spec, precisely so this stays
    deterministic.
    """
    return min(start * (2 ** max(attempt, 0)), cap)


class Uploader:
    """Daemon thread that repeatedly drains `queue` to `settings.recordings_url`."""

    def __init__(
        self,
        settings: ClientSettings,
        queue: RecordingQueue,
        ring: RingBackend | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.ring = ring
        # Injectable so Step 6 can pass a fake and never touch a real socket.
        self.session = session or requests.Session()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Consecutive *passes* over the queue that ended with a transient failure
        # still pending. Drives backoff; deliberately not the same counter as a
        # single recording's `attempts` (which is per-item and persisted).
        self._pass_failures = 0

    def start(self) -> None:
        """Start the daemon thread. No-op if already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="banter-uploader", daemon=True)
        self._thread.start()
        log.info("event=uploader_started")

    def stop(self) -> None:
        """Signal shutdown and wait for the thread to exit.

        Waits on `_wake_event` rather than `time.sleep` during backoff, so this
        returns promptly even mid-backoff (systemd SIGTERM must not hang ~60s).
        """
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("event=uploader_stopped")

    def wake(self) -> None:
        """Nudge the loop to run immediately instead of waiting out a backoff/idle wait."""
        self._wake_event.set()

    def depth(self) -> int:
        """Pending (not yet uploaded) recording count, for startup/status logging."""
        return self.queue.depth()

    # -- loop --------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop_event.is_set():
            pending = self.queue.pending()
            if not pending:
                self._pass_failures = 0
                self._show("idle")
                self._wait(None)  # block until woken by a new recording or stop()
                continue
            had_success, had_transient = self._upload_pass(pending)
            if had_success:
                self._pass_failures = 0
            if had_transient:
                self._pass_failures += 1
                self._show("queued")
                self._wait(self._next_delay())
            else:
                self._show("idle")

    def _upload_pass(self, pending: list[RecordingMeta]) -> tuple[bool, bool]:
        """Upload every pending item oldest-first. Returns (had_success, had_transient)."""
        had_success = False
        had_transient = False
        for meta in pending:
            if self._stop_event.is_set():
                break
            outcome = self._upload_one(meta)
            had_success |= outcome == "success"
            had_transient |= outcome == "transient"
        return had_success, had_transient

    def _next_delay(self) -> float:
        base = backoff_delay(
            self._pass_failures - 1,
            self.settings.backoff_start_seconds,
            self.settings.backoff_max_seconds,
        )
        return base + random.uniform(0, base * 0.2)

    def _wait(self, timeout: float | None) -> None:
        self._wake_event.wait(timeout)
        self._wake_event.clear()

    # -- one upload ----------------------------------------------------------
    def _upload_one(self, meta: RecordingMeta) -> UploadOutcome:
        """POST one recording. Never deletes except via `queue.done()` on a 2xx."""
        self._show("uploading")
        headers = {"X-API-Key": self.settings.api_key}
        data = {
            "id": meta.id,
            "source": meta.source,
            "device_id": meta.device_id,
            "recorded_at": meta.recorded_at,
            "duration_ms": str(meta.duration_ms),
        }
        try:
            with meta.path.open("rb") as fh:
                files = {"audio": (f"{meta.id}.wav", fh, "audio/wav")}
                resp = self.session.post(
                    self.settings.recordings_url,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=self.settings.http_timeout,
                )
        except requests.RequestException as exc:
            # Offline, DNS failure, timeout, etc. Transient by definition (FR-6).
            # MUST be caught before OSError: requests.RequestException subclasses
            # IOError, so an OSError arm above this one would swallow every network
            # failure and quarantine a perfectly good joke.
            log.warning("event=upload_error id=%s error=%s", meta.id, exc.__class__.__name__)
            self.queue.mark_attempt(meta)
            return "transient"
        except OSError as exc:
            # File missing/unreadable on disk: retrying won't fix it, and it would
            # wedge the queue forever. reject() tolerates an already-missing file.
            log.error("event=upload_file_error id=%s error=%s", meta.id, exc.__class__.__name__)
            self.queue.reject(meta, "file_error")
            return "permanent"

        return self._handle_response(meta, resp)

    def _handle_response(self, meta: RecordingMeta, resp: requests.Response) -> UploadOutcome:
        status = resp.status_code
        if 200 <= status < 300:
            # 201 created or 200 duplicate (idempotent upsert on client id) are both
            # success — this is the only path that may delete the local file.
            self.queue.done(meta)
            log.info("event=uploaded id=%s status=%d", meta.id, status)
            return "success"

        if status in _TRANSIENT_STATUSES or status >= 500:
            log.warning("event=upload_retry id=%s status=%d", meta.id, status)
            self.queue.mark_attempt(meta)
            return "transient"

        # Permanent 4xx. 401 is quarantined here too, deliberately: a bad API key is
        # a config error the operator must see on the device panel / logs, not
        # something to retry forever and silently never upload.
        reason = f"http_{status}"
        log.error("event=upload_rejected id=%s status=%d", meta.id, status)
        self.queue.reject(meta, reason)
        return "permanent"

    def _show(self, state: str) -> None:
        """Ring feedback, guarded: a `None` or misbehaving ring must never break uploads."""
        if self.ring is None:
            return
        try:
            self.ring.show(state)
        except Exception:
            log.debug("event=ring_error state=%s", state)
