# Banter — PRD

**A joke exchange between a kid and their parents.** The kid records jokes on a
physical box (two arcade buttons, a glowing ring). Parents get them on Telegram and
send jokes back as voice notes. The box plays them.

Status: spec. Version 0.1.0.

---

## 1. Actors

| Actor | Interface | Can do |
|---|---|---|
| Kid | **kidbox** (Pi Zero 2 W in a box) | Record a joke; play a random joke |
| Parent (mom, dad) | Telegram bot | Get notified of new kid jokes; send voice notes; `/joke` to hear kid jokes |
| Either | HTMX web UI (LAN) | Browse, play, see source, soft-delete |

## 2. Components

```
   ┌──────── kidbox (Pi Zero 2 W) ────────┐        ┌─── home server (Docker) ───┐
   │ banter-client.service (systemd)      │  HTTP  │ banter-server (FastAPI)    │
   │  BTN1 hold → record → POST           │───────▶│  SQLite + audio store      │
   │  BTN2 tap  → GET next → play         │◀───────│  HTMX UI  ·  REST API      │
   │  NeoPixel ring = state               │        │  Telegram bot (polling)    │
   └──────────────────────────────────────┘        └───────────┬───────────────┘
                                                               │ Bot API
                                                        Mom / Dad on Telegram
```

Server runs in Docker on the home server. Client runs as a **systemd service** on the
Pi (not Docker — 512 MB RAM, and ALSA passthrough isn't worth the trouble).

## 3. Functional requirements

### 3.1 kidbox — record (BTN1, GPIO17)
- **FR-1** Hold-to-record: recording starts on press, stops on release. Config
  `BUTTON_MODE=hold|toggle` (toggle = tap to start, tap to stop). Default `hold`.
- **FR-2** Hard cap `MAX_SECONDS` (default 60). Auto-stop and keep at the cap.
- **FR-3** Clips shorter than `MIN_SECONDS` (default 0.8) are discarded silently —
  ignores accidental taps. Ring flashes the discard color once.
- **FR-4** Audio captured 16 kHz mono 16-bit WAV from the Codec Zero mic.
- **FR-5** On stop: write to local queue dir, then upload async. Queue survives
  reboot; on start, re-enqueue anything left over.
- **FR-6** Upload retries with exponential backoff (2s → 60s cap), indefinitely.
  A dead server or dropped WiFi must never lose a joke.
- **FR-7** Recordings from kidbox always carry `source=kid`, `origin=kidbox`.

### 3.2 kidbox — play (BTN2; GPIO22, or GPIO27 on the Pi 4 build)
- **FR-8** Tap requests one recording from the server and plays it through the
  Codec Zero speaker.
- **FR-9** A tap while playing **stops** playback (does not queue another).
- **FR-10** Buttons are mutually exclusive: no playback while recording, and BTN1 is
  ignored during playback.
- **FR-11** After successful playback, POST a play receipt so the server can track
  `play_count` / `last_played_at`.
- **FR-12** Server unreachable → play a locally cached fallback clip if present,
  otherwise the error tone. Cache the last N (`PLAY_CACHE_SIZE`, default 10) played
  clips on disk.

### 3.3 Selection algorithm (server-side, the interesting bit)
`GET /api/recordings/next` picks in strict tier order. First non-empty tier wins;
random uniform choice within it.

| Tier | Pool | Rationale |
|---|---|---|
| 1 | Parent recordings **never played on this device** | Fresh parent jokes are the payoff |
| 2 | Parent recordings not played in the last `PARENT_COOLDOWN_HOURS` (default 12) | Rotate the parent set |
| 3 | Kid recordings, weighted toward least-recently-played | Fall back to their own material |
| 4 | Any non-deleted recording | Last resort |

- **FR-13** Exclude `deleted=1` and the immediately previous recording for the device
  (no instant repeats) unless the pool has only one item.
- **FR-14** Tier logic lives in one pure function, `select_next(...)`, unit-tested
  against a fixture set. Do not scatter it across route handlers.

### 3.4 Telegram bot
- **FR-15** On a new kidbox recording, push the audio to every registered parent chat
  with a caption (timestamp, duration).
- **FR-16** A parent sending a **voice note** stores it as a recording with
  `source=mom|dad` (per chat mapping), `origin=telegram`. Reply confirms receipt.
- **FR-17** `/joke [n]` returns `n` random kid recordings (default 1, max 5) as voice
  messages. Kid pool only (`source=kid`).
- **FR-18** `/stats` — counts by source, total duration, last activity.
- **FR-19** Unknown chat IDs are ignored entirely. Allowlist via
  `TELEGRAM_CHAT_MOM` / `TELEGRAM_CHAT_DAD`.
- **FR-20** Telegram voice notes arrive as OGG/Opus. Transcode to 16 kHz mono WAV
  with ffmpeg for kidbox playback; keep the original.

### 3.5 Web UI (HTMX + Jinja2)
- **FR-21** List all recordings, newest first: source badge (mom·tg / dad·tg /
  kid·kidbox), timestamp, duration, play count, inline `<audio>` player.
- **FR-22** Filter by source; HTMX partial swap, no full page reload.
- **FR-23** Soft-delete (sets `deleted=1`, file retained) with undo. **Never** hard-delete
  audio from the UI — quarantine, don't destroy.
- **FR-24** Read-only device panel: last seen, queue depth reported by kidbox.
- **FR-25** LAN-only. Single shared password or trusted-network assumption; no user accounts.

### 3.6 Ring states (NeoPixel 16, GPIO10)
| State | Behavior |
|---|---|
| Idle | Off, or a dim warm ember (`IDLE_GLOW`, default off) |
| Recording | Red/amber breathing pulse |
| Discarded (too short) | Two quick amber blinks |
| Uploading | Amber comet spin |
| Queued (offline) | Slow amber double-blink, repeats until drained |
| Playing | Green rotating chase |
| Success | Green flash 400 ms |
| Error | Red flash ×3 |

Brightness capped at `LED_MAX_BRIGHTNESS` (default 0.3) — 16 RGBW at full white is
~1 A and it's a bedside device.

## 4. Data model (SQLite, WAL)

```sql
CREATE TABLE recordings (
  id            TEXT PRIMARY KEY,           -- uuid4 hex[:12]
  source        TEXT NOT NULL CHECK (source IN ('kid','mom','dad')),
  origin        TEXT NOT NULL CHECK (origin IN ('kidbox','telegram')),
  path          TEXT NOT NULL,              -- canonical 16k mono wav
  original_path TEXT,                       -- telegram ogg, if any
  duration_ms   INTEGER,
  bytes         INTEGER,
  created_at    TEXT NOT NULL,              -- ISO8601 UTC
  play_count    INTEGER NOT NULL DEFAULT 0,
  last_played_at TEXT,
  deleted       INTEGER NOT NULL DEFAULT 0,
  telegram_file_id TEXT,
  notified      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE plays (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  recording_id TEXT NOT NULL REFERENCES recordings(id),
  device_id    TEXT NOT NULL,
  played_at    TEXT NOT NULL
);

CREATE TABLE devices (
  id         TEXT PRIMARY KEY,              -- 'kidbox-01'
  last_seen  TEXT,
  queue_depth INTEGER DEFAULT 0
);

CREATE INDEX idx_rec_source  ON recordings(source, deleted);
CREATE INDEX idx_rec_created ON recordings(created_at DESC);
CREATE INDEX idx_plays_dev   ON plays(device_id, recording_id);
```

Audio on disk: `/data/audio/{source}/{YYYYMM}/{id}.wav`. DB stores paths, never blobs.

## 5. API contract

All `/api/*` require header `X-API-Key`. JSON errors: `{"detail": "..."}`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/recordings` | multipart: `audio`, `source`, `device_id`, `recorded_at`, `duration_ms` → `201 {id, status}` |
| GET | `/api/recordings/next?device_id=` | Selection algorithm → `{id, source, duration_ms, audio_url}` or `204` if empty |
| GET | `/api/recordings/{id}/audio` | Streams canonical WAV |
| POST | `/api/recordings/{id}/played` | body `{device_id}` → records play receipt |
| GET | `/api/recordings?source=&limit=` | JSON list |
| POST | `/api/devices/{id}/heartbeat` | body `{queue_depth}` → device liveness |
| GET | `/healthz` | `{"ok": true}` — no auth |
| GET | `/` , `/ui/*` | HTMX UI (session/basic auth, not API key) |

## 6. Non-functional

- **NFR-1** Python 3.12+, dependency + venv management via **uv** everywhere.
- **NFR-2** Server: FastAPI + Jinja2 + HTMX + SQLite (WAL). Docker Compose deploy.
- **NFR-3** Parameterized SQL only. No ORM required; plain `sqlite3` is fine.
- **NFR-4** Client: Python 3.12 + gpiozero + `arecord`/`aplay` + requests. systemd unit,
  `Restart=on-failure`.
- **NFR-5** BTN2 tap → audio starts within **1.5 s** on a warm LAN.
- **NFR-6** No secrets in the repo. `.env` only, `.env.example` committed.
- **NFR-7** Audio files are never hard-deleted by application code.
- **NFR-8** Server survives kidbox being offline for days; kidbox survives the server
  being offline for days.
- **NFR-9** Tests: pytest. Required coverage on `select_next()`, upload queue
  drain/retry, and Telegram voice ingest.

## 7. Out of scope (v1)
Music/Raspotify · LCD/LED display · night light · speech-to-text or transcripts ·
multi-kid or multi-device fleet · cloud hosting · mobile app · joke ratings.

## 8. Open questions
1. Should `/joke` mark those recordings as "played" for tier purposes? (Assume **no** —
   Telegram plays and box plays are tracked separately.)
2. Retention policy — keep forever (assumed) or age out after N months?
3. Does the kid get a "new joke waiting" indicator, or is the surprise the point?
   (Assumed: idle ring turns a soft green when unplayed parent jokes exist.)
