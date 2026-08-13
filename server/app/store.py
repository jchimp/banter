"""Data-access layer for `recordings`. No ORM — plain sqlite3, parameterized SQL only.

Kept small and local per CLAUDE.md. Callers own the connection/transaction via
`app.db.session()` / `app.db.transaction()`.
"""

import sqlite3

from app.audio import parse_iso8601
from app.selection import Candidate


def insert_recording(
    conn: sqlite3.Connection,
    *,
    id: str,
    source: str,
    origin: str,
    path: str,
    duration_ms: int,
    bytes: int,
    created_at: str,
) -> bool:
    """Insert a recording row, no-op if `id` already exists.

    This is the idempotency primitive: the client generates `id`, the server
    upserts on it so a retried upload never creates a duplicate row.

    Returns:
        True if a new row was created, False if `id` already existed.
    """
    cursor = conn.execute(
        """
        INSERT INTO recordings (id, source, origin, path, duration_ms, bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (id, source, origin, path, duration_ms, bytes, created_at),
    )
    return cursor.rowcount > 0


def get_recording(conn: sqlite3.Connection, rec_id: str) -> sqlite3.Row | None:
    """Fetch a recording by id, or None if it doesn't exist."""
    return conn.execute(
        "SELECT * FROM recordings WHERE id = ?",
        (rec_id,),
    ).fetchone()


def get_playable(conn: sqlite3.Connection, rec_id: str) -> sqlite3.Row | None:
    """Fetch a recording by id, but only if it hasn't been soft-deleted.

    Used by the audio-streaming route so a `deleted=1` row 404s instead of being
    served. `get_recording` stays deleted-agnostic for the upload/upsert path.
    """
    return conn.execute(
        "SELECT * FROM recordings WHERE id = ? AND deleted = 0",
        (rec_id,),
    ).fetchone()


def _row_to_candidate(row: sqlite3.Row) -> Candidate:
    """Convert a `selectable_candidates` row into a `Candidate` with UTC datetimes."""
    last_played_at = row["last_played_at"]
    return Candidate(
        id=row["id"],
        source=row["source"],
        created_at=parse_iso8601(row["created_at"]),
        last_played_at=parse_iso8601(last_played_at) if last_played_at is not None else None,
        play_count=row["play_count"],
        deleted=bool(row["deleted"]),
    )


def selectable_candidates(conn: sqlite3.Connection, device_id: str) -> list[Candidate]:
    """All non-deleted recordings, with per-device last-played time for `select_next()`.

    `last_played_at` on each `Candidate` is scoped to `device_id`: it's the max
    `plays.played_at` for that recording on that device, or None if this device has
    never played it. `recordings.last_played_at` (global, across all devices) is not
    used here — that's a separate, unrelated column.
    """
    rows = conn.execute(
        """
        SELECT
            r.id,
            r.source,
            r.created_at,
            r.play_count,
            r.deleted,
            dp.last_played_at AS last_played_at
        FROM recordings r
        LEFT JOIN (
            SELECT recording_id, MAX(played_at) AS last_played_at
            FROM plays
            WHERE device_id = ?
            GROUP BY recording_id
        ) dp ON dp.recording_id = r.id
        WHERE r.deleted = 0
        """,
        (device_id,),
    ).fetchall()
    return [_row_to_candidate(row) for row in rows]


def last_played_recording_id(conn: sqlite3.Connection, device_id: str) -> str | None:
    """The most recently played recording id for this device, or None.

    Feeds `DeviceHistory.last_recording_id` for the no-instant-repeat rule (FR-13).
    """
    row = conn.execute(
        """
        SELECT recording_id
        FROM plays
        WHERE device_id = ?
        ORDER BY played_at DESC
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()
    return row["recording_id"] if row is not None else None


def record_play(
    conn: sqlite3.Connection,
    *,
    recording_id: str,
    device_id: str,
    played_at: str,
) -> None:
    """Record a play: insert a `plays` row and bump the recording's play stats.

    Idempotent on the exact `(recording_id, device_id, played_at)` triple: a client
    retrying a `/played` receipt after a dropped response resends the same
    `played_at`, and that must not double-count a play the server already recorded.
    A fresh receipt (different `played_at`, e.g. a genuine replay) still counts.

    Both statements run inside the caller's transaction (`app.db.transaction()`) —
    this function never opens its own.
    """
    exists = conn.execute(
        """
        SELECT 1 FROM plays
        WHERE recording_id = ? AND device_id = ? AND played_at = ?
        """,
        (recording_id, device_id, played_at),
    ).fetchone()
    if exists is not None:
        return

    conn.execute(
        "INSERT INTO plays (recording_id, device_id, played_at) VALUES (?, ?, ?)",
        (recording_id, device_id, played_at),
    )
    conn.execute(
        """
        UPDATE recordings
        SET play_count = play_count + 1, last_played_at = ?
        WHERE id = ?
        """,
        (played_at, recording_id),
    )
