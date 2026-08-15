"""Web UI routes: recording list, source filter, browser audio route (M4 step 6)."""

from app import db, store


def _insert(settings, *, id, source, origin="kidbox", created_at, deleted=0):
    """Insert a recording row directly via `store`, bypassing the upload route.

    The UI routes only read; exercising them doesn't need a real WAV upload, so
    rows are seeded straight into the DB for speed and to control `created_at`
    ordering precisely.
    """
    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn, db.transaction(conn):
        store.insert_recording(
            conn,
            id=id,
            source=source,
            origin=origin,
            path=f"{source}/202608/{id}.wav",
            duration_ms=1500,
            bytes=1234,
            created_at=created_at,
        )
        if deleted:
            store.soft_delete_recording(conn, id)


# --- index / trusted-LAN --------------------------------------------------------


def test_index_no_api_key_required(client, settings):
    _insert(settings, id="idx0000001", source="kid", created_at="2026-08-12T10:00:00Z")
    resp = client.get("/")
    assert resp.status_code == 200


def test_index_newest_first(client, settings):
    _insert(settings, id="order000001", source="kid", created_at="2026-08-12T10:00:00Z")
    _insert(settings, id="order000002", source="kid", created_at="2026-08-13T10:00:00Z")
    resp = client.get("/")
    body = resp.text
    assert body.index("rec-order000002") < body.index("rec-order000001")


def test_index_excludes_soft_deleted(client, settings):
    _insert(settings, id="del00000001", source="kid", created_at="2026-08-12T10:00:00Z")
    _insert(
        settings,
        id="del00000002",
        source="kid",
        created_at="2026-08-13T10:00:00Z",
        deleted=1,
    )
    resp = client.get("/")
    assert "rec-del00000001" in resp.text
    assert "rec-del00000002" not in resp.text


# --- filter partial --------------------------------------------------------------


def test_partial_filters_by_source(client, settings):
    _insert(settings, id="filt0000001", source="kid", created_at="2026-08-12T10:00:00Z")
    _insert(settings, id="filt0000002", source="mom", created_at="2026-08-12T11:00:00Z")
    resp = client.get("/ui/recordings?source=kid")
    assert resp.status_code == 200
    assert "rec-filt0000001" in resp.text
    assert "rec-filt0000002" not in resp.text


def test_partial_is_bare_fragment(client, settings):
    _insert(settings, id="frag0000001", source="kid", created_at="2026-08-12T10:00:00Z")
    resp = client.get("/ui/recordings")
    assert "<html" not in resp.text


def test_no_filter_returns_all_sources(client, settings):
    _insert(settings, id="all00000001", source="kid", created_at="2026-08-12T10:00:00Z")
    _insert(settings, id="all00000002", source="mom", created_at="2026-08-12T11:00:00Z")
    _insert(settings, id="all00000003", source="dad", created_at="2026-08-12T12:00:00Z")
    resp = client.get("/ui/recordings")
    assert "rec-all00000001" in resp.text
    assert "rec-all00000002" in resp.text
    assert "rec-all00000003" in resp.text


def test_invalid_source_treated_as_no_filter(client, settings):
    """An unrecognized `?source=` degrades to "show everything" rather than 422 —
    see `app.ui.routes._normalize_source` for the rationale.
    """
    _insert(settings, id="bad00000001", source="kid", created_at="2026-08-12T10:00:00Z")
    _insert(settings, id="bad00000002", source="mom", created_at="2026-08-12T11:00:00Z")
    resp = client.get("/ui/recordings?source=grandma")
    assert resp.status_code == 200
    assert "rec-bad00000001" in resp.text
    assert "rec-bad00000002" in resp.text


# --- audio route -------------------------------------------------------------


def test_ui_audio_route_serves_bytes_no_api_key(client, settings):
    rid = "aud00000001"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z")
    path = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF....WAVEfmt ")
    resp = client.get(f"/ui/recordings/{rid}/audio")
    assert resp.status_code == 200
    assert resp.content == b"RIFF....WAVEfmt "


def test_ui_audio_route_404_unknown_id(client, settings):
    db.migrate(settings.db_path)
    resp = client.get("/ui/recordings/doesnotexist/audio")
    assert resp.status_code == 404


def test_ui_audio_route_404_soft_deleted(client, settings):
    rid = "del00000003"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z", deleted=1)
    path = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF")
    resp = client.get(f"/ui/recordings/{rid}/audio")
    assert resp.status_code == 404


# --- row markup ----------------------------------------------------------------


def test_row_markup_id_and_audio_src(client, settings):
    rid = "row00000001"
    _insert(settings, id=rid, source="mom", origin="telegram", created_at="2026-08-12T10:00:00Z")
    resp = client.get("/")
    body = resp.text
    assert f'id="rec-{rid}"' in body
    assert f"/ui/recordings/{rid}/audio" in body
    assert f"/api/recordings/{rid}/audio" not in body


# --- soft-delete / undo (FR-23) --------------------------------------------------


def test_delete_sets_deleted_flag_and_returns_undo_fragment(client, settings):
    rid = "delx0000001"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z")
    resp = client.post(f"/ui/recordings/{rid}/delete")
    assert resp.status_code == 200
    assert f"/ui/recordings/{rid}/restore" in resp.text

    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
    assert row["deleted"] == 1


def test_delete_never_removes_the_audio_file(client, settings):
    rid = "delx0000002"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z")
    path = settings.audio_dir / "kid" / "202608" / f"{rid}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF....WAVEfmt ")

    resp = client.post(f"/ui/recordings/{rid}/delete")
    assert resp.status_code == 200
    assert path.exists(), "soft-delete must never unlink the audio file (CLAUDE.md)"


def test_delete_fragment_keeps_row_id_and_drops_audio_element(client, settings):
    rid = "delx0000003"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z")
    resp = client.post(f"/ui/recordings/{rid}/delete")
    body = resp.text
    assert f'id="rec-{rid}"' in body
    assert "<audio" not in body


def test_restore_clears_flag_and_returns_normal_row(client, settings):
    rid = "delx0000004"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z", deleted=1)
    resp = client.post(f"/ui/recordings/{rid}/restore")
    assert resp.status_code == 200
    body = resp.text
    assert f'id="rec-{rid}"' in body
    assert "<audio" in body

    db.migrate(settings.db_path)
    with db.session(settings.db_path) as conn:
        row = store.get_recording(conn, rid)
    assert row["deleted"] == 0


def test_deleted_row_absent_from_index(client, settings):
    rid = "delx0000005"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z")
    client.post(f"/ui/recordings/{rid}/delete")
    resp = client.get("/")
    assert f"rec-{rid}" not in resp.text


def test_restored_row_present_in_index(client, settings):
    rid = "delx0000006"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z", deleted=1)
    resp = client.get("/")
    assert f"rec-{rid}" not in resp.text

    client.post(f"/ui/recordings/{rid}/restore")
    resp = client.get("/")
    assert f"rec-{rid}" in resp.text


def test_delete_unknown_id_404(client, settings):
    db.migrate(settings.db_path)
    resp = client.post("/ui/recordings/doesnotexist/delete")
    assert resp.status_code == 404


def test_restore_unknown_id_404(client, settings):
    db.migrate(settings.db_path)
    resp = client.post("/ui/recordings/doesnotexist/restore")
    assert resp.status_code == 404


def test_delete_already_deleted_is_idempotent_200(client, settings):
    rid = "delx0000007"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z", deleted=1)
    resp = client.post(f"/ui/recordings/{rid}/delete")
    assert resp.status_code == 200
    assert f'id="rec-{rid}"' in resp.text


def test_restore_not_deleted_is_idempotent_200(client, settings):
    rid = "delx0000008"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z")
    resp = client.post(f"/ui/recordings/{rid}/restore")
    assert resp.status_code == 200
    assert f'id="rec-{rid}"' in resp.text


def test_delete_route_no_api_key_required(client, settings):
    rid = "delx0000009"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z")
    resp = client.post(f"/ui/recordings/{rid}/delete")
    assert resp.status_code == 200


def test_restore_route_no_api_key_required(client, settings):
    rid = "delx0000010"
    _insert(settings, id=rid, source="kid", created_at="2026-08-12T10:00:00Z", deleted=1)
    resp = client.post(f"/ui/recordings/{rid}/restore")
    assert resp.status_code == 200
