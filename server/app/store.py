"""Data-access layer for `recordings`. No ORM — plain sqlite3, parameterized SQL only.

Kept small and local per CLAUDE.md. Callers own the connection/transaction via
`app.db.session()` / `app.db.transaction()`.
"""

import sqlite3
from collections.abc import Sequence

from app.audio import parse_iso8601
from app.selection import Candidate

# Literal mapping from parent role -> its notified-flag column. Never build this
# column name from caller input — an unknown role must raise, not interpolate.
_NOTIFIED_COLUMN = {"mom": "notified_mom", "dad": "notified_dad"}


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
    original_path: str | None = None,
    telegram_file_id: str | None = None,
) -> bool:
    """Insert a recording row, no-op if `id` already exists.

    This is the idempotency primitive: the client generates `id`, the server
    upserts on it so a retried upload never creates a duplicate row.

    `original_path` and `telegram_file_id` are only ever set for `origin='telegram'`
    rows: the untranscoded OGG/Opus voice note kept alongside the transcoded WAV
    (CLAUDE.md gotcha 1), and the Telegram file id used to avoid re-downloading on
    a resend. Both default to None so the kidbox upload path (which has neither)
    needs no change at its call site.

    Returns:
        True if a new row was created, False if `id` already existed.
    """
    cursor = conn.execute(
        """
        INSERT INTO recordings (
            id, source, origin, path, duration_ms, bytes, created_at,
            original_path, telegram_file_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (id, source, origin, path, duration_ms, bytes, created_at, original_path, telegram_file_id),
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


def pending_notifications(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Kidbox recordings not yet fully notified to parents, oldest first.

    Feeds the Telegram send loop: only `origin='kidbox'` rows need a parent
    notification (a `telegram`-origin row is a reply already delivered by hand),
    and `notified=0` is the cheap pre-computed filter set by `finalize_notified`.
    """
    return conn.execute(
        """
        SELECT * FROM recordings
        WHERE origin = 'kidbox' AND notified = 0 AND deleted = 0
        ORDER BY created_at
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def mark_notified(
    conn: sqlite3.Connection,
    rec_id: str,
    role: str,
    *,
    file_id: str | None = None,
) -> None:
    """Flag `rec_id` as sent to the given parent `role` ('mom' or 'dad').

    Runs inside the caller's transaction — does not open or commit its own. The
    column to set is looked up from a literal dict, never built from `role`
    directly, so a bad role can't reach the SQL text.

    `file_id`, when given, is written with `COALESCE(telegram_file_id, ?)` so the
    first successfully-sent file id wins; a later resend (e.g. to the other
    parent, or a retry with a re-uploaded file) doesn't churn a value already on
    the row.

    Raises:
        ValueError: `role` is not a known parent role.
    """
    column = _NOTIFIED_COLUMN.get(role)
    if column is None:
        raise ValueError(f"unknown parent role: {role!r}")

    if file_id is not None:
        conn.execute(
            f"""
            UPDATE recordings
            SET {column} = 1, telegram_file_id = COALESCE(telegram_file_id, ?)
            WHERE id = ?
            """,
            (file_id, rec_id),
        )
    else:
        conn.execute(
            f"UPDATE recordings SET {column} = 1 WHERE id = ?",
            (rec_id,),
        )


def finalize_notified(conn: sqlite3.Connection, rec_id: str, roles: Sequence[str]) -> bool:
    """Set `notified = 1` on `rec_id` once every role in `roles` is flagged.

    `roles` is the *configured* parent roles for this deployment (a parent with
    a blank chat id is excluded by the caller before this is called), so a
    single-parent deployment still reaches `notified=1` off just that one flag.

    An empty `roles` deliberately never flips `notified` — "notified" should mean
    "sent to at least the configured parents", and with zero parents configured
    there is nothing to have sent it to, so the row would sit pending forever
    rather than being silently marked done.

    Returns:
        True if this call flipped `notified` to 1, False otherwise (already set,
        or not every configured role's flag is set yet).
    """
    if not roles:
        return False

    columns = []
    for role in roles:
        column = _NOTIFIED_COLUMN.get(role)
        if column is None:
            raise ValueError(f"unknown parent role: {role!r}")
        columns.append(column)

    condition = " AND ".join(f"{column} = 1" for column in columns)
    cursor = conn.execute(
        f"""
        UPDATE recordings
        SET notified = 1
        WHERE id = ? AND notified = 0 AND {condition}
        """,
        (rec_id,),
    )
    return cursor.rowcount > 0


def random_kid_recordings(conn: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    """Up to `n` random non-deleted kid recordings, for `/joke` (FR-17)."""
    return conn.execute(
        """
        SELECT * FROM recordings
        WHERE source = 'kid' AND deleted = 0
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (n,),
    ).fetchall()


def source_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per-source counts/durations over non-deleted recordings, for `/stats` (FR-18).

    One row per `source` ('kid', 'mom', 'dad'); no synthetic "total" row — summing
    three rows client-side is trivial and keeping this a plain GROUP BY keeps the
    query (and its result shape) simple to test.
    """
    return conn.execute(
        """
        SELECT
            source,
            COUNT(*) AS count,
            COALESCE(SUM(duration_ms), 0) AS total_ms,
            MAX(created_at) AS last_at
        FROM recordings
        WHERE deleted = 0
        GROUP BY source
        ORDER BY source
        """
    ).fetchall()
