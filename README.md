# Banter

A joke exchange between a kid and their parents. The kid records jokes on a physical
box (**kidbox** — Pi Zero 2 W, two arcade buttons, a glowing ring). Parents receive
them on Telegram and send jokes back as voice notes. The box plays them.

Docs: [`PRD.md`](PRD.md) (what) · [`ROADMAP.md`](ROADMAP.md) (order) ·
[`CLAUDE.md`](CLAUDE.md) (how) · [`PARTS.md`](PARTS.md) · [`wiring.svg`](wiring.svg)

**Status: M1 + M2 complete** — the record loop works end-to-end (kid holds BTN1, the
WAV lands in the client's on-disk queue, the uploader delivers it to
`POST /api/recordings` with retry) and so does playback: BTN2 fetches a joke from
`select_next()`, plays it, and posts a play receipt; offline taps fall back to a local
cache. No Telegram, no web UI yet.

```
   ┌──────── kidbox (Pi Zero 2 W) ────────┐        ┌─── home server (Docker) ───┐
   │ banter-client.service (systemd)      │  HTTP  │ banter-server (FastAPI)    │
   │  BTN1 hold → record → POST           │───────▶│  SQLite + audio store      │
   │  BTN2 tap  → GET next → play         │◀───────│  HTMX UI · Telegram bot    │
   └──────────────────────────────────────┘        └───────────┬───────────────┘
                                                        Mom / Dad on Telegram
```

## Layout
```
banter/
├── server/   FastAPI + SQLite + HTMX, runs in Docker on the home server
└── client/   kidbox device agent, runs as a systemd service on the Pi
```

## Developing without the Pi

Nothing here requires hardware to build or test. Three profiles, chosen by three env
vars — the code above the backend seam (`banter_client/backends/base.py`) is identical
in all of them.

| Profile | audio | buttons | ring | Use |
|---|---|---|---|---|
| Pi | `alsa` | `gpio` | `neopixel` | The real box |
| Laptop | `sounddevice` | `keyboard` | `terminal` | Real mic/speakers, keys for buttons |
| CI | `synthetic` | `keyboard` | `null` | No audio device at all |

Run the local record/playback loop on your own machine:

```bash
cd client
cp .env.dev.example .env
uv sync --extra dev-audio        # PortAudio; Linux/macOS/Windows
uv run banter-demo
#   [r] record toggle   [p] play   [q] quit
```

`r` starts recording from your laptop mic, `r` again stops and writes a 16 kHz mono
WAV to the queue dir, `p` plays the newest clip back. The terminal ring prints the
state the NeoPixel would be showing. Hold-to-record is GPIO-only — line-based stdin
can't express a hold, so the sim is always toggle mode.

With `BANTER_AUDIO_BACKEND=synthetic` the loop runs with no sound card whatsoever and
still writes genuine playable WAVs — that's how the loop is tested in CI.

**What still needs hardware:** hold-to-record timing feel, Codec Zero mic quality and
levels, real NeoPixel animations, and speaker volume. Everything else — the queue and
retry logic, the selection algorithm, the Telegram bot, the web UI — is fully
developable now.

## Try it: the M1 record loop
Run the server and a dev-profile client against each other on one machine — no Pi
needed.

```bash
# terminal 1: server
cd server
cp .env.example .env          # set API_KEY, e.g. openssl rand -hex 24
uv sync
DATA_DIR=./data uv run uvicorn app.main:app --reload --port 8080

# terminal 2: client, dev profile
cd client
cp .env.dev.example .env      # set BANTER_API_KEY to match the server's
uv sync --extra dev-audio
uv run banter-client
#   [r]+Enter to start recording, [r]+Enter again to stop and enqueue; ctrl-c to quit
```

Speak a joke, then press `r` again. The client writes a WAV to `BANTER_QUEUE_DIR`
(`./devdata/queue` by default), the uploader thread picks it up immediately, and
within a request or two you should see:
- a new row in the server's `recordings` table, `source=kid origin=kidbox`
- a playable WAV under `server/data/audio/kid/YYYYMM/{id}.wav`
- the sidecar and WAV gone from `BANTER_QUEUE_DIR` (uploader deleted them on the 2xx)

Kill the server mid-recording, record a couple more jokes, then restart the server —
the client keeps retrying with backoff and they all arrive without touching the
client again.

**Queue durability.** A recording lives in `BANTER_QUEUE_DIR` (as `{id}.wav` +
`{id}.json`) from the moment it's captured until the server returns a 2xx; nothing in
the client deletes it before then. The queue is plain files, so it survives a client
restart or a Pi reboot — `recover()` re-enqueues anything left over on startup. An
upload that fails with a permanent error (bad API key, malformed request) is moved
into `BANTER_QUEUE_DIR/rejected/` instead of retried forever, and is never deleted —
only `queue.done()`, called after a 2xx, removes a file.

## Try it: the M2 playback loop
Same two-terminal setup as above. Once a recording or two has been uploaded (seed a
couple with the record loop, or a parent voice note once M3 lands), tap `p`:

```bash
# terminal 2: client, dev profile (as above)
uv run banter-client
#   [r]+Enter record, [p]+Enter play/stop, ctrl-c to quit
```

`p` does two round trips, not one: `GET /api/recordings/next?device_id=...` picks a
recording (`select_next()`, tier 1 → 4 per `PRD.md` §3.3), then
`GET /api/recordings/{id}/audio` streams the WAV, which is written into
`BANTER_CACHE_DIR` before it plays. A `204` from `/next` means "nothing selectable
right now" — the client does *not* fall back to the cache in that case, only on a
network error or a non-2xx/204 status. Tapping `p` again while a clip is playing stops
it; tapping while still fetching cancels the fetch instead of starting playback once it
lands. After a clip finishes, the client posts a best-effort play receipt
(`POST /api/recordings/{id}/played`) — a dropped receipt never delays or blocks audio.

**Play cache / offline fallback.** `BANTER_CACHE_DIR` holds the last
`BANTER_PLAY_CACHE_SIZE` clips actually played, oldest evicted first (LRU by mtime).
Pull the network and tap `p` again: `Player` catches the request failure and falls back
to the oldest cached clip instead. Each served fallback is moved to the back of the LRU,
so repeated offline taps cycle through the whole cache rather than alternating between
the two oldest clips. A fallback clip has no server-known id, so no play receipt is
posted for it.

**Only real audio gets cached.** `store()` parses the downloaded body as a WAV before
committing it, so a 200 carrying something other than audio — a captive portal or proxy
sign-in page is the realistic case on guest WiFi — is rejected rather than cached and
later played back as silence. That also memoes the network as unreachable, so the same
junk isn't re-fetched on every press. Entries that arrive some other way (an older
client, a hand-copied file) are swept at startup, and `oldest()` skips anything
unplayable so a bad entry can't wedge the offline fallback.

**Failing fast when the server is down.** A stopped server doesn't always refuse
connections — Docker keeps its port proxy bound after the container stops, so the TCP
connect succeeds and nothing ever answers. Two settings keep BTN2 responsive in that
case: `BANTER_NEXT_TIMEOUT` (default 3s) bounds the small `/next` and `/played` calls,
while `BANTER_HTTP_TIMEOUT` (30s) still covers the audio download; and after any failed
fetch the client treats the server as unreachable for `BANTER_OFFLINE_MEMO_SECONDS`
(default 30s), skipping the network entirely and going straight to cache. Only the first
tap of an outage pays a timeout — the rest are instant.

## Server — local dev
```bash
cd server
cp .env.example .env          # set API_KEY:  openssl rand -hex 24
                              # and the TELEGRAM_* vars (see below) to enable the bot
uv sync
DATA_DIR=./data uv run uvicorn app.main:app --reload --port 8080
curl localhost:8080/healthz   # {"ok":true}
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

## Server — Docker
```bash
cd server
cp .env.example .env
docker compose up -d --build
curl localhost:8080/healthz
```
Audio and the SQLite file live in `./data` on the host (mounted at `/data`).

## Telegram bot
The bot is a long-polling background task inside the server's lifespan — no webhook,
no public URL. A blank `TELEGRAM_BOT_TOKEN` disables it entirely; the rest of the
server runs as normal.

1. Message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Have each parent send the bot any message, then read the chat ids from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and set `TELEGRAM_CHAT_MOM` /
   `TELEGRAM_CHAT_DAD`. Leave one blank for a single-parent setup.
3. `docker compose up -d --build`. The image already carries ffmpeg.

Only those two chat ids are served. Anything from anywhere else is dropped with no
reply and no database write.

| Direction | Behaviour |
|---|---|
| Kid records | Both parents get the joke as a voice message with a timestamp/duration caption, within ~10 s |
| Parent sends a voice note | Stored as `source=mom\|dad`, `origin=telegram`; the OGG is kept alongside the transcoded 16 kHz WAV; BTN2 plays it next (tier 1) |
| `/joke [n]` | 1–5 random kid recordings (default 1). Doesn't count as a play on the box |
| `/stats` | Counts, total duration and last activity per source |

## Client — on the Pi
```bash
sudo apt install -y alsa-utils
curl -LsSf https://astral.sh/uv/install.sh | sh

cd ~/banter/client
cp .env.example .env          # set BANTER_API_URL + BANTER_API_KEY
arecord -l && aplay -l        # fill in BANTER_ALSA_CAPTURE / _PLAYBACK
uv sync --extra hardware      # gpiozero + neopixel; omit --extra off-hardware
uv run banter-client          # hold BTN1 to record; runs until SIGINT/SIGTERM

sudo cp banter-client.service /etc/systemd/system/
sudo systemctl enable --now banter-client
journalctl -u banter-client -f
```

### Pi one-time system config
`/boot/firmware/config.txt` — the NeoPixel ring runs on SPI (not PWM, which fights
the onboard audio):
```
dtparam=spi=on
core_freq_min=500
```
Codec Zero setup follows the official Raspberry Pi HAT instructions.

## API
Implemented so far (full contract in `PRD.md` §5). All `/api/*` require header
`X-API-Key`. JSON errors: `{"detail": "..."}`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/recordings` | multipart: `id`, `audio`, `source`, `device_id`, `recorded_at`, `duration_ms` → `201 {id, status}` created, `200 {id, status}` duplicate |
| GET | `/api/recordings/next?device_id=` | `select_next()` → `200 {id, source, duration_ms, created_at, play_count, audio_url}`, or `204` if nothing selectable |
| GET | `/api/recordings/{id}/audio` | Streams the canonical WAV; `404` on unknown or soft-deleted id |
| POST | `/api/recordings/{id}/played` | body `{device_id, played_at?}` → `200 {id, play_count}`; idempotent on the exact `(id, device_id, played_at)` triple |
| GET | `/healthz` | `{"ok": true}` — no auth |

`id` is client-generated; a repeat upload of the same `id` is a no-op 200, not a new
row, which is what makes the client's retry-on-failure safe. A missing or malformed
`id` is a `400`, not FastAPI's default `422` — the kidbox shouldn't have to
distinguish the two, both are just a bad request.

## Migrations
Plain SQL in `server/migrations/NNN_name.sql`, applied on startup and tracked in
`schema_version`. **Write them idempotently** (`IF NOT EXISTS`) — `executescript()`
commits implicitly, so a file can't be wrapped in one transaction and may replay
after a mid-file crash. Details in `app/db.py`.

`003_parent_notifications.sql` is the one exception: SQLite has no
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. If it ever half-applies, drop the
partially-added column by hand and let it rerun.

## Next
M3 — the Telegram bot (outbound notifications, inbound voice notes, `/joke`,
`/stats`). See `ROADMAP.md`.
