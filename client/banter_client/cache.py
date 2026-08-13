"""On-disk LRU cache of played clips.

The offline fallback layer for PRD FR-12: "server unreachable -> play a locally
cached fallback clip if present". Bounded to `max_entries` so the kidbox never fills
its SD card; least-recently-played clips are evicted first.

One cached clip = `{rec_id}.wav` directly in `cache_dir`. Writes are atomic (tmp file
+ `os.replace`, same discipline as `queue.py`) so a truncated download or a crash
mid-write can never become a playable cache entry.
"""

import contextlib
import logging
import os
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger("banter.cache")


class PlayCache:
    """Bounded, on-disk LRU cache of played clips rooted at `cache_dir`."""

    def __init__(self, cache_dir: Path, max_entries: int) -> None:
        self.cache_dir = cache_dir
        self.max_entries = max(1, max_entries)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # A tmp left over from a crash mid-write is not a valid entry; drop it so
        # it can't be mistaken for one and doesn't linger forever.
        for tmp in self.cache_dir.glob("*.wav.tmp"):
            tmp.unlink(missing_ok=True)

    def path_for(self, rec_id: str) -> Path:
        """Where a clip would live. Does not imply the file exists."""
        return self.cache_dir / f"{rec_id}.wav"

    def has(self, rec_id: str) -> bool:
        """Whether `rec_id` is currently cached."""
        return self.path_for(rec_id).exists()

    def store(
        self, rec_id: str, data: bytes | Iterable[bytes], *, keep: Path | None = None
    ) -> Path:
        """Write a clip into the cache and evict down to `max_entries`.

        `data` may be a single `bytes` blob or an iterable of chunks (the player
        streams the HTTP response). Written to a `.tmp` sibling first so a failure
        partway through never leaves a truncated file at the real path.
        """
        dest = self.path_for(rec_id)
        tmp = dest.with_suffix(".wav.tmp")
        try:
            with tmp.open("wb") as f:
                if isinstance(data, bytes):
                    f.write(data)
                else:
                    for chunk in data:
                        f.write(chunk)
            os.replace(tmp, dest)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        log.info("event=cached id=%s", rec_id)
        # `dest` was just written so it has the newest mtime and would survive
        # eviction anyway; `keep` additionally protects a *different* clip
        # (e.g. one mid-playback) from being evicted by this store().
        self._evict(keep=keep)
        return dest

    def touch(self, rec_id: str) -> None:
        """Mark `rec_id` as most-recently-used by bumping its mtime.

        Never raises: a vanished file (evicted or manually removed) is a no-op, not
        an error worth interrupting playback for.
        """
        path = self.path_for(rec_id)
        try:
            os.utime(path, None)
        except FileNotFoundError:
            return
        log.info("event=cache_hit id=%s", rec_id)

    def oldest(self, *, exclude: Path | None = None) -> Path | None:
        """Least-recently-used entry, skipping `exclude`. The offline fallback pick.

        Skipping `exclude` (the clip just played or currently playing) means
        repeated offline taps rotate through the cache instead of replaying one clip.
        """
        candidates = [p for p in self.entries() if p != exclude]
        if not candidates:
            return None
        return min(candidates, key=self._safe_mtime)

    def entries(self) -> list[Path]:
        """Every cached clip, newest-first by mtime.

        Tolerates a file vanishing between the glob and the stat (another process,
        or a manual `rm`).
        """
        paths = []
        for p in self.cache_dir.glob("*.wav"):
            try:
                paths.append((p.stat().st_mtime, p))
            except FileNotFoundError:
                continue
        return [p for _, p in sorted(paths, key=lambda item: item[0], reverse=True)]

    def _safe_mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return float("inf")  # already gone; sort last, never picked as "oldest"

    def _evict(self, *, keep: Path | None) -> None:
        """Delete oldest cache copies down to `max_entries`.

        Never evicts `keep` (the clip that just landed, or the one currently
        playing) and never evicts below one entry. Only ever touches files inside
        `cache_dir` — the upload queue is a different directory entirely.
        """
        entries = self.entries()  # newest-first
        if len(entries) <= self.max_entries:
            return
        # Oldest-first among the surplus, protecting `keep` from eviction.
        surplus = entries[self.max_entries :]
        for path in reversed(surplus):
            if keep is not None and path == keep:
                continue
            if len(self.entries()) <= 1:
                break
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
                log.info("event=evicted id=%s", path.stem)
