"""M3 step 1 acceptance: per-parent notification flags and the store helpers
that read/write them (`store.py`, migration 003).
"""

import pytest

from app import db, store
from app.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, api_key="test-key", _env_file=None)


def _insert(conn, *, id, source="kid", origin="kidbox", created_at, **kw):
    with db.transaction(conn):
        store.insert_recording(
            conn,
            id=id,
            source=source,
            origin=origin,
            path=f"{id}.wav",
            duration_ms=kw.pop("duration_ms", 1000),
            bytes=kw.pop("bytes", 100),
            created_at=created_at,
            **kw,
        )


# --- migration 003 -----------------------------------------------------------


def test_migration_adds_notified_columns_defaulting_zero(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(recordings)")}
        assert "notified_mom" in cols
        assert "notified_dad" in cols
        row = conn.execute("SELECT notified_mom, notified_dad FROM recordings WHERE id = 'a'")
        row = row.fetchone()
        assert row["notified_mom"] == 0
        assert row["notified_dad"] == 0


def test_migrations_are_idempotent(settings):
    first = db.migrate(settings.db_path)
    second = db.migrate(settings.db_path)
    assert 3 in first
    assert second == []


# --- insert_recording (telegram fields) --------------------------------------


def test_insert_recording_persists_telegram_fields(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(
            conn,
            id="t1",
            source="mom",
            origin="telegram",
            created_at="2026-01-01T00:00:00Z",
            original_path="orig/t1.ogg",
            telegram_file_id="file-abc",
        )
        row = store.get_recording(conn, "t1")
        assert row["original_path"] == "orig/t1.ogg"
        assert row["telegram_file_id"] == "file-abc"


def test_insert_recording_without_telegram_fields_is_null(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="k1", created_at="2026-01-01T00:00:00Z")
        row = store.get_recording(conn, "k1")
        assert row["original_path"] is None
        assert row["telegram_file_id"] is None


def test_insert_recording_duplicate_id_is_noop(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="dup", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            created = store.insert_recording(
                conn,
                id="dup",
                source="kid",
                origin="kidbox",
                path="dup2.wav",
                duration_ms=999,
                bytes=1,
                created_at="2026-02-02T00:00:00Z",
            )
        assert created is False


# --- pending_notifications -----------------------------------------------------


def test_pending_notifications_filters_and_orders(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="kid1", origin="kidbox", created_at="2026-01-02T00:00:00Z")
        _insert(conn, id="kid2", origin="kidbox", created_at="2026-01-01T00:00:00Z")
        _insert(conn, id="tg1", source="mom", origin="telegram", created_at="2026-01-01T00:00:00Z")
        _insert(conn, id="deleted1", origin="kidbox", created_at="2026-01-01T00:00:00Z")
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = 'deleted1'")
        _insert(conn, id="already", origin="kidbox", created_at="2026-01-01T00:00:00Z")
        conn.execute("UPDATE recordings SET notified = 1 WHERE id = 'already'")

        rows = store.pending_notifications(conn)
        ids = [r["id"] for r in rows]
        assert ids == ["kid2", "kid1"]  # oldest first, excludes telegram/deleted/notified


def test_pending_notifications_respects_limit(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        for i in range(5):
            _insert(conn, id=f"k{i}", created_at=f"2026-01-0{i + 1}T00:00:00Z")
        rows = store.pending_notifications(conn, limit=2)
        assert len(rows) == 2
        assert [r["id"] for r in rows] == ["k0", "k1"]


# --- mark_notified -------------------------------------------------------------


def test_mark_notified_sets_only_named_column(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            store.mark_notified(conn, "a", "mom")
        row = store.get_recording(conn, "a")
        assert row["notified_mom"] == 1
        assert row["notified_dad"] == 0


def test_mark_notified_unknown_role_raises(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with pytest.raises(ValueError):
            store.mark_notified(conn, "a", "grandma")


def test_mark_notified_file_id_first_wins(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            store.mark_notified(conn, "a", "mom", file_id="first-id")
        with db.transaction(conn):
            store.mark_notified(conn, "a", "dad", file_id="second-id")
        row = store.get_recording(conn, "a")
        assert row["telegram_file_id"] == "first-id"
        assert row["notified_mom"] == 1
        assert row["notified_dad"] == 1


# --- finalize_notified ----------------------------------------------------------


def test_finalize_notified_flips_only_when_all_roles_set(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            store.mark_notified(conn, "a", "mom")
            flipped = store.finalize_notified(conn, "a", ["mom", "dad"])
        assert flipped is False
        row = store.get_recording(conn, "a")
        assert row["notified"] == 0

        with db.transaction(conn):
            store.mark_notified(conn, "a", "dad")
            flipped = store.finalize_notified(conn, "a", ["mom", "dad"])
        assert flipped is True
        row = store.get_recording(conn, "a")
        assert row["notified"] == 1


def test_finalize_notified_single_role(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            store.mark_notified(conn, "a", "dad")
            flipped = store.finalize_notified(conn, "a", ["dad"])
        assert flipped is True
        row = store.get_recording(conn, "a")
        assert row["notified"] == 1


def test_finalize_notified_empty_roles_never_flips(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            store.mark_notified(conn, "a", "mom")
            store.mark_notified(conn, "a", "dad")
            flipped = store.finalize_notified(conn, "a", [])
        assert flipped is False
        row = store.get_recording(conn, "a")
        assert row["notified"] == 0


# --- random_kid_recordings -----------------------------------------------------


def test_random_kid_recordings_filters_source_and_deleted(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="k1", source="kid", created_at="2026-01-01T00:00:00Z")
        _insert(conn, id="k2", source="kid", created_at="2026-01-02T00:00:00Z")
        _insert(conn, id="m1", source="mom", created_at="2026-01-01T00:00:00Z")
        _insert(conn, id="k3", source="kid", created_at="2026-01-03T00:00:00Z")
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = 'k3'")

        rows = store.random_kid_recordings(conn, 10)
        ids = {r["id"] for r in rows}
        assert ids == {"k1", "k2"}


def test_random_kid_recordings_caps_at_n(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        for i in range(5):
            _insert(conn, id=f"k{i}", source="kid", created_at=f"2026-01-0{i + 1}T00:00:00Z")
        rows = store.random_kid_recordings(conn, 3)
        assert len(rows) == 3


def test_random_kid_recordings_fewer_than_n_when_pool_small(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="k0", source="kid", created_at="2026-01-01T00:00:00Z")
        rows = store.random_kid_recordings(conn, 10)
        assert len(rows) == 1


# --- source_stats ---------------------------------------------------------------


def test_source_stats_counts_and_sums_ignoring_deleted(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="k1", source="kid", created_at="2026-01-01T00:00:00Z", duration_ms=1000)
        _insert(conn, id="k2", source="kid", created_at="2026-01-02T00:00:00Z", duration_ms=2000)
        _insert(conn, id="m1", source="mom", created_at="2026-01-01T00:00:00Z", duration_ms=500)
        _insert(conn, id="k3", source="kid", created_at="2026-01-03T00:00:00Z", duration_ms=9999)
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = 'k3'")

        rows = {r["source"]: r for r in store.source_stats(conn)}
        assert rows["kid"]["count"] == 2
        assert rows["kid"]["total_ms"] == 3000
        assert rows["mom"]["count"] == 1
        assert rows["mom"]["total_ms"] == 500
        assert "dad" not in rows


def test_source_stats_total_ms_zero_not_none_when_duration_null(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO recordings (id, source, origin, path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("nulldur", "kid", "kidbox", "nulldur.wav", "2026-01-01T00:00:00Z"),
            )
        rows = {r["source"]: r for r in store.source_stats(conn)}
        assert rows["kid"]["total_ms"] == 0
