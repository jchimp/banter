"""Unit tests for `app.audio.resolve_playable_audio`, the shared path-escape guard.

This helper is the single source of truth for turning a recording id into an
on-disk `Path`, called by both the auth-gated `/api/recordings/{id}/audio` route
(see `test_playback_api.py`) and the unauthenticated web-UI playback route added
later in M4. Exercised directly here so the guard's four failure modes — unknown
id, soft-deleted row, path escape, missing file — are covered independent of any
particular route's HTTP wiring.
"""

import pytest
from fastapi import HTTPException

from app import db, store
from app.audio import resolve_playable_audio


def test_unknown_id_404(settings, client):
    # `client` is unused directly but its fixture runs the app lifespan, which
    # applies migrations — this test needs the `recordings` table to exist.
    with db.session(settings.db_path) as conn, pytest.raises(HTTPException) as exc_info:
        resolve_playable_audio(conn, settings, "does-not-exist")
    assert exc_info.value.status_code == 404


def test_soft_deleted_row_404(settings, client, post_recording):
    rid = "shared0000001"
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn, db.transaction(conn):
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = ?", (rid,))

    with db.session(settings.db_path) as conn, pytest.raises(HTTPException) as exc_info:
        resolve_playable_audio(conn, settings, rid)
    assert exc_info.value.status_code == 404


def test_path_escape_attempt_404(settings, client):
    # Insert a row directly (bypassing the upload route's own validation) whose
    # `path` tries to climb out of `audio_dir` — this is the scenario the guard
    # exists for: a malformed/hand-edited row must never be used to read arbitrary
    # files off disk.
    rid = "shared0000002"
    with db.session(settings.db_path) as conn, db.transaction(conn):
        store.insert_recording(
            conn,
            id=rid,
            source="kid",
            origin="kidbox",
            path="../../../../etc/passwd",
            duration_ms=1000,
            bytes=10,
            created_at="2026-08-12T10:30:00Z",
        )

    with db.session(settings.db_path) as conn, pytest.raises(HTTPException) as exc_info:
        resolve_playable_audio(conn, settings, rid)
    assert exc_info.value.status_code == 404


def test_happy_path_returns_existing_file(settings, client, post_recording):
    rid = "shared0000003"
    post_recording(client, id=rid)

    with db.session(settings.db_path) as conn:
        resolved = resolve_playable_audio(conn, settings, rid)

    assert resolved.is_file()
    assert resolved.is_relative_to(settings.audio_dir.resolve())
