"""M0 acceptance: healthz answers, migrations create every table, reruns are safe."""

from app import db


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_migrations_create_all_tables(client, settings):
    with db.session(settings.db_path) as conn:
        tables = db.table_names(conn)
    assert {"recordings", "plays", "devices", "schema_version"} <= tables


def test_migrations_are_idempotent(settings):
    first = db.migrate(settings.db_path)
    second = db.migrate(settings.db_path)
    # Version-agnostic on purpose: the claim is "applies every pending migration
    # once, then nothing". Hardcoding the list makes every new migration break an
    # unrelated M0 test.
    assert first == sorted(first)
    assert first[0] == 1
    assert second == []


def test_wal_mode_enabled(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_source_check_constraint_rejects_junk(settings):
    import sqlite3

    import pytest

    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recordings (id, source, origin, path, created_at) VALUES (?,?,?,?,?)",
            ("x1", "grandma", "kidbox", "/tmp/a.wav", "2026-01-01T00:00:00Z"),
        )


def test_settings_chat_allowlist(settings):
    s = settings.model_copy(update={"telegram_chat_mom": "111", "telegram_chat_dad": "222"})
    assert s.source_for_chat("111") == "mom"
    assert s.source_for_chat(222) == "dad"
    assert s.source_for_chat("999") is None
    assert s.source_for_chat("") is None
