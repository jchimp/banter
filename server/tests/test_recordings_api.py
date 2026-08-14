"""POST /api/recordings — server test suite (M1 step 5, see ROADMAP + CLAUDE.md)."""

import logging

import pytest
from fastapi.testclient import TestClient

from app import db, store
from app.config import Settings
from app.main import create_app

# --- auth --------------------------------------------------------------------


def test_missing_api_key_401(client, post_recording):
    resp = post_recording(client, api_key=None)
    assert resp.status_code == 401


def test_wrong_api_key_401(client, post_recording):
    resp = post_recording(client, api_key="wrong-key")
    assert resp.status_code == 401


def test_correct_api_key_201(client, post_recording):
    resp = post_recording(client)
    assert resp.status_code == 201


def test_auth_disabled_when_api_key_empty(tmp_path, post_recording):
    settings = Settings(data_dir=tmp_path, api_key="", _env_file=None)
    with TestClient(create_app(settings)) as c:
        resp = post_recording(c, api_key=None)
    assert resp.status_code == 201


# --- happy path ----------------------------------------------------------------


def test_happy_path_response_body(client, post_recording):
    resp = post_recording(client, id="happypath1")
    assert resp.status_code == 201
    assert resp.json() == {"id": "happypath1", "status": "created"}


def test_happy_path_row(client, settings, post_recording):
    rid = "happyrow01"
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
    assert row is not None
    assert row["source"] == "kid"
    assert row["origin"] == "kidbox"
    assert row["deleted"] == 0
    assert row["play_count"] == 0


def test_happy_path_file_location(client, settings, post_recording):
    rid = "happyfile1"
    post_recording(client, id=rid)
    expected = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    assert expected.exists()


def test_happy_path_db_path_is_relative_posix(client, settings, post_recording):
    rid = "happypath2"
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
    assert row["path"] == f"kid/202608/{rid}.wav"


def test_duration_ms_comes_from_probe_not_form(client, settings, post_recording, wav_bytes):
    rid = "happydur01"
    audio = wav_bytes(seconds=2.0)
    resp = post_recording(client, id=rid, audio=audio, duration_ms=99999)
    assert resp.status_code == 201
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
    assert abs(row["duration_ms"] - 2000) <= 50
    assert row["duration_ms"] != 99999


def test_bytes_equals_on_disk_file_size(client, settings, post_recording, wav_bytes):
    rid = "happybyte1"
    audio = wav_bytes(seconds=1.0)
    post_recording(client, id=rid, audio=audio)
    dest = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
    assert row["bytes"] == dest.stat().st_size


def test_stored_file_is_byte_identical(client, settings, post_recording, wav_bytes):
    rid = "happyid001"
    audio = wav_bytes(seconds=1.0)
    post_recording(client, id=rid, audio=audio)
    dest = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    assert dest.read_bytes() == audio


# --- idempotency -----------------------------------------------------------------


def test_duplicate_id_second_post_returns_200_duplicate(client, post_recording):
    rid = "dupid00001"
    first = post_recording(client, id=rid)
    second = post_recording(client, id=rid)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == {"id": rid, "status": "duplicate"}


def test_duplicate_id_still_one_row(client, settings, post_recording):
    rid = "dupid00002"
    post_recording(client, id=rid)
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM recordings WHERE id = ?", (rid,)).fetchone()[0]
    assert count == 1


def test_duplicate_post_does_not_modify_stored_file(client, settings, post_recording, wav_bytes):
    rid = "dupid00003"
    post_recording(client, id=rid, audio=wav_bytes(seconds=1.0))
    dest = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    before = dest.stat()
    post_recording(client, id=rid, audio=wav_bytes(seconds=1.0))
    after = dest.stat()
    assert before.st_mtime_ns == after.st_mtime_ns
    assert before.st_size == after.st_size


def test_duplicate_post_with_different_audio_does_not_overwrite(
    client, settings, post_recording, wav_bytes
):
    rid = "dupid00004"
    original = wav_bytes(seconds=1.0)
    post_recording(client, id=rid, audio=original)
    different = wav_bytes(seconds=3.0)
    post_recording(client, id=rid, audio=different)
    dest = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    assert dest.read_bytes() == original


# --- validation ------------------------------------------------------------------


def test_invalid_source_400_no_row_no_file(client, settings, post_recording):
    rid = "badsource01"
    resp = post_recording(client, id=rid, source="grandma")
    assert resp.status_code == 400
    with db.session(settings.db_path) as conn:
        assert store.get_recording(conn, rid) is None
    assert list(settings.audio_dir.rglob(f"{rid}.wav")) == []


def test_non_wav_file_400(client, post_recording):
    resp = post_recording(client, id="notawav0001", audio=b"this is not a wav file at all")
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "a/b", "ab"])
def test_invalid_id_400(client, post_recording, bad_id):
    resp = post_recording(client, id=bad_id)
    assert resp.status_code == 400


def test_empty_id_is_400(client, post_recording):
    """`id: str = Form(default="")` lets an empty value reach the handler instead of
    short-circuiting with FastAPI's 422, so it falls through to the same `_ID_RE`
    400 as any other malformed id — the kidbox doesn't have to distinguish the two.
    """
    resp = post_recording(client, id="")
    assert resp.status_code == 400


def test_missing_id_field_is_400(client, settings, wav_bytes):
    """A form POST that omits the `id` field entirely (not just empty) still 400s,
    not FastAPI's default 422 — same reasoning as `test_empty_id_is_400`.
    """
    data = {
        "source": "kid",
        "device_id": "kidbox-01",
        "recorded_at": "2026-08-12T10:30:00Z",
        "duration_ms": "1000",
    }
    files = {"audio": ("rec.wav", wav_bytes(), "audio/wav")}
    resp = client.post("/api/recordings", data=data, files=files, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 400


def test_malformed_recorded_at_400(client, post_recording):
    resp = post_recording(client, id="baddate0001", recorded_at="not-a-date")
    assert resp.status_code == 400


def test_oversized_upload_413(tmp_path, post_recording, wav_bytes):
    settings = Settings(
        data_dir=tmp_path, api_key="test-key", max_upload_bytes=2048, _env_file=None
    )
    with TestClient(create_app(settings)) as c:
        audio = wav_bytes(seconds=5.0)  # well over 2048 bytes
        resp = post_recording(c, audio=audio)
    assert resp.status_code == 413


# --- off-spec audio (warn and store) ----------------------------------------------


def test_offspec_wav_stores_and_warns(client, settings, post_recording, wav_bytes, caplog):
    rid = "offspec0001"
    audio = wav_bytes(seconds=1.0, rate=44100, channels=2)
    with caplog.at_level(logging.WARNING, logger="banter.api"):
        resp = post_recording(client, id=rid, audio=audio)
    assert resp.status_code == 201
    assert "offspec_wav" in caplog.text
    with db.session(settings.db_path) as conn:
        assert store.get_recording(conn, rid) is not None


# --- staging hygiene ---------------------------------------------------------------


def test_incoming_empty_after_success(client, settings, post_recording):
    post_recording(client, id="stage0001")
    incoming = settings.audio_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_incoming_empty_after_invalid_audio(client, settings, post_recording):
    post_recording(client, id="stage0002", audio=b"not a real wav")
    incoming = settings.audio_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_incoming_empty_after_oversize(tmp_path, post_recording, wav_bytes):
    settings = Settings(
        data_dir=tmp_path, api_key="test-key", max_upload_bytes=2048, _env_file=None
    )
    with TestClient(create_app(settings)) as c:
        post_recording(c, id="stage0003", audio=wav_bytes(seconds=5.0))
    incoming = settings.audio_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


# --- store.py directly --------------------------------------------------------------


def test_insert_recording_idempotent(settings):
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        with db.transaction(conn):
            created_first = store.insert_recording(
                conn,
                id="storeid0001",
                source="kid",
                origin="kidbox",
                path="kid/202608/storeid0001.wav",
                duration_ms=1000,
                bytes=1234,
                created_at="2026-08-12T10:30:00Z",
            )
        with db.transaction(conn):
            created_second = store.insert_recording(
                conn,
                id="storeid0001",
                source="kid",
                origin="kidbox",
                path="kid/202608/storeid0001.wav",
                duration_ms=1000,
                bytes=1234,
                created_at="2026-08-12T10:30:00Z",
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM recordings WHERE id = ?", ("storeid0001",)
        ).fetchone()[0]
    assert created_first is True
    assert created_second is False
    assert count == 1
