"""POST /api/devices/{id}/heartbeat — server test suite (M4 step 4)."""

from app import db, store

# --- auth --------------------------------------------------------------------


def test_missing_api_key_401(client):
    resp = client.post("/api/devices/kidbox-01/heartbeat", json={"queue_depth": 0})
    assert resp.status_code == 401


def test_wrong_api_key_401(client):
    resp = client.post(
        "/api/devices/kidbox-01/heartbeat",
        json={"queue_depth": 0},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


# --- happy path ----------------------------------------------------------------


def test_happy_path_response_body(client):
    resp = client.post(
        "/api/devices/kidbox-01/heartbeat",
        json={"queue_depth": 3},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "kidbox-01"
    assert body["last_seen"].endswith("Z")


def test_happy_path_row(client, settings):
    client.post(
        "/api/devices/kidbox-02/heartbeat",
        json={"queue_depth": 5},
        headers={"X-API-Key": "test-key"},
    )
    with db.session(settings.db_path) as conn:
        rows = store.list_devices(conn)
    matching = [r for r in rows if r["id"] == "kidbox-02"]
    assert len(matching) == 1
    assert matching[0]["last_seen"] is not None
    assert matching[0]["queue_depth"] == 5


def test_second_heartbeat_updates_not_duplicates(client, settings):
    client.post(
        "/api/devices/kidbox-03/heartbeat",
        json={"queue_depth": 1},
        headers={"X-API-Key": "test-key"},
    )
    client.post(
        "/api/devices/kidbox-03/heartbeat",
        json={"queue_depth": 9},
        headers={"X-API-Key": "test-key"},
    )
    with db.session(settings.db_path) as conn:
        rows = store.list_devices(conn)
    matching = [r for r in rows if r["id"] == "kidbox-03"]
    assert len(matching) == 1
    assert matching[0]["queue_depth"] == 9


def test_unknown_device_id_creates_row(client, settings):
    resp = client.post(
        "/api/devices/never-seen-before/heartbeat",
        json={"queue_depth": 0},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    with db.session(settings.db_path) as conn:
        rows = store.list_devices(conn)
    assert any(r["id"] == "never-seen-before" for r in rows)


# --- validation ------------------------------------------------------------------


def test_negative_queue_depth_422(client):
    resp = client.post(
        "/api/devices/kidbox-01/heartbeat",
        json={"queue_depth": -1},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422


def test_missing_queue_depth_422(client):
    resp = client.post(
        "/api/devices/kidbox-01/heartbeat",
        json={},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422
