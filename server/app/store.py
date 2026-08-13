"""Data-access layer for `recordings`. No ORM — plain sqlite3, parameterized SQL only.

Kept small and local per CLAUDE.md. Callers own the connection/transaction via
`app.db.session()` / `app.db.transaction()`.
"""

import sqlite3


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
