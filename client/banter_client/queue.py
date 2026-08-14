"""On-disk upload queue.

The durability layer for CLAUDE.md gotcha 7: "the client's queue is the source of
truth for unsent audio. Only delete a local file after a 2xx from the server." Nothing
in this module deletes a queued recording except `done()`; a permanent failure gets
quarantined via `reject()`, never destroyed (CLAUDE.md: "non-destructive by default").

One pending recording = `{id}.wav` + `{id}.json` sidecar in `queue_dir`. The sidecar
is written atomically (tmp file + `os.replace`) so a crash mid-write can never leave a
half-written sidecar behind.
"""

import contextlib
import json
import logging
import os
import wave
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("banter.queue")


@dataclass(frozen=True)
class RecordingMeta:
    """One queued recording plus its upload bookkeeping."""

    id: str
    path: Path
    source: str
    device_id: str
    recorded_at: str  # ISO8601 UTC
    duration_ms: int
    attempts: int = 0

    def to_dict(self) -> dict:
        """Serialise for the sidecar.

        `path` is stored as a bare filename (not absolute) so a queue dir survives
        being copied or moved to a different machine.
        """
        return {
            "id": self.id,
            "path": self.path.name,
            "source": self.source,
            "device_id": self.device_id,
            "recorded_at": self.recorded_at,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict, queue_dir: Path) -> "RecordingMeta":
        """Reconstruct from a sidecar dict, resolving `path` relative to `queue_dir`."""
        return cls(
            id=data["id"],
            path=queue_dir / data["path"],
            source=data["source"],
            device_id=data["device_id"],
            recorded_at=data["recorded_at"],
            duration_ms=int(data["duration_ms"]),
            attempts=int(data.get("attempts", 0)),
        )


def probe_duration_ms(path: Path) -> int:
    """Milliseconds of audio actually in `path`, or 0 if it can't be read.

    Best-effort by design: used both to recover an orphan wav and to decide whether a
    just-finished capture is worth keeping, and in neither case should an unreadable
    file raise — 0 means "nothing here", which is the answer the caller acts on.
    """
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate() or 1
            return int(1000 * wf.getnframes() / rate)
    except (wave.Error, OSError, EOFError):
        return 0


class RecordingQueue:
    """On-disk upload queue rooted at `queue_dir`, plus a `rejected/` quarantine dir."""

    def __init__(self, queue_dir: Path, device_id: str = "", source: str = "kid") -> None:
        self.queue_dir = queue_dir
        self.rejected_dir = queue_dir / "rejected"
        self.device_id = device_id
        self.source = source
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.rejected_dir.mkdir(parents=True, exist_ok=True)

    def _sidecar_path(self, id_: str) -> Path:
        return self.queue_dir / f"{id_}.json"

    def enqueue(self, meta: RecordingMeta) -> None:
        """Write the sidecar atomically: tmp file then `os.replace`.

        Also used internally by `mark_attempt`/`recover` to (re)persist a sidecar.
        """
        sidecar = self._sidecar_path(meta.id)
        tmp = sidecar.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta.to_dict()), encoding="utf-8")
        os.replace(tmp, sidecar)
        log.info("event=enqueued id=%s", meta.id)

    def pending(self) -> list[RecordingMeta]:
        """Every `.wav` with a readable sidecar, oldest `recorded_at` first.

        Tolerates a sidecar vanishing or being malformed between the glob and the read
        (the uploader thread and a button callback both touch this directory).
        """
        metas = []
        for wav in self.queue_dir.glob("*.wav"):
            try:
                data = json.loads(self._sidecar_path(wav.stem).read_text(encoding="utf-8"))
                metas.append(RecordingMeta.from_dict(data, self.queue_dir))
            except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
                continue
        return sorted(metas, key=lambda m: m.recorded_at)

    def recover(self) -> int:
        """Startup scan (PRD FR-5): re-enqueue anything left over from a prior run.

        Sweeps stale `.json.tmp` files from an interrupted `enqueue`, and synthesizes a
        sidecar for any `.wav` that has none (or a corrupt one). Returns the number of
        orphans recovered.
        """
        for tmp in self.queue_dir.glob("*.json.tmp"):
            tmp.unlink(missing_ok=True)

        recovered = 0
        for wav in self.queue_dir.glob("*.wav"):
            sidecar = self._sidecar_path(wav.stem)
            if sidecar.exists():
                try:
                    # Parse-to-meta, not just json.loads: a well-formed JSON blob that
                    # is missing a required key would be skipped by pending() forever,
                    # leaving the wav invisible. Anything pending() can't load is
                    # treated as corrupt and resynthesized.
                    RecordingMeta.from_dict(
                        json.loads(sidecar.read_text(encoding="utf-8")), self.queue_dir
                    )
                    continue  # already has a usable sidecar
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    log.warning("event=corrupt_sidecar id=%s", wav.stem)
            meta = self._synthesize_meta(wav)
            self.enqueue(meta)
            recovered += 1
            log.info("event=recovered id=%s", meta.id)
        return recovered

    def _synthesize_meta(self, wav: Path) -> RecordingMeta:
        recorded_at = datetime.fromtimestamp(wav.stat().st_mtime, tz=UTC).isoformat()
        return RecordingMeta(
            id=wav.stem,
            path=wav,
            source=self.source,
            device_id=self.device_id,
            recorded_at=recorded_at,
            duration_ms=probe_duration_ms(wav),
        )

    def done(self, meta: RecordingMeta) -> None:
        """Delete the wav + sidecar. Call ONLY after a 2xx (CLAUDE.md gotcha 7).

        The only place in the client that unlinks a queued recording.
        """
        meta.path.unlink(missing_ok=True)
        self._sidecar_path(meta.id).unlink(missing_ok=True)
        log.info("event=done id=%s", meta.id)

    def reject(self, meta: RecordingMeta, reason: str) -> None:
        """Quarantine into `rejected/` on a permanent (4xx) failure. Never deletes.

        Keeps one bad file from wedging the retry loop forever while still honoring
        "never hard-delete audio".
        """
        dest_wav = self.rejected_dir / meta.path.name
        with contextlib.suppress(FileNotFoundError):
            os.replace(meta.path, dest_wav)
        data = replace(meta, path=dest_wav).to_dict()
        data["rejected_reason"] = reason
        (self.rejected_dir / f"{meta.id}.json").write_text(json.dumps(data), encoding="utf-8")
        self._sidecar_path(meta.id).unlink(missing_ok=True)
        log.info("event=rejected id=%s reason=%s", meta.id, reason)

    def mark_attempt(self, meta: RecordingMeta) -> RecordingMeta:
        """Increment and persist `attempts`. Returns the updated meta."""
        updated = replace(meta, attempts=meta.attempts + 1)
        self.enqueue(updated)
        return updated

    def depth(self) -> int:
        """Count of pending (not yet uploaded) recordings."""
        return len(self.pending())
