"""Network + cache half of the playback path (PRD FR-8, FR-11, FR-12).

Deliberately does no threading, no state machine, and no audio backend calls: this
module only resolves *which file to play* and reports receipts. Step 8's controller
runs `fetch_next()` on a worker thread, calls `AudioBackend.play()` on the result, and
calls `report_played()` afterward. Keeping I/O here and threading there is what makes
both halves testable in isolation.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from banter_client.cache import CorruptAudioError, PlayCache
from banter_client.config import ClientSettings

log = logging.getLogger("banter.player")


@dataclass(frozen=True)
class Clip:
    """A resolved, playable clip and where it came from."""

    path: Path
    id: str | None  # None when the clip came from the offline cache fallback
    from_cache: bool


class Player:
    """Resolves the next clip to play and reports play receipts. No threading, no I/O
    beyond `requests` and `PlayCache`."""

    def __init__(
        self,
        settings: ClientSettings,
        cache: PlayCache | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache or PlayCache(settings.cache_dir, settings.play_cache_size)
        # Injectable so tests never open a socket, same discipline as Uploader.
        self.session = session or requests.Session()
        # Last clip handed back from the offline fallback, so repeated offline taps
        # rotate through the cache (PlayCache.oldest(exclude=...)) instead of
        # replaying the same clip over and over.
        self._last_offline_path: Path | None = None
        # monotonic() deadline until which we treat the server as unreachable and skip
        # the network entirely. Without it every tap during an outage pays the full
        # connect/read timeout before reaching the cache. monotonic, not wall clock, so
        # an NTP step on a just-booted Pi can't strand us offline.
        self._offline_until = 0.0

    def _mark_offline(self) -> None:
        self._offline_until = time.monotonic() + self.settings.offline_memo_seconds

    def _mark_online(self) -> None:
        """Clear the offline memo. A 204 counts: the server answered, it's just empty."""
        self._offline_until = 0.0

    def _is_offline(self) -> bool:
        return time.monotonic() < self._offline_until

    def fetch_next(self) -> Clip | None:
        """Ask the server what to play next; fall back to the offline cache on error.

        Blocking — intended to be called from a worker thread. Resolution order:
        1. GET .../recordings/next. 204 means "nothing to play" -> None, no fallback.
        2. 200 with a cached id -> touch() and return the cached copy (no re-download).
        3. 200 with an uncached id -> stream-download into the cache.
        4. Any network error / timeout / non-2xx (other than 204) -> offline fallback
           to the oldest cached clip, rotating past whatever was last returned.
        5. Fallback attempted but the cache is empty -> None.
        """
        if self._is_offline():
            # Known-unreachable within the memo window: don't pay the timeout again.
            log.info("event=play_offline_memo")
            return self._offline_fallback()

        headers = {"X-API-Key": self.settings.api_key}
        params = {"device_id": self.settings.device_id}
        try:
            resp = self.session.get(
                self.settings.next_url,
                headers=headers,
                params=params,
                timeout=self.settings.next_timeout,
            )
        except requests.RequestException as exc:
            # Offline, DNS failure, timeout, etc. MUST be caught before any OSError
            # arm: requests.RequestException subclasses IOError, so an OSError arm
            # above this one would swallow every network failure (see the M1 bug in
            # uploader.py line ~160 — caught only by a manual offline drill).
            log.warning("event=play_fetch_error error=%s", exc.__class__.__name__)
            self._mark_offline()
            return self._offline_fallback()

        if resp.status_code == 204:
            # The server answered — it's reachable, just has nothing to offer.
            self._mark_online()
            log.info("event=play_none")
            return None

        if not (200 <= resp.status_code < 300):
            # Includes 401 on a bad key: memo it so a misconfigured box stops hammering
            # the server on every button press.
            log.warning("event=play_fetch_error status=%d", resp.status_code)
            self._mark_offline()
            return self._offline_fallback()

        self._mark_online()
        return self._resolve_from_response(resp)

    def _resolve_from_response(self, resp: requests.Response) -> Clip | None:
        """Handle a 200 from `/recordings/next`: cache hit, or download-then-cache."""
        try:
            body = resp.json()
            rec_id = str(body["id"])
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("event=play_fetch_error error=bad_response detail=%s", exc)
            return self._offline_fallback()

        if self.cache.has(rec_id):
            self.cache.touch(rec_id)
            log.info("event=play_cache_hit id=%s", rec_id)
            return Clip(path=self.cache.path_for(rec_id), id=rec_id, from_cache=True)

        return self._download(rec_id)

    def _download(self, rec_id: str) -> Clip | None:
        """Stream `/recordings/{id}/audio` straight into the cache; never buffer a
        whole clip in memory."""
        audio_url = f"{self.settings.recordings_url}/{rec_id}/audio"
        headers = {"X-API-Key": self.settings.api_key}
        log.info("event=play_fetch id=%s", rec_id)
        try:
            resp = self.session.get(
                audio_url,
                headers=headers,
                timeout=self.settings.http_timeout,
                stream=True,
            )
            if not (200 <= resp.status_code < 300):
                log.warning("event=play_fetch_error id=%s status=%d", rec_id, resp.status_code)
                return self._offline_fallback()
            path = self.cache.store(rec_id, resp.iter_content(chunk_size=8192))
        except CorruptAudioError:
            # 200 but the body isn't audio — a captive portal or proxy intercepting us.
            # Memo it: the network is lying about being usable, and re-downloading the
            # same junk on every button press helps nobody.
            log.warning("event=play_corrupt_download id=%s", rec_id)
            self._mark_offline()
            return self._offline_fallback()
        except requests.RequestException as exc:
            # Must precede any OSError handling — see note in fetch_next().
            log.warning("event=play_fetch_error id=%s error=%s", rec_id, exc.__class__.__name__)
            self._mark_offline()
            return self._offline_fallback()
        except OSError as exc:
            # cache.store's atomic write leaves nothing playable behind on a partial
            # download; fall through to the offline cache like any other failure.
            log.warning("event=play_fetch_error id=%s error=%s", rec_id, exc.__class__.__name__)
            self._mark_offline()
            return self._offline_fallback()

        return Clip(path=path, id=rec_id, from_cache=False)

    def _offline_fallback(self) -> Clip | None:
        """Oldest cached clip, excluding whatever offline fallback we last returned.

        `id=None` is deliberate: the client must not POST a play receipt for a clip
        it can't attribute to a server-side id. The id could be derived from the
        filename, but if the server just told us it was down, an invented receipt
        is noise.
        """
        path = self.cache.oldest(exclude=self._last_offline_path)
        if path is None:
            log.info("event=play_none")
            return None
        self._last_offline_path = path
        # Advance the LRU position, or `oldest()` keeps returning this same clip and the
        # box ping-pongs between the two oldest entries for the whole outage. Touching
        # also protects a just-played fallback from eviction, which is what we want.
        self.cache.touch(path.stem)
        log.info("event=play_offline_fallback path=%s", path.name)
        return Clip(path=path, id=None, from_cache=True)

    def report_played(self, rec_id: str, played_at: str | None = None) -> None:
        """POST a best-effort play receipt. Never raises.

        No retry loop: the server dedupes on (recording_id, device_id, played_at), so
        a retry would be safe, but durable receipts are out of scope for M2 — a
        dropped receipt must never delay or block audio. This is a deliberate scope
        cut, not an oversight.
        """
        if self._is_offline():
            # Server known unreachable. This runs on the playback-completion thread, so
            # paying a timeout here would stall the box's return to idle for nothing.
            log.info("event=receipt_skipped id=%s reason=offline", rec_id)
            return
        url = f"{self.settings.recordings_url}/{rec_id}/played"
        headers = {"X-API-Key": self.settings.api_key}
        payload = {
            "device_id": self.settings.device_id,
            "played_at": played_at or datetime.now(UTC).isoformat(),
        }
        try:
            resp = self.session.post(
                url, headers=headers, json=payload, timeout=self.settings.next_timeout
            )
            if not (200 <= resp.status_code < 300):
                log.warning("event=receipt_failed id=%s status=%d", rec_id, resp.status_code)
        except requests.RequestException as exc:
            # Must precede any OSError handling — see note in fetch_next().
            log.warning("event=receipt_failed id=%s error=%s", rec_id, exc.__class__.__name__)
            self._mark_offline()
