"""PlayCache: atomic writes, LRU eviction, and the offline-fallback pick.

mtime is the LRU key and filesystem mtime resolution is coarse, so every test that
cares about ordering sets mtimes explicitly with `os.utime` instead of relying on
write order or `time.sleep`. Tests that need a *tight* cap build the cache with a
generous `max_entries`, store + stamp all entries first (so `store()`'s own internal
eviction never fires on uncontrolled real timestamps), then tighten `max_entries` and
call `_evict()` directly -- the same "reach into the private method" style the
uploader suite already uses for deterministic control.
"""

import io
import os
import wave
from pathlib import Path

import pytest

from banter_client.cache import CorruptAudioError, PlayCache

BASE_MTIME = 1_700_000_000


def _wav_bytes(seconds: float = 0.05, rate: int = 16000) -> bytes:
    """Real mono 16-bit WAV bytes. The cache rejects anything `wave` can't parse, so
    fixtures have to be actual audio rather than a b"data" placeholder."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def _cache_with_entries(tmp_path: Path, ids: list[str], *, max_entries: int = 10) -> PlayCache:
    """A cache holding one small clip per id, mtimes stamped oldest-to-newest in
    the order given (ids[0] is oldest)."""
    cache = PlayCache(tmp_path / "cache", max_entries=max_entries)
    for i, rec_id in enumerate(ids):
        cache.store(rec_id, _wav_bytes())
        os.utime(cache.path_for(rec_id), (BASE_MTIME + i, BASE_MTIME + i))
    return cache


# ------------------------------------------------------------------------- store/has
def test_store_bytes_then_has_and_path_for_round_trip(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)

    wav = _wav_bytes()

    path = cache.store("abc", wav)

    assert cache.has("abc")
    assert cache.path_for("abc") == path
    assert path.read_bytes() == wav


def test_store_chunked_iterable_round_trips_exact_bytes(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)
    wav = _wav_bytes()
    # Split mid-file so reassembly, not just passthrough, is what's being asserted.
    chunks = [wav[:20], wav[20:60], wav[60:]]

    path = cache.store("abc", iter(chunks))

    assert path.read_bytes() == wav


# ------------------------------------------------------------------------- atomicity
def test_store_leaves_no_tmp_file_behind(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)

    cache.store("abc", _wav_bytes())

    assert list(cache.cache_dir.glob("*.tmp")) == []


def test_stale_tmp_present_at_construction_is_swept(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stale = cache_dir / "orphan.wav.tmp"
    stale.write_bytes(b"partial")

    PlayCache(cache_dir, max_entries=5)

    assert not stale.exists()


# --------------------------------------------------------------------------- oldest
def test_oldest_returns_least_recently_used(tmp_path):
    cache = _cache_with_entries(tmp_path, ["a", "b", "c"])

    assert cache.oldest() == cache.path_for("a")


def test_oldest_skips_excluded_path(tmp_path):
    cache = _cache_with_entries(tmp_path, ["a", "b", "c"])

    assert cache.oldest(exclude=cache.path_for("a")) == cache.path_for("b")


def test_oldest_on_empty_cache_returns_none(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)

    assert cache.oldest() is None


# ---------------------------------------------------------------------------- touch
def test_touch_missing_id_is_silent_noop(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)

    cache.touch("does-not-exist")  # must not raise


def test_touch_promotes_entry_so_it_survives_eviction(tmp_path):
    cache = _cache_with_entries(tmp_path, ["a", "b", "c", "d"])
    # "a" is currently the oldest. Touch it, then pin its mtime newer than
    # everything else so the promotion is unambiguous under a tight cap.
    cache.touch("a")
    os.utime(cache.path_for("a"), (BASE_MTIME + 100, BASE_MTIME + 100))

    cache.max_entries = 3
    cache._evict(keep=None)

    remaining = {p.stem for p in cache.entries()}
    assert "a" in remaining
    assert "b" not in remaining  # now the least-recently-used


# -------------------------------------------------------------------------- eviction
def test_lru_eviction_evicts_least_recently_used_first(tmp_path):
    cache = _cache_with_entries(tmp_path, ["a", "b", "c", "d", "e"])

    cache.max_entries = 3
    cache._evict(keep=None)

    assert {p.stem for p in cache.entries()} == {"c", "d", "e"}


def test_eviction_keeps_cache_at_the_cap(tmp_path):
    cache = _cache_with_entries(tmp_path, ["a", "b", "c", "d", "e"])

    cache.max_entries = 3
    cache._evict(keep=None)

    assert len(cache.entries()) == 3


def test_keep_protects_named_path_from_eviction(tmp_path):
    cache = _cache_with_entries(tmp_path, ["a", "b", "c"])

    cache.max_entries = 1
    cache._evict(keep=cache.path_for("a"))  # "a" is the oldest, but protected

    remaining = {p.stem for p in cache.entries()}
    assert "a" in remaining
    assert "b" not in remaining  # evicted in a's place
    assert len(remaining) == 2


def test_eviction_never_drops_below_one_entry(tmp_path):
    cache = _cache_with_entries(tmp_path, ["a", "b", "c"])

    cache.max_entries = 1
    cache._evict(keep=None)

    assert len(cache.entries()) == 1


def test_eviction_never_touches_files_outside_cache_dir(tmp_path):
    sibling_dir = tmp_path / "sibling"
    sibling_dir.mkdir()
    sentinel = sibling_dir / "keep_me.wav"
    sentinel.write_bytes(b"important")

    cache = _cache_with_entries(tmp_path, [f"id{i}" for i in range(10)])
    cache.max_entries = 1
    cache._evict(keep=None)

    assert sentinel.exists()
    assert sentinel.read_bytes() == b"important"


def test_store_itself_triggers_eviction_at_the_cap(tmp_path):
    """The other eviction tests call `_evict()` directly for determinism, which means
    none of them would notice if `store()` stopped calling it. This one closes that
    gap end-to-end: no private access, just stores past the cap.
    """
    cache = PlayCache(tmp_path / "cache", max_entries=2)
    for i, rec_id in enumerate(["old", "mid"]):
        cache.store(rec_id, _wav_bytes())
        os.utime(cache.path_for(rec_id), (BASE_MTIME + i, BASE_MTIME + i))

    cache.store("new", _wav_bytes())  # third entry with a cap of 2 -> "old" must go

    assert not cache.has("old")
    assert cache.has("mid")
    assert cache.has("new")
    assert len(cache.entries()) == 2


# -------------------------------------------------------------- corrupt-entry guard
# The failure this guards against is a captive portal answering 200 with an HTML body:
# it has no RIFF magic at all, and a truncated WAV has magic but no usable chunks.
HTML_BODY = b"<html><body>Sign in to WiFi</body></html>"
TRUNCATED_WAV = b"RIFF....WAVEcached"


def test_store_rejects_a_non_wav_body(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)

    with pytest.raises(CorruptAudioError):
        cache.store("abc", HTML_BODY)

    assert not cache.has("abc")
    assert list(cache.cache_dir.glob("*")) == []  # no entry, no leftover tmp


def test_store_rejects_a_wav_header_with_no_audio(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)

    with pytest.raises(CorruptAudioError):
        cache.store("abc", TRUNCATED_WAV)

    assert not cache.has("abc")


def test_store_rejection_leaves_an_existing_good_entry_alone(tmp_path):
    cache = PlayCache(tmp_path / "cache", max_entries=5)
    cache.store("good", _wav_bytes())

    with pytest.raises(CorruptAudioError):
        cache.store("bad", HTML_BODY)

    assert cache.has("good")


def test_construction_sweeps_a_corrupt_entry(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "junk.wav").write_bytes(TRUNCATED_WAV)
    (cache_dir / "good.wav").write_bytes(_wav_bytes())

    cache = PlayCache(cache_dir, max_entries=5)

    assert not cache.has("junk")
    assert cache.has("good")


def test_oldest_skips_a_corrupt_entry_that_appears_after_startup(tmp_path):
    cache = _cache_with_entries(tmp_path, ["good"])
    # Written after construction, so the startup sweep never saw it.
    corrupt = cache.cache_dir / "junk.wav"
    corrupt.write_bytes(TRUNCATED_WAV)
    os.utime(corrupt, (BASE_MTIME - 100, BASE_MTIME - 100))  # oldest, so it'd win

    assert cache.oldest() == cache.path_for("good")
