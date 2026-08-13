"""SQLite access + migration runner.

No ORM by design (see CLAUDE.md). Plain sqlite3 with Row factory, WAL mode,
and a busy timeout because the Telegram bot task and HTTP handlers both write.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("banter.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a tuned connection. Caller owns closing it."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Keep these short."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_version_table(conn)
    return {r["version"] for r in conn.execute("SELECT version FROM schema_version")}


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, str, Path]]:
    """Return [(version, name, path)] sorted by version. Files are NNN_name.sql."""
    found: list[tuple[int, str, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        stem = path.stem
        num, _, name = stem.partition("_")
        if not num.isdigit():
            log.warning("skipping migration with non-numeric prefix: %s", path.name)
            continue
        found.append((int(num), name or stem, path))
    return sorted(found, key=lambda t: t[0])


def migrate(db_path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied this run.

    NOTE: sqlite3's executescript() issues an implicit COMMIT before running, so a
    migration file cannot be wrapped in our own transaction. Consequence: **every
    migration must be written idempotently** (IF NOT EXISTS / guarded ALTERs), because
    a crash mid-file can leave the schema partially applied with no version row, and
    the next startup will replay that file from the top.
    """
    applied_now: list[int] = []
    with session(db_path) as conn:
        done = applied_versions(conn)
        for version, name, path in discover_migrations(migrations_dir):
            if version in done:
                continue
            sql = path.read_text()
            log.info("applying migration %03d_%s", version, name)
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)", (version, name)
            )
            applied_now.append(version)
    return applied_now


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in rows}
