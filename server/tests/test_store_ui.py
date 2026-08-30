"""M4 step 2 acceptance: web-UI data-access helpers in `store.py`
(list_recordings, soft_delete_recording, restore_recording,
upsert_device_heartbeat, list_devices).
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


# --- list_recordings -----------------------------------------------------------


def test_list_recordings_newest_first(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        _insert(conn, id="b", created_at="2026-01-03T00:00:00Z")
        _insert(conn, id="c", created_at="2026-01-02T00:00:00Z")

        rows = store.list_recordings(conn)
        assert [r["id"] for r in rows] == ["b", "c", "a"]


def test_list_recordings_filters_by_source(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="k1", source="kid", created_at="2026-01-01T00:00:00Z")
        _insert(conn, id="m1", source="mom", created_at="2026-01-02T00:00:00Z")
        _insert(conn, id="k2", source="kid", created_at="2026-01-03T00:00:00Z")

        rows = store.list_recordings(conn, source="kid")
        assert [r["id"] for r in rows] == ["k2", "k1"]


def test_list_recordings_excludes_deleted(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        _insert(conn, id="b", created_at="2026-01-02T00:00:00Z")
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = 'b'")

        rows = store.list_recordings(conn)
        assert [r["id"] for r in rows] == ["a"]


def test_list_recordings_respects_limit_and_offset(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        for i in range(5):
            _insert(conn, id=f"r{i}", created_at=f"2026-01-0{i + 1}T00:00:00Z")

        rows = store.list_recordings(conn, limit=2, offset=1)
        # newest-first order is r4, r3, r2, r1, r0 -> offset 1, limit 2 -> r3, r2
        assert [r["id"] for r in rows] == ["r3", "r2"]


# --- soft_delete_recording -------------------------------------------------------


def test_soft_delete_recording_sets_flag_and_returns_true(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            flipped = store.soft_delete_recording(conn, "a")
        assert flipped is True
        row = store.get_recording(conn, "a")
        assert row["deleted"] == 1


def test_soft_delete_recording_already_deleted_returns_false(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            store.soft_delete_recording(conn, "a")
        with db.transaction(conn):
            flipped = store.soft_delete_recording(conn, "a")
        assert flipped is False


def test_soft_delete_recording_unknown_id_returns_false(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        with db.transaction(conn):
            flipped = store.soft_delete_recording(conn, "nope")
        assert flipped is False


# --- restore_recording -------------------------------------------------------------


def test_restore_recording_clears_flag_and_returns_true(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            store.soft_delete_recording(conn, "a")
        with db.transaction(conn):
            flipped = store.restore_recording(conn, "a")
        assert flipped is True
        row = store.get_recording(conn, "a")
        assert row["deleted"] == 0


def test_restore_recording_not_deleted_returns_false(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        _insert(conn, id="a", created_at="2026-01-01T00:00:00Z")
        with db.transaction(conn):
            flipped = store.restore_recording(conn, "a")
        assert flipped is False


# --- upsert_device_heartbeat --------------------------------------------------------


def test_upsert_device_heartbeat_inserts_new_device(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        with db.transaction(conn):
            store.upsert_device_heartbeat(
                conn, device_id="kidbox-01", last_seen="2026-01-01T00:00:00Z", queue_depth=3
            )
        row = conn.execute("SELECT * FROM devices WHERE id = 'kidbox-01'").fetchone()
        assert row["last_seen"] == "2026-01-01T00:00:00Z"
        assert row["queue_depth"] == 3


def test_upsert_device_heartbeat_updates_existing_device(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        with db.transaction(conn):
            store.upsert_device_heartbeat(
                conn, device_id="kidbox-01", last_seen="2026-01-01T00:00:00Z", queue_depth=3
            )
        with db.transaction(conn):
            store.upsert_device_heartbeat(
                conn, device_id="kidbox-01", last_seen="2026-01-02T00:00:00Z", queue_depth=0
            )
        row = conn.execute("SELECT * FROM devices WHERE id = 'kidbox-01'").fetchone()
        assert row["last_seen"] == "2026-01-02T00:00:00Z"
        assert row["queue_depth"] == 0
        count = conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
        assert count == 1


# --- list_devices ------------------------------------------------------------------


def test_list_devices_orders_by_last_seen_descending(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        with db.transaction(conn):
            store.upsert_device_heartbeat(
                conn, device_id="dev-a", last_seen="2026-01-01T00:00:00Z", queue_depth=0
            )
            store.upsert_device_heartbeat(
                conn, device_id="dev-b", last_seen="2026-01-03T00:00:00Z", queue_depth=0
            )
            store.upsert_device_heartbeat(
                conn, device_id="dev-c", last_seen="2026-01-02T00:00:00Z", queue_depth=0
            )

        rows = store.list_devices(conn)
        assert [r["id"] for r in rows] == ["dev-b", "dev-c", "dev-a"]
