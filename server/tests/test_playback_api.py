"""Integration tests for the M2 playback routes (PRD §3.3, §5).

`GET /api/recordings/next` (selection), `GET /api/recordings/{id}/audio` (stream),
`POST /api/recordings/{id}/played` (receipt). These are thin adapters over
`app.selection`/`app.store` — tier/cooldown logic is covered by `test_selection.py`,
not re-tested here. This file exercises the HTTP layer: auth, status codes, payload
shape, and idempotency at the wire.
"""

from app import db, store

# --- GET /api/recordings/next: auth ---------------------------------------------


def test_next_missing_api_key_401(client):
    resp = client.get("/api/recordings/next", params={"device_id": "kidbox-01"})
    assert resp.status_code == 401


def test_next_wrong_api_key_401(client):
    resp = client.get(
        "/api/recordings/next",
        params={"device_id": "kidbox-01"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


# --- GET /api/recordings/next: behavior -----------------------------------------


def test_next_empty_db_204(client):
    resp = client.get(
        "/api/recordings/next",
        params={"device_id": "kidbox-01"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 204
    assert resp.content == b""


def test_next_with_one_recording_returns_payload_shape(client, post_recording):
    rid = "next0000001"
    post_recording(client, id=rid)
    resp = client.get(
        "/api/recordings/next",
        params={"device_id": "kidbox-01"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == rid
    assert body["source"] == "kid"
    assert isinstance(body["duration_ms"], int)
    assert isinstance(body["created_at"], str)
    assert body["play_count"] == 0
    assert body["audio_url"] == f"/api/recordings/{rid}/audio"


def test_next_soft_deleted_only_is_204(client, settings, post_recording):
    rid = "next0000002"
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn, db.transaction(conn):
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = ?", (rid,))
    resp = client.get(
        "/api/recordings/next",
        params={"device_id": "kidbox-01"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 204


def test_next_missing_device_id_is_client_error_not_500(client):
    resp = client.get("/api/recordings/next", headers={"X-API-Key": "test-key"})
    assert resp.status_code < 500
    assert resp.status_code != 204


# --- GET /api/recordings/{id}/audio: auth ---------------------------------------


def test_audio_missing_api_key_401(client, post_recording):
    rid = "audio0000001"
    post_recording(client, id=rid)
    resp = client.get(f"/api/recordings/{rid}/audio")
    assert resp.status_code == 401


# --- GET /api/recordings/{id}/audio: behavior -----------------------------------


def test_audio_returns_exact_uploaded_bytes_and_content_type(client, post_recording, wav_bytes):
    rid = "audio0000002"
    audio = wav_bytes(seconds=1.0)
    post_recording(client, id=rid, audio=audio)
    resp = client.get(f"/api/recordings/{rid}/audio", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == audio


def test_audio_unknown_id_404(client):
    resp = client.get("/api/recordings/does-not-exist/audio", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


def test_audio_soft_deleted_id_404(client, settings, post_recording):
    rid = "audio0000003"
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn, db.transaction(conn):
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = ?", (rid,))
    resp = client.get(f"/api/recordings/{rid}/audio", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


def test_audio_file_missing_on_disk_404(client, settings, post_recording):
    rid = "audio0000004"
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
    dest = settings.audio_dir / row["path"]
    dest.unlink()
    resp = client.get(f"/api/recordings/{rid}/audio", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


def test_audio_path_escape_row_404(client, settings):
    """Regression test for the refactor that moved the escape guard into
    `app.audio.resolve_playable_audio` (shared with the M4 web-UI playback route):
    a row whose `path` climbs out of `audio_dir` must still 404 at the route level,
    identically to every other failure mode, and auth must still be required first.
    """
    rid = "audio0000005"
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

    unauth = client.get(f"/api/recordings/{rid}/audio")
    assert unauth.status_code == 401

    resp = client.get(f"/api/recordings/{rid}/audio", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


# --- POST /api/recordings/{id}/played: auth --------------------------------------


def test_played_missing_api_key_401(client, post_recording):
    rid = "played0000001"
    post_recording(client, id=rid)
    resp = client.post(f"/api/recordings/{rid}/played", json={"device_id": "kidbox-01"})
    assert resp.status_code == 401


# --- POST /api/recordings/{id}/played: behavior -----------------------------------


def test_played_happy_path_creates_row_and_bumps_count(client, settings, post_recording):
    rid = "played0000002"
    post_recording(client, id=rid)
    resp = client.post(
        f"/api/recordings/{rid}/played",
        json={"device_id": "kidbox-01", "played_at": "2026-08-12T11:00:00Z"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["play_count"] == 1
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
        plays = conn.execute("SELECT * FROM plays WHERE recording_id = ?", (rid,)).fetchall()
    assert row["play_count"] == 1
    assert row["last_played_at"] == "2026-08-12T11:00:00Z"
    assert len(plays) == 1


def test_played_retry_same_played_at_is_idempotent(client, settings, post_recording):
    """The most important test in this file: a retried receipt with the same
    (device_id, played_at) must not double-count — that's what `store.record_play`
    dedupes on, and the client's queue/uploader will retry on any dropped response.
    """
    rid = "played0000003"
    post_recording(client, id=rid)
    body = {"device_id": "kidbox-01", "played_at": "2026-08-12T11:00:00Z"}
    first = client.post(
        f"/api/recordings/{rid}/played", json=body, headers={"X-API-Key": "test-key"}
    )
    second = client.post(
        f"/api/recordings/{rid}/played", json=body, headers={"X-API-Key": "test-key"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["play_count"] == 1
    with db.session(settings.db_path) as conn:
        plays = conn.execute("SELECT * FROM plays WHERE recording_id = ?", (rid,)).fetchall()
    assert len(plays) == 1


def test_played_different_played_at_counts_as_second_play(client, settings, post_recording):
    rid = "played0000004"
    post_recording(client, id=rid)
    client.post(
        f"/api/recordings/{rid}/played",
        json={"device_id": "kidbox-01", "played_at": "2026-08-12T11:00:00Z"},
        headers={"X-API-Key": "test-key"},
    )
    second = client.post(
        f"/api/recordings/{rid}/played",
        json={"device_id": "kidbox-01", "played_at": "2026-08-12T12:00:00Z"},
        headers={"X-API-Key": "test-key"},
    )
    assert second.status_code == 200
    assert second.json()["play_count"] == 2
    with db.session(settings.db_path) as conn:
        plays = conn.execute("SELECT * FROM plays WHERE recording_id = ?", (rid,)).fetchall()
    assert len(plays) == 2


def test_played_omitted_played_at_is_filled_in_server_side(client, post_recording):
    rid = "played0000005"
    post_recording(client, id=rid)
    resp = client.post(
        f"/api/recordings/{rid}/played",
        json={"device_id": "kidbox-01"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["play_count"] == 1


def test_played_garbage_played_at_400(client, post_recording):
    rid = "played0000006"
    post_recording(client, id=rid)
    resp = client.post(
        f"/api/recordings/{rid}/played",
        json={"device_id": "kidbox-01", "played_at": "not-a-date"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 400


def test_played_unknown_id_404(client):
    resp = client.post(
        "/api/recordings/does-not-exist/played",
        json={"device_id": "kidbox-01"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 404


def test_played_soft_deleted_recording_still_accepts_receipt(client, settings, post_recording):
    rid = "played0000007"
    post_recording(client, id=rid)
    with db.session(settings.db_path) as conn, db.transaction(conn):
        conn.execute("UPDATE recordings SET deleted = 1 WHERE id = ?", (rid,))
    resp = client.post(
        f"/api/recordings/{rid}/played",
        json={"device_id": "kidbox-01"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200


# --- end-to-end --------------------------------------------------------------------


def test_full_loop_upload_next_audio_played_next(client, wav_bytes, post_recording):
    """Upload -> /next -> /{id}/audio -> /played -> /next again.

    With a single recording seeded, FR-13's single-item exception means `/next`
    hands back the same recording rather than 204 once there's nothing else to
    offer, even though it's the device's own immediately-previous play.
    """
    rid = "loop0000001"
    audio = wav_bytes(seconds=1.0)
    upload = post_recording(client, id=rid, audio=audio)
    assert upload.status_code == 201

    headers = {"X-API-Key": "test-key"}
    device_id = "kidbox-01"

    first_next = client.get(
        "/api/recordings/next", params={"device_id": device_id}, headers=headers
    )
    assert first_next.status_code == 200
    payload = first_next.json()
    assert payload["id"] == rid

    audio_resp = client.get(payload["audio_url"], headers=headers)
    assert audio_resp.status_code == 200
    assert audio_resp.content == audio

    played = client.post(
        f"/api/recordings/{rid}/played",
        json={"device_id": device_id},
        headers=headers,
    )
    assert played.status_code == 200
    assert played.json()["play_count"] == 1

    second_next = client.get(
        "/api/recordings/next", params={"device_id": device_id}, headers=headers
    )
    assert second_next.status_code == 200
    assert second_next.json()["id"] == rid
    assert second_next.json()["play_count"] == 1
