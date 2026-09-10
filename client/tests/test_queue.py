"""On-disk queue durability: restart, move, recover, reject, corrupt sidecars.

CLAUDE.md: "reboot the Pi mid-queue -> nothing lost" is the acceptance bar. Every test
here uses real WAV files (stdlib `wave`) and a fresh `RecordingQueue` instance where
the scenario calls for surviving a process restart.
"""

import json
import shutil
import wave
from pathlib import Path

from banter_client.queue import RecordingMeta, RecordingQueue, probe_duration_ms


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> None:
    """A real mono 16-bit WAV, not an empty file, so duration probing is meaningful."""
    nframes = int(seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * nframes)


def _meta(queue_dir: Path, id_: str, recorded_at: str, **overrides) -> RecordingMeta:
    defaults = dict(
        id=id_,
        path=queue_dir / f"{id_}.wav",
        source="kid",
        device_id="kidbox-01",
        recorded_at=recorded_at,
        duration_ms=1000,
    )
    defaults.update(overrides)
    return RecordingMeta(**defaults)


def _enqueued(queue: RecordingQueue, id_: str, recorded_at: str, **overrides) -> RecordingMeta:
    meta = _meta(queue.queue_dir, id_, recorded_at, **overrides)
    _write_wav(meta.path)
    queue.enqueue(meta)
    return meta


# --------------------------------------------------------------------- round trip
def test_enqueue_pending_round_trips_every_field(tmp_path):
    queue = RecordingQueue(tmp_path / "q", device_id="kidbox-01", source="kid")
    meta = _enqueued(queue, "abc123", "2026-08-12T10:00:00+00:00", duration_ms=4200, attempts=2)

    [got] = queue.pending()
    assert got.id == meta.id
    assert got.path == meta.path
    assert got.source == meta.source
    assert got.device_id == meta.device_id
    assert got.recorded_at == meta.recorded_at
    assert got.duration_ms == meta.duration_ms
    assert got.attempts == meta.attempts


def test_pending_sorted_oldest_first(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    _enqueued(queue, "c", "2026-08-12T10:02:00+00:00")
    _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    _enqueued(queue, "b", "2026-08-12T10:01:00+00:00")

    assert [m.id for m in queue.pending()] == ["a", "b", "c"]


# ---------------------------------------------------------------- restart durability
def test_restart_durability_survives_fresh_instance(tmp_path):
    queue_dir = tmp_path / "q"
    queue = RecordingQueue(queue_dir)
    _enqueued(queue, "a", "2026-08-12T10:00:00+00:00")
    _enqueued(queue, "b", "2026-08-12T10:01:00+00:00")
    _enqueued(queue, "c", "2026-08-12T10:02:00+00:00")
    del queue

    fresh = RecordingQueue(queue_dir)
    assert {m.id for m in fresh.pending()} == {"a", "b", "c"}


# ------------------------------------------------------------------- path portability
def test_sidecar_stores_bare_filename_no_drive_or_separator(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "abc", "2026-08-12T10:00:00+00:00")

    sidecar = queue.queue_dir / f"{meta.id}.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert ":" not in data["path"]
    assert "/" not in data["path"]
    assert "\\" not in data["path"]
    assert data["path"] == "abc.wav"


def test_queue_dir_survives_being_moved(tmp_path):
    original_dir = tmp_path / "orig" / "q"
    queue = RecordingQueue(original_dir)
    _enqueued(queue, "abc", "2026-08-12T10:00:00+00:00")

    moved_dir = tmp_path / "moved" / "q2"
    moved_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(original_dir, moved_dir)

    moved_queue = RecordingQueue(moved_dir)
    [got] = moved_queue.pending()
    assert got.path.parent == moved_dir
    assert got.path.exists()


# -------------------------------------------------------------------------- done
def test_done_removes_wav_and_sidecar_and_drops_depth(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "abc", "2026-08-12T10:00:00+00:00")
    assert queue.depth() == 1

    queue.done(meta)

    assert not meta.path.exists()
    assert not (queue.queue_dir / "abc.json").exists()
    assert queue.depth() == 0


# ------------------------------------------------------------------------ reject
def test_reject_quarantines_without_deleting(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "abc", "2026-08-12T10:00:00+00:00")

    queue.reject(meta, "http_400")

    quarantined_wav = queue.rejected_dir / "abc.wav"
    quarantined_json = queue.rejected_dir / "abc.json"
    assert quarantined_wav.exists()  # never deleted, just moved
    assert quarantined_json.exists()
    data = json.loads(quarantined_json.read_text(encoding="utf-8"))
    assert data["rejected_reason"] == "http_400"
    assert not meta.path.exists()  # gone from its original location
    assert not (queue.queue_dir / "abc.json").exists()
    assert queue.pending() == []


# ------------------------------------------------------------------- mark_attempt
def test_mark_attempt_increments_and_persists_across_restart(tmp_path):
    queue_dir = tmp_path / "q"
    queue = RecordingQueue(queue_dir)
    meta = _enqueued(queue, "abc", "2026-08-12T10:00:00+00:00")

    updated = queue.mark_attempt(meta)
    assert updated.attempts == 1
    updated = queue.mark_attempt(updated)
    assert updated.attempts == 2

    fresh = RecordingQueue(queue_dir)
    [got] = fresh.pending()
    assert got.attempts == 2


# ------------------------------------------------------------------------ recover
def test_recover_synthesizes_sidecar_for_orphan_wav(tmp_path):
    queue_dir = tmp_path / "q"
    queue = RecordingQueue(queue_dir, device_id="kidbox-02", source="parent")
    orphan = queue_dir / "orphan123.wav"
    _write_wav(orphan, seconds=2.0)

    recovered = queue.recover()

    assert recovered == 1
    [got] = queue.pending()
    assert got.id == "orphan123"
    assert got.source == "parent"
    assert got.device_id == "kidbox-02"
    assert got.duration_ms > 0


def test_recover_replaces_corrupt_json_sidecar(tmp_path):
    queue_dir = tmp_path / "q"
    queue = RecordingQueue(queue_dir)
    wav = queue_dir / "bad1.wav"
    _write_wav(wav)
    (queue_dir / "bad1.json").write_text("{not valid json", encoding="utf-8")

    queue.recover()

    assert [m.id for m in queue.pending()] == ["bad1"]


def test_recover_replaces_sidecar_missing_required_key(tmp_path):
    queue_dir = tmp_path / "q"
    queue = RecordingQueue(queue_dir)
    wav = queue_dir / "bad2.wav"
    _write_wav(wav)
    # Valid JSON, but missing "duration_ms" (and others) -> from_dict raises KeyError.
    (queue_dir / "bad2.json").write_text(json.dumps({"id": "bad2"}), encoding="utf-8")

    queue.recover()

    assert [m.id for m in queue.pending()] == ["bad2"]


def test_recover_sweeps_stale_tmp_files(tmp_path):
    queue_dir = tmp_path / "q"
    queue = RecordingQueue(queue_dir)
    stale = queue_dir / "half_written.json.tmp"
    stale.write_text("{", encoding="utf-8")

    queue.recover()

    assert not stale.exists()


def test_pending_tolerates_sidecar_deleted_mid_flight(tmp_path):
    queue = RecordingQueue(tmp_path / "q")
    meta = _enqueued(queue, "abc", "2026-08-12T10:00:00+00:00")
    (queue.queue_dir / f"{meta.id}.json").unlink()

    assert queue.pending() == []  # no exception, just skipped


# --------------------------------------------------------------------- duration probe
def test_probe_ignores_header_claim_when_data_missing(tmp_path):
    """A SIGKILLed arecord leaves a header claiming the full `-d` duration over an
    empty data chunk (the Pi 4 stalled-capture failure: duration=60.00 bytes=44).
    The probe must report what is on disk, not what the header promises."""
    wav = tmp_path / "lying.wav"
    _write_wav(wav, seconds=60.0)
    wav.write_bytes(wav.read_bytes()[:44])  # canonical PCM header only, zero data

    assert probe_duration_ms(wav) == 0


def test_probe_reports_actual_data_for_truncated_wav(tmp_path):
    rate = 16000
    wav = tmp_path / "partial.wav"
    _write_wav(wav, seconds=2.0, rate=rate)
    wav.write_bytes(wav.read_bytes()[: 44 + rate * 2])  # header says 2s, data holds 1s

    assert probe_duration_ms(wav) == 1000
