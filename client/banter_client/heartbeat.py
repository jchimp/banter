"""Background heartbeat thread: tells the server this device is alive.

PRD: the device panel shows "last seen" per kidbox. That only works if something
pings the server on a fixed cadence, independent of whether a joke is in flight.

Deliberately NOT folded into `Uploader`'s loop: that loop is queue-driven and blocks
until a recording arrives, but a heartbeat must fire while the box is idle AND while
it is offline (so the server can notice the gap and mark the device down).

Deliberately NOT using `uploader.backoff_delay`. Backing off after a failed heartbeat
would delay the server noticing a dead device -- exactly what this thread exists to
report. This thread always waits a fixed interval, success or failure. Do not "fix"
this into exponential backoff.
"""

import logging
import threading

import requests

from banter_client.config import ClientSettings
from banter_client.queue import RecordingQueue

log = logging.getLogger("banter.heartbeat")


class Heartbeat:
    """Daemon thread that POSTs queue depth to `settings.heartbeat_url()` on a timer."""

    def __init__(
        self,
        settings: ClientSettings,
        queue: RecordingQueue,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        # Injectable so tests can pass a fake and never touch a real socket.
        self.session = session or requests.Session()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the daemon thread. No-op if already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="banter-heartbeat", daemon=True
        )
        self._thread.start()
        log.info("event=heartbeat_started")

    def stop(self) -> None:
        """Signal shutdown and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("event=heartbeat_stopped")

    # -- loop --------------------------------------------------------------
    def _run(self) -> None:
        # Send-then-wait: fire immediately on start() so the device panel populates
        # at boot rather than one interval later.
        while not self._stop_event.is_set():
            self._send()
            self._stop_event.wait(self.settings.heartbeat_interval_seconds)

    def _send(self) -> None:
        """POST current queue depth. Never raises; never blocks the caller on failure."""
        headers = {"X-API-Key": self.settings.api_key}
        body = {"queue_depth": self.queue.depth()}
        try:
            resp = self.session.post(
                self.settings.heartbeat_url(),
                headers=headers,
                json=body,
                timeout=self.settings.next_timeout,
            )
        except requests.RequestException as exc:
            # Offline, DNS failure, timeout, etc. Expected during an outage -- that's
            # exactly what this thread exists to eventually report, so DEBUG not
            # INFO/WARNING to avoid spamming the journal.
            # MUST be caught before OSError: requests.RequestException subclasses
            # IOError, so an OSError arm above this one would swallow every network
            # failure here too (the exact bug that bit the M1 uploader).
            log.debug("event=heartbeat_error error=%s", exc.__class__.__name__)
            return

        if not (200 <= resp.status_code < 300):
            log.debug("event=heartbeat_rejected status=%d", resp.status_code)
            return

        log.debug("event=heartbeat_sent status=%d", resp.status_code)
