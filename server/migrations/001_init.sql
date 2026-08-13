-- 001_init.sql — Banter initial schema (PRD section 4)

CREATE TABLE IF NOT EXISTS recordings (
  id               TEXT PRIMARY KEY,
  source           TEXT NOT NULL CHECK (source IN ('kid','mom','dad')),
  origin           TEXT NOT NULL CHECK (origin IN ('kidbox','telegram')),
  path             TEXT NOT NULL,
  original_path    TEXT,
  duration_ms      INTEGER,
  bytes            INTEGER,
  created_at       TEXT NOT NULL,
  play_count       INTEGER NOT NULL DEFAULT 0,
  last_played_at   TEXT,
  deleted          INTEGER NOT NULL DEFAULT 0,
  telegram_file_id TEXT,
  notified         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plays (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  recording_id TEXT NOT NULL REFERENCES recordings(id),
  device_id    TEXT NOT NULL,
  played_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id          TEXT PRIMARY KEY,
  last_seen   TEXT,
  queue_depth INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_rec_source  ON recordings(source, deleted);
CREATE INDEX IF NOT EXISTS idx_rec_created ON recordings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plays_dev   ON plays(device_id, recording_id);
